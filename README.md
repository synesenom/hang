# hang

> **hang** /hɒŋɡ/ *(Hungarian)*: voice. Roughly “hong”, with a soft *g* at the end.

Talk to [Claude Code](https://docs.claude.com/en/docs/claude-code) instead of typing, and hear
what it's doing. Audio is recorded, checked and transcribed **locally, and never leaves your
computer**: speech recognition, voice recognition and language detection are local models, and
only the transcribed text goes to Claude, as if you had typed it.

> “Claude, the other door.”
> *A Terminal window opens in `~/Desktop/the-other-door` with Claude Code, listening.*
> “Run the tests and tell me what fails.”
> *“Working on it.” … “Running the tests.” … “Found a problem. Looking into it.” … “Two tests fail, both in the parser.”*
> “We're done.”
> *Claude Code exits and the window closes.*

## Getting started

**You need:** a Mac, [Homebrew](https://brew.sh) and Claude Code (`claude` on your PATH).
Everything else is installed for you.

```sh
cd ~/Desktop/hang
./hang
```

The first run walks you through everything, with progress in the terminal:

1. **Bootstrap**: Python, Poetry and the Python packages, installed into `.venv/` here.
2. **Setup**: `ffmpeg` and `whisper-cpp` via Homebrew, then the models (about 250 MB, with
   download bars). macOS asks once to let your terminal use the microphone; allow it.
3. **Voice training**: you read a short passage aloud. The words light up as they're
   recognized while the voice recognition learns you; you can watch its estimated accuracy
   climb with each sentence. About a minute.
4. **Background listening** starts, now and every time you log in. You're ready: say
   **“Claude, \<folder on your Desktop\>”**.

That's the last time you need the keyboard. Running `./hang` again just shows the status (and
repairs anything missing); `./hang off` turns it all off. Tip: `alias hang=~/Desktop/hang/hang`.

## Talking to Claude

### Opening a session

With the background listening on, say **“Claude,”** followed by the name of any folder on your
Desktop: “Claude, hang”, “Claude, the other door”. A new Terminal window opens there
with Claude Code. Names are matched loosely (hyphens and spaces don't matter). “Claude, listen”
opens this folder.

**It picks up where you left off.** If you've used Claude Code in that folder before, its last
conversation is reopened; say “clear the conversation” in the session to start afresh. “Claude,
continue where we left off” reopens the folder you opened last.

If it doesn't understand, it asks: “Sorry, there's no folder called pong. Which folder?”, or
“Sorry, I didn't catch that. Say it again?” for a short sentence of yours that isn't English.
For the next 10 seconds the folder name alone is enough (“The other door.”). After two
questions in a row it gives up with an error sound.

The first time Claude Code opens a folder it asks whether you trust it. hang reads that out
too: say **“yes”** to trust it or **“no”** to leave.

You can also start a session yourself in any folder: `./hang talk`. It also picks up where you
left off (extra arguments go to `claude`; `HANG_RESUME=0` always starts afresh).

### In a session

Just talk. When you **pause for one second**, what you said is sent to Claude, right away even
while it's working (Claude Code's “send now”, Ctrl+X Ctrl+S, rather than Enter, which would
wait until Claude is done).

**Long sentences appear as you speak.** Once what you've said is longer than any command
(more than 8 words), it's typed into Claude Code while you're still talking, and corrected as
the speech recognizer hears more (the last word or two may change). The pause then fixes it
to the final version and sends it. Short sentences appear at the end, since they might be
commands. Nothing is typed live until your voice and English have been confirmed, nor while
a dialog is open or Claude is talking.

**Not finished yet?** End with **“amm”** before you pause: the text stays in the prompt and your
next sentence is added to it. (“umm”, “hmm” and “uh” work too.) The pause after a sentence
without “amm” sends everything. While it waits, you can fix what you said (see below).

| Say | What happens |
|---|---|
| anything, then a 1 s pause | sent to Claude as a prompt |
| … “amm”, then a pause | kept; keep talking |
| “send it” | sends the prompt now |
| “scratch that” | clears the prompt |
| “delete the last sentence” (“undo”) | removes the last thing you dictated |
| “replace parser with lexer” | fixes a word in what you dictated, before it's sent |
| “read it back” | reads out what's in the prompt |
| “stop” / “cancel” | interrupts Claude (Esc); while Claude is talking, stops the talking |
| “say that again” | repeats the last thing Claude said |
| “read me the whole answer” | reads Claude's last reply in full (code and tables are only mentioned) |
| “How is it going?” | the current phase and the last step (“Reading parser.”) |
| “plan mode” / “accept edits” / “auto mode” / “manual mode” | switches Claude Code's mode (Shift+Tab) |
| “compact the conversation” | `/compact` |
| “clear the conversation” | `/clear` (a new conversation) |
| “change the model” | `/model`, then pick one by voice |
| “slash \<command\>” | any slash command, e.g. “slash review” |
| “stop listening” / “start listening” | ignore everything until “start listening” |
| “Claude, don't talk” / “Claude, talk” | spoken updates off / on |
| “We're done” (or “Claude, done”, “exit”) | exits Claude Code and closes the window |

Commands count only when they're the whole sentence, so “…and stop the server” is sent as text.
A short lead-in or tail is fine (“Okay Claude, we're done here.”, “Exit, we're done.”), and common
mishearings of “Claude” (“cloth”, “close”) are accepted in commands. “Replace … with …” only
counts while there's dictated text waiting that contains the word; otherwise it's a prompt.

**File names.** hang tells the speech recognizer the names of the files in your project, so
“session.py” or “pyproject” come out spelled the way they are. You can also spell them out:
“session dot py” becomes `session.py`, “test underscore e2e” becomes `test_e2e`.

`Ctrl+\` mutes the microphone completely (and stops Claude talking). The window title shows the
state: 🎙 listening, 🗣 hearing, ⏳ transcribing, ✍ waiting for more, 🔇 muted.

### Answering questions, permissions and plans

hang watches Claude Code's screen, so whenever a dialog opens it reads it out with its options
and your next sentence answers it. This works for Claude's questions, permission prompts, the
plan approval, the folder trust check and menus like `/model`.

| Dialog | You can say |
|---|---|
| a question | “Option two.”, “The second one.”, the option's name (“SQLite.”), “Option two, but keep the old file.”, or anything in your own words (“Neither, just use a JSON file.”) |
| a question with checkboxes | “Apple and cherry.”, “Options one and three.”, “All of them.”, then it moves on; “Next.” skips |
| several questions | each is read out in turn; at the end, “Submit.” |
| a permission prompt | “Yes.”, “Yes, always.”, “No.”, “Option three.”, “Yes, but keep a backup.”, or what to do instead (“No, move it to the archive folder instead.”) |
| the plan | “Approve.”, “Approve manually.”, “Read me the plan.”, or what to change (“Make it three steps and add tests.”) |
| the trust check | “Yes.” / “No.” |
| any of them | “What were the options?”, “Stop.” (closes it) |

How your own words get there:

- **Questions and plans** have a row you can type into (“Type something”, “Tell Claude what to
  change”): your words go there, as a real answer. “Option two, but keep the old file” becomes
  the answer “SQLite, but keep the old file”.
- **Permission prompts** have no such row, so your words close the prompt (as if you said no)
  and are sent as your next message. “Yes, but …” allows it and sends the rest as a message.

In a dialog every answer is final: “amm” doesn't hold it back.

### What you hear

- “Working on it.” when your prompt is sent.
- Claude's own short progress notes as it finds things (“The bug is in the parser.”). Routine
  steps like reading files are not spoken; “How is it going?” tells you the latest one.
- Every dialog, with its options.
- A one-sentence summary when it's done. “Read me the whole answer” for all of it.

**Talking over Claude.** Once you've trained your voice, the microphone stays on while Claude
speaks, and a short command cuts in: “Stop.” silences it, “Option two.” answers the question it
is reading out. Your voiceprint is what tells your voice from Claude's (training checks that
macOS voices are rejected), so without one the microphone is off while Claude speaks. Longer
sentences said over Claude are ignored (you hear a short sound), so its voice can never end up
in a prompt. Tired of it? “Claude, don't talk.”

## What hang can and can't do

**You can, by voice alone:**

- open Claude Code in any Desktop folder, right where you left off, and trust a new folder
- dictate prompts (long ones appear as you speak), add to them, fix them and hear them back
  before sending, and send them even while Claude works
- interrupt Claude, and stop it talking
- answer every question, permission prompt and plan approval, with a comment if you like
- switch modes (plan, accept edits, auto, manual), compact or clear the conversation, change the
  model, run any slash command
- hear what's happening, ask again, and have whole replies read out
- end the session and close the window
- have all of this start by itself when you log in

**You can't (yet):**

- **Edit text you typed by hand**: “delete the last sentence” and “replace …” work on what you
  dictated in this prompt; once you've typed on the keyboard, only “scratch that” (clear all)
  and “read it back” still work.
- **Move the cursor or select text** inside a prompt, or edit a file directly: ask Claude to.
- **Pick from long lists** like `/resume`'s conversation list by name; options are read out and
  picked by number, and only the ones on screen.
- **Hear code, diffs or tables** read character by character: they're mentioned, not read.
  Ask Claude to explain them instead.
- **Talk over Claude without a voiceprint**: the mic is off while it speaks.
- **Use another language than English**, or another speaker's voice once you've trained yours.
- **Paste images**, drag files in, or use Claude Code's keyboard-only features like Vim mode,
  Ctrl+R history search or the `!` shell mode prefix.
- **Use it off macOS**, or with terminals other than Terminal for the window handling.

## Only your voice, only English

**Your voice.** Training saves a *voiceprint* (`models/voiceprint.json`, a list of numbers; the
recordings are deleted). Every sentence is compared with it before anything else happens; other
voices, including the TV or a colleague, are ignored. This matters because a pause sends a prompt.

The numbers on the training screen:

- **Recognizes you**: how many of your sentences match a voiceprint built from the *other*
  sentences, a fair preview of how you'll score later.
- **Rejects others**: built-in macOS voices stand in for other people and should all fall below
  the threshold.
- **Estimated accuracy**: both together. It's an estimate on a small sample with synthetic
  “other people”; real voices in a real room vary more.

`./hang train --check` lets you test it live: every sentence shows its score and “you” / “not you”
(training ends with the same test).
Retrain (`./hang train`) after changing microphone or room, or if you're not recognized.
It keeps other voices out; it isn't security (a recording of you would pass).

**English.** Each sentence's language is detected first (a small multilingual model, ~0.2 s);
anything that isn't English is ignored rather than turned into made-up English, and you hear a
short sound so you know it wasn't sent. Spoken commands are always kept (a name or a single word
can fool the check). A foreign word or two can still slip through now and then.

## Commands

| Command | |
|---|---|
| `./hang` | **on**: sets up whatever is missing, trains your voice the first time, listens in the background now and at every login, shows the status |
| `./hang off` | **off**: stops listening, and it won't start at login |
| `./hang talk` | Claude Code in this folder, by voice, where you left off (extra arguments go to `claude`) |
| `./hang train` | retrain your voice, then try it out; `--check` only tries it out, `--reset` deletes your voiceprint |

At login, a Terminal window opens for a few seconds, starts the listening and closes again.
It goes through Terminal because Terminal already has microphone access; the first time, macOS
may ask whether Terminal may control Terminal (to close the window). While it listens, macOS
shows the microphone indicator.

## Settings

Environment variables, read when hang starts. After changing one, turn it off and on again:
`./hang off`, then `./hang`. (After updating hang itself you don't need to: the background
listening restarts with the new code by itself, between sessions.)

| Variable | Default | |
|---|---|---|
| `HANG_SILENCE_MS` | `1000` | pause that ends a sentence (and sends it) |
| `HANG_SPEAK` | `1` | `0` = no spoken updates; dialogs play a sound instead (“What were the options?” reads them) |
| `HANG_VOICE` | system voice | voice for spoken updates (`say -v '?'` lists them) |
| `HANG_MIC` | `default` | input device index (`ffmpeg -f avfoundation -list_devices true -i ""`) |

<details>
<summary>Advanced settings</summary>

| Variable | Default | |
|---|---|---|
| `HANG_LOGIN` | `1` | `0` = `./hang` doesn't make it start at login |
| `HANG_SUBMIT` | `1` | `0` = only type what you say; send with “send it” |
| `HANG_RESUME` | `1` | `0` = every session starts a new conversation instead of picking up the last one |
| `HANG_LIVE` | `1` | `0` = don't type long sentences while you talk, only at the pause |
| `HANG_BARGE_IN` | `auto` | talking over Claude: `auto` = with a voiceprint, `1` = always, `0` = never (mic off while it speaks) |
| `HANG_VOCAB` | `1` | `0` = don't give the speech recognizer your project's file names |
| `HANG_SPEAKER_THRESHOLD` | from training | lower accepts you more easily, and others too |
| `HANG_SPEAKER_TOLERANCE` | `0.08` | how far below the threshold a spoken command (“We're done”, “Stop”) may score and still count; dictation always needs the full threshold |
| `HANG_LANGID_MIN_P` | `0.1` | confidence needed to drop a non-English sentence (spoken commands are never dropped; the log shows each sentence's detected language) |
| `HANG_MIN_LEVEL` | `250` | loudness counted as speech; raise in noisy rooms |
| `HANG_WAKE_ROOT` | `~/Desktop` | the folders you can name |
| `HANG_WAKE_DIR` | this folder | where “Claude, listen” opens |
| `HANG_MODE` | `handsfree` | `ptt`: push-to-talk with `Ctrl+\` instead |
| `HANG_HOTKEY` | `ctrl+\` | any `ctrl+<char>` |
| `HANG_MODEL` | `models/ggml-base.en.bin` | whisper model for transcription |
| `HANG_PROMPT` | – | vocabulary hint for whisper, e.g. `"pytest, TypeScript, refactor"` |
| `HANG_SOUNDS` | `1` | `0` = no sound effects |

</details>

## Troubleshooting

- **Nothing happens when I talk.** Check the microphone permission for your terminal app in
  System Settings → Privacy & Security → Microphone, then `./hang`. `./hang train --check` shows
  whether you're heard and recognized.
- **“not you” when it is you.** Retrain where you usually sit (`./hang train`), or lower the
  threshold: `./hang off`, then `HANG_SPEAKER_THRESHOLD=0.4 ./hang`.
- **The window doesn't close after “We're done”.** The first time, macOS asks whether Terminal
  may control Terminal; allow it (System Settings → Privacy & Security → Automation).
- **Logs**: `$TMPDIR/hang.log` (sessions; contains transcripts) and
  `$TMPDIR/hang-wake.log` (background; only wake events). `HANG_LOG=/dev/null` disables
  the session log.

## How it works

```
 mic ─► ffmpeg ─► pause detection ─► your voice? ─► English? ─► whisper ─► command / text
                                    (WeSpeaker)    (whisper     (base.en)         │
                                                    tiny)                         ▼
 keyboard ─────────────────────────────────────────► hang talk (PTY) ─────► claude
                                                         ▲                     │ hooks
                                     macOS `say` ◄───────┴──── short notes ◄───┘
```

- In a session, the speech recognizer runs as a local server (`whisper-server` on 127.0.0.1,
  for the length of the session), so the model stays loaded: a sentence takes about 0.1 s
  instead of 0.4 s. That's also what makes typing while you talk possible: about once a
  second, what you've said so far is transcribed again. If the server isn't available,
  `whisper-cli` is used as before.
- `hang talk` runs `claude` inside a pseudo-terminal: your keyboard works as usual, and
  transcribed speech is typed in. No Accessibility permission, no clipboard.
- Spoken updates come from Claude Code hooks (prompt submitted, before each tool, finished), passed to `claude --settings` for that session only; your own settings are
  untouched. Claude is asked to end each reply with a `Voice summary:` line, which is read out.
- Everything Claude Code prints also goes into a terminal emulator (pyte), so hang sees the
  screen as you would: which dialog is open and its options, the mode in the footer, the text in
  the prompt. Dialogs are answered with the keys you'd press (a number, the arrows, Enter, or
  typing into the dialog's answer row).
- The background listening uses the same pipeline and only reacts to “Claude, \<folder\>”. It
  lets go of the microphone while a session is open, so only one thing listens at a time.

```
hang                launcher: runs `python -m hang` in the Poetry environment
setup.sh             bootstrap: Python, Poetry, .venv (./hang runs it the first time)
src/hang/
  cli.py             the commands (Click + rich)
  install.py         tools and model downloads, with progress
  training.py        the training screen and its accuracy figures
  speaker.py         voiceprints (sherpa-onnx + WeSpeaker)
  audio.py           capture, pause detection, language check, transcription
  session.py         `hang talk`: the Claude Code wrapper
  hook.py            Claude Code hooks → short spoken notes
  screen.py          Claude Code's screen: dialogs, mode, prompt text
  dialogs.py         reading dialogs out and answering them by voice
  autostart.py       starting at login (turned on by ./hang, off by ./hang off)
  wake.py            the background listening
models/              downloaded models and your voiceprint (not in git)
tests/test_e2e.py    end-to-end tests
tests/screens/       real Claude Code dialogs, for the screen tests
```

## Privacy

- Audio is recorded, checked and transcribed locally and never leaves your computer. It exists
  only in memory and briefly as a temporary WAV file, deleted right after transcription.
  Training recordings are deleted too.
- The network is used only during setup (Homebrew, Poetry, model downloads). In a session
  the speech recognizer listens on a local port (127.0.0.1 only, never the network).
- The transcribed **text** is sent to Anthropic as your prompt, like typed text. Don't use
  Claude Code's built-in `/voice` if you want audio to stay local; it uses a cloud service.
- The background listening hears everything near the microphone but only acts on
  “Claude, \<folder\>”; what it hears is discarded and not logged.

## Development

```sh
poetry install                  # dependencies (pyproject.toml)
.venv/bin/python tests/test_e2e.py       # all end-to-end tests (~1 min); no microphone needed
.venv/bin/python tests/test_e2e.py amm   # only tests whose name contains "amm"
```

The tests feed synthesized speech (`say`) instead of the microphone, with `cat` or a fake `claude`
standing in for Claude Code, in isolated temp folders: they don't touch your voiceprint or the
running background listening.

## Limitations and ideas

- macOS only (microphone via AVFoundation, `say`, Terminal).
- Talking over Claude relies on your voiceprint, not echo cancellation: with loudspeakers,
  your voice and Claude's mix, so a command said over it may be missed. Headphones help.
- Dialogs are recognized from how Claude Code draws them; a Claude Code update that changes
  that can break it (the screens in tests/screens show what is expected).
- A faster English model (Parakeet) would cut latency further.
