"""
Claude Code hook for spoken status in `hang talk` sessions.

`hang talk` passes these hooks to `claude` with --settings, so they only run
in voice sessions (as `python -m hang.hook <event>`, standard library only, so
it starts fast). The hook does not speak itself: it hands the text to the
session through the FIFO in $HANG_SPEAK_FIFO, and the session mutes the mic while
it speaks so Claude's voice is not transcribed back into the prompt.

  python -m hang.hook prompt   UserPromptSubmit: say "Working on it", and ask
                             Claude to end its reply with a spoken summary line
  python -m hang.hook tool     PreToolUse: speaks new "Voice update:" lines that
                             Claude wrote since the last tool call (findings and
                             progress, in its own words); routine steps are
                             silent, but their phase and the step itself
                             ("Reading cli.") are kept for "How is it going?"
  python -m hang.hook stop     Stop: speak that summary line, and keep the whole
                             reply for "read me the whole answer"

Dialogs (questions, permission prompts, the plan) are not spoken from here: the
session reads them off the screen, options and all (hang.screen, hang.dialogs).

Progress (phase, last step, latest update, summary) is kept in status.json next
to the FIFO, which the session reads when asked "How is it going?".

Lines starting with "!" are urgent (summary, permission, questions): they cut
off whatever is being said. Other notes are dropped if newer ones arrive first.
"""
import json
import os
import re
import sys
import time

TAG = "Voice summary:"
UPDATE = "Voice update:"
INSTRUCTION = ("(Voice mode: the user is listening, not reading. While you work, when you find "
               "something or finish a meaningful step, put a line '%s <at most 12 words of plain "
               "speech: the finding or progress>' in your message, e.g. '%s The bug is in the "
               "parser: it skips empty lines.' Not for routine steps like reading files or "
               "running commands. End your reply with one final line '%s <at most 15 words of "
               "plain speech: what you did or found; no code, paths or symbols>'.)"
               % (UPDATE, UPDATE, TAG))


def speak(text, urgent=False):
    fifo = os.environ.get("HANG_SPEAK_FIFO")
    text = " ".join(text.split())
    if urgent:
        text = "!" + text
    if not fifo or not text:
        return
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return  # the session is gone
    try:
        os.write(fd, (text + "\n").encode())
    finally:
        os.close(fd)


def status_path():
    fifo = os.environ.get("HANG_SPEAK_FIFO")
    return os.path.join(os.path.dirname(fifo), "status.json") if fifo else None


def read_status():
    path = status_path()
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, TypeError, ValueError):
        return {}


def write_status(**changes):
    """Merge changes into status.json: working, phase, last, update, updates_said,
    summary, reply."""
    path = status_path()
    if not path:
        return
    status = dict(read_status(), **changes)
    try:
        with open(path + ".tmp", "w") as f:
            json.dump(status, f)
        os.replace(path + ".tmp", path)  # atomic: the session never reads half a file
    except OSError:
        pass


def last_assistant_text(event):
    text = event.get("last_assistant_message")
    if isinstance(text, str) and text.strip():
        return text
    path = event.get("transcript_path")
    if not path:
        return ""
    for _ in range(5):  # the final message can land in the transcript a moment late
        found = ""
        try:
            with open(path) as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if entry.get("type") == "user" and not entry.get("isMeta"):
                        content = entry.get("message", {}).get("content")
                        if isinstance(content, str):
                            found = ""  # a new prompt: earlier replies don't count
                    if entry.get("type") != "assistant":
                        continue
                    parts = entry.get("message", {}).get("content", [])
                    texts = [p.get("text", "") for p in parts
                             if isinstance(p, dict) and p.get("type") == "text"]
                    if any(t.strip() for t in texts):
                        found = "\n".join(texts)
        except OSError:
            return ""
        if TAG in found:
            return found
        time.sleep(0.3)
    return found


def voice_updates(event):
    """The "Voice update:" lines Claude wrote since the user's last prompt, in order."""
    path = event.get("transcript_path")
    updates = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if entry.get("type") == "user" and not entry.get("isMeta"):
                    if isinstance(entry.get("message", {}).get("content"), str):
                        updates = []  # a new prompt
                    continue
                if entry.get("type") != "assistant":
                    continue
                for part in entry.get("message", {}).get("content", []):
                    if isinstance(part, dict) and part.get("type") == "text":
                        for row in part.get("text", "").splitlines():
                            if UPDATE.lower() in row.lower():
                                said = row[row.lower().index(UPDATE.lower()) + len(UPDATE):].strip(" *_")
                                if said:
                                    updates.append(said)
    except (OSError, TypeError):
        return []
    return updates


def summary(text):
    for line in reversed(text.splitlines()):
        if TAG.lower() in line.lower():
            return line[line.lower().index(TAG.lower()) + len(TAG):].strip(" *_")
    # No summary line: fall back to the first sentence, without markdown.
    plain = re.sub(r"`[^`]*`|[*_#>]|\[([^\]]*)\]\([^)]*\)", r"\1", text).strip()
    first = re.split(r"(?<=[.!?])\s", plain, maxsplit=1)[0]
    return first[:200] if first else "Done."


def spoken_reply(text):
    """Claude's whole reply as plain speech: no markdown, code blocks and tables
    summed up in a word, no voice lines."""
    text = re.sub(r"```.*?(```|$)", " (There's a code block here.) ", text or "", flags=re.S)
    rows = []
    for row in text.splitlines():
        low = row.lower()
        if TAG.lower() in low or UPDATE.lower() in low:
            continue
        if re.match(r"^\s*\|", row):
            if not rows or rows[-1] != "(There's a table here.)":
                rows.append("(There's a table here.)")
            continue
        rows.append(row)
    plain = "\n".join(rows)
    plain = re.sub(r"`([^`]*)`", r"\1", plain)
    plain = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", plain)
    plain = re.sub(r"^\s*#+\s*|[*_>]+", "", plain, flags=re.M)
    plain = re.sub(r"\s+", " ", plain).strip()
    return plain[:6000]


def file_name(path):
    """'/x/tests/test_e2e.py' -> 'test e2e': how a person would say it."""
    name = os.path.splitext(os.path.basename(path or ""))[0] or os.path.basename(path or "")
    return re.sub(r"[-_.]+", " ", name).strip() or "a file"


def lower_first(text):
    return text[:1].lower() + text[1:] if text else text


def describe(tool, inp):
    """(note, urgent) for a tool call, or None to stay quiet."""
    inp = inp if isinstance(inp, dict) else {}
    desc = (inp.get("description") or "").strip().rstrip(".")
    if tool == "Bash":
        return ("Running a command: %s." % lower_first(desc) if desc else "Running a command."), False
    if tool == "Read":
        return "Reading %s." % file_name(inp.get("file_path")), False
    if tool in ("Edit", "MultiEdit", "NotebookEdit"):
        return "Editing %s." % file_name(inp.get("file_path") or inp.get("notebook_path")), False
    if tool == "Write":
        return "Writing %s." % file_name(inp.get("file_path")), False
    if tool == "Grep":
        pattern = inp.get("pattern") or ""
        if re.fullmatch(r"[\w ]{1,30}", pattern):
            return "Searching the code for %s." % pattern.replace("_", " "), False
        return "Searching the code.", False
    if tool == "Glob":
        return "Looking for files.", False
    if tool == "WebSearch":
        return "Searching the web for %s." % (inp.get("query") or "something"), False
    if tool == "WebFetch":
        host = re.sub(r"^https?://(www\.)?", "", inp.get("url") or "").split("/")[0]
        return "Reading a web page%s." % (" on " + host if host else ""), False
    if tool in ("Task", "Agent"):
        return ("Starting a helper to %s." % lower_first(desc) if desc else "Starting a helper."), False
    if tool == "Skill":
        return "Using the %s skill." % (inp.get("skill") or inp.get("command") or "a"), False
    if tool == "AskUserQuestion":
        return "Asking you a question.", False  # the session reads it off the screen
    if tool == "ExitPlanMode":
        return "Showing you the plan.", False  # read off the screen, with its options
    if tool.startswith("Todo") or tool.startswith("Task"):
        return None  # bookkeeping, not worth saying
    if tool.startswith("mcp__"):
        parts = tool.split("__")
        server = parts[1].replace("_", " ") if len(parts) > 2 else ""
        name = parts[-1].replace("_", " ")
        return "Using %s%s." % (name, " in " + server if server else ""), False
    return "Using %s." % re.sub(r"(?<=[a-z])(?=[A-Z])", " ", tool).lower(), False


# ---------------------------------------------------------------- phases

# Not spoken (they say little): kept for "How is it going?" when there is no update yet.
PHASES = {
    "research": "Researching.",
    "investigate": "Looking into the code.",
    "plan": "Planning.",
    "implement": "Making the changes.",
    "test": "Running the tests.",
}
TEST_CMD = re.compile(r"\b(test|tests|pytest|unittest|jest|vitest|mocha|rspec|go test|cargo test|"
                      r"make check|npm (run )?test|yarn test|pnpm test|tox|nox)\b", re.I)
READ_ONLY_CMD = re.compile(r"^\s*(cd [^;&|]+(&&|;)\s*)?(cat|head|tail|less|ls|find|grep|rg|ag|sed -n|awk|wc|"
                           r"tree|file|stat|which|type|git (log|show|diff|status|blame|grep)|ps|pwd|echo|"
                           r"du|df|lsof|env|printenv)\b")


def phase_of(tool, inp):
    """The phase this tool call belongs to, or None (bookkeeping or an ordinary command)."""
    inp = inp if isinstance(inp, dict) else {}
    if tool in ("WebSearch", "WebFetch"):
        return "research"
    if tool in ("Task", "Agent"):
        return "plan" if inp.get("subagent_type") == "Plan" else "research"
    if tool in ("EnterPlanMode", "TodoWrite", "TaskCreate", "TaskUpdate"):
        return "plan"
    if tool in ("Read", "Grep", "Glob", "NotebookRead"):
        return "investigate"
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return "implement"
    if tool == "Bash":
        cmd = inp.get("command") or ""
        if TEST_CMD.search(cmd) or TEST_CMD.search(inp.get("description") or ""):
            return "test"
        if READ_ONLY_CMD.match(cmd):
            return "investigate"
    return None


def on_tool(event):
    tool, inp = event.get("tool_name", ""), event.get("tool_input")
    status = read_status()
    changes = {}
    updates = voice_updates(event)
    new = updates[status.get("updates_said", 0):]
    if new:
        for said in new:
            speak(said)  # the session says them in order, mic off
        changes.update(update=new[-1], updates_said=len(updates))
    note = describe(tool, inp)
    if note:
        changes["last"] = note[0]
        if note[1]:  # questions, plans to review: always said
            speak(note[0], urgent=True)
    phase = phase_of(tool, inp)
    if phase:
        changes["phase"] = phase
    if changes:
        write_status(**changes)


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    if what == "prompt":
        write_status(working=True, phase=None, last=None, update=None, updates_said=0)
        speak("Working on it.")
        if os.environ.get("HANG_SPEAK", "1") == "1":
            print(INSTRUCTION)  # UserPromptSubmit: stdout is added to Claude's context
    elif what == "tool":
        on_tool(event)
    elif what == "stop":
        text = last_assistant_text(event)
        said = summary(text)
        write_status(working=False, summary=said, reply=spoken_reply(text))
        speak(said, urgent=True)


if __name__ == "__main__":
    main()
