"""The background service: say "Claude, <folder>" to open Claude Code there.

Listens with the same local pipeline as `hang talk`. On "Claude, the other door"
it opens a new Terminal window in ~/Desktop/the-other-door running `hang talk`;
"Claude, listen" opens HANG_WAKE_DIR. While a voice session (or training) runs it
releases the mic, and resumes when the last one exits.

Configuration (environment variables, read at start):
  HANG_WAKE_ROOT   folder whose subfolders can be named (default: ~/Desktop)
  HANG_WAKE_DIR    folder for "Claude, listen" (default: the hang folder)
  HANG_WAKE_CMD    shell command to run on wake instead of opening Terminal
                 (runs in the chosen folder)
  HANG_WAKE_LOG    log file (default: $TMPDIR/hang-wake.log)
  HANG_WAKE_DEBUG  "1" to log every transcript (off: only wake events are logged)
"""
import difflib
import os
import queue
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time

from hang import ROOT, audio

TMP = tempfile.gettempdir()
WAKE_LOG = os.environ.get("HANG_WAKE_LOG", os.path.join(TMP, "hang-wake.log"))
PID_FILE = os.path.join(TMP, "hang-wake.pid")
WAKE_ROOT = os.path.realpath(os.path.expanduser(os.environ.get("HANG_WAKE_ROOT", "~/Desktop")))
WAKE_DIR = os.path.realpath(os.environ.get("HANG_WAKE_DIR", ROOT))
WAKE_CMD = os.environ.get("HANG_WAKE_CMD", "")
DEBUG = os.environ.get("HANG_WAKE_DEBUG", "0") == "1"
COOLDOWN_S = 15  # ignore further wake phrases while the new session starts
MIN_SCORE = 0.75  # how closely the spoken name must match a folder name
LISTEN = {"listen", "listen now", "listen up"}


def log(msg):
    audio.log("[wake] " + msg)


# ---------------------------------------------------------------- folders

def spoken(name):
    """'the-other-door' -> 'theotherdoor': compare without spaces, since whisper
    may write 'voice command' or 'voicecommand'."""
    return re.sub(r"[^a-z0-9]+", "", name.lower())


def folders():
    try:
        return sorted(n for n in os.listdir(WAKE_ROOT)
                      if not n.startswith(".") and os.path.isdir(os.path.join(WAKE_ROOT, n)))
    except OSError as e:
        log("cannot list %s: %s" % (WAKE_ROOT, e))
        return []


def match_folder(said):
    """Best folder in WAKE_ROOT for the spoken name, or None."""
    target = spoken(said)
    scored = sorted(((difflib.SequenceMatcher(None, target, spoken(n)).ratio(), n) for n in folders()),
                    reverse=True)
    if scored and scored[0][0] >= MIN_SCORE:
        return os.path.join(WAKE_ROOT, scored[0][1])
    return None


def wake_target(text):
    """Folder to open for this utterance, or None if it is not a wake phrase."""
    said = audio.addressed(text or "")
    if not said or audio.says_done(text):  # "Claude, done" is meant for a session
        return None
    if said in LISTEN:
        return WAKE_DIR
    folder = match_folder(said)
    if not folder:
        log("no folder in %s matches %r" % (WAKE_ROOT, said))
        audio.sound("Basso")
    return folder


def open_claude(folder):
    if WAKE_CMD:
        subprocess.Popen(WAKE_CMD, shell=True, cwd=folder, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    # A .command file opens in a new Terminal window without needing
    # Automation (Apple Events) permission. Carry over our PATH so `claude`,
    # whisper-cli and ffmpeg are found even if they are set up in .zshrc.
    script = os.path.join(TMP, "hang-open.command")
    with open(script, "w") as f:
        f.write("#!/bin/zsh\nexport PATH=%s HANG_CLOSE_WINDOW=1\ncd %s && exec %s talk\n" % (
            shlex.quote(os.environ.get("PATH", "")), shlex.quote(folder),
            shlex.quote(os.path.join(ROOT, "hang"))))
    os.chmod(script, 0o700)
    subprocess.run(["open", "-a", "Terminal", script], check=False)


# ---------------------------------------------------------------- the service

def run():
    """The service itself (`hang service-run`, started in the background by start())."""
    if running() and read_pid() != os.getpid():
        sys.exit("the voice service is already running (pid %s)" % read_pid())
    problem = audio.check_tools()
    if problem:
        sys.exit(problem)
    audio.LOG_PATH = WAKE_LOG
    audio.LOG_TRANSCRIPTS = DEBUG
    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))
    audio.load_verifier()  # with a voiceprint, only your voice can open Claude
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    events = queue.Queue()
    jobs = queue.Queue()

    def transcriber():
        while True:
            events.put(("text", audio.transcribe(jobs.get())))

    threading.Thread(target=transcriber, daemon=True).start()
    listener = audio.Listener(on_utterance=jobs.put, on_state=lambda s: events.put(("state", s)))

    last_wake = 0.0
    retry_at = 0.0
    log("started: say \"Claude, <folder in %s>\" or \"Claude, listen\" (%s)" % (WAKE_ROOT, WAKE_DIR))
    try:
        while True:
            busy = bool(audio.live_sessions())
            now = time.time()
            if busy and listener.active:
                listener.stop()
                log("session running; mic released")
            elif not busy and not listener.active and now >= retry_at:
                listener.start()
            try:
                kind, value = events.get(timeout=1.0)
            except queue.Empty:
                continue
            if kind == "state" and value == "error":
                listener.stop()
                retry_at = time.time() + 10  # mic unavailable: try again later
            elif kind == "text" and value:
                if DEBUG:
                    log("heard %r" % value)
                if busy or time.time() - last_wake < COOLDOWN_S:
                    continue
                folder = wake_target(value)
                if not folder:
                    continue
                last_wake = time.time()
                log("wake phrase heard; opening Claude Code in %s" % folder)
                audio.sound("Glass")
                listener.stop()  # hand the mic to the new session
                retry_at = last_wake + COOLDOWN_S
                open_claude(folder)
    except KeyboardInterrupt:
        pass
    finally:
        if listener.active:
            listener.stop()
        if read_pid() == os.getpid():
            os.unlink(PID_FILE)
        log("stopped")


def read_pid():
    try:
        return int(open(PID_FILE).read().strip())
    except (OSError, ValueError):
        return None


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def running():
    return _alive(read_pid())


def start():
    """Start the service in the background; returns (pid, None) or (None, error).
    Started from your terminal, so macOS credits the mic access to the terminal app."""
    if running():
        return read_pid(), None
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"))
    with open(WAKE_LOG, "a") as logf:
        proc = subprocess.Popen([sys.executable, "-m", "hang", "service-run"], cwd=ROOT, env=env,
                                stdin=subprocess.DEVNULL, stdout=logf, stderr=logf, start_new_session=True)
    for _ in range(30):
        time.sleep(0.1)
        if proc.poll() is not None:
            return None, "the service stopped right away; see %s" % WAKE_LOG
        if read_pid() == proc.pid:
            return proc.pid, None
    return proc.pid, None


def stop():
    """Stop the service; True if it was running."""
    pid = read_pid()
    if not _alive(pid):
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        if not _alive(pid):
            break
        time.sleep(0.1)
    return True


def state():
    """'listening', 'paused' (a voice session is open) or 'stopped'."""
    if not running():
        return "stopped"
    return "paused" if audio.live_sessions() else "listening"
