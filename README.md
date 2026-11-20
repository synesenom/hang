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
4. **Background listening** starts. You're ready: say **“Claude, \<folder on your Desktop\>”**.

Running `./hang` again later shows the status and starts the background listening if it
isn't running. Tip: `alias hang=~/Desktop/hang/hang`.

## Talking to Claude

### Opening a session

With the background listening on, say **“Claude,”** followed by the name of any folder on your
Desktop: “Claude, hang”, “Claude, the other door”. A new Terminal window opens there
with Claude Code. Names are matched loosely (hyphens and spaces don't matter); if nothing
matches you hear an error sound. “Claude, listen” opens this folder.

You can also start a session yourself in any folder: `./hang talk` (extra arguments go to
`claude`, e.g. `./hang talk --continue`).

### In a session

Just talk. When you **pause for one second**, what you said is sent to Claude, right away even
while it's working (Claude Code's “send now”, Ctrl+X Ctrl+S, rather than Enter, which would
wait until Claude is done).

**Not finished yet?** End with **“amm”** before you pause: the text stays in the prompt and your
next sentence is added to it. (“umm”, “hmm” and “uh” work too.) The pause after a sentence
without “amm” sends everything.

| Say | What happens |
|---|---|
| anything, then a 1 s pause | sent to Claude as a prompt |
| … “amm”, then a pause | kept; keep talking |
| “stop” / “cancel” | interrupts Claude (Esc) |
| “option one” / “two” / “three” | answers a permission prompt or question |
| “scratch that” | clears the prompt |
| “send it” | sends the prompt now |
| “stop listening” / “start listening” | ignore everything until “start listening” |
| “Claude, don't talk” / “Claude, talk” | spoken updates off / on |
| “How is it going?” | the current phase and the last step (“Reading parser.”) |
| “We're done” (or “Claude, done”, “exit”) | exits Claude Code and closes the window |

Commands count only when they're the whole sentence, so “…and stop the server” is sent as text.
A short lead-in or tail is fine (“Okay Claude, we're done here.”, “Exit, we're done.”), and common
mishearings of “Claude” (“cloth”, “close”) are accepted in commands.
`Ctrl+\` mutes the microphone completely (and stops Claude talking). The window title shows the
state: 🎙 listening, 🗣 hearing, ⏳ transcribing, ✍ waiting for more, 🔇 muted.

### What you hear

Claude Code tells you what stage it's at, not every step:

- “Working on it.” when your prompt is sent
- the phase, each time it changes: “Researching.”, “Looking into the code.”, “Planning.”,
  “Making the changes.”, “Running the tests.”, “Fixing the problem.”
- “Found a problem. Looking into it.” when tests fail or a command crashes, and “Tests pass
  now.” once they're fixed
- a one-sentence summary of what happened when it's done
- permission prompts and questions, read out so you can answer by voice

The microphone is off while Claude speaks, so
its voice is never sent back as a prompt (it also means you can't talk over it; `Ctrl+\` stops
it). Tired of it? “Claude, don't talk.”

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

`./hang check` lets you test it live: every sentence shows its score and “you” / “not you”.
Retrain (`./hang train`) after changing microphone or room, or if you're not recognized.
It keeps other voices out; it isn't security (a recording of you would pass).

**English.** Each sentence's language is detected first (a small multilingual model, ~0.2 s);
anything that isn't English is ignored rather than turned into made-up English, and you hear a
short sound so you know it wasn't sent. Spoken commands are always kept (a name or a single word
can fool the check). A foreign word or two can still slip through now and then.

## Commands

| Command | |
|---|---|
| `./hang` | set up or repair, train if needed, start background listening, show status and what to say |
| `./hang talk [claude args]` | Claude Code in this folder, by voice |
| `./hang train` | (re)train your voice; `--reset` deletes your voiceprint (any voice accepted again) |
| `./hang check` | see live whether each sentence is recognized as you |
| `./hang off` | stop the background listening |

The background listening keeps running when you close the terminal, but not after a restart or
log-out: run `./hang` again. While it runs, macOS shows the microphone indicator.

## Configuration

Environment variables, read when a command starts (restart the background listening after
changing them: `./hang off`, then `./hang`).

| Variable | Default | |
|---|---|---|
| `HANG_SILENCE_MS` | `1000` | pause that ends a sentence (and sends it) |
| `HANG_SUBMIT` | `1` | `0` = only type what you say; send with “send it” |
| `HANG_SPEAK` | `1` | `0` = no spoken updates |
| `HANG_VOICE` | system voice | voice for spoken updates (`say -v '?'` lists them) |
| `HANG_SPEAKER_THRESHOLD` | from training | lower accepts you more easily, and others too |
| `HANG_LANGID_MIN_P` | `0.1` | confidence needed to drop a non-English sentence (spoken commands are never dropped; the log shows each sentence's detected language) |
| `HANG_MIN_LEVEL` | `250` | loudness counted as speech; raise in noisy rooms |
| `HANG_MIC` | `default` | input device index (`ffmpeg -f avfoundation -list_devices true -i ""`) |
| `HANG_WAKE_ROOT` | `~/Desktop` | the folders you can name |
| `HANG_WAKE_DIR` | this folder | where “Claude, listen” opens |
| `HANG_MODE` | `handsfree` | `ptt`: push-to-talk with `Ctrl+\` instead |
| `HANG_HOTKEY` | `ctrl+\` | any `ctrl+<char>` |
| `HANG_MODEL` | `models/ggml-base.en.bin` | whisper model for transcription |
| `HANG_PROMPT` | – | vocabulary hint for whisper, e.g. `"pytest, TypeScript, refactor"` |
| `HANG_SOUNDS` | `1` | `0` = no sound effects |

## Troubleshooting

- **Nothing happens when I talk.** Check the microphone permission for your terminal app in
  System Settings → Privacy & Security → Microphone, then `./hang`. `./hang check` shows
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

- `hang talk` runs `claude` inside a pseudo-terminal: your keyboard works as usual, and
  transcribed speech is typed in. No Accessibility permission, no clipboard.
- Spoken updates come from Claude Code hooks (prompt submitted, before each tool, finished,
  notifications), passed to `claude --settings` for that session only; your own settings are
  untouched. Claude is asked to end each reply with a `Voice summary:` line, which is read out.
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
  wake.py            the background listening
models/              downloaded models and your voiceprint (not in git)
tests/test_e2e.py    end-to-end tests
```

## Privacy

- Audio is recorded, checked and transcribed locally and never leaves your computer. It exists
  only in memory and briefly as a temporary WAV file, deleted right after transcription.
  Training recordings are deleted too.
- The network is used only during setup (Homebrew, Poetry, model downloads).
- The transcribed **text** is sent to Anthropic as your prompt, like typed text. Don't use
  Claude Code's built-in `/voice` if you want audio to stay local; it uses a cloud service.
- The background listening hears everything near the microphone but only acts on
  “Claude, \<folder\>”; what it hears is discarded and not logged.

## Development

```sh
poetry install                  # dependencies (pyproject.toml)
python3 tests/test_e2e.py       # all end-to-end tests (~3 min); no microphone needed
python3 tests/test_e2e.py amm   # only tests whose name contains "amm"
```

The tests feed synthesized speech (`say`) instead of the microphone, with `cat` or a fake `claude`
standing in for Claude Code, in isolated temp folders: they don't touch your voiceprint or the
running background listening.

## Limitations and ideas

- macOS only (microphone via AVFoundation, `say`, Terminal).
- You can't interrupt Claude by voice while it's speaking; echo cancellation would allow that.
- The background listening doesn't start at login yet (a LaunchAgent needs its own mic permission).
- A faster English model (Parakeet) and a resident whisper server would cut latency further.
