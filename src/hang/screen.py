"""What Claude Code shows on screen, for a voice user who isn't looking.

`hang talk` feeds everything Claude Code prints into a terminal emulator
(pyte), so it can tell when a dialog is open (a question, a permission prompt,
the plan approval, the folder trust check, /model, ...) and what its options
are, which permission mode is on, and what is in the input box. Claude Code
draws all of these the same way, so one parser covers them all:

     Which size?                      <- the question: the line above the options
   ❯ 1. Small                         <- ❯ marks the highlighted option
        Small size                    <- descriptions are indented further
     2. Large
     3. Type something.               <- a row you can type an answer into
     4. [✔] Apple                     <- checkboxes in a multi-select question
   Enter to select · Esc to cancel    <- a hint line: a dialog is open

The trust check's options have no numbers ("❯ No, exit" / "Yes, I trust this
folder"); they are picked with the arrow keys instead.
"""
import re

import pyte

# A dialog is open when one of these is on screen below the options.
HINTS = ("Enter to select", "Enter to confirm", "Esc to cancel", "to navigate",
         "Would you like to proceed", "Do you want to proceed", "Review your answers")
NUMBERED = re.compile(r"^(\s*)(❯\s*)?(\d{1,2})\.\s+(.*\S)\s*$")
CHECKBOX = re.compile(r"^\[(.)\]\s*(.*)$")
# Rows you answer by typing (pick the row, type, Enter).
TEXT_ROWS = re.compile(r"^(type something|tell claude what to change|type here|other)\b", re.I)
RULE = re.compile(r"^\s*[─╌━═-]{10,}\s*$")
SOLID_RULE = re.compile(r"^\s*[─━]{10,}\s*$")
MODE = re.compile(r"(auto mode|manual mode|accept edits|plan mode|bypass permissions|default mode) on", re.I)
SKIP_LABELS = ("chat about this",)  # always offered, never worth reading out


class Option:
    def __init__(self, number, label, highlighted, checked=None):
        self.number = number  # None for unnumbered options (picked with the arrows)
        self.label = label
        self.highlighted = highlighted
        self.checked = checked  # True/False in a multi-select question, else None

    @property
    def text_row(self):
        return bool(TEXT_ROWS.match(self.label))

    def __repr__(self):
        return "Option(%r, %r%s%s)" % (self.number, self.label, ", ❯" if self.highlighted else "",
                                       "" if self.checked is None else ", checked=%s" % self.checked)


class Menu:
    """An open dialog: its question and options, as shown right now."""

    def __init__(self, question, options, title="", body=""):
        self.question = question
        self.options = options
        self.title = title  # e.g. "Bash command", "Ready to code?"
        self.body = body  # the text between the title and the question (a command, a plan)

    @property
    def multi(self):
        return any(o.checked is not None for o in self.options)

    @property
    def kind(self):
        q = (self.question + " " + self.title).lower()
        if "trust" in q or any("trust this folder" in o.label.lower() for o in self.options):
            return "trust"
        if "plan" in q and "proceed" in q or self.title.lower().startswith("ready to code"):
            return "plan"
        if "proceed" in q:
            return "permission"
        if any(o.label.lower() == "submit answers" for o in self.options):
            return "review"
        if any(o.text_row for o in self.options):
            return "question"
        return "menu"

    def find(self, number):
        for o in self.options:
            if o.number == number:
                return o
        return None

    def text_row(self):
        for o in self.options:
            if o.text_row:
                return o
        return None

    def highlighted(self):
        for i, o in enumerate(self.options):
            if o.highlighted:
                return i
        return 0

    def spoken_options(self):
        return [o for o in self.options if o.label.lower() not in SKIP_LABELS and not o.text_row]

    def signature(self):
        """Same dialog (even when the highlight or a checkbox changes)?"""
        return (self.question, self.title, tuple(o.label for o in self.spoken_options()))

    def __repr__(self):
        return "Menu(%r, %r, title=%r)" % (self.question, self.options, self.title)


class Screen:
    def __init__(self, cols=80, rows=24):
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)

    def resize(self, cols, rows):
        self.screen.resize(rows, cols)

    def feed(self, data):
        try:
            self.stream.feed(data)
        except Exception:  # never let a rendering quirk break the session
            pass

    def lines(self):
        return [line.rstrip() for line in self.screen.display]

    def text(self):
        return "\n".join(line for line in self.lines() if line.strip())

    def mode(self):
        """'plan mode', 'auto mode', 'accept edits', ... from the footer, or None."""
        for line in reversed(self.lines()):
            m = MODE.search(line)
            if m:
                return m.group(1).lower()
        return None

    def input_text(self):
        """What is typed in the input box (between the two rules, after ❯), or None."""
        lines = self.lines()
        for i in range(len(lines) - 1, 0, -1):
            if lines[i].lstrip().startswith("❯") and RULE.match(lines[i - 1]):
                body = [lines[i].lstrip()[1:].strip()]
                for nxt in lines[i + 1:]:
                    if RULE.match(nxt) or not nxt.strip():
                        break
                    body.append(nxt.strip())
                return " ".join(b for b in body if b)
        return None

    def menu(self):
        return parse_menu(self.lines())

    def plan_text(self):
        """The plan in the plan approval dialog (between the dashed rules), or None."""
        lines = self.lines()
        for i, line in enumerate(lines):
            if "Here is Claude's plan" in line:
                body, inside = [], False
                for nxt in lines[i + 1:]:
                    if re.match(r"^\s*╌{10,}", nxt):
                        if inside:
                            break
                        inside = True
                        continue
                    if inside and nxt.strip():
                        body.append(nxt.strip())
                return "\n".join(body) or None
        return None


BOX = re.compile(r"^(\s*)[│┃]\s?|\s*[│┃]\s*$")


def parse_menu(lines):
    """The open dialog in these screen lines, or None."""
    lines = [BOX.sub(lambda m: m.group(1) or "", line) for line in lines]  # a dialog drawn in a box
    hint = None
    for i in range(len(lines) - 1, -1, -1):
        if any(h in lines[i] for h in HINTS):
            hint = i
            break
    if hint is None:
        return None
    # Numbered options: the last run 1, 2, 3, ... on screen (a dialog sits at the bottom).
    runs, run = [], []
    for i, line in enumerate(lines):
        m = NUMBERED.match(line)
        if not m:
            continue
        n = int(m.group(3))
        if n == 1:
            run = [(i, m)]
            runs.append(run)
        elif run and n == int(run[-1][1].group(3)) + 1:
            run.append((i, m))
    options, first = [], None
    if runs and len(runs[-1]) >= 2 and runs[-1][-1][0] >= hint - 25:
        run = runs[-1]
        first = run[0][0]
        for i, m in run:
            label, checked = m.group(4), None
            box = CHECKBOX.match(label)
            if box:
                checked, label = box.group(1).strip() not in ("", " "), box.group(2)
            options.append(Option(int(m.group(3)), label.strip(), bool(m.group(2)), checked))
    else:
        # Unnumbered options right above the hint: "❯ No, exit" / "  Yes, I trust …".
        rows = []
        for i in range(hint - 1, -1, -1):
            s = lines[i]
            if not s.strip():
                if rows:
                    break
                continue
            if not re.match(r"^\s*(❯\s|\s{2})", s) or RULE.match(s):
                break
            rows.insert(0, i)
        if len(rows) < 2 or not any(lines[i].lstrip().startswith("❯") for i in rows):
            return None
        first = rows[0]
        for i in rows:
            s = lines[i].strip()
            options.append(Option(None, s.lstrip("❯").strip(), s.startswith("❯")))
    # The question: the nearest text line above the options; the title and body above it.
    above = []
    for i in range(first - 1, -1, -1):
        s = lines[i].strip()
        if SOLID_RULE.match(lines[i]) and above:
            break  # the dialog's top edge (dashed rules frame a command or plan inside it)
        if s and not RULE.match(lines[i]):
            above.insert(0, s)
        if len(above) >= 30:
            break
    question = above[-1] if above else ""
    if question.startswith("←") or question.startswith("☐") or question.startswith("☒"):
        question = ""  # the tab bar of a multi-question dialog, not a question
    title = above[0] if len(above) > 1 else ""
    body = " ".join(above[1:-1]) if len(above) > 2 else ""
    return Menu(question, options, title, body)
