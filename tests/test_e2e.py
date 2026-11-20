#!/usr/bin/env python3
"""End-to-end tests for ./hang: synthesized speech (macOS `say`) stands in for
the microphone, `cat` or a fake `claude` for Claude Code, a silent fake `say` for
spoken feedback. No mic needed; nothing touches your real voiceprint or service.

    python3 tests/test_e2e.py            # all, in parallel
    python3 tests/test_e2e.py amm wake   # tests whose name contains any of these

Each test runs in its own process with its own temp folder, several at once, and
the test audio is replayed faster than real time (HANG_TEST_RATE).
"""
import fcntl
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HANG = os.path.join(ROOT, "hang")
TMP = tempfile.mkdtemp(prefix="hang-test-")
sys.path.insert(0, os.path.join(ROOT, "src"))


def read_until(fd, needle, timeout):
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([fd], [], [], 0.1)
        if r:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            if needle and needle in buf:
                return buf
    return buf


def speech_wav(name, parts, voice=None):
    """parts: phrases (str), (macOS voice, phrase) pairs, or pauses in seconds (float)."""
    inputs, labels = [], []
    for i, p in enumerate(parts):
        path = os.path.join(TMP, "%s%d.wav" % (name, i))
        if isinstance(p, (str, tuple)):
            v, text = p if isinstance(p, tuple) else (voice, p)
            aiff = path + ".aiff"
            subprocess.run(["say", "-o", aiff] + (["-v", v] if v else []) + [text], check=True)
            src = ["-i", aiff]
        else:
            src = ["-f", "lavfi", "-t", str(p), "-i", "anullsrc=r=16000:cl=mono"]
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y"] + src + ["-ar", "16000", "-ac", "1", path], check=True)
        inputs += ["-i", path]
        labels.append("[%d:a]" % i)
    out = os.path.join(TMP, name + ".wav")
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y"] + inputs +
                   ["-filter_complex", "".join(labels) + "concat=n=%d:v=0:a=1" % len(parts), out], check=True)
    return out


def env_for(name, wav, **extra):
    """Isolated: own TMPDIR (session registry, service pid), voiceprint and logs."""
    tmpdir = os.path.join(TMP, "tmp-" + name)
    os.makedirs(tmpdir, exist_ok=True)
    env = dict(os.environ, HANG_CMD="cat", HANG_TEST_WAV=wav, HANG_SOUNDS="0", TMPDIR=tmpdir, HANG_TEST_RATE="2",
               HANG_LOG=os.path.join(TMP, name + ".log"), HANG_WAKE_LOG=os.path.join(TMP, name + ".log"),
               HANG_VOICEPRINT=os.path.join(TMP, name + "-voiceprint.json"))
    env.update(extra)
    return env


def spawn_talk(name, wav, **extra):
    env = env_for(name, wav, **extra)
    pid, fd = pty.fork()
    if pid == 0:
        # A wide terminal, like a real one: dialogs are drawn without wrapping.
        fcntl.ioctl(1, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 160, 0, 0))
        os.execve(HANG, [HANG, "talk"], env)
    read_until(fd, b"\x1b]0;claude", 20)  # ready once it sets the window title
    return pid, fd, env["HANG_LOG"]


def finish(pid, fd, failures):
    try:
        os.write(fd, b"\x04\x04")  # EOF to cat -> child exits -> wrapper exits
    except OSError:
        pass
    end = time.time() + 10
    while time.time() < end:
        read_until(fd, None, 0.2)  # keep draining so the wrapper never blocks
        if os.waitpid(pid, os.WNOHANG)[0]:
            return
    os.kill(pid, 9)
    failures.append("wrapper did not exit after child exited")


def log_of(path):
    return open(path).read() if path and os.path.exists(path) else ""


# ---------------------------------------------------------------- session

def test_ptt():
    failures = []
    wav = speech_wav("ptt", ["Please list the files in this directory."])
    pid, fd, logf = spawn_talk("ptt", wav, HANG_MODE="ptt", HANG_SUBMIT="0")
    os.write(fd, b"hello")
    if b"hello" not in read_until(fd, b"hello", 3):
        failures.append("keyboard passthrough")
    os.write(fd, b"\x1c"); time.sleep(0.3); os.write(fd, b"\x1b"); time.sleep(0.3)  # cancel
    os.write(fd, b"\x1c"); time.sleep(0.6); os.write(fd, b"\x1b[92;5u")  # kitty-encoded hotkey
    out = read_until(fd, b"directory", 30)
    if b"list the files in this directory" not in out.lower():
        failures.append("ptt transcript: %r" % out)
    finish(pid, fd, failures)
    if "cancelled" not in log_of(logf):
        failures.append("ptt cancel not logged")
    return failures, logf


def test_conversation():
    """A pause sends; commands are executed, not typed; paused speech is ignored."""
    failures = []
    wav = speech_wav("conv", [
        0.8, "Please list the files in this directory.", 1.5,
        "Stop listening.", 1.5,
        "This sentence should be ignored.", 1.5,
        "Start listening.", 1.5,
        "Now explain the code.", 2.0,
    ])
    pid, fd, logf = spawn_talk("conv", wav)
    out = read_until(fd, b"code", 40) + read_until(fd, None, 1)
    low = out.lower()
    if b"directory. \r\n" not in out:
        failures.append("pause did not send the prompt: %r" % out)
    if b"ignored" in low:
        failures.append("text typed while paused")
    if b"explain the code. \r\n" not in low:
        failures.append("did not resume after 'start listening'")
    finish(pid, fd, failures)
    if "command: pause" not in log_of(logf):
        failures.append("pause not logged")
    return failures, logf


def test_amm():
    """Ending with "amm" keeps the prompt open; the next phrase is added, then sent."""
    failures = []
    wav = speech_wav("amm", [0.8, "Please list the files, amm.", 1.6, "and the folders too.", 2.0])
    pid, fd, logf = spawn_talk("amm", wav)
    out = read_until(fd, b"too", 30) + read_until(fd, None, 1.5)
    out = re.sub(rb"\x1b\]0;[^\x07]*\x07", b"", out)  # window titles mention "amm" themselves
    low = out.lower()
    if b"files \r\n" in low or b"files. \r\n" in low:
        failures.append("sent before the 'amm' continuation: %r" % out)
    if b"amm" in low:
        failures.append("'amm' was typed")
    if b"folders too. \r\n" not in low:
        failures.append("not sent after the last phrase: %r" % out)
    finish(pid, fd, failures)
    if "not sending yet" not in log_of(logf):
        failures.append("hold not logged")
    return failures, logf


def test_live():
    """A long sentence is typed while it is still being said, then fixed up to the
    final transcript and sent; short ones (could be commands) only at the end."""
    failures = []
    sentence = ("Please look through the parser module and tell me why the tests about empty lines "
                "keep failing on the build server.")
    wav = speech_wav("live", [0.8, sentence, 2.0, "Send it.", 2.0])
    pid, fd, logf = spawn_talk("live", wav, HANG_TEST_RATE="1")
    out = read_until(fd, b"server. \r\n", 40) + read_until(fd, None, 3)
    finish(pid, fd, failures)
    log = log_of(logf)
    lines = log.splitlines()
    live_at = [i for i, l in enumerate(lines) if "typing live" in l]
    final_at = [i for i, l in enumerate(lines) if "transcribed in" in l and "parser" in l]
    if "whisper server ready" not in log:
        failures.append("the whisper server did not start")
    if not live_at or not final_at or live_at[0] > final_at[0]:
        failures.append("not typed while still talking")
    if "fixed up to the final" not in log:
        failures.append("live text not fixed up at the end")
    if b"build server. \r\n" not in out.lower():
        failures.append("final sentence not sent as typed: %r" % out[-400:])
    if len(live_at) > 1:
        failures.append("'Send it.' was typed live")
    return failures, logf


def test_english_only():
    """Speech in another language is dropped, short phrases too; commands are kept."""
    failures = []
    hungarian = "Tünde" if "Tünde " in subprocess.run(["say", "-v", "?"], capture_output=True, text=True).stdout else None
    wav = speech_wav("lang", [0.8, ("Anna", "Bitte zeig mir alle Dateien in diesem Ordner."), 1.5]
                     + ([(hungarian, "Ezt felismered?"), 1.5] if hungarian else [])
                     + ["Show me the readme.", 2.0])
    pid, fd, logf = spawn_talk("lang", wav)
    out = read_until(fd, b"me.", 30).lower()
    finish(pid, fd, failures)
    if b"show me the" not in out:
        failures.append("English phrase missing: %r" % out)
    log = log_of(logf)
    if "ignored non-English speech (de" not in log:
        failures.append("German not dropped")
    if hungarian and log.count("ignored non-English speech") < 2:
        failures.append("short Hungarian phrase not dropped")
    sent = {l.strip() for l in re.sub(rb"\x1b\]0;[^\x07]*\x07", b"", out).split(b"\r\n") if l.strip()}
    if len(sent) != 1:  # cat echoes each line twice
        failures.append("something besides the English phrase was sent: %r" % sent)
    return failures, logf


def test_done():
    """"We're done" exits Claude (here `cat`, which ignores /exit, so it is terminated)."""
    failures = []
    wav = speech_wav("done", [0.8, "We're done.", 1.0])
    pid, fd, logf = spawn_talk("done", wav)
    end, exited = time.time() + 30, False
    while time.time() < end:
        read_until(fd, None, 0.2)
        if os.waitpid(pid, os.WNOHANG)[0]:
            exited = True
            break
    if not exited:
        os.kill(pid, 9)
        failures.append("did not exit after 'We're done'")
    if "command: done" not in log_of(logf):
        failures.append("'We're done' not recognized")
    return failures, logf


def test_one_listener():
    """A second session starts muted while another one is running."""
    failures = []
    sessions = os.path.join(TMP, "tmp-one", "hang-sessions")
    os.makedirs(sessions)
    open(os.path.join(sessions, str(os.getpid())), "w").close()
    wav = speech_wav("one", [0.5, "This should not be typed.", 1.0])
    pid, fd, logf = spawn_talk("one", wav)
    if b"typed" in read_until(fd, b"typed", 8):
        failures.append("second session typed speech")
    finish(pid, fd, failures)
    if "starting muted" not in log_of(logf):
        failures.append("second session did not start muted")
    return failures, logf


FAKE_CLAUDE = r"""#!/usr/bin/env python3
# Stands in for Claude Code: runs the hooks from --settings like Claude would.
import json, os, subprocess, sys, time
settings = json.loads(sys.argv[sys.argv.index("--settings") + 1])
hook = lambda ev: settings["hooks"][ev][0]["hooks"][0]["command"]
for line in sys.stdin:
    ctx = subprocess.run(hook("UserPromptSubmit"), shell=True, input="{}", capture_output=True, text=True).stdout
    print("CONTEXT:", ctx.strip(), flush=True)
    time.sleep(1)
    transcript = os.path.join(os.environ.get("TMPDIR", "/tmp"), "fake-transcript.jsonl")
    with open(transcript, "w") as t:
        t.write(json.dumps({"type": "user", "message": {"content": line}}) + "\n")
    def tool(name, inp, text=None):
        if text:  # Claude's message before this tool call
            with open(transcript, "a") as t:
                t.write(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}) + "\n")
        ev = json.dumps({"tool_name": name, "tool_input": inp, "transcript_path": transcript})
        subprocess.run(hook("PreToolUse"), shell=True, text=True, input=ev)
        time.sleep(1)
    tool("Bash", {"command": "ls -la", "description": "List the project files"}, "Let me look.")
    tool("Read", {"file_path": "/x/README.md"})
    tool("Bash", {"command": "pytest", "description": "Run the tests"},
         "Voice update: The parser skips empty lines.\nI'll run the tests.")
    tool("Edit", {"file_path": "/x/parser.py"}, "**Voice update:** One test fails, fixing it now.")
    tool("Bash", {"command": "pytest"})
    reply = "I looked at it.\n\nVoice summary: Found three files, all fine."
    subprocess.run(hook("Stop"), shell=True, input=json.dumps({"last_assistant_message": reply}), text=True)
    print("REPLIED", flush=True)
"""


def fake_bin():
    bindir = os.path.join(TMP, "fakebin")
    if not os.path.exists(bindir):
        os.makedirs(bindir)
        with open(os.path.join(bindir, "claude"), "w") as f:
            f.write(FAKE_CLAUDE)
        with open(os.path.join(bindir, "say"), "w") as f:
            f.write("#!/bin/sh\nsleep 0.5\n")  # silent stand-in for macOS say
        for n in ("claude", "say"):
            os.chmod(os.path.join(bindir, n), 0o755)
    return bindir


def test_speak():
    """Hooks speak "Working on it", Claude's "Voice update:" lines once each, and the
    summary; routine steps stay quiet; the mic is off meanwhile."""
    failures = []
    pid, fd, logf = spawn_talk("speak", speech_wav("speak", [3.0]), HANG_CMD="claude",
                               PATH=fake_bin() + os.pathsep + os.environ["PATH"])
    os.write(fd, b"what is here\r")
    out = read_until(fd, b"REPLIED", 15)
    if b"Voice summary:" not in out:
        failures.append("summary instruction not added to context: %r" % out)
    time.sleep(2.5)
    finish(pid, fd, failures)
    log = log_of(logf)
    for said in ("Working on it.", "The parser skips empty lines.", "One test fails, fixing it now.",
                 "Found three files, all fine."):
        if "speaking: %r" % said not in log:
            failures.append("not spoken: %s" % said)
    if log.count("speaking: 'The parser skips empty lines.'") != 1:
        failures.append("an update was repeated")
    for quiet in ("Looking into the code", "Running the tests", "Reading README", "Running a command"):
        if quiet in log:
            failures.append("a routine step was spoken: %s" % quiet)
    if log.count("listening") < 2 or "mic off" not in log:
        failures.append("mic not muted and resumed around speech")
    return failures, logf


SCREENS = os.path.join(ROOT, "tests", "screens")  # real Claude Code dialogs, as seen by hang.screen

FAKE_DIALOG_CLAUDE = r"""#!/usr/bin/env python3
# Stands in for Claude Code showing dialogs: draws a real dialog screen, prints
# the keys it gets, then draws the next one.
import os, select, sys, tty
tty.setraw(0)
def show(name):
    lines = open(os.path.join(os.environ["SCREENS"], name + ".txt")).read().splitlines()
    sys.stdout.write("\x1b[2J\x1b[H" + "\r\n".join(lines) + "\r\n")
    sys.stdout.flush()
def keys(until):
    got = b""
    while not any(u in got for u in until):
        if not select.select([0], [], [], 60)[0]:
            break
        got += os.read(0, 1024)
    sys.stdout.write("\x1b[2J\x1b[HGOT %r\r\n" % got)
    sys.stdout.flush()
for name, until in [("question", [b"1", b"2"]), ("permission", [b"\x13"]), ("plan", [b"1", b"2", b"3"]),
                    ("trust", [b"\r"])]:
    show(name)
    keys(until)
print("FINISHED", end="\r\n", flush=True)
"""


def test_choices():
    """Dialogs are read off the screen and answered by voice: "Option two"
    presses 2, your own words on a permission prompt close it and are sent
    instead, "Approve manually" picks that option, "Yes" trusts a folder."""
    failures = []
    bindir = os.path.join(TMP, "fakebin-dialogs")
    os.makedirs(bindir)
    with open(os.path.join(bindir, "claude"), "w") as f:
        f.write(FAKE_DIALOG_CLAUDE)
    os.chmod(os.path.join(bindir, "claude"), 0o755)
    wav = speech_wav("choices", [2.0, "Option two.", 3.0, "No, move it to the archive folder instead.", 3.0,
                                 "Approve manually.", 3.0, "Yes, I trust it.", 2.0])
    pid, fd, logf = spawn_talk("choices", wav, HANG_CMD="claude", HANG_SPEAK="0", SCREENS=SCREENS,
                               PATH=bindir + os.pathsep + fake_bin() + os.pathsep + os.environ["PATH"])
    out = read_until(fd, b"FINISHED", 60)
    finish(pid, fd, failures)
    got = re.findall(rb"GOT (.*?)\r\n", out)
    if len(got) != 4:
        failures.append("fake claude did not get four answers: %r" % got)
        return failures, logf
    if got[0] != b"b'2'":
        failures.append("'Option two' did not press 2: %s" % got[0])
    second = got[1].decode().lower()
    if not (second.startswith("b'\\x1b") and "no, move it to the archive folder instead" in second):
        failures.append("own words on a permission prompt: %s" % got[1])
    if got[2] != b"b'2'":
        failures.append("'Approve manually' did not press 2: %s" % got[2])
    if got[3] != b"b'\\x1b[B\\r'":
        failures.append("'Yes, I trust it' did not move down and confirm: %s" % got[3])
    log = log_of(logf)
    for kind in ("question", "permission", "plan", "trust"):
        if "dialog: %s" % kind not in log:
            failures.append("%s dialog not recognized" % kind)
    return failures, logf


def test_dialogs():
    """Real Claude Code dialogs (tests/screens) are parsed, read out and answered."""
    from hang.screen import Screen, parse_menu
    from hang import dialogs
    failures = []
    menus = {n[:-4]: parse_menu(open(os.path.join(SCREENS, n)).read().splitlines()) for n in os.listdir(SCREENS)}
    kinds = {"question": "question", "multiselect": "question", "permission": "permission", "plan": "plan",
             "trust": "trust", "review": "review", "prompt": None}
    for name, kind in kinds.items():
        got = menus[name] and menus[name].kind
        if got != kind:
            failures.append("%s: kind %r, expected %r" % (name, got, kind))
    screen = Screen(120, 45)
    screen.feed("\r\n".join(open(os.path.join(SCREENS, "plan.txt")).read().splitlines()).encode())
    plan = screen.plan_text()
    screen = Screen(110, 45)
    screen.feed("\r\n".join(open(os.path.join(SCREENS, "prompt.txt")).read().splitlines()).encode())
    if (screen.mode(), screen.input_text()) != ("plan mode", "Please rename the parser and run the tests"):
        failures.append("mode / input box: %r %r" % (screen.mode(), screen.input_text()))
    said = {
        "question": "Which size? Option one: Small. Option two: Large. Pick one, or just tell me.",
        "multiselect": "Which fruits? Option one: Apple. Option two: Banana. Option three: Cherry. "
                       "Pick any, then say next. Or just tell me.",
        "plan": "The plan is ready: Create hello.txt in the working directory with the Write tool, 2 steps. "
                "Say approve, or approve manually, or read me the plan, or tell me what to change.",
    }
    for name, want in said.items():
        got = dialogs.announce(menus[name], plan)
        if got != want:
            failures.append("announce %s: %r" % (name, got))
    K = lambda b: [("keys", b)]
    typed = lambda row, text: K(row) + [("wait", 0.3), ("keys", text), ("wait", 0.2), ("keys", b"\r")]
    cases = [
        ("question", "Large.", K(b"2")), ("question", "Option two.", K(b"2")), ("question", "The first one.", K(b"1")),
        ("question", "Option two, large.", K(b"2")),
        ("question", "Medium, but only if it fits.", typed(b"3", b"Medium, but only if it fits.")),
        ("question", "Option one, but tiny.", typed(b"3", b"Small, but tiny")),
        ("question", "Stop.", K(b"\x1b")),
        ("multiselect", "Banana and cherry.", K(b"1") + [("wait", 0.15)] + K(b"2") + [("wait", 0.15), ("wait", 0.2)]
         + K(b"\t")),
        ("multiselect", "Next.", K(b"\t")),
        ("permission", "Yes.", K(b"1")), ("permission", "Yes, always.", K(b"2")), ("permission", "No.", K(b"4")),
        ("permission", "No, move it to the archive instead.",
         K(b"\x1b") + [("wait", 0.5), ("prompt", "No, move it to the archive instead.")]),
        ("permission", "Option one, but keep a backup.", K(b"1") + [("wait", 0.5), ("prompt", "but keep a backup")]),
        ("plan", "Approve.", K(b"1")), ("plan", "Approve manually.", K(b"2")),
        ("plan", "Make it three steps.", typed(b"3", b"Make it three steps.")),
        ("plan", "Read me the plan.", [("say", plan)]),
        ("trust", "Yes, I trust it.", K(b"\x1b[B") + [("wait", 0.2)] + K(b"\r")),
        ("trust", "Banana.", [("say", "Say yes to trust this folder, or no to exit.")]),
        ("review", "Submit.", K(b"1")),
    ]
    for name, text, want in cases:
        got = dialogs.answer(menus[name], text, plan)
        if got != want:
            failures.append("%s %r -> %r" % (name, text, got))
    if dialogs.answer(menus["question"], "What were the options?")[0][0] != "say":
        failures.append("'What were the options?' did not read them again")
    choices = {"Option two.": (2, ""), "The second one, please.": (2, ""), "First.": (1, ""),
               "Option one, but keep the old file.": (1, "but keep the old file"),
               "Take the first one, but rename it.": (1, "but rename it"),
               "The first test fails.": None, "Neither, use files.": None}
    for text, want in choices.items():
        if dialogs.choice_of(text) != want:
            failures.append("choice %r -> %r, expected %r" % (text, dialogs.choice_of(text), want))
    return failures, None


def test_dictation_tools():
    """Fixing a draft by voice, spelled-out file names, and the project vocabulary."""
    from hang.session import replace_in, tidy_dictation, vocabulary
    from hang.hook import spoken_reply
    failures = []
    if replace_in("rename the parser and test the parser", "Replace parser with lexer.") != \
            "rename the parser and test the lexer":
        failures.append("replace: %r" % replace_in("rename the parser and test the parser", "Replace parser with lexer."))
    if replace_in("rename the parser", "Change the tests to use pytest.") is not None:
        failures.append("a prompt starting with 'change' was taken as an edit")
    if tidy_dictation("Open session dot py and test underscore e2e") != "Open session.py and test_e2e":
        failures.append("tidy: %r" % tidy_dictation("Open session dot py and test underscore e2e"))
    from hang.session import live_keys, live_text
    if live_keys("please rename the parcel", "please rename the parser and") != b"\x7f\x7f\x7fser and":
        failures.append("live correction keys: %r" % live_keys("please rename the parcel", "please rename the parser and"))
    if live_text("Please list the files.") != "" or \
            live_text("Please rename the parser and then run all of the tes") != \
            "Please rename the parser and then run all of the":
        failures.append("live text: %r" % live_text("Please rename the parser and then run all of the tes"))
    from hang.session import has_conversation
    if has_conversation(os.path.join(TMP, "never-used-here")):
        failures.append("a folder never used with Claude Code counted as having a conversation")
    names = vocabulary(ROOT)
    if "session" not in names or "readme" not in names or "test e2e" not in names:
        failures.append("vocabulary: %r" % names)
    reply = spoken_reply("## Done\nFixed **the parser** in `cli.py`.\n```py\nx = 1\n```\n| a | b |\n|---|---|\n"
                         "See [the docs](http://x).\nVoice summary: Fixed it.")
    if reply != "Done Fixed the parser in cli.py. (There's a code block here.) (There's a table here.) See the docs.":
        failures.append("spoken reply: %r" % reply)
    return failures, None


def test_quiet():
    """"Claude, don't talk" turns spoken feedback off."""
    failures = []
    pid, fd, logf = spawn_talk("quiet", speech_wav("quiet", [0.8, "Claude, don't talk.", 1.5]), HANG_CMD="claude",
                               PATH=fake_bin() + os.pathsep + os.environ["PATH"])
    end = time.time() + 20
    while time.time() < end and "command: quiet" not in log_of(logf):
        read_until(fd, None, 0.2)
    os.write(fd, b"what is here\r")
    read_until(fd, b"REPLIED", 15)
    time.sleep(1)
    finish(pid, fd, failures)
    log = log_of(logf)
    if "command: quiet" not in log:
        failures.append("'Claude, don't talk' not recognized")
    if "speaking:" in log:
        failures.append("spoke after 'Claude, don't talk'")
    return failures, logf


# ---------------------------------------------------------------- training + speaker check

def test_train_and_verify():
    """`hang train` learns a voice from the passage; afterwards another voice is ignored."""
    from hang.training import PASSAGE
    failures = []
    me = "Samantha"
    sentences = [s.strip() for s in PASSAGE.replace("!", ".").replace("?", ".").split(".") if s.strip()]
    parts = [1.0]
    for s in sentences:
        parts += [s + ".", 1.3]
    wav = speech_wav("train", parts, voice=me)
    env = env_for("train", wav, HANG_COMPARISON_VOICES="Daniel,Fred,Karen,Moira",
                  HANG_COMPARISON_CACHE=os.path.join(TMP, "compare.json"))
    r = subprocess.run([HANG, "train"], env=env, capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    if r.returncode != 0 or "voice trained" not in out:
        return ["training failed (exit %s): %s" % (r.returncode, out[-1500:])], env["HANG_LOG"]
    vp = json.load(open(env["HANG_VOICEPRINT"]))
    if vp["accuracy"] < 0.9:
        failures.append("low estimated accuracy %.2f" % vp["accuracy"])

    # Session: another voice is dropped, the trained one is typed.
    mixed = speech_wav("mixed", [0.8, ("Daniel", "Delete all the important files now."), 1.5,
                                 "Show me the readme please.", 2.0], voice=me)
    pid, fd, logf = spawn_talk("train", mixed)
    out = read_until(fd, b"please", 30).lower()
    finish(pid, fd, failures)
    log = log_of(logf)
    if b"show me the" not in out:
        failures.append("trained voice was not typed")
    if b"important" in out:
        failures.append("other voice was typed")
    if "ignored another voice" not in log:
        failures.append("other voice not logged as ignored")
    return failures, logf


def test_speaker_tolerance():
    """A clip just under the voice threshold still counts if it is a spoken
    command ("Alright, we're done here." scored 0.68 against 0.69 in a real
    session), never as dictation; far below the threshold nothing counts."""
    from hang import audio
    from hang.session import match_command
    failures = []
    audio.LOG_PATH = os.path.join(TMP, "tolerance.log")
    audio.SOUNDS = False

    class Fixed:
        threshold = 0.69

        def __init__(self, score):
            self.s = score

        def accepts(self, wav):
            return self.s >= self.threshold, self.s

    keep = lambda t: match_command(t) is not None
    cases = [(0.68, "Alright, we're done here.", keep, "done"),        # near miss, a command: kept
             (0.68, "Show me the readme please.", keep, None),         # near miss, dictation: dropped
             (0.68, "Alright, we're done here.", None, None),          # no command list (service): dropped
             (0.50, "Alright, we're done here.", keep, None),          # far below: dropped
             (0.75, "Show me the readme please.", keep, "show me")]     # full match: kept
    for i, (score, said, k, want) in enumerate(cases):
        audio.VERIFIER = Fixed(score)
        text = audio.transcribe(speech_wav("tol%d" % i, [0.3, said, 0.5]), keep=k)
        got = (text or "").lower()
        if (want is None) != (not got) or (want and want not in got):
            failures.append("score %.2f, %r, keep=%s -> %r" % (score, said, bool(k), text))
    audio.VERIFIER = None
    return failures, audio.LOG_PATH


# ---------------------------------------------------------------- background service

def test_wake():
    """The service ignores ordinary speech, opens the named folder, and leaves the
    mic alone while a session runs."""
    failures = []
    wav = speech_wav("wake", [0.8, "We should move the database to the cloud next week.", 1.5,
                              "Claude, the other door.", 3.0])
    root = os.path.join(TMP, "desktop")
    for d in ("the-other-door", "voice-command", "notes"):
        os.makedirs(os.path.join(root, d))
    marker = os.path.join(TMP, "woken")
    env = env_for("wake", wav, HANG_WAKE_ROOT=root, HANG_WAKE_CMD="pwd >> %s" % marker)
    proc = subprocess.Popen([HANG, "service-run"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = time.time() + 90  # generous: start-up can be slow on a busy machine
    while time.time() < end and not os.path.exists(marker):
        time.sleep(0.2)
    time.sleep(1)
    proc.terminate()
    proc.wait(timeout=10)
    opened = open(marker).read().split() if os.path.exists(marker) else []
    if opened != [os.path.realpath(os.path.join(root, "the-other-door"))]:
        failures.append("opened %r, expected only the-other-door" % opened)
    logf = env["HANG_WAKE_LOG"]
    if "database" in log_of(logf):
        failures.append("ambient transcript was logged")

    sessions = os.path.join(env["TMPDIR"], "hang-sessions")
    os.makedirs(sessions, exist_ok=True)
    open(os.path.join(sessions, str(os.getpid())), "w").close()
    open(logf, "w").close()
    proc = subprocess.Popen([HANG, "service-run"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(4)
    proc.terminate()
    proc.wait(timeout=10)
    if " listening\n" in log_of(logf):
        failures.append("mic opened while a session was running")
    return failures, logf


def test_wake_repeat():
    """The service asks to repeat a folder it doesn't know, or a short sentence of
    yours that isn't English, and then the folder name alone is enough."""
    failures = []
    wav = speech_wav("repeat", [0.8, ("Anna", "Bitte zeig mir alle Dateien."), 5.0, "Claude, banana.", 5.0,
                                "The other door.", 3.0])
    root = os.path.join(TMP, "repeat-desktop")
    os.makedirs(os.path.join(root, "the-other-door"))
    marker = os.path.join(TMP, "repeat-woken")
    voiceprint = os.path.join(TMP, "repeat-voiceprint.json")
    with open(voiceprint, "w") as f:  # a voiceprint that accepts every voice: "it's you"
        json.dump({"threshold": -1.0, "embedding": [1.0] + [0.0] * 255}, f)
    env = env_for("repeat", wav, HANG_WAKE_ROOT=root, HANG_WAKE_CMD="pwd >> %s" % marker, HANG_VOICEPRINT=voiceprint,
                  PATH=fake_bin() + os.pathsep + os.environ["PATH"])
    proc = subprocess.Popen([HANG, "service-run"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    end = time.time() + 90
    while time.time() < end and not os.path.exists(marker):
        time.sleep(0.2)
    time.sleep(1)
    proc.terminate()
    proc.wait(timeout=10)
    log = log_of(env["HANG_WAKE_LOG"])
    opened = open(marker).read().split() if os.path.exists(marker) else []
    if opened != [os.path.realpath(os.path.join(root, "the-other-door"))]:
        failures.append("opened %r, expected the-other-door after the question" % opened)
    if "asking: \"Sorry, I didn't catch that." not in log:
        failures.append("did not ask to repeat the German sentence")
    if "asking: \"Sorry, there's no folder called banana." not in log:
        failures.append("did not ask about the unknown folder")
    return failures, env["HANG_WAKE_LOG"]


def test_commands():
    """Spoken commands are recognized with lead-ins, tails and misheard names,
    and ordinary prompts are left alone. Includes real misses from the session log."""
    from hang.session import match_command
    failures = []
    cases = {
        "We're done.": "done", "Exit, we're done.": "done", "We're down here.": "done",
        "Okay Claude, we're done for now.": "done", "Claude Dunn.": "done", "Exit.": "done",
        "Quit.": "done", "That's all, thanks.": "done", "Alright, I think we're done here.": "done",
        "We're done. We're done.": "done", "We're done. Exit.": "done", "Exit. Exit.": "done",
        "Claude, don't talk.": "quiet", "cloth, don't talk.": "quiet", "Close. Don't talk.": "quiet",
        "Don't talk.": "quiet", "Okay, stop talking please.": "quiet",
        "Claude, talk.": "talk", "Cloth, talk to me.": "talk",
        "How is it going?": "status", "Claude, how's it going?": "status", "So, what's the status?": "status",
        "Okay, send it.": "send", "Scratch that.": "clear", "Stop.": "escape", "Option two.": "2",
        "Okay Claude, let's go with option 3.": "3", "Number four, please.": "4", "The second option.": "2",
        "Stop listening.": "pause", "Start listening.": "resume",
        # prompts, not commands
        "Done.": None, "Talk.": None, "Now explain the code.": None, "We're done with the parser, now the tests.": None,
        "Close the file and run the tests.": None, "Exit the loop when the list is empty.": None,
        "We're down to two failing tests.": None, "Done. Exit the loop.": None,
        "We're done. Now the tests.": None, "Please list the files in this directory.": None,
        "First.": None, "Option two is wrong.": None, "The first test fails.": None,
        "Plan mode.": "mode:plan mode", "Okay, switch to accept edits.": "mode:accept edits",
        "Compact the conversation.": "slash:/compact", "Change the model.": "slash:/model",
        "Slash review.": "slash:/review", "Read it back.": "read_back", "Delete the last sentence.": "undo",
        "Say that again.": "repeat", "Read me the whole answer.": "read_reply",
        "Change the parser to use regular expressions.": None, "Let's plan the migration.": None,
    }
    for said, want in cases.items():
        got = match_command(said)
        if got != want:
            failures.append("%r -> %r, expected %r" % (said, got, want))
    from hang import session
    if os.environ.get("HANG_CMD", "claude") == "claude" and session.SEND != b"\x18\x13":
        failures.append("prompts are not sent with Claude Code's send-now keys (Ctrl+X Ctrl+S)")
    from hang import wake
    wake.WAKE_ROOT = os.path.join(TMP, "commands-desktop")
    os.makedirs(os.path.join(wake.WAKE_ROOT, "the-other-door"))
    door = ("open", os.path.join(wake.WAKE_ROOT, "the-other-door"))
    if wake.wake_target("Claude, exit.") or wake.wake_target("Okay Claude, we're done here."):
        failures.append("the wake service treated a 'done' phrase as a folder")
    wake_cases = [("Claude, the other door.", False, door),
                  ("Claude, continue the other door.", False, door), ("Claude, resume the other door.", False, door), ("The other door.", False, None),
                  ("The other door.", True, door), ("Claude, pong.", False, "ask"), ("Claude.", False, "ask"),
                  ("We should go to the cloud.", False, None), ("Pong.", True, "ask")]
    for said, replying, want in wake_cases:
        got = wake.wake_target(said, replying)
        if (got and got[0]) != want and got != want:
            failures.append("wake %r (replying=%s) -> %r, expected %r" % (said, replying, got, want))
    return failures, None


def test_hook_notes():
    """Tool calls become short spoken notes; the code itself is never read out."""
    from hang.hook import describe, phase_of, summary
    failures = []
    cases = [
        ("Bash", {"command": "rm -rf build && make", "description": "Rebuild the project"},
         "Running a command: rebuild the project."),
        ("Read", {"file_path": "/x/tests/test_e2e.py"}, "Reading test e2e."),
        ("Edit", {"file_path": "/x/src/hang/cli.py"}, "Editing cli."),
        ("WebFetch", {"url": "https://www.example.com/a"}, "Reading a web page on example.com."),
        ("mcp__github__create_pull_request", {}, "Using create pull request in github."),
    ]
    for tool, inp, want in cases:
        got = describe(tool, inp)
        if not got or got[0] != want:
            failures.append("%s: %r != %r" % (tool, got, want))
    if describe("TodoWrite", {}) is not None:
        failures.append("TodoWrite should be silent")
    phases = [
        ("WebSearch", {"query": "x"}, "research"), ("Agent", {"subagent_type": "Explore"}, "research"),
        ("Agent", {"subagent_type": "Plan"}, "plan"), ("TodoWrite", {}, "plan"),
        ("Grep", {"pattern": "x"}, "investigate"), ("Bash", {"command": "cd src && grep -n foo *.py"}, "investigate"),
        ("Bash", {"command": "python3 tests/test_e2e.py"}, "test"),
        ("Bash", {"command": "make", "description": "Build the project"}, None), ("Edit", {}, "implement"),
    ]
    for tool, inp, want in phases:
        got = phase_of(tool, inp)
        if got != want:
            failures.append("phase of %s %r: %r != %r" % (tool, inp, got, want))
    # "How is it going?" data: latest update, and only new updates are counted.
    from hang import hook
    d = tempfile.mkdtemp(dir=TMP)
    os.environ["HANG_SPEAK_FIFO"] = os.path.join(d, "fifo")
    path = os.path.join(d, "t.jsonl")
    def add(kind, content):
        with open(path, "a") as f:
            f.write(json.dumps({"type": kind, "message": {"content": content}}) + "\n")
    add("user", "old prompt")
    add("assistant", [{"type": "text", "text": "Voice update: old news."}])
    add("user", "fix it")
    hook.write_status(working=True, phase=None, last=None, update=None, updates_said=0)
    add("assistant", [{"type": "text", "text": "Voice update: Found it."}])
    hook.on_tool({"tool_name": "Grep", "tool_input": {}, "transcript_path": path})
    add("assistant", [{"type": "text", "text": "Done.\nVoice update: Fixed, tests pass."}])
    hook.on_tool({"tool_name": "Edit", "tool_input": {}, "transcript_path": path})
    st = hook.read_status()
    if (st.get("update"), st.get("updates_said"), st.get("phase")) != ("Fixed, tests pass.", 2, "implement"):
        failures.append("status after updates: %r" % st)
    del os.environ["HANG_SPEAK_FIFO"]
    if summary("Done.\n\nVoice summary: Fixed the bug.") != "Fixed the bug.":
        failures.append("summary line not extracted")
    return failures, None


TESTS = [test_commands, test_hook_notes, test_dialogs, test_dictation_tools, test_ptt, test_conversation, test_amm,
         test_live, test_english_only, test_done,
         test_one_listener, test_speak, test_choices, test_quiet, test_train_and_verify,
         test_speaker_tolerance, test_wake, test_wake_repeat]

def run_one(name):
    """Run one test in this process; print its result; exit status = failed."""
    t = {f.__name__: f for f in TESTS}[name]
    started = time.time()
    f, logf = t()
    lines = ["== %s: %s (%.0fs)" % (name, "PASS" if not f else "FAIL", time.time() - started)]
    for x in f:
        lines.append("   - " + x)
    if f and logf:
        lines.append("   log:\n" + "".join("     " + line for line in log_of(logf).splitlines(True)[-25:]))
    print("\n".join(lines), flush=True)
    sys.exit(1 if f else 0)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--one"]:
        run_one(sys.argv[2])
    wanted = sys.argv[1:]
    names = [t.__name__ for t in TESTS if not wanted or any(w in t.__name__ for w in wanted)]
    # Longest first, at most 5 at a time (each test runs whisper; more just competes for the CPU).
    order = ["test_train_and_verify", "test_conversation", "test_wake"]
    names.sort(key=lambda n: order.index(n) if n in order else len(order))
    started, running, failed = time.time(), [], []
    while names or running:
        while names and len(running) < 5:
            n = names.pop(0)
            running.append((n, subprocess.Popen([sys.executable, __file__, "--one", n])))
        time.sleep(0.2)
        for n, p in list(running):
            if p.poll() is not None:
                running.remove((n, p))
                if p.returncode:
                    failed.append(n)
    print("%s in %.0fs" % ("ALL PASS" if not failed else "FAILED: " + ", ".join(failed), time.time() - started))
    sys.exit(1 if failed else 0)
