"""`hang talk`: Claude Code with hands-free voice input and spoken feedback.

Runs `claude` inside a pseudo-terminal and passes all I/O through untouched.
Meanwhile it listens to the microphone, transcribes each utterance locally and
types it into Claude Code; a 1 s pause sends it, unless it ended in "amm".
Claude Code hooks (hang.hook) report what Claude is doing, and everything
Claude Code prints is also fed to a terminal emulator (hang.screen), so the
session sees its dialogs, permission mode and input box like you would.

Spoken commands (said on their own, after a pause; a lead-in like "okay" or
"Claude," and a tail like "here" or "please" are fine):
  "send it" / "submit"                 press Enter
  "scratch that" / "clear"             clear the input line
  "delete the last sentence"           remove what you dictated last
  "replace <this> with <that>"         fix a word in what you dictated (before sending)
  "read it back"                       read out what is in the input box
  "stop" / "cancel" / "escape"         press Esc (interrupts Claude); while
                                       Claude is talking, stops the talking
  "say that again"                     repeat the last thing Claude said
  "read me the whole answer"           read out Claude's last reply in full
  "plan mode" / "accept edits" /       switch Claude Code's permission mode
  "auto mode" / "manual mode"
  "compact the conversation" / "clear the conversation" / "change the model"
  / "slash <command>"                  Claude Code's slash commands
  "stop listening" / "start listening" pause / resume
  "We're done" / "Claude, done" / "exit"  exit Claude Code (and close the
                                       window if HANG_CLOSE_WINDOW=1)
  "Claude, don't talk" / "Claude, talk"  spoken feedback off / on
  "How is it going?"                   say what Claude is doing now
While a dialog is open (a question, a permission prompt, the plan, the folder
trust check, /model, ...), what you say answers it: see hang.dialogs.
Long sentences appear while you are still talking: once what you've said so far
is longer than any command, it is typed live and corrected as whisper hears
more; the pause then fixes it to the final transcript and sends it.
Hotkey (Ctrl+\\) mutes/unmutes the mic. HANG_MODE=ptt: push-to-talk instead.
"""
import collections
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
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import tty

from hang import ROOT, audio, dialogs
from hang.dialogs import NUMBER_WORDS, choice_of
from hang.hook import PHASES, read_status
from hang.screen import Screen
from hang.audio import (HALLUCINATIONS, command_core, says_done, Listener, Recorder, live_sessions,
                         load_verifier, log, normalize, sound, transcribe)

MODE = os.environ.get("HANG_MODE", "handsfree").lower()
HOTKEY = os.environ.get("HANG_HOTKEY", "ctrl+\\")
# Conversation mode (default): a 1 s pause sends what you said.
SUBMIT = os.environ.get("HANG_SUBMIT", "1") == "1"
CMD = os.environ.get("HANG_CMD", "claude")
IS_CLAUDE = os.path.basename(CMD.split()[0]) == "claude"
# Claude Code's "send now" (chat:sendNow, Ctrl+X Ctrl+S): while Claude is working,
# Enter would queue the prompt until it is done; this hands it over right away.
# Only for claude: in a plain terminal Ctrl+S would freeze the output.
SEND = b"\x18\x13" if IS_CLAUDE else b"\r"
CLOSE_WINDOW = os.environ.get("HANG_CLOSE_WINDOW", "0") == "1"
SPEAK = os.environ.get("HANG_SPEAK", "1") == "1"
VOICE = os.environ.get("HANG_VOICE", "")
# Talking over Claude: the mic stays on while it speaks, and a short command
# ("stop", "option two") cuts it off. "auto": only with a voiceprint, which
# tells your voice from Claude's (the training checks macOS voices are rejected).
BARGE_IN = os.environ.get("HANG_BARGE_IN", "auto").lower()
VOCAB = os.environ.get("HANG_VOCAB", "1") == "1"
# Type long sentences while you are still talking (see live_keys).
LIVE = os.environ.get("HANG_LIVE", "1") == "1"
# Pick up the folder's last conversation (claude --continue) instead of a new one.
RESUME = os.environ.get("HANG_RESUME", "1") == "1"
RESUME_FLAGS = ("-c", "--continue", "-r", "--resume", "--session-id", "-p", "--print")

# "amm" before a pause means "I'm not finished": the text is typed but not sent.
FILLER = re.compile(r"\b(?:a+h*m{2,}|ah+m+|u+h*m+|h+m{2,}|hm|u+h+|huh|e+r+m+)\b[,.…!?]*", re.I)
# whisper leaves out fillers unless its prompt is full of them.
# Starting with "Claude," also teaches it the name, so "Claude, don't talk" is
# less often heard as "Cloth, don't talk".
FILLER_PROMPT = "Claude, umm, let me think, amm... So, uh, I want to, hmm, change this. Amm"

ESC = b"\x1b"
CTRL_C = b"\x03"
CTRL_U = b"\x15"
SHIFT_TAB = b"\x1b[Z"


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
    "undo": {"delete the last sentence", "remove the last sentence", "delete the last part", "undo",
             "undo that", "take that back", "delete the last bit", "scratch the last sentence"},
    "read_back": {"read it back", "read back", "read that back", "read it back to me", "what did i say",
                  "what have i said", "read me what i said", "read my prompt", "read the prompt",
                  "what did i write"},
    "repeat": {"say that again", "repeat that", "come again", "what did you say", "repeat", "pardon",
               "say it again", "can you repeat that", "repeat please", "one more time"},
    "read_reply": {"read me the whole answer", "read me the answer", "read the whole answer", "read me everything",
                   "read it all", "read me the full answer", "read the full answer", "read me your answer",
                   "read the answer", "read me the whole thing", "read the whole thing", "read me the reply",
                   "read me the details", "tell me more", "more details", "read me all of it"},
    "mode:plan mode": {"plan mode", "switch to plan mode", "go to plan mode", "enter plan mode",
                       "plan mode on", "planning mode", "switch to planning mode"},
    "mode:auto mode": {"auto mode", "switch to auto mode", "go to auto mode", "auto mode on"},
    "mode:accept edits": {"accept edits", "accept edits mode", "auto accept edits", "switch to accept edits",
                          "accept edits on", "accept all edits"},
    "mode:manual mode": {"manual mode", "normal mode", "default mode", "switch to manual mode",
                         "switch to normal mode", "leave plan mode", "exit plan mode", "manual mode on"},
    "slash:/compact": {"compact", "compact the conversation", "compact the context", "compact it",
                       "compact the chat"},
    "slash:/clear": {"clear the conversation", "new conversation", "start a new conversation",
                     "clear the context", "clear the chat", "fresh start", "start fresh"},
    "slash:/model": {"change the model", "switch model", "switch the model", "change model", "choose a model",
                     "pick a model", "which model"},
}
SLASH = re.compile(r"^(?:slash|/)\s*([a-z][a-z -]{0,30})$")
REPLACE = re.compile(r"^\W*(?:replace|change|swap)\s+(.+?)\s+(?:with|to|for|by)\s+(.+?)[.!?]*$", re.I)

QUIET = {"dont talk", "do not talk", "stop talking", "be quiet", "no talking", "quiet", "shut up", "silence",
         "quiet please", "no talk"}
QUIET_UNNAMED = {"dont talk", "do not talk", "stop talking", "be quiet", "no talking"}
TALK = {"talk", "talk to me", "start talking", "you can talk", "talk again", "speak", "you can speak"}
TALK_UNNAMED = {"start talking", "talk to me", "you can talk", "talk again", "you can speak"}
STATUS = {"how is it going", "hows it going", "how are you doing", "how are we doing", "hows it looking",
          "how is it looking", "how far are you", "how far along are you", "whats the status", "what is the status",
          "status", "status update", "progress", "progress update", "what are you doing", "whatre you doing",
          "what are you doing now", "where are you", "where are we", "are you done", "are you still working"}
# Commands that work the same whether or not a dialog is open.
GLOBAL = ("done", "quiet", "talk", "status", "pause", "resume")


def match_command(text):
    core, named = command_core(text)
    if not core:
        return None
    if says_done(text):
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
    m = SLASH.match(core)
    if m:
        return "slash:/" + m.group(1).strip().replace(" ", "-")
    choice = choice_of(text)
    if choice and not choice[1] and re.search(r"\b(option|number|choice)\b", core):
        return str(choice[0])  # "option two": press 2 (a bare "First." only counts in a dialog)
    return None


# ---------------------------------------------------------------- dictation

# Spoken file names: "session dot py" -> "session.py", "test underscore e2e" -> "test_e2e".
EXTENSIONS = ("py|js|ts|tsx|jsx|md|json|yaml|yml|toml|sh|txt|html|css|go|rs|rb|java|kt|swift|c|h|cpp|"
              "lock|cfg|ini|sql|csv|env|xml|vue|php")
DOT = re.compile(r"\b([\w-]+) dot (%s)\b" % EXTENSIONS, re.I)
UNDERSCORE = re.compile(r"(\w) underscore (\w)", re.I)


def tidy_dictation(text):
    text = DOT.sub(lambda m: "%s.%s" % (m.group(1), m.group(2).lower()), text)
    return UNDERSCORE.sub(r"\1_\2", text)


def vocabulary(folder, limit=40):
    """The project's file names as words ("test e2e", "pyproject"), for
    whisper's prompt, so it knows them. (Names with dots and underscores in the
    prompt make whisper add odd punctuation; "session dot py" is spelled out
    by tidy_dictation instead.)"""
    try:
        out = subprocess.run(["git", "ls-files"], cwd=folder, capture_output=True, text=True, timeout=3)
        paths = out.stdout.split() if out.returncode == 0 else []
    except (OSError, subprocess.TimeoutExpired):
        paths = []
    if not paths:
        for top, dirs, files in os.walk(folder):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("node_modules", "venv", "__pycache__",
                                                                              "build", "dist", "target")]
            if top.count(os.sep) - folder.count(os.sep) > 2:
                dirs[:] = []
            paths += [os.path.relpath(os.path.join(top, f), folder) for f in files if not f.startswith(".")]
            if len(paths) > 2000:
                break
    names = []
    for p in sorted(paths, key=lambda p: (p.count("/"), p)):
        if re.search(r"\.(png|jpg|jpeg|gif|ico|svg|pdf|bin|lock)$", p, re.I):
            continue
        stem = os.path.splitext(os.path.basename(p))[0]
        word = re.sub(r"[-_.]+", " ", stem).strip().lower()
        if word and word not in names and len(word) < 30 and not word.isdigit():
            names.append(word)
        if len(names) >= limit:
            break
    return names


def live_text(partial):
    """What to show of a partial transcript: without fillers and without its
    last word, which is often cut off mid-word and changes next time."""
    words = split_filler(partial)[0].split()
    return tidy_dictation(" ".join(words[:-1])) if len(words) > audio.MAX_COMMAND_WORDS else ""


def live_keys(old, new):
    """Keys that turn the typed text `old` into `new`: backspace to where they
    differ, then type the rest (the start of a sentence rarely changes)."""
    same = 0
    while same < min(len(old), len(new)) and old[same] == new[same]:
        same += 1
    return b"\x7f" * len(old[same:]) + new[same:].encode()


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


def replace_in(draft, text):
    """The draft with "replace <old> with <new>" applied, or None if it isn't
    such a command or <old> isn't in the draft (then it's a prompt)."""
    m = REPLACE.match(text or "")
    if not m or not draft:
        return None
    old, new = m.group(1).strip(" ,\"'"), m.group(2).strip(" ,\"'")
    hits = list(re.finditer(r"\b%s\b" % re.escape(old), draft, re.I))
    if not hits:
        return None
    last = hits[-1]  # the most recent mention
    return draft[:last.start()] + new + draft[last.end():]


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
                                 "PreToolUse": hook("tool")}})


def has_conversation(folder):
    """Has Claude Code been used in this folder before? (It keeps conversations
    in ~/.claude/projects/<the folder's path, with - for every other character>.)"""
    name = re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(folder))
    path = os.path.join(os.path.expanduser("~/.claude/projects"), name)
    try:
        return any(f.endswith(".jsonl") for f in os.listdir(path))
    except OSError:
        return False


def get_winsize(fd):
    return fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8)


def wav_seconds(wav):
    try:
        return max(0, os.path.getsize(wav) - 44) / (2.0 * audio.RATE)
    except OSError:
        return 0.0


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
    rows, cols = struct.unpack("HHHH", winsize)[:2]
    screen = Screen(cols or 80, rows or 24)
    folder = os.path.basename(os.getcwd())

    speak_dir = speak_fd = None
    # The hooks run even with HANG_SPEAK=0 (which only starts the session quiet).
    if MODE == "handsfree" and IS_CLAUDE:
        speak_dir = tempfile.mkdtemp(prefix="hang-speak-")
        fifo = os.path.join(speak_dir, "fifo")
        os.mkfifo(fifo, 0o600)
        speak_fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
        os.open(fifo, os.O_WRONLY)  # keep a writer open so the reader never sees EOF
        os.environ["HANG_SPEAK_FIFO"] = fifo

    args = list(args)
    if IS_CLAUDE and RESUME and not any(a in RESUME_FLAGS for a in args) and has_conversation(os.getcwd()):
        args.insert(0, "--continue")  # where you left off; "clear the conversation" starts afresh
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

    # whisper's prompt: the project's file names (so they are spelled right),
    # then the fillers (whisper follows the end of its prompt most closely).
    names = vocabulary(os.getcwd()) if VOCAB else []
    prompt = ((audio.PROMPT + " ") if audio.PROMPT else "") + (
        "In this project: %s. " % ", ".join(names) if names else "") + FILLER_PROMPT
    log("vocabulary: %d file names" % len(names))

    def keep(text):
        """Kept even if it sounds foreign or scores just under your voice:
        commands, and short answers while a dialog is open."""
        return match_command(text) is not None or (state["menu_sig"] is not None and bool(command_core(text)[0]))

    # Transcription runs sequentially on its own thread so capture never stalls.
    jobs = queue.Queue()

    def transcriber():
        while True:
            wav, t0, t1, utt = jobs.get()
            post("text", (transcribe(wav, prompt=prompt, keep=keep, beep=True), t0, t1, utt))

    threading.Thread(target=transcriber, daemon=True).start()

    # Partial clips while you talk, newest first (older ones are skipped):
    # transcribed for typing live, once your voice and English are confirmed.
    partials = queue.Queue()
    voice_checked = {}

    def partial_worker():
        while True:
            utt, pcm = partials.get()
            while not partials.empty():
                utt, pcm = partials.get()
            if utt <= state["final_utt"] or not audio.SERVER.ready:
                continue
            wav = audio.write_wav(pcm)
            try:
                if utt not in voice_checked:
                    voice_checked.clear()
                    voice_checked[utt] = audio.voice_ok(wav)
                if voice_checked[utt]:
                    post("partial", (utt, audio.run_whisper(wav, prompt) or ""))
            finally:
                os.unlink(wav)

    if LIVE and MODE == "handsfree":
        threading.Thread(target=partial_worker, daemon=True).start()

    def on_winch(_sig, _frm):
        try:
            size = get_winsize(stdin_fd)
            fcntl.ioctl(master, termios.TIOCSWINSZ, size)
            r, c = struct.unpack("HHHH", size)[:2]
            screen.resize(c, r)
        except OSError:
            pass

    signal.signal(signal.SIGWINCH, on_winch)

    state = {"paused": False, "busy": False, "quit_at": None, "say": None, "unmute": False,
             "quitting": False, "quiet": not SPEAK, "pending": [], "last_said": None,
             "spoken": collections.deque(maxlen=8),  # [start, end or None, text] of recent speech
             "draft": [],  # what you dictated since the last send (None: typed on the keyboard too)
             "menu_sig": None, "screen_at": 0.0, "screen_checked": True,
             "live": "", "live_utt": 0,  # text typed while you are still talking, and for which utterance
             "final_utt": 0}  # the last utterance whose final transcript arrived

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

    def from_claude(data):
        os.write(stdout_fd, data)
        screen.feed(data)
        state["screen_at"], state["screen_checked"] = time.time(), False

    def pump(seconds):
        """Pass Claude Code's output through for a moment, so the screen is
        current after keys we typed (the main loop is busy meanwhile)."""
        end = time.time() + seconds
        while True:
            left = end - time.time()
            if left <= 0:
                return
            r, _, _ = select.select([master], [], [], left)
            if r:
                try:
                    data = os.read(master, 65536)
                except OSError:
                    return
                if not data:
                    return
                from_claude(data)

    def speak_line(line):
        """A line from hang.hook. '!' = urgent: interrupt and say now (anything
        still queued is dropped). Otherwise it is said after what is queued."""
        urgent = line.startswith("!")
        text = line.lstrip("!").strip()
        if state["quiet"] or not text:
            return
        if urgent or not speaking():
            state["pending"] = []
            say(text)
        else:
            state["pending"].append(text)

    def speaking():
        return bool(state["say"] and state["say"].poll() is None)

    def speech_ended():
        for item in state["spoken"]:
            if item[1] is None:
                item[1] = time.time()

    def say(text):
        """Speak. Without barge-in the mic is off meanwhile, so the voice is not
        typed back into Claude; with it, only short commands count meanwhile."""
        if speaking():
            state["say"].terminate()  # newer status replaces older
        speech_ended()
        if listener and listener.active and not barge_in:
            listener.stop()
            state["unmute"] = True
        log("speaking: %r" % text)
        state["last_said"] = text
        state["spoken"].append([time.time(), None, text])
        cmd = ["say"] + (["-v", VOICE] if VOICE else []) + [text]
        state["say"] = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop_speaking():
        state["pending"] = []
        if speaking():
            state["say"].terminate()
        speech_ended()

    def after_speech():
        if state["say"] and state["say"].poll() is not None:
            state["say"] = None
            speech_ended()
            if state["pending"] and not state["quiet"]:
                say(state["pending"].pop(0))  # mic stays off between back-to-back notes
                return
            if state["unmute"] and not state["quitting"]:
                state["unmute"] = False
                time.sleep(0.2)  # let the room go quiet
                listener.start()
                set_title(idle_title())

    def over_speech(t0, t1):
        """Did this utterance overlap Claude's speech (or its echo right after)?"""
        if t0 is None:
            return False
        return any(s - 0.2 < t1 and (e is None or t0 < e + 0.6) for s, e, _ in state["spoken"])

    def status_report():
        """What to answer to "How is it going?", from the hooks' status.json."""
        status = read_status() if speak_dir else {}
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
        stop_speaking()
        sound("Bottle")
        type_keys(ESC)
        time.sleep(0.3)
        type_keys(CTRL_U + b"/exit")  # Ctrl+U clears any half-dictated text first
        time.sleep(0.3)
        type_keys(b"\r")
        state["quit_at"] = time.time() + 4
        if listener and listener.active:
            listener.stop()
        if CLOSE_WINDOW:
            close_window_later()

    # --- dialogs ----------------------------------------------------------

    def check_screen():
        """Read out a dialog when one appears (once the screen has settled)."""
        if state["screen_checked"] or time.time() - state["screen_at"] < 0.4:
            return
        state["screen_checked"] = True
        menu = screen.menu()
        sig = menu.signature() if menu else None
        if sig == state["menu_sig"]:
            return
        state["menu_sig"] = sig
        if not menu:
            return
        log("dialog: %s %r" % (menu.kind, menu.question))
        if state["quiet"]:
            sound("Glass")  # something to answer; "what were the options?" reads it out
            return
        state["pending"] = []
        say(dialogs.announce(menu, screen.plan_text(), folder))

    def run_steps(steps):
        for kind, value in steps:
            if kind == "keys":
                type_keys(value)
            elif kind == "wait":
                pump(value)
            elif kind == "say":
                say(value)  # asked for: said even when told not to talk
            elif kind == "prompt":
                type_keys(CTRL_U + tidy_dictation(value).encode() + b" ")
                pump(0.15)
                type_keys(SEND)
                state["draft"] = []
        pump(0.3)
        state["screen_checked"] = False  # read out what comes next (the next question)

    def answer_dialog(menu, text):
        clean, _ = split_filler(text)  # in a dialog every answer is final
        steps = dialogs.answer(menu, clean or text, screen.plan_text(), folder)
        log("dialog %s: %r -> %s" % (menu.kind, text, [s[0] for s in steps]))
        sound("Pop")
        run_steps(steps)

    def switch_mode(target):
        """Shift+Tab until the footer shows the mode you asked for."""
        for _ in range(7):
            if screen.mode() == target:
                say("%s on." % target.capitalize())
                return
            type_keys(SHIFT_TAB)
            pump(0.5)
        log("could not switch to %s; the screen ends with %r" % (target, [l for l in screen.lines() if l][-4:]))
        say("Sorry, I couldn't switch to %s." % target)

    def slash(command):
        if screen.input_text():
            say("Your prompt isn't empty. Send it or clear it first.")
            return
        type_keys(command.encode())
        pump(0.4)
        type_keys(b"\r")

    def read_reply():
        reply = (read_status() if speak_dir else {}).get("reply")
        say(reply or "There's no answer to read yet.")

    def on_partial(utt, text):
        """Type what you've said so far, if it can only be a prompt."""
        if utt <= state["final_utt"] or state["paused"] or state["menu_sig"] is not None or speaking():
            return
        shown = live_text(text)
        if utt != state["live_utt"]:
            state["live"], state["live_utt"] = "", utt
        if not shown or shown == state["live"]:
            return
        if not state["live"]:
            log("typing live (utterance %d)" % utt)
        type_keys(live_keys(state["live"], shown))
        state["live"] = shown

    def take_live(utt):
        """The text typed live for this utterance (now final), to be replaced."""
        if utt is not None:
            state["final_utt"] = max(state["final_utt"], utt)
        typed = state["live"] if state["live_utt"] == utt else ""
        state["live"] = ""
        return typed

    def deliver(text, t0=None, t1=None, utt=None):
        if MODE == "ptt":
            state["busy"] = False
        set_title(idle_title())
        live = take_live(utt)
        if live:
            # Typed while you talked: fixed up below for a prompt, else taken back.
            final = tidy_dictation(split_filler(text)[0]) if text else ""
            if not text or match_command(text) or screen.menu() or not final:
                type_keys(b"\x7f" * len(live))
                live = ""
        if not text:
            return
        if MODE == "handsfree":
            if normalize(text) in HALLUCINATIONS:
                log("dropped likely hallucination %r" % text)
                if live:
                    type_keys(b"\x7f" * len(live))
                return
            cmd = match_command(text)
            menu = screen.menu()
            if barge_in and over_speech(t0, t1):
                # Said while Claude was talking: only short commands and answers count.
                if not command_core(text)[0]:
                    log("ignored dictation while speaking: %r" % text)
                    sound("Funk")
                    if live:
                        type_keys(b"\x7f" * len(live))
                    return
                if speaking():
                    log("interrupted the speech")
                    stop_speaking()
                if cmd == "escape" and not menu:
                    stop_speaking()
                    return  # "stop" while Claude talks: stop the talking, not Claude
            if cmd == "done":
                log("command: done")
                quit_claude()
                return
            if cmd == "quiet":
                log("command: quiet")
                state["quiet"] = True
                stop_speaking()
                sound("Bottle")
                return
            if cmd == "status":
                log("command: status")
                say(status_report())  # answered even when told not to talk: it was asked for
                return
            if cmd == "talk":
                log("command: talk")
                state["quiet"] = False
                say("Okay, I'll talk again.")
                return
            if state["paused"]:
                if cmd == "resume":
                    state["paused"] = False
                    sound("Tink")
                    set_title(idle_title())
                return
            if menu and cmd not in GLOBAL:
                answer_dialog(menu, text)
                return
            draft = " ".join(state["draft"] or [])
            fixed = replace_in(draft, text) if state["draft"] else None
            if fixed is not None:
                log("replaced in the draft")
                type_keys(CTRL_U + fixed.encode() + b" ")
                state["draft"] = [fixed]
                sound("Pop")
                return
            if cmd:
                log("command: %s" % cmd)
                if cmd == "send":
                    type_keys(SEND)
                    state["draft"] = []
                elif cmd == "clear":
                    type_keys(CTRL_U)  # delete to start of line
                    state["draft"] = []
                elif cmd == "escape":
                    type_keys(ESC)
                elif cmd == "pause":
                    state["paused"] = True
                    sound("Bottle")
                    set_title(idle_title())
                elif cmd == "undo":
                    if state["draft"]:
                        last = state["draft"].pop()
                        type_keys(b"\x7f" * (len(last) + 1))
                        sound("Pop")
                    elif state["draft"] is None:
                        say("I can only undo what you said, not what you typed.")
                    else:
                        say("There's nothing to undo.")
                elif cmd == "read_back":
                    typed = screen.input_text()
                    say(typed if typed else "The prompt is empty.")
                elif cmd == "repeat":
                    say(state["last_said"] or "I haven't said anything yet.")
                elif cmd == "read_reply":
                    read_reply()
                elif cmd.startswith("mode:"):
                    switch_mode(cmd[5:])
                elif cmd.startswith("slash:"):
                    slash(cmd[6:])
                elif cmd.isdigit():
                    type_keys(cmd.encode())
                return  # "resume" while not paused: nothing to do
        text, held = split_filler(text)
        text = tidy_dictation(text)
        if live:
            log("live text fixed up to the final transcript")
        if text or live:
            type_keys(live_keys(live, text + " " if text else ""))
            if state["draft"] is not None:
                state["draft"].append(text)
            sound("Pop")
        if held:
            log("ends with 'amm': not sending yet")
            set_title("✍ go on…  (pause sends, \"amm\" keeps going)")
            return
        if SUBMIT and text:
            time.sleep(0.15)  # let the TUI treat the send keys as separate keypresses
            type_keys(SEND)
            state["draft"] = []

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
        jobs.put((wav, None, None, None))

    # --- hands-free -----------------------------------------------------
    listener = None
    if MODE == "handsfree":
        def on_utterance(wav):
            end = time.time()
            jobs.put((wav, end - wav_seconds(wav), end, listener.utterance))
            post("state", "transcribing")

        listener = Listener(on_utterance=on_utterance, on_state=lambda s: post("state", s),
                            on_partial=(lambda utt, pcm: partials.put((utt, pcm))) if LIVE else None)

    def handsfree_hotkey():
        if speaking():
            stop_speaking()
            state["unmute"] = False  # stay muted: the user asked for it
            state["say"] = None
            sound("Bottle")
            if not barge_in:
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
        if out:
            # Typed by hand: "delete the last sentence" can't know what is there any more.
            state["draft"] = [] if out in (b"\r", SEND) else None
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
    load_verifier()
    audio.SERVER.start()  # transcription ~4x faster once it's up; whisper-cli until then
    barge_in = MODE == "handsfree" and (BARGE_IN == "1" or (BARGE_IN == "auto" and audio.VERIFIER is not None))
    log("started: mode=%s cmd=%s model=%s submit=%s barge_in=%s resume=%s" % (
        MODE, CMD, audio.MODEL, SUBMIT, barge_in, "--continue" in args))
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
                r, _, _ = select.select(fds, [], [], 0.2)
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
            if MODE == "handsfree" and not state["quitting"]:
                check_screen()
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
                from_claude(data)
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
                        deliver(*value)
                    elif kind == "partial":
                        on_partial(*value)
                    elif kind == "state" and listener:
                        on_state(value)
    finally:
        termios.tcsetattr(stdin_fd, termios.TCSANOW, old_attrs)
        if rec and rec.active:
            rec.cancel()
        if listener and listener.active:
            listener.stop()
        audio.SERVER.stop()
        if speaking():
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
