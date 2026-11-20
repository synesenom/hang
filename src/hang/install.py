"""Run by `hang` when something is missing: everything after the Python environment, with progress.

setup.sh bootstraps Homebrew, Python and the Poetry environment; this installs
the speech tools (ffmpeg, whisper.cpp) with Homebrew, downloads the models with
progress bars, and checks that the terminal may use the microphone.
Safe to run again: finished steps are skipped.
"""
import os
import shutil
import subprocess
import tempfile
import urllib.request

from hang import ROOT

MODELS_DIR = os.path.join(ROOT, "models")
HF = "https://huggingface.co"
MODELS = [
    # (file, url, what it is for)
    ("ggml-base.en.bin", HF + "/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin",
     "speech recognition (whisper base.en)"),
    ("ggml-tiny.bin", HF + "/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin",
     "language check: English only (whisper tiny)"),
    ("ggml-silero-v5.1.2.bin", HF + "/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin",
     "voice activity detection (Silero)"),
    ("wespeaker_en_voxceleb_resnet34.onnx",
     "https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
     "wespeaker_en_voxceleb_resnet34.onnx",
     "speaker recognition: only your voice (WeSpeaker)"),
]
BREW = [("ffmpeg", "ffmpeg", "audio capture"), ("whisper-cli", "whisper-cpp", "local transcription")]


def missing():
    """What `setup` still has to do (empty when everything is in place)."""
    todo = [pkg for tool, pkg, _ in BREW if not shutil.which(tool)]
    todo += [name for name, _, _ in MODELS if not os.path.exists(os.path.join(MODELS_DIR, name))]
    return todo


def _ok(console, text):
    console.print("  [green]✓[/] " + text)


def _fail(console, text, detail=None):
    console.print("  [red]✗[/] " + text)
    if detail:
        console.print("      [grey62]%s[/]" % detail)


def brew_packages(console):
    for tool, pkg, why in BREW:
        if shutil.which(tool):
            _ok(console, "%s [grey62](%s)[/]" % (pkg, why))
            continue
        if not shutil.which("brew"):
            _fail(console, "%s needs Homebrew" % pkg, "Install it from https://brew.sh, then run ./hang again.")
            return False
        with console.status("Installing [bold]%s[/] with Homebrew (%s)…" % (pkg, why)):
            r = subprocess.run(["brew", "install", pkg], capture_output=True, text=True)
        if r.returncode != 0:
            _fail(console, "brew install %s failed" % pkg, (r.stderr or r.stdout).strip()[-400:])
            return False
        _ok(console, "%s installed [grey62](%s)[/]" % (pkg, why))
    return True


def download_models(console, extra=()):
    from rich.progress import (BarColumn, DownloadColumn, Progress, TextColumn, TimeRemainingColumn,
                               TransferSpeedColumn)

    os.makedirs(MODELS_DIR, exist_ok=True)
    todo = [m for m in list(MODELS) + list(extra) if not os.path.exists(os.path.join(MODELS_DIR, m[0]))]
    for name, _, why in MODELS:
        if os.path.exists(os.path.join(MODELS_DIR, name)):
            _ok(console, "%s [grey62](%s)[/]" % (name, why))
    if not todo:
        return True
    columns = (TextColumn("  [cyan]↓[/] {task.description}"), BarColumn(bar_width=28), DownloadColumn(),
               TransferSpeedColumn(), TimeRemainingColumn())
    with Progress(*columns, console=console, transient=True) as progress:
        for name, url, why in todo:
            dest = os.path.join(MODELS_DIR, name)
            task = progress.add_task(name, total=None)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "hang-setup"})
                with urllib.request.urlopen(req, timeout=60) as resp, open(dest + ".part", "wb") as out:
                    total = resp.headers.get("Content-Length")
                    progress.update(task, total=int(total) if total else None)
                    while True:
                        chunk = resp.read(1 << 16)
                        if not chunk:
                            break
                        out.write(chunk)
                        progress.advance(task, len(chunk))
                os.replace(dest + ".part", dest)
            except Exception as e:
                progress.stop()
                _fail(console, "downloading %s failed" % name, str(e))
                try:
                    os.unlink(dest + ".part")
                except OSError:
                    pass
                return False
            progress.remove_task(task)
            _ok(console, "%s downloaded [grey62](%s)[/]" % (name, why))
    return True


def check_microphone(console):
    """Record half a second: the first time, macOS asks to allow the mic for this terminal."""
    if os.environ.get("HANG_TEST_WAV"):
        _ok(console, "microphone [grey62](test recording)[/]")
        return True
    mic = os.environ.get("HANG_MIC", "default")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "mic.wav")
        with console.status("Checking microphone access (macOS may ask you to allow it)…"):
            r = subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "avfoundation",
                                "-i", ":" + mic, "-t", "0.5", path], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL)
        if r.returncode != 0 or not os.path.exists(path) or os.path.getsize(path) < 1000:
            _fail(console, "the microphone could not be opened",
                  "Allow your terminal app in System Settings → Privacy & Security → Microphone, "
                  "then run ./hang again.")
            return False
    _ok(console, "microphone")
    return True


def check_claude(console):
    if shutil.which(os.environ.get("HANG_CMD", "claude").split()[0]):
        _ok(console, "Claude Code")
        return True
    _fail(console, "Claude Code (`claude`) not found",
          "Install it: https://docs.claude.com/en/docs/claude-code — then run ./hang again.")
    return False


def run(console, mic=True):
    """All steps; True when everything is ready."""
    console.print("[bold]Setting up[/]")
    ok = brew_packages(console) and download_models(console)
    ok = check_claude(console) and ok
    if ok and mic:
        ok = check_microphone(console)
    return ok
