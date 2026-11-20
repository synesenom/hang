"""Answering Claude Code's dialogs by voice.

Given the dialog on screen (hang.screen.Menu) and what you said, work out the
keys to press. Pure functions, so they can be tested without a terminal:

  announce(menu, plan)  what to say when a dialog appears
  answer(menu, text)    a list of steps for the session to carry out:
                          ("keys", bytes)   type these keys
                          ("wait", seconds) let the screen catch up
                          ("say", text)     say this (e.g. a hint)
                          ("prompt", text)  the dialog is closed: send this as
                                            your next prompt
"""
import re

from hang.audio import LEAD_WORDS, LOOSE_NAMES, NAMES, TAIL_WORDS, command_core, normalize

NUMBER_WORDS = ["one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
ORDINALS = ["first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"]
ESC, ENTER, TAB, UP, DOWN = b"\x1b", b"\r", b"\t", b"\x1b[A", b"\x1b[B"

# Picking an option: "option two", "number 2", "the second one", "let's go with
# option two", optionally followed by a comment ("…, but keep the old file").
CHOICE = re.compile(
    r"^\W*(?:(?:%s)\W+)*(?:(?:%s)\W+)?"  # "Okay, Claude, …"
    r"(?:(?:let'?s|let us|i'?ll|i will|we'?ll|i'?d|i would)\s+(?:go with|take|pick|choose|do|say)\s+|"
    r"(?:go with|take|pick|choose|i choose|i pick)\s+)?(?:the\s+)?"
    # "option two …" takes any comment; a bare ordinal only before punctuation
    # ("First, but …"), so "The first test fails" stays a sentence.
    r"(?:(?:option|number|choice)\s+(%s|[1-9])\b(.*)|(%s)(?:\s+(?:one|option|choice)\b(.*)|([^\w\s].*)?))$"
    % ("|".join(sorted(LEAD_WORDS)), "|".join(sorted(NAMES | LOOSE_NAMES)),
       "|".join(NUMBER_WORDS), "|".join(ORDINALS)), re.I | re.S)


def choice_of(text):
    """(2, "but keep the old file") for "Option two, but keep the old file.",
    (2, "") for "The second one, please.", None if no option is picked."""
    m = CHOICE.match(text or "")
    if not m:
        return None
    word = (m.group(1) or m.group(3)).lower()
    n = int(word) if word.isdigit() else (NUMBER_WORDS + ORDINALS).index(word) % len(NUMBER_WORDS) + 1
    rest = (m.group(2) or m.group(4) or m.group(5) or "").strip(" ,.;:!?-–—")
    if all(w in TAIL_WORDS or w in ("you", "thank") for w in normalize(rest).split()):
        rest = ""  # "…, please." / "…, thank you."
    return n, rest


# ---------------------------------------------------------------- reading out

def short_label(label):
    """'Yes, and switch to auto mode · auto mode handles …' -> 'Yes, and switch to auto mode'."""
    return label.split(" · ")[0].rstrip(" .:")


def words(text, limit):
    w = text.split()
    return " ".join(w[:limit]) + ("…" if len(w) > limit else "")


def listing(menu):
    return " ".join("Option %s: %s." % (NUMBER_WORDS[o.number - 1] if o.number and o.number <= 9 else
                                         "", short_label(o.label)) for o in menu.spoken_options())


def announce(menu, plan=None, folder=None):
    kind = menu.kind
    if kind == "trust":
        return ("Claude Code asks whether you trust this folder%s. Say yes to trust it, or no to exit."
                % (", " + folder if folder else ""))
    if kind == "plan":
        first = (plan or "").strip().splitlines()
        steps = len([l for l in first if re.match(r"^(\d+\.|[-*•])\s", l)])
        gist = words(re.sub(r"^(#+|\d+\.|[-*•])\s*", "", first[0]).rstrip(". "), 20) if first else ""
        return ("The plan is ready%s%s. Say approve, or approve manually, or read me the plan, "
                "or tell me what to change." % (": " + gist if gist else "",
                                                ", %d steps" % steps if steps > 1 else ""))
    if kind == "permission":
        what = menu.title + (": " + words(menu.body, 18) if menu.body else "")
        return "Permission needed. %s. %s Or tell me what to do instead." % (what.rstrip(". "), listing(menu))
    if kind == "review":
        return "Submit your answers? Say submit, or cancel."
    said = (menu.question + " " if menu.question else "") + listing(menu)
    if menu.multi:
        return said + " Pick any, then say next. Or just tell me."
    if menu.text_row():
        return said + " Pick one, or just tell me."
    return said + " Say a number, or stop."


# ---------------------------------------------------------------- answering

YES = {"yes", "yeah", "yep", "sure", "ok", "okay", "yes please", "approve", "approved", "allow", "allow it",
       "go ahead", "do it", "accept", "accept it", "i trust it", "trust it", "yes i trust it", "trusted",
       "yes trust it", "looks good", "sounds good", "lgtm", "approve it", "approve the plan", "yes go ahead"}
NO = {"no", "nope", "no thanks", "deny", "dont", "do not", "no exit", "exit", "dont allow", "dont trust it",
      "reject", "no dont"}
ALWAYS = {"always", "always allow", "yes always", "dont ask again", "yes dont ask again", "allow always"}
MANUAL = {"approve manually", "approved manually", "manually approved", "approve it manually", "manually", "manual", "yes manually", "manually approve", "approve with manual edits",
          "manually approve edits", "yes manually approve edits"}
AUTO = {"auto", "auto mode", "approved auto mode", "approve auto mode", "approve auto", "yes auto", "approve with auto mode", "auto accept", "yes auto mode",
        "accept edits", "auto accept edits"}
NEXT = {"next", "next question", "done", "thats it", "thats all", "continue", "move on", "go on", "submit",
        "submit it", "submit answers", "send it", "send"}
STOP = {"stop", "cancel", "escape", "never mind", "nevermind", "forget it", "close it"}
REPEAT = {"what were the options", "what are the options", "read the options", "repeat the options",
          "read them again", "options", "say the options", "say the options again", "read the question",
          "what was the question", "repeat the question", "repeat that", "say that again", "come again",
          "what did you say", "pardon", "sorry"}
READ_PLAN = {"read me the plan", "read the plan", "read it", "what is the plan", "whats the plan",
             "read me the whole plan", "read the whole plan", "tell me the plan"}


def pick(menu, option):
    """Keys that pick this option: its number, or the arrows and Enter."""
    if option.number:
        return [("keys", str(option.number).encode())]
    i, j = menu.highlighted(), menu.options.index(option)
    return [("keys", (DOWN if j > i else UP) * abs(j - i)), ("wait", 0.2), ("keys", ENTER)]


def by_label(menu, core):
    """The option whose name you said ("SQLite", "Large, please")."""
    if not core:
        return None
    for o in menu.options:
        if normalize(short_label(o.label)) == core:
            return o
    return None


def first_matching(menu, test):
    for o in menu.options:
        if test(normalize(o.label)):
            return o
    return None


def numbers_in(menu, text):
    """Options named in "Options one and three" / "Apple and cherry" / "all of them"."""
    norm = normalize(text)
    if re.search(r"\b(all|every|everything|all of them)\b", norm):
        return [o for o in menu.spoken_options()]
    found = []
    for w in re.findall(r"[a-z0-9]+", norm):
        n = None
        if w.isdigit() and 0 < int(w) < 10:
            n = int(w)
        elif w in NUMBER_WORDS:
            n = NUMBER_WORDS.index(w) + 1
        elif w in ORDINALS:
            n = ORDINALS.index(w) + 1
        o = menu.find(n) if n else None
        if o and o not in found:
            found.append(o)
    for o in menu.spoken_options():
        if re.search(r"\b%s\b" % re.escape(normalize(short_label(o.label))), norm) and o not in found:
            found.append(o)
    return found


def type_into(menu, row, text):
    """Pick the row you can type into (unless it is already), type your answer, Enter."""
    steps = [] if row.highlighted else pick(menu, row) + [("wait", 0.3)]
    return steps + [("keys", text.encode()), ("wait", 0.2), ("keys", ENTER)]


def answer(menu, text, plan=None, folder=None):
    """What to do with what you said while this dialog is open (see module doc)."""
    core, _ = command_core(text)
    core = core or normalize(text)
    kind = menu.kind
    if core in STOP:
        return [("keys", ESC)]
    if core in REPEAT:
        return [("say", announce(menu, plan, folder))]
    if kind == "plan" and core in READ_PLAN:
        return [("say", plan or "I can't see the plan on screen.")]

    if kind == "trust":
        yes = first_matching(menu, lambda l: l.startswith("yes"))
        no = first_matching(menu, lambda l: l.startswith("no"))
        if core in YES and yes:
            return pick(menu, yes)
        if core in NO and no:
            return pick(menu, no)
        return [("say", "Say yes to trust this folder, or no to exit.")]

    if kind == "review":
        submit = first_matching(menu, lambda l: l.startswith("submit"))
        if (core in NEXT or core in YES) and submit:
            return pick(menu, submit)

    choice = choice_of(text)
    option = None
    rest = ""
    if choice:
        option, rest = menu.find(choice[0]), choice[1]
        if option and rest and normalize(rest) == normalize(short_label(option.label)):
            rest = ""  # "Option two, green.": the option's own name is no comment
    if option is None:
        option = by_label(menu, core)
    if option is None and core in NUMBER_WORDS:
        option = menu.find(NUMBER_WORDS.index(core) + 1)  # just "Two."
    if option is None and kind in ("permission", "plan"):
        if core in ALWAYS:
            option = first_matching(menu, lambda l: "always" in l or "dont ask again" in l)
        elif core in MANUAL:
            option = first_matching(menu, lambda l: "manually" in l)
        elif core in AUTO:
            option = first_matching(menu, lambda l: l.startswith("yes") and "auto" in l)
        elif core in YES:
            option = first_matching(menu, lambda l: l.startswith("yes"))
        elif core in NO and kind == "permission":
            option = first_matching(menu, lambda l: l == "no" or l.startswith("no"))

    if menu.multi:
        named = numbers_in(menu, text) if not rest else []
        if option and not rest and option not in named:
            named.insert(0, option)
        if core in NEXT and not named:
            return [("keys", TAB)]
        if named:
            # What you said is the whole answer: tick those, untick the rest.
            steps = []
            for o in menu.options:
                if o.checked is not None and o.checked != (o in named):
                    steps += pick(menu, o) + [("wait", 0.15)]
            return steps + [("wait", 0.2), ("keys", TAB)]  # on to the next question (or the review)

    if option and option.text_row:
        if not rest:  # "Option three" on "Type something": ready for your answer
            return pick(menu, option) + [("say", "Go ahead.")] if not option.highlighted else [("say", "Go ahead.")]
        option = None
    if option and not rest:
        return pick(menu, option)
    if kind == "permission" and option and rest:
        if normalize(option.label).startswith("no"):
            return [("keys", ESC), ("wait", 0.5), ("prompt", rest)]
        # "Yes, but keep a backup": allow it, and tell Claude the rest right away.
        return pick(menu, option) + [("wait", 0.5), ("prompt", rest)]

    # Your own words: typed into the dialog's answer row when it has one,
    # otherwise the dialog is closed and they are sent as your next prompt.
    said = text.strip()
    if option and rest:
        said = "%s, %s" % (short_label(option.label), rest)
    row = menu.text_row()
    if row:
        return type_into(menu, row, said)
    if kind == "permission":
        return [("keys", ESC), ("wait", 0.5), ("prompt", said)]
    return [("say", "Say a number, or stop.")]
