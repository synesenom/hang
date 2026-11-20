"""`hang talk`: Claude Code with hands-free voice input and spoken feedback.

Runs `claude` inside a pseudo-terminal and passes all I/O through untouched.
Meanwhile it listens to the microphone, transcribes each utterance locally and
types it into Claude Code; a 1 s pause sends it, unless it ended in "amm".
Claude Code hooks (hang.hook) report what Claude is doing; this process speaks
it with the mic off.

Spoken commands (said on their own, after a pause; a lead-in like "okay" or
"Claude," and a tail like "here" or "please" are fine):
  "send it" / "submit"                 press Enter
  "scratch that" / "clear"             clear the input line
  "stop" / "cancel" / "escape"         press Esc (interrupts Claude)
  "option one|two|three"               press 1/2/3 (permission prompts)
  "stop listening" / "start listening" pause / resume
  "We're done" / "Claude, done" / "exit"  exit Claude Code (and close the
                                       window if HANG_CLOSE_WINDOW=1)
  "Claude, don't talk" / "Claude, talk"  spoken feedback off / on
  "How is it going?"                   say what Claude is doing now (otherwise
                                       only phase changes are spoken)
Hotkey (Ctrl+\\) mutes/unmutes the mic. HANG_MODE=ptt: push-to-talk instead.
"""
import errno
import fcntl
import json
import os
import pty
import queue
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty

from hang import ROOT, audio
from hang.hook import PHASES
from hang.audio import (HALLUCINATIONS, command_core, is_done, Listener, Recorder, addressed, live_sessions,
                         load_verifier, log, normalize, sound, transcribe)

MODE = os.environ.get("HANG_MODE", "handsfree").lower()
HOTKEY = os.environ.get("HANG_HOTKEY", "ctrl+\\")
# Conversation mode (default): a 1 s pause sends what you said.
SUBMIT = os.environ.get("HANG_SUBMIT", "1") == "1"
CMD = os.environ.get("HANG_CMD", "claude")
# Claude Code's "send now" (chat:sendNow, Ctrl+X Ctrl+S): while Claude is working,
# Enter would queue the prompt until it is done; this hands it over right away.
# Only for claude: in a plain terminal Ctrl+S would freeze the output.
SEND = b"\x18\x13" if os.path.basename(CMD.split()[0]) == "claude" else b"\r"
CLOSE_WINDOW = os.environ.get("HANG_CLOSE_WINDOW", "0") == "1"
SPEAK = os.environ.get("HANG_SPEAK", "1") == "1"
VOICE = os.environ.get("HANG_VOICE", "")

# "amm" before a pause means "I'm not finished": the text is typed but not sent.
FILLER = re.compile(r"\b(?:a+h*m{2,}|ah+m+|u+h*m+|h+m{2,}|hm|u+h+|huh|e+r+m+)\b[,.…!?]*", re.I)
# whisper leaves out fillers unless its prompt is full of them.
# Starting with "Claude," also teaches it the name, so "Claude, don't talk" is
# less often heard as "Cloth, don't talk".
FILLER_PROMPT = "Claude, umm, let me think, amm... So, uh, I want to, hmm, change this. Amm"

ESC = b"\x1b"
CTRL_C = b"\x03"


def hotkey_sequences(spec):
    """Byte sequences a terminal may send for ctrl+<char>: the legacy control
    byte plus kitty keyboard protocol (CSI u) and xterm modifyOtherKeys forms,
    since Claude Code may enable those."""
    spec = spec.strip().lower()
    if not spec.startswith("ctrl+") or len(spec) != 6:
        sys.exit("HANG_HOTKEY must look like 'ctrl+<char>', got %r" % spec)
    ch = spec[5]
    code = ord(ch)
    return [bytes([ord(ch.upper()) & 0x1F]), b"\x1b[%d;5u" % code, b"\x1b[27;5;%d~" % code]


HOTKEY_SEQS = hotkey_sequences(HOTKEY)
HOTKEY_LABEL = "Ctrl+" + HOTKEY[5:]


def set_title(text):
    # OSC 0 sets the terminal title: a status indicator that does not disturb
    # Claude Code's TUI rendering.
    try:
        os.write(sys.stdout.fileno(), b"\x1b]0;" + text.encode() + b"\x07")
    except OSError:
        pass


# Spoken commands: matched only when the whole utterance is the command.
COMMANDS = {
    "send": {"send", "send it", "send that", "submit", "submit it", "go ahead"},
    "clear": {"scratch that", "clear", "clear that", "delete that", "start over"},
    "escape": {"stop", "cancel", "escape", "interrupt"},
    "pause": {"stop listening", "pause listening", "mute"},
    "resume": {"start listening", "resume listening", "unmute"},
    "1": {"option one", "option 1", "number one", "number 1"},
    "2": {"option two", "option 2", "number two", "number 2"},
    "3": {"option three", "option 3", "number three", "number 3"},
}


QUIET = {"dont talk", "do not talk", "stop talking", "be quiet", "no talking", "quiet", "shut up", "silence",
         "quiet please", "no talk"}
QUIET_UNNAMED = {"dont talk", "do not talk", "stop talking", "be quiet", "no talking"}
TALK = {"talk", "talk to me", "start talking", "you can talk", "talk again", "speak", "you can speak"}
TALK_UNNAMED = {"start talking", "talk to me", "you can talk", "talk again", "you can speak"}
STATUS = {"how is it going", "hows it going", "how are you doing", "how are we doing", "hows it looking",
          "how is it looking", "how far are you", "how far along are you", "whats the status", "what is the status",
          "status", "status update", "progress", "progress update", "what are you doing", "whatre you doing",
          "what are you doing now", "where are you", "where are we", "are you done", "are you still working"}


def match_command(text):
    core, named = command_core(text)
    if not core:
        return None
    if is_done(core, named):
        return "done"
    if core in QUIET_UNNAMED or (named and core in QUIET):
        return "quiet"
    if core in TALK_UNNAMED or (named and core in TALK):
        return "talk"
    if core in STATUS:
        return "status"
    for name, phrases in COMMANDS.items():
        if core in phrases:
            return name
    return None


def close_window_later():
    """Close this Terminal window once its process has exited (closing it while
    the process runs would make Terminal ask for confirmation)."""
    try:
        tty_name = os.ttyname(sys.stdin.fileno())
    except OSError:
        return
    script = ('tell application "Terminal"\n'
              '  repeat with w in windows\n'
              '    repeat with t in tabs of w\n'
              '      if tty of t is "%s" then\n'
              '        close w\n'
              '        return\n'
              '      end if\n'
              '    end repeat\n'
              '  end repeat\n'
              'end tell' % tty_name)
    pid = os.getpid()
    # Detached helper: waits for this process to exit, then closes the window.
    subprocess.Popen(["/bin/sh", "-c",
                      "while kill -0 %d 2>/dev/null; do sleep 0.2; done; sleep 0.5; "
                      "osascript -e \"$0\"" % pid, script],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=open(audio.LOG_PATH, "a"), start_new_session=True)


def split_filler(text):
    """('please list the files', True) for 'Please list the files, amm.':
    fillers are removed; True if the utterance ended in one."""
    words = text.split()
    held = bool(words) and bool(FILLER.fullmatch(words[-1]))
    # A trailing "am" is a filler only after a comma or on its own ("Yes, I am." is not).
    if words and re.fullmatch(r"am[,.…!?]*", words[-1], re.I) and (
            len(words) == 1 or words[-2][-1] in ",.…"):
        held = True
        text = " ".join(words[:-1])
    clean = FILLER.sub(" ", text)
    clean = re.sub(r"\s+([,.!?])", r"\1", re.sub(r"\s+", " ", clean)).strip(" ,")
    return clean, held


def hook_settings():
    """Claude Code settings (passed with --settings, so they apply only to this
    session) that run hang.hook for spoken status. The hook runs straight from
    this Python (no Poetry start-up): it runs before every tool call."""
    def hook(arg, matcher=None):
        cmd = "PYTHONPATH=%s %s -m hang.hook %s" % (
            shlex.quote(os.path.join(ROOT, "src")), shlex.quote(sys.executable), arg)
        entry = {"hooks": [{"type": "command", "command": cmd}]}
        if matcher:
            entry["matcher"] = matcher
        return [entry]
    return json.dumps({"hooks": {"UserPromptSubmit": hook("prompt"),
                                 "Stop": hook("stop"),
                                 "PreToolUse": hook("tool"),
                                 "Notification": hook("notify")}})


def get_winsize(fd):
    return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)


def main(args=()):
    problem = audio.check_tools()
    if problem:
        sys.exit("hang talk: " + problem)
    if MODE not in ("handsfree", "ptt"):
        sys.exit("HANG_MODE must be 'handsfree' or 'ptt'")
    if not os.isatty(sys.stdin.fileno()):
        sys.exit("hang talk: must be run in an interactive terminal")

    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    winsize = get_winsize(stdin_fd)

    speak_dir = speak_fd = None
    if SPEAK and MODE == "handsfree" and os.path.basename(CMD.split()[0]) == "claude":
        speak_dir = tempfile.mkdtemp(prefix="hang-speak-")
        fifo = os.path.join(speak_dir, "fifo")
        os.mkfifo(fifo, 0o600)
        speak_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        os.open(fifo, os.O_WRONLY)  # keep a writer open so the reader never sees EOF
        os.environ["HANG_SPEAK_FIFO"] = fifo

    pid, master = pty.fork()
    if pid == 0:
        fcntl.ioctl(sys.stdout.fileno(), termios.TIOCSWINSZ, winsize)
        argv = CMD.split() + list(args)
        if speak_fd is not None:
            argv[1:1] = ["--settings", hook_settings()]
        try:
            os.execvp(argv[0], argv)
        except OSError as e:
            sys.stderr.write("hang talk: cannot run %s: %s\n" % (argv[0], e))
            os._exit(127)

    # Background threads post events here; the wake pipe interrupts select().
    events = queue.Queue()
    wake_r, wake_w = os.pipe()

    def post(kind, value=None):
        events.put((kind, value))
        os.write(wake_w, b"x")

    # Transcription runs sequentially on its own thread so capture never stalls.
    jobs = queue.Queue()

    def transcriber():
        while True:
            wav = jobs.get()
            post("text", transcribe(wav, prompt=None if audio.PROMPT else FILLER_PROMPT,
                                    keep=lambda t: match_command(t) is not None, beep=True))

    threading.Thread(target=transcriber, daemon=True).start()

    def on_winch(_sig, _frm):
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, get_winsize(stdin_fd))
        except OSError:
            pass

    signal.signal(signal.SIGWINCH, on_winch)

    state = {"paused": False, "busy": False, "quit_at": None, "say": None, "unmute": False,
             "quitting": False, "quiet": False, "pending": []}

    def idle_title():
        if MODE == "ptt":
            return "claude  (\U0001F399 %s to talk)" % HOTKEY_LABEL
        if listener and not listener.active:
            return "claude  (\U0001F507 muted, %s to unmute)" % HOTKEY_LABEL
        if state["paused"]:
            return "claude  (⏸ paused, say \"start listening\")"
        return "claude  (\U0001F399 listening)"

    def type_keys(data):
        os.write(master, data)

    def speak_line(line):
        """A line from hang.hook. '!' = urgent: interrupt and say now (anything
        still queued is dropped). Otherwise it is said after what is queued."""
        urgent = line.startswith("!")
        text = line.lstrip("!").strip()
        if state["quiet"] or not text:
            return
        if urgent or not (state["say"] and state["say"].poll() is None):
            state["pending"] = []
            say(text)
        else:
            state["pending"].append(text)

    def say(text):
        """Speak with the mic off, so the voice is not typed back into Claude."""
        if state["say"] and state["say"].poll() is None:
            state["say"].terminate()  # newer status replaces older
        if listener.active:
            listener.stop()
            state["unmute"] = True
        log("speaking: %r" % text)
        cmd = ["say"] + (["-v", VOICE] if VOICE else []) + [text]
        state["say"] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def after_speech():
        if state["say"] and state["say"].poll() is not None:
            state["say"] = None
            if state["pending"] and not state["quiet"]:
                say(state["pending"].pop(0))  # mic stays off between back-to-back notes
                return
            if state["unmute"] and not state["quitting"]:
                state["unmute"] = False
                time.sleep(0.2)  # let the room go quiet
                listener.start()
                set_title(idle_title())

    def status_report():
        """What to answer to "How is it going?", from the hooks' status.json."""
        status = {}
        if speak_dir:
            try:
                with open(os.path.join(speak_dir, "status.json")) as f:
                    status = json.load(f)
            except (OSError, ValueError):
                pass
        if status.get("working"):
            if status.get("update"):
                return "Still working. Latest: %s" % status["update"]
            phase = PHASES.get(status.get("phase"), "").rstrip(".")
            last = status.get("last")
            said = "Still working" + (": %s" % phase.lower() if phase else "") + "."
            return said + (" Last step: %s" % last if last else "")
        if status.get("summary"):
            return "I'm done. %s" % status["summary"]
        return "Waiting for you."

    def quit_claude():
        """Interrupt whatever Claude is doing and type /exit; if Claude has not
        exited a few seconds later, the main loop terminates it."""
        state["quitting"] = True
        sound("Bottle")
        type_keys(ESC)
        time.sleep(0.3)
        type_keys(b"\x15/exit")  # Ctrl+U clears any half-dictated text first
        time.sleep(0.3)
        type_keys(b"\r")
        state["quit_at"] = time.time() + 4
        if listener and listener.active:
            listener.stop()
        if CLOSE_WINDOW:
            close_window_later()

    def deliver(text):
        if MODE == "ptt":
            state["busy"] = False
        set_title(idle_title())
        if not text:
            return
        if MODE == "handsfree":
            if normalize(text) in HALLUCINATIONS:
                log("dropped likely hallucination %r" % text)
                return
            cmd = match_command(text)
            if cmd == "done":
                log("command: done")
                quit_claude()
                return
            if cmd == "quiet":
                log("command: quiet")
                state["quiet"], state["pending"] = True, []
                if state["say"] and state["say"].poll() is None:
                    state["say"].terminate()
                sound("Bottle")
                return
            if cmd == "status":
                log("command: status")
                say(status_report())  # answered even when told not to talk: it was asked for
                return
            if cmd == "talk":
                log("command: talk")
                state["quiet"] = False
                if speak_fd is not None:
                    say("Okay, I'll talk again.")
                return
            if state["paused"]:
                if cmd == "resume":
                    state["paused"] = False
                    sound("Tink")
                    set_title(idle_title())
                return
            if cmd:
                log("command: %s" % cmd)
                if cmd == "send":
                    type_keys(SEND)
                elif cmd == "clear":
                    type_keys(b"\x15")  # Ctrl+U: delete to start of line
                elif cmd == "escape":
                    type_keys(ESC)
                elif cmd == "pause":
                    state["paused"] = True
                    sound("Bottle")
                    set_title(idle_title())
                elif cmd in ("1", "2", "3"):
                    type_keys(cmd.encode())
                return  # "resume" while not paused: nothing to do
        text, held = split_filler(text)
        if text:
            type_keys((text + " ").encode())
            sound("Pop")
        if held:
            log("ends with 'amm': not sending yet")
            set_title("\u270D go on…  (pause sends, \"amm\" keeps going)")
            return
        if SUBMIT and text:
            time.sleep(0.15)  # let the TUI treat the send keys as separate keypresses
            type_keys(SEND)

    # --- push-to-talk ---------------------------------------------------
    rec = Recorder() if MODE == "ptt" else None

    def ptt_hotkey():
        if state["busy"]:
            sound("Funk")
            return
        if not rec.active:
            rec.start()
            sound("Tink")
            set_title("\U0001F534 REC  (%s to stop, Esc to cancel)" % HOTKEY_LABEL)
            return
        duration = time.time() - rec.started
        wav = rec.stop()
        if wav is None:
            sound("Basso")
            set_title("hang: mic error (see %s)" % audio.LOG_PATH)
            return
        if duration < 0.3:
            os.unlink(wav)
            set_title(idle_title())
            return
        sound("Pop")
        set_title("⏳ transcribing…")
        state["busy"] = True
        jobs.put(wav)

    # --- hands-free -----------------------------------------------------
    listener = None
    if MODE == "handsfree":
        listener = Listener(
            on_utterance=lambda wav: (jobs.put(wav), post("state", "transcribing")),
            on_state=lambda s: post("state", s),
        )

    def handsfree_hotkey():
        if state["say"] and state["say"].poll() is None:
            state["say"].terminate()
            state["unmute"] = False  # stay muted: the user asked for it
            state["say"] = None
            sound("Bottle")
            set_title(idle_title())
            return
        if listener.active:
            listener.stop()
            sound("Bottle")
        else:
            listener.start()
            sound("Tink")
        set_title(idle_title())

    def on_state(s):
        if s == "error":
            sound("Basso")
            set_title("hang: mic error (see %s)" % audio.LOG_PATH)
            listener.stop()
        elif s == "hearing" and not state["paused"]:
            set_title("\U0001F5E3 hearing…")
        elif s == "transcribing" and not state["paused"]:
            set_title("⏳ transcribing…")
        else:
            set_title(idle_title())

    def filter_input(data):
        """Strip hotkey sequences from keyboard input, acting on each one."""
        out = b""
        while data:
            if rec and rec.active and (data == ESC or data.startswith(CTRL_C)):
                rec.cancel()
                sound("Basso")
                set_title(idle_title())
                log("recording cancelled")
                data = data[1:]
                continue
            for seq in HOTKEY_SEQS:
                if data.startswith(seq):
                    (ptt_hotkey if rec else handsfree_hotkey)()
                    data = data[len(seq):]
                    break
            else:
                if not (rec and rec.active):  # swallow keys while recording (ptt)
                    out += data[:1]
                data = data[1:]
        return out

    # Only one session listens: if another one already is, start muted.
    other_session = live_sessions()
    session_file = os.path.join(audio.SESSIONS_DIR, str(os.getpid()))
    try:
        os.makedirs(audio.SESSIONS_DIR, exist_ok=True)
        open(session_file, "w").close()
    except OSError:
        session_file = None

    old_attrs = termios.tcgetattr(stdin_fd)
    tty.setraw(stdin_fd)
    log("started: mode=%s cmd=%s model=%s submit=%s" % (MODE, CMD, audio.MODEL, SUBMIT))
    load_verifier()
    if listener and other_session:
        log("another voice session is listening (pid %s); starting muted" % other_session[0])
    elif listener:
        listener.start()
    set_title(idle_title())

    status = None
    try:
        while True:
            try:
                fds = [stdin_fd, master, wake_r] + ([speak_fd] if speak_fd is not None else [])
                r, _, _ = select.select(fds, [], [], 0.2 if state["say"] else 0.5)
            except InterruptedError:
                continue
            if state["quit_at"] and time.time() > state["quit_at"]:
                log("claude did not exit after /exit; terminating it")
                state["quit_at"] = None
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            if state["say"]:
                after_speech()
            if not r:
                # macOS does not always report EOF on the pty master when the
                # child exits, so poll for it explicitly.
                done, st = os.waitpid(pid, os.WNOHANG)
                if done:
                    status = st
                    break
                continue
            if master in r:
                try:
                    data = os.read(master, 65536)
                except OSError as e:
                    if e.errno == errno.EIO:  # child exited
                        break
                    raise
                if not data:
                    break
                os.write(stdout_fd, data)
            if stdin_fd in r:
                data = os.read(stdin_fd, 4096)
                if not data:
                    break
                data = filter_input(data)
                if data:
                    os.write(master, data)
            if speak_fd is not None and speak_fd in r:
                try:
                    lines = os.read(speak_fd, 65536).decode(errors="replace").splitlines()
                except BlockingIOError:
                    lines = []
                lines = [l.strip() for l in lines if l.strip()]
                for line in lines:
                    speak_line(line)
            if wake_r in r:
                os.read(wake_r, 4096)
                while not events.empty():
                    kind, value = events.get()
                    if kind == "text":
                        deliver(value)
                    elif kind == "state" and listener:
                        on_state(value)
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSANOW, old_attrs)
        if rec and rec.active:
            rec.cancel()
        if listener and listener.active:
            listener.stop()
        if state["say"] and state["say"].poll() is None:
            state["say"].terminate()
        if speak_dir:
            shutil.rmtree(speak_dir, ignore_errors=True)
        set_title("")
        if session_file:
            try:
                os.unlink(session_file)
            except OSError:
                pass
    if status is None:
        _, status = os.waitpid(pid, 0)
    sys.exit(os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else status >> 8)
