"""The `hang` command line (Click + rich), run by ./hang.

  ./hang         on: set up whatever is missing, train your voice the first time,
                 listen in the background now and at every login; show the status
  ./hang off     off: stop listening, and don't start at login
  ./hang talk    Claude Code in this folder, by voice
  ./hang train   retrain your voice, then try it out
"""
import os
import sys

import click


def _console():
    from rich.console import Console
    return Console(highlight=False)


def _interactive():
    return sys.stdin.isatty() and sys.stdout.isatty() and not os.environ.get("HANG_TEST_WAV")


def _banner(console):
    from rich.panel import Panel
    from rich.text import Text
    title = Text.assemble(("hang", "bold cyan"), ("  ·  talk to Claude Code, audio never leaves your computer", "grey62"))
    console.print(Panel(title, border_style="cyan", expand=False, padding=(0, 2)))


# ---------------------------------------------------------------- status

def _spoken(folder):
    import re
    return re.sub(r"[-_.]+", " ", folder).strip()


def _status(console):
    from rich.panel import Panel
    from rich.table import Table
    from hang import audio, speaker, wake

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold", no_wrap=True)
    grid.add_column()

    problem = speaker.available()
    info = speaker.info()
    if problem:
        grid.add_row("Your voice", "[yellow]⚠ unavailable:[/] %s" % problem)
    elif info:
        acc = info.get("accuracy")
        grid.add_row("Your voice", "[green]✓ trained[/] %s  [grey62]threshold %.2f%s[/]" % (
            info.get("trained", ""), info["threshold"],
            "  ·  est. accuracy %.0f%%" % (acc * 100) if acc is not None else ""))
    else:
        grid.add_row("Your voice", "[yellow]○ not trained[/] [grey62]— anyone's voice is accepted; run[/] ./hang train")

    english = audio.LANG == "en" and os.path.exists(audio.LANGID_MODEL)
    grid.add_row("English only", "[green]✓ on[/]" if english else "[grey62]off[/]")

    from hang import autostart
    grid.add_row("At login", "[green]✓ starts by itself[/]" if autostart.enabled() else
                 "[grey62]○ doesn't start by itself (HANG_LOGIN=0)[/]")

    state = wake.state()
    grid.add_row("Background", {
        "listening": "[green]● listening[/] [grey62]for “Claude, <folder>”[/]",
        "paused": "[yellow]◐ paused[/] [grey62]while a voice session is open[/]",
        "stopped": "[red]○ stopped[/] [grey62]— start with[/] ./hang",
    }[state])
    console.print(Panel(grid, title="Status", title_align="left", border_style="magenta"))

    say = Table(show_header=True, header_style="bold", box=None, padding=(0, 2))
    say.add_column("Say")
    say.add_column("What happens", style="grey70")
    folders = wake.folders()[:5]
    for f in folders:
        say.add_row("“Claude, %s”" % _spoken(f), "opens Claude Code in ~/Desktop/%s, where you left off" % f)
    say.add_row("[grey62]in a session:[/]", "")
    say.add_row("anything, then a 1 s pause", "sends it to Claude")
    say.add_row("… “amm” before the pause", "waits: keep talking, it is added to the prompt")
    say.add_row("“stop”", "interrupts Claude (or its talking)")
    say.add_row("“option two” / “yes” / your own words", "answers a question, permission or plan")
    say.add_row("“plan mode” / “accept edits”", "switches Claude Code's mode")
    say.add_row("“say that again” / “read me the whole answer”", "hear it again, or in full")
    say.add_row("“Claude, don't talk” / “Claude, talk”", "spoken updates off / on")
    say.add_row("“How is it going?”", "says what Claude is doing right now")
    say.add_row("“We're done” / “exit”", "exits Claude Code and closes the window")
    console.print(Panel(say, title="What to say", title_align="left", border_style="cyan"))


def _start_service(console, restart=False):
    from hang import wake
    if restart and wake.running():
        with console.status("Restarting the background service…"):
            wake.stop()
    if wake.running():
        return True
    with console.status("Starting the background service…"):
        pid, error = wake.start()
    if error:
        console.print("  [red]✗[/] %s" % error)
        return False
    console.print("  [green]✓[/] background service started [grey62](pid %d)[/]" % pid)
    return True


# ---------------------------------------------------------------- training

def _train(console, start_service=True):
    from rich.panel import Panel
    from hang import speaker, training, wake

    problem = speaker.available()
    if problem:
        console.print("  [red]✗[/] voice training needs %s" % problem)
        return False
    console.print(Panel(
        "You'll read a short passage aloud. While you read, the words you've said light up and the\n"
        "voice recognition learns your voice: watch [bold]Estimated accuracy[/] climb as each phrase is added.\n\n"
        "[grey62]Read at your normal pace and volume, from where you usually sit, with a short pause\n"
        "between sentences. It stops by itself when the passage is read. Nothing leaves your computer.[/]",
        title="Train your voice", title_align="left", border_style="cyan", padding=(1, 2)))
    if _interactive():
        click.prompt(click.style("  Press Enter to start", fg="cyan"), default="", show_default=False,
                     prompt_suffix="")
    result = training.run(console)
    if not result:
        return False
    console.print("  [green]✓[/] voice trained: estimated accuracy [bold]%.0f%%[/], threshold %.2f"
                  % (result["accuracy"] * 100, result["threshold"]))
    if start_service or wake.running():
        return _start_service(console, restart=True)  # load the new voiceprint
    return True


# ---------------------------------------------------------------- commands

@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx):
    """Talk to Claude Code by voice; audio never leaves your computer.

    Without a command: turn it on. Sets up whatever is missing, trains your
    voice the first time, listens in the background now and at every login,
    and shows the status. `hang off` turns it off.
    """
    if ctx.invoked_subcommand is not None:
        return
    from hang import install, speaker
    console = _console()
    _banner(console)
    if install.missing():
        if not install.run(console):
            sys.exit(1)
    if speaker.available() is None and not speaker.info():  # can train, not trained yet
        if not _train(console, start_service=False):
            console.print("  [yellow]⚠[/] continuing without voice recognition; train later with ./hang train")
    if not _start_service(console):
        sys.exit(1)
    from hang import autostart
    if os.environ.get("HANG_LOGIN", "1") != "0" and not autostart.enabled():
        autostart.enable()
        console.print("  [green]✓[/] starts by itself when you log in [grey62](./hang off to stop)[/]")
    _status(console)


@cli.command()
@click.option("--check", "check_only", is_flag=True, help="Only try out your current voiceprint, live.")
@click.option("--reset", is_flag=True, help="Delete your voiceprint instead (any voice is accepted again).")
def train(check_only, reset):
    """Retrain your voice, then try it out."""
    from hang import wake
    console = _console()
    if check_only:
        _banner(console)
        sys.exit(0 if _check(console) else 1)
    if reset:
        from hang import speaker, wake
        if speaker.forget():
            console.print("  [green]✓[/] voiceprint deleted")
            if wake.running():
                _start_service(console, restart=True)
        else:
            console.print("  [grey62]· there was no voiceprint[/]")
        return
    _banner(console)
    if not _train(console, start_service=wake.running()):
        sys.exit(1)
    if _interactive():
        console.print("\n  Now try it: say a few sentences and see whether each is recognized as you.")
        _check(console)


def _check(console):
    """Talk and see, phrase by phrase, whether it's recognized as you."""
    import queue
    import threading
    import time

    from rich.live import Live
    from rich.table import Table
    from hang import audio, speaker

    verifier = speaker.Verifier.load()
    if not verifier:
        console.print("  [yellow]⚠[/] no voiceprint yet; run ./hang train first")
        return False
    rows, clips = [], queue.Queue()

    def worker():
        while True:
            wav = clips.get()
            score = verifier.score(wav)
            lang = audio.detect_language(wav)
            text = audio.transcribe(wav) or ""
            rows.append((time.strftime("%H:%M:%S"), score, lang, text))

    def table():
        t = Table(title="Say anything (Ctrl+C to stop) · threshold %.2f" % verifier.threshold,
                  title_justify="left", header_style="bold", expand=False)
        for col in ("Time", "Score", "Verdict", "Language", "Heard"):
            t.add_column(col)
        for when, score, lang, text in rows[-10:]:
            you = score >= verifier.threshold
            english = not lang or lang[0] == "en" or lang[1] < audio.LANGID_MIN_P
            verdict = "[green]✓ you[/]" if you and english else "[red]✗ not you[/]" if not you else \
                "[red]✗ not English[/]"
            t.add_row(when, "%.2f" % score, verdict, "%s %.0f%%" % (lang[0], lang[1] * 100) if lang else "–",
                      text[:60])
        return t

    threading.Thread(target=worker, daemon=True).start()
    listener = audio.Listener(on_utterance=clips.put, on_state=lambda s: None)
    with audio.registered_session():
        listener.start()
        try:
            with Live(table(), console=console, refresh_per_second=4) as live:
                while True:
                    time.sleep(0.25)
                    live.update(table())
        except KeyboardInterrupt:
            pass
        finally:
            listener.stop()
    return True


@cli.command(hidden=True)
def start():
    """Start the background listening, nothing else (run at login)."""
    console = _console()
    sys.exit(0 if _start_service(console) else 1)


@cli.command()
def off():
    """Turn it off: stop listening, also at login."""
    from hang import autostart, wake
    console = _console()
    stopped, unhooked = wake.stop(), autostart.disable()
    console.print("  [green]✓[/] off: not listening, and won't start at login" if stopped or unhooked else
                  "  [grey62]· it was already off[/]")


@cli.command(context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
@click.argument("claude_args", nargs=-1, type=click.UNPROCESSED)
def talk(claude_args):
    """Claude Code here, by voice, where you left off.

    Extra arguments go to claude."""
    from hang import session
    session.main(claude_args)


@cli.command("service-run", hidden=True)
def service_run():
    """The background service itself (started by `hang`)."""
    from hang import wake
    wake.run()

