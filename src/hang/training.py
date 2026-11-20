"""`hang train`: read a passage aloud and watch the voice recognition learn.

Each phrase you read (cut at pauses) is transcribed, so the words you have read
light up, and turned into a voiceprint sample. After every sample the accuracy
figures are recomputed:

- consistency: how similar each of your samples is to a voiceprint built from
  the others (leave-one-out), i.e. how you will score later;
- comparison voices: built-in macOS voices standing in for other people, scored
  against your voiceprint;
- the threshold that separates the two, and the share of all samples it puts on
  the right side ("estimated accuracy").
"""
import queue
import re
import threading
from dataclasses import dataclass, field

from hang import audio, speaker

PASSAGE = (
    "Claude, voice command. Good morning! Today I'd like to work on something new. "
    "Please read the project files and tell me what each one does. Then run the tests, "
    "and if anything fails, explain why before you change the code. "
    "Claude, the other door. Let's look at the last commit together and summarize what changed. "
    "Could you make the error messages a little friendlier, and check the quiz page as well? "
    "Claude, don't talk. Claude, talk. Option one. Send it. We're done."
)
MIN_CLIPS = 6          # fewer samples than this can't be saved
MIN_CLIP_S = 0.8       # shorter phrases make poor voiceprint samples
DONE_SHARE = 0.85      # share of the passage read that ends training by itself


def norm(word):
    return re.sub(r"[^a-z0-9]+", "", word.lower())


@dataclass
class Trainer:
    """The training state; feed it samples with add()."""
    others: dict  # comparison voice name -> embedding
    words: list = field(default_factory=lambda: PASSAGE.split())
    embeddings: list = field(default_factory=list)
    seconds: list = field(default_factory=list)
    texts: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def __post_init__(self):
        self.keys = [norm(w) for w in self.words]
        self.read = [False] * len(self.words)
        self.cursor = 0

    # -- passage tracking: highlight the words heard, in order
    def mark(self, text):
        for w in (norm(x) for x in (text or "").split()):
            if not w:
                continue
            for j in range(self.cursor, min(self.cursor + 8, len(self.keys))):
                if self.keys[j] == w or (len(w) > 3 and self.keys[j].startswith(w[:4])):
                    for k in range(self.cursor, j + 1):
                        self.read[k] = True  # skipped-over words count as read too
                    self.cursor = j + 1
                    break

    @property
    def share_read(self):
        return sum(self.read) / len(self.read)

    def add(self, embedding, seconds, text):
        self.mark(text)
        if embedding is not None:
            self.embeddings.append(embedding)
            self.seconds.append(seconds)
            self.texts.append(text or "")
            self.stats = self.compute()

    def compute(self):
        import numpy as np
        n = len(self.embeddings)
        if n < 2:
            return {}
        centroid = speaker.unit(np.mean(self.embeddings, axis=0))
        own = []
        for i, e in enumerate(self.embeddings):
            rest = speaker.unit(np.mean([x for j, x in enumerate(self.embeddings) if j != i], axis=0))
            own.append(float(e @ rest))
        others = {name: float(e @ centroid) for name, e in self.others.items()}
        threshold = speaker.choose_threshold(own, list(others.values()))
        accepted = sum(s >= threshold for s in own)
        rejected = sum(s < threshold for s in others.values())
        total = n + len(others)
        return {
            "centroid": centroid, "own": own, "others": others, "threshold": threshold,
            "accepted": accepted, "rejected": rejected,
            "accuracy": (accepted + rejected) / total if total else 0.0,
            "consistency": sum(own) / n,
            "margin": min(own) - max(others.values()) if others else None,
        }

    @property
    def finished(self):
        return self.share_read >= DONE_SHARE and len(self.embeddings) >= MIN_CLIPS

    def save(self):
        s = self.stats
        return speaker.save(s["centroid"], s["threshold"], s["own"], round(s["accuracy"], 3))


# ---------------------------------------------------------------- rendering

def _bar(fraction, width=24, color="green"):
    from rich.text import Text
    fraction = max(0.0, min(1.0, fraction))
    full = int(round(fraction * width))
    return Text("█" * full, style=color) + Text("░" * (width - full), style="grey35")


def render(trainer, live):
    from rich.console import Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text

    passage = Text()
    for i, word in enumerate(trainer.words):
        if trainer.read[i]:
            style = "bold green"
        elif i == trainer.cursor:
            style = "bold yellow underline"
        else:
            style = "grey62"
        passage.append(word, style=style)
        passage.append(" ")
    read_pct = trainer.share_read
    title = "Read this aloud, at your normal pace" if read_pct < 1 else "All read"
    passage_panel = Panel(passage, title=title, title_align="left", border_style="cyan",
                          subtitle="%d%% read" % (read_pct * 100), subtitle_align="right", padding=(1, 2))

    st = trainer.stats
    n = len(trainer.embeddings)
    m = len(trainer.others)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column(no_wrap=True)
    grid.add_column(style="grey62")
    grid.add_row("Samples", Text("%d" % n, style="bold"),
                 "%.0f s of speech; at least %d needed" % (sum(trainer.seconds), MIN_CLIPS))
    if st:
        acc = st["accuracy"]
        color = "green" if acc >= 0.95 else "yellow" if acc >= 0.8 else "red"
        grid.add_row("Estimated accuracy", _bar(acc, color=color) + Text("  %3.0f%%" % (acc * 100), style="bold " + color),
                     "on your samples + %d comparison voices" % m)
        grid.add_row("Recognizes you", _bar(st["accepted"] / n) + Text("  %d/%d" % (st["accepted"], n)),
                     "consistency %.2f" % st["consistency"])
        if m:
            closest, closest_score = max(st["others"].items(), key=lambda kv: kv[1])
            grid.add_row("Rejects others", _bar(st["rejected"] / m) + Text("  %d/%d" % (st["rejected"], m)),
                         "closest: %s %.2f" % (closest, closest_score))
        if st["margin"] is not None:
            margin_color = "green" if st["margin"] > 0.1 else "yellow" if st["margin"] > 0 else "red"
            grid.add_row("Margin", Text("%+.2f" % st["margin"], style=margin_color),
                         "your weakest sample minus the closest other voice")
        grid.add_row("Threshold", Text("%.2f" % st["threshold"]), "")
    else:
        grid.add_row("Estimated accuracy", Text("…", style="grey62"), "after 2 samples")

    # one bar per sample: how well it matches the rest of your voice
    samples = Table.grid(padding=(0, 1))
    if st:
        for i, (score, text) in enumerate(list(zip(st["own"], trainer.texts))[-6:], start=max(1, n - 5)):
            ok = score >= st["threshold"]
            samples.add_row(Text("#%d" % i, style="grey62"), _bar(score, width=16, color="green" if ok else "red"),
                            Text("%.2f" % score, style="green" if ok else "red"),
                            Text((text[:48] + "…") if len(text) > 48 else text, style="grey62"))

    level = live.get("level", 0.0)
    status = {"listening": ("🎙", "listening…"), "hearing": ("🗣", "hearing you…"),
              "processing": ("⏳", "learning from that phrase…")}.get(live.get("state"), ("🎙", ""))
    mic = Text("%s Mic " % status[0]) + _bar(level, width=20, color="cyan") + Text("  " + status[1], style="grey62")

    body = [passage_panel, Panel(grid, title="Voice recognition", title_align="left", border_style="magenta")]
    if st:
        body.append(Panel(samples, title="Latest samples (how well each matches your voice)", title_align="left",
                          border_style="grey35"))
    body.append(mic)
    body.append(Text("Pause briefly between sentences. Ctrl+C finishes early.", style="grey50"))
    return Group(*body)


# ---------------------------------------------------------------- the session

def run(console):
    """Train interactively; returns the saved voiceprint summary, or None."""
    from rich.live import Live

    ex = speaker.extractor()
    with console.status("Preparing comparison voices (built-in macOS voices standing in for other people)…") as s:
        others = speaker.comparison_embeddings(ex, on_voice=lambda name: s.update(
            "Preparing comparison voices… [bold]%s[/]" % name))
    trainer = Trainer(others=others)
    live_state = {"state": "listening", "level": 0.0}
    clips = queue.Queue()
    results = queue.Queue()

    def on_level(rms, threshold):
        live_state["level"] = 0.8 * live_state["level"] + 0.2 * min(1.0, rms / (threshold * 2.5))

    def on_state(state):
        if state in ("hearing", "listening"):
            live_state["state"] = state
        elif state == "error":
            results.put(("error", None))

    def on_utterance(wav):
        live_state["state"] = "processing"
        clips.put(wav)

    def worker():
        while True:
            wav = clips.get()
            try:
                import wave
                with wave.open(wav) as w:
                    seconds = w.getnframes() / w.getframerate()
                emb = speaker.embed(ex, speaker.trim(speaker.read_wav(wav))) if seconds >= MIN_CLIP_S else None
                text = audio.transcribe(wav)  # deletes the WAV
            except Exception as e:  # keep training going
                audio.log("training sample failed: %s" % e)
                emb, seconds, text = None, 0.0, ""
            results.put(("sample", (emb, seconds, text)))

    threading.Thread(target=worker, daemon=True).start()
    listener = audio.Listener(on_utterance=on_utterance, on_state=on_state, on_level=on_level)

    error = None
    with audio.registered_session():  # the wake service lets go of the mic meanwhile
        listener.start()
        try:
            with Live(render(trainer, live_state), console=console, refresh_per_second=10,
                      transient=False) as live:
                while not trainer.finished:
                    try:
                        kind, value = results.get(timeout=0.1)
                    except queue.Empty:
                        live.update(render(trainer, live_state))
                        continue
                    if kind == "error":
                        error = "The microphone stopped. Check that your terminal app may use the mic " \
                                "(System Settings → Privacy & Security → Microphone)."
                        break
                    trainer.add(*value)
                    live_state["state"] = "listening"
                    live.update(render(trainer, live_state))
                live.update(render(trainer, live_state))
        except KeyboardInterrupt:
            pass
        finally:
            listener.stop()
    if error:
        console.print("[red]✗[/] " + error)
        return None
    if len(trainer.embeddings) < MIN_CLIPS or not trainer.stats:
        console.print("[yellow]⚠[/] Only %d samples; at least %d are needed. Nothing was saved."
                      % (len(trainer.embeddings), MIN_CLIPS))
        return None
    return trainer.save()

