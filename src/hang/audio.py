"""Microphone capture, speech segmentation and local transcription.

Shared by the Claude Code session (`hang talk`), the background wake service and
the voice training. Audio never leaves the Mac: ffmpeg captures it, whisper.cpp
transcribes it on-device, temp WAVs are deleted immediately.
"""
import array
import collections
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from contextlib import contextmanager

from hang import ROOT

MODEL = os.environ.get("HANG_MODEL", os.path.join(ROOT, "models", "ggml-base.en.bin"))
VAD_MODEL = os.environ.get("HANG_VAD_MODEL", os.path.join(ROOT, "models", "ggml-silero-v5.1.2.bin"))
LANG = os.environ.get("HANG_LANG", "en")
# English only: a small multilingual model detects each clip's language first and
# other languages are dropped (base.en would turn them into made-up English).
LANGID_MODEL = os.environ.get("HANG_LANGID_MODEL", os.path.join(ROOT, "models", "ggml-tiny.bin"))
# Confidence needed to drop a clip as not English. Low, because the small model is
# unsure on short clips: Hungarian "Ezt felismered?" comes out as German 0.28 or
# Arabic 0.16, while short English sentences reliably come out as English. The
# log shows each sentence's verdict, to check your own English isn't caught.
LANGID_MIN_P = float(os.environ.get("HANG_LANGID_MIN_P", "0.1"))
PROMPT = os.environ.get("HANG_PROMPT", "")
MIC = os.environ.get("HANG_MIC", "default")
SILENCE_MS = int(os.environ.get("HANG_SILENCE_MS", "1000"))
MIN_LEVEL = float(os.environ.get("HANG_MIN_LEVEL", "250"))
SOUNDS = os.environ.get("HANG_SOUNDS", "1") == "1"
TEST_WAV = os.environ.get("HANG_TEST_WAV", "")
# Each entry point points this at its own log (session, service, training).
LOG_PATH = os.environ.get("HANG_LOG", os.path.join(tempfile.gettempdir(), "hang.log"))
LOG_TRANSCRIPTS = True  # the background service turns this off: ambient speech stays out of logs
# Each running session (and training) leaves <pid> here so the wake service pauses its mic.
SESSIONS_DIR = os.path.join(tempfile.gettempdir(), "hang-sessions")

RATE = 16000
FRAME = 480  # 30 ms of 16-bit mono samples
FRAME_MS = 30


def log(msg):
    try:
        with open(LOG_PATH, "a") as f:
            f.write(time.strftime("%H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def sound(name):
    if not SOUNDS:
        return
    path = "/System/Library/Sounds/%s.aiff" % name
    if os.path.exists(path):
        subprocess.Popen(["afplay", "-v", "0.4", path],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def check_tools():
    """None if whisper-cli, ffmpeg and the model are there, else what is missing."""
    for tool in ("whisper-cli", "ffmpeg"):
        if not shutil.which(tool):
            return "'%s' not found. Run ./setup.sh first." % tool
    if not os.path.exists(MODEL):
        return "whisper model not found at %s. Run ./setup.sh first." % MODEL
    return None


# ---------------------------------------------------------------- transcription

NOISE = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*")  # [BLANK_AUDIO], (music), *sigh*
HALLUCINATIONS = {"you", "thank you", "thanks for watching", "bye", ""}


def clean_transcript(text):
    text = NOISE.sub(" ", text)
    text = "".join(c if c.isprintable() else " " for c in text)
    return re.sub(r"\s+", " ", text).strip()



VERIFIER = None  # set by load_verifier(); None = accept every voice


def load_verifier():
    """Turn on the speaker check if a voiceprint exists (see `hang train`)."""
    global VERIFIER
    from hang import speaker
    VERIFIER = speaker.Verifier.load(log)


DETECTED = re.compile(r"auto-detected language: (\w+) \(p = ([\d.]+)\)")


def detect_language(wav):
    """(language, probability) of a clip, or None if it cannot be told."""
    if LANG != "en" or not os.path.exists(LANGID_MODEL):
        return None
    try:
        out = subprocess.run(["whisper-cli", "-m", LANGID_MODEL, "-f", wav, "-l", "auto", "-dl"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = DETECTED.search(out.stderr + out.stdout)
    return (m.group(1), float(m.group(2))) if m else None


def transcribe(wav, prompt=None, keep=None, beep=False):
    """Text of the clip, or None if it is another voice, not English, or empty.
    keep(text): kept even if it sounds foreign (default: addressed to Claude;
    a session passes its spoken commands, as names and short words fool the
    language check). beep: play a sound when a clip is dropped as not English
    (in a session; the background service hears too much ambient speech)."""
    if VERIFIER:
        try:
            ok, score = VERIFIER.accepts(wav)
        except Exception as e:
            ok, score = True, 0.0
            log("speaker check failed, accepting: %s" % e)
        if not ok:
            log("ignored another voice (score %.2f < %.2f)" % (score, VERIFIER.threshold))
            os.unlink(wav)
            return None
    # Language detection is unreliable on clips built around a name ("Claude,
    # voice command" can come out as Swedish) or a single word, so a non-English
    # verdict drops speech unless keep() says it is a command.
    lang = detect_language(wav)
    foreign = bool(lang and lang[0] != "en" and lang[1] >= LANGID_MIN_P)
    cmd = ["whisper-cli", "-m", MODEL, "-f", wav, "-l", LANG, "-nt", "-np"]
    if os.path.exists(VAD_MODEL):
        # Silero VAD drops non-speech (keyboard, coughs) so whisper does not
        # hallucinate text from noise.
        cmd += ["--vad", "-vm", VAD_MODEL]
    if prompt or PROMPT:
        cmd += ["--prompt", prompt or PROMPT]
    t0 = time.time()
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    finally:
        try:
            os.unlink(wav)  # never keep the audio around
        except OSError:
            pass
    if out.returncode != 0:
        log("whisper-cli failed: %s" % out.stderr.strip()[-500:])
        return None
    text = clean_transcript(out.stdout)
    heard = " [%s %.2f]" % lang if lang else ""
    if foreign and text and not (keep or addressed)(text):
        log("ignored non-English speech (%s, p=%.2f)" % lang)
        if beep:
            sound("Funk")  # so you know it was not sent
        return None
    if foreign and text:
        log("kept despite language check (%s, p=%.2f): a command" % lang)
    if LOG_TRANSCRIPTS:
        log("transcribed in %.2fs%s: %r" % (time.time() - t0, heard, text))
    return text


def normalize(text):
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


# How whisper spells "Claude" (also used by hang-wake).
NAMES = {"claude", "cloud", "clod", "claud", "clawed", "clode", "klaud", "claudia", "clawd", "klod", "klaude",
         "claudes"}
# Further mishearings, accepted only in short spoken commands ("Cloth, don't talk.",
# "Close. Don't talk."), where they can't be mistaken for anything else.
LOOSE_NAMES = {"cloth", "clot", "close", "clothes", "clause", "claw", "clued", "clyde"}
NAME = r"(?:hey |ok |okay )?(?:%s)" % "|".join(sorted(NAMES))
ADDRESSED = re.compile("^" + NAME + " (.+)$")  # "Claude, <rest>"


def addressed(text):
    """'Claude, the other door.' -> 'the other door'; None if not addressed to Claude."""
    m = ADDRESSED.match(normalize(text))
    return m.group(1) if m else None


# Words around a spoken command that don't change it: "Okay, we're done here, thanks."
LEAD_WORDS = {"ok", "okay", "alright", "right", "so", "and", "now", "please", "well", "um", "uh", "hey",
              "yes", "yeah", "great", "good", "cool", "thanks", "oh", "fine"}
TAIL_WORDS = {"please", "now", "here", "then", "thanks", "today", "for", "already"}
MAX_COMMAND_WORDS = 8  # longer utterances are prompts, never commands


def command_core(text):
    """('we are done', named) for "Okay Claude, we are done here.": the command
    words without lead-in or trailing filler, and whether Claude was named.
    (None, False) for anything too long to be a command."""
    words = normalize(text).replace("all right", "alright").split()
    if not words or len(words) > MAX_COMMAND_WORDS:
        return None, False
    named = False
    while len(words) > 1 and (words[0] in LEAD_WORDS or words[0] in NAMES or words[0] in LOOSE_NAMES):
        named = named or words[0] not in LEAD_WORDS
        words.pop(0)
    while len(words) > 1 and (words[-1] in TAIL_WORDS or words[-1] in NAMES):
        named = named or words[-1] in NAMES
        words.pop()
    return " ".join(words), named


# "We're done" and friends; "Claude, done" (whisper often hears "Claude Dunn").
_DONE_WORDS = ("done", "dunn", "dun", "dunne", "donne", "finished")
DONE_NAMED = set(_DONE_WORDS) | {"bye", "good night", "goodnight"}
DONE = ({p + w for p in ("were ", "we are ", "im ", "i am ", "all ", "were all ", "we are all ", "weve ",
                         "we have ", "ive ", "i have ", "thats ", "that is ", "think were ", "i think were ")
         for w in _DONE_WORDS}
        | {"exit", "quit", "goodbye", "good bye", "were down", "we are down", "thats all", "that is all",
           "end session", "end the session", "close session", "close the session"})


def is_done(core, named=False):
    """Is this command (see command_core) "exit Claude Code"? Also "Exit, we're done."."""
    if not core:
        return False
    if core in DONE or (named and core in DONE_NAMED):
        return True
    first, _, rest = core.partition(" ")
    return first in ("exit", "quit", "close") and (rest in DONE or rest in DONE_NAMED)


def says_done(text):
    """Does this utterance ask to exit Claude Code? Also when the phrase is
    repeated or combined ("We're done. We're done.", "We're done. Exit."): every
    sentence must then be a done phrase on its own."""
    core, named = command_core(text)
    if is_done(core, named):
        return True
    if not core:  # too long (or empty) to be a command
        return False
    parts = [p for p in re.split(r"[.!?;]+", text) if normalize(p)]
    return len(parts) > 1 and all(is_done(*command_core(p)) for p in parts)


def write_wav(pcm):
    fd, path = tempfile.mkstemp(prefix="vc-", suffix=".wav")
    os.close(fd)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return path


def mic_command():
    if TEST_WAV:
        # Replay the file in real time, then keep the stream open with silence.
        # HANG_TEST_RATE > 1 replays faster than real time; pauses are counted in
        # audio frames, not wall-clock time, so segmentation is unchanged.
        rate = os.environ.get("HANG_TEST_RATE", "1")
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-readrate", rate, "-i", TEST_WAV,
                "-af", "apad", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
            "-f", "avfoundation", "-i", ":" + MIC,
            "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"]


class Listener:
    """Hands-free: streams the mic, segments speech with an adaptive energy
    gate, and hands each utterance (as a temp WAV) to `on_utterance`."""

    def __init__(self, on_utterance, on_state, on_level=None):
        self.on_utterance = on_utterance
        self.on_state = on_state
        self.on_level = on_level  # (rms, speech threshold) every 30 ms, for level meters
        self.proc = None
        self.thread = None

    @property
    def active(self):
        return self.proc is not None

    def start(self):
        self.proc = subprocess.Popen(mic_command(), stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=open(LOG_PATH, "a"))
        self.thread = threading.Thread(target=self._run, args=(self.proc,), daemon=True)
        self.thread.start()
        log("listening")

    def stop(self):
        proc, self.proc = self.proc, None
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        log("mic off")

    def _run(self, proc):
        nbytes = FRAME * 2
        preroll = collections.deque(maxlen=10)  # 300 ms before speech onset
        floor = None
        speech, voiced, loud_run, quiet_ms = [], 0, 0, 0
        in_speech = False
        while True:
            chunk = proc.stdout.read(nbytes)
            if not chunk or len(chunk) < nbytes:
                if proc is self.proc:
                    log("mic stream ended (ffmpeg exit %s); check mic permission" % proc.poll())
                    self.on_state("error")
                return
            if proc is not self.proc:
                return
            samples = array.array("h", chunk)
            rms = math.sqrt(sum(s * s for s in samples) / FRAME)
            if floor is None:
                floor = rms
            start_th = max(floor * 3.0, MIN_LEVEL)
            if self.on_level:
                self.on_level(rms, start_th)
            end_th = max(floor * 2.0, MIN_LEVEL * 0.6)

            if not in_speech:
                preroll.append(chunk)
                if rms > start_th:
                    loud_run += 1
                    if loud_run >= 3:  # 90 ms of sound: speech onset
                        in_speech = True
                        speech = list(preroll)
                        voiced, quiet_ms = loud_run, 0
                        self.on_state("hearing")
                else:
                    loud_run = 0
                    floor = 0.95 * floor + 0.05 * rms  # track background noise
                continue

            speech.append(chunk)
            if rms > end_th:
                voiced += 1
                quiet_ms = 0
            else:
                quiet_ms += FRAME_MS
            too_long = len(speech) * FRAME_MS > 30000
            if quiet_ms >= SILENCE_MS or too_long:
                in_speech, loud_run = False, 0
                preroll.clear()
                if voiced * FRAME_MS >= 300:
                    self.on_utterance(write_wav(b"".join(speech)))
                else:
                    self.on_state("listening")
                speech = []


class Recorder:
    """Push-to-talk: record between two hotkey presses."""

    def __init__(self):
        self.proc = None
        self.path = None
        self.started = 0.0

    @property
    def active(self):
        return self.proc is not None

    def start(self):
        fd, self.path = tempfile.mkstemp(prefix="vc-", suffix=".wav")
        os.close(fd)
        self.started = time.time()
        if TEST_WAV:
            self.proc = subprocess.Popen(["cat"], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            return
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats", "-y",
             "-f", "avfoundation", "-i", ":" + MIC,
             "-ac", "1", "-ar", str(RATE), "-c:a", "pcm_s16le", self.path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=open(LOG_PATH, "a"),
        )

    def stop(self):
        """Stop recording; return the WAV path (or None if it failed)."""
        proc, self.proc = self.proc, None
        try:
            proc.stdin.write(b"q")  # ffmpeg: graceful stop, finalizes WAV header
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if TEST_WAV:
            shutil.copyfile(TEST_WAV, self.path)
        if not os.path.exists(self.path) or os.path.getsize(self.path) < 1000:
            log("recording failed (ffmpeg exit %s); check mic permission" % proc.returncode)
            self.discard()
            return None
        return self.path

    def cancel(self):
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None
        self.discard()

    def discard(self):
        if self.path and os.path.exists(self.path):
            os.unlink(self.path)
        self.path = None


def live_sessions():
    """PIDs of other running voice sessions (stale entries are removed)."""
    alive = []
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return alive
    for name in names:
        try:
            pid = int(name)
            if pid != os.getpid():
                os.kill(pid, 0)
                alive.append(pid)
        except (ValueError, ProcessLookupError):
            try:
                os.unlink(os.path.join(SESSIONS_DIR, name))
            except OSError:
                pass
        except PermissionError:
            alive.append(pid)
    return alive


@contextmanager
def registered_session():
    """While inside, the wake service releases the mic (it treats this process
    like an open voice session)."""
    path = os.path.join(SESSIONS_DIR, str(os.getpid()))
    try:
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        open(path, "w").close()
    except OSError:
        path = None
    try:
        yield
    finally:
        if path:
            try:
                os.unlink(path)
            except OSError:
                pass
