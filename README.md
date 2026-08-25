# LocalFlow

A local replacement for Wispr Flow. Hold a key, talk, release — cleaned text
appears at your cursor in whatever app has focus.

Nothing leaves the machine. No subscription, no account, no audio uploaded to
anyone's servers.

## How it works

Two pieces, on purpose:

| | |
|---|---|
| **`LocalFlow.app`** | Swift menu-bar app. Owns the hotkey, the microphone, the HUD, and the paste. |
| **`flowd`** | Python daemon holding both models resident, reachable only over a `0600` unix socket. |

They are split because loading Parakeet plus a 4B LLM takes ~25 seconds. Keeping
them warm in a daemon turns that into ~1.1s per utterance. The app is native
because macOS binds Accessibility and Microphone grants to a code signature —
a signed `.app` keeps its permissions across rebuilds, and a Python process
launched from a venv does not.

### The pipeline

```
hold Right Option
   ↓  AVAudioEngine → 16 kHz mono Float32
   ↓  unix socket (newline-delimited JSON, base64 PCM)
   ↓  Parakeet TDT 0.6B v3          ~200ms   speech → text
   ↓  deterministic rules             <1ms   fillers, stutters, dictionary
   ↓  Qwen3-4B-Instruct              ~900ms  punctuation, grammar, false starts
   ↓  guardrails                            reject or accept the model's work
   ↓  clipboard + synthetic ⌘V
text at your cursor
```

### Why Parakeet and not Whisper

Whisper `large-v3-turbo` is still installed as a fallback and covers more
languages, but it is the wrong default for dictation: it **hallucinates
confident text over silence**, and silence is exactly what the leading and
trailing edges of a push-to-talk recording contain. Parakeet returns an empty
string instead. Measured here: a 440 Hz tone and 2s of digital silence both
transcribe to `""`.

Parakeet TDT 0.6B v3 — NVIDIA, CC-BY-4.0, 25 languages, 2.51 GB, ~1.9% WER on
LibriSpeech clean, and ~40× realtime on an M4 Pro.

### Why the LLM pass is on a leash

A small instruct model told to "clean this up" will sometimes answer the
question you dictated, summarise a long passage, or quietly drop a clause. Those
failures are fluent and plausible — you notice when the message you already sent
says something you did not say.

So the model's output is a *proposal*. Four guards run before it is used, and
anything that fails falls back to the deterministic rule-cleaned text:

1. **Length** — grew >1.3× (added content) or shrank <0.55× (summarised).
2. **Content retention** — <70% of input content words survived. Apostrophe-blind,
   because restoring `whats` → `what's` is the job, not a lost word.
3. **Interrogative** — a dictated question that came back as a statement. This
   catches the dangerous near-miss the first two guards miss: *"what is the
   capital of france"* → *"The capital of France is Paris."* is the same length
   and keeps every content word, and is still an answer rather than a cleanup.
4. **Meta-commentary** — the model describing the task instead of doing it.

When a guard fires it is logged with the reason, so you can see which one caught
it rather than guessing.

## Install

```
make bootstrap        # venv + ~4.5 GB of model weights
make install-daemon   # flowd as a LaunchAgent (survives reboot)
make install          # build, sign, install to ~/Applications, launch
```

Then grant two permissions — macOS will prompt, or set them by hand in
**System Settings → Privacy & Security**:

- **Microphone** — to hear you.
- **Accessibility** — to see the hotkey and to paste. Without it macOS silently
  delivers no global key events, so the app looks broken rather than blocked.

Confirm it is alive:

```
make status                                    # daemon + loaded models
~/Applications/LocalFlow.app/Contents/MacOS/LocalFlow --selftest sample.wav
```

## Use

Two ways to capture:

- **Hold Right Option**, speak, release. Push-to-talk, for a sentence or two.
- **Right Option + Right Shift** latches: recording continues with no key held,
  until you press Right Option again. For dictation too long to hold a key for.

Pressing the chord necessarily involves pressing the primary key, so press order
is handled explicitly. With Shift already down, capture starts latched. Adding
Shift *mid-hold* promotes the recording already in progress and keeps the audio
so far — so either order works, and you can decide a sentence in that this one is
going long.

- Holding under 0.3s is a fumbled keypress and is ignored. This does not apply to
  a latched session, which is deliberate however briefly it ran.
- Pressing any other key mid-hold cancels — that was a chord, not dictation. A
  latched session is not cancelled this way; you may well type during one.
- A latched session stops itself after 5 minutes, so a forgotten one does not
  record the room indefinitely. The HUD says "latched" the whole time.
- Both keys are rebindable in the menu, and the same key cannot be bound to both
  (a press would be ambiguous). **Already taken on this machine:** Fn is Wispr
  Flow's trigger, and Right Control double-tap is a Claude desktop shortcut.
- The menu bar toggles **Polished** vs **Verbatim** (rules only, ~200ms, no LLM),
  and rebinds the key (Right/Left Option, Right Command, Right Control, Right
  Shift, Fn).
- **Copy Last Transcript** recovers text if a paste went somewhere unexpected.

### When there is nowhere to paste

Dictating into a window with no editable field would otherwise lose the text
silently: ⌘V goes to whatever has focus, and if that is a file list or a web
page, nothing happens and the words are gone. Two guards:

- **At capture start**, if nothing editable has focus, the HUD dot turns amber
  and reads "⚠︎ no text field focused". Better to learn that before speaking for
  a minute than after.
- **At delivery**, the check runs again — focus can move while you talk — and if
  there is still no target the text is held and a panel appears with a **Copy**
  button. It stays until dismissed, because the case it exists for is finishing a
  long dictation and only then noticing, which is exactly when you may have
  looked away.

Detection uses the accessibility tree, and it deliberately **fails open**: only a
confident "not editable" holds text back. Browsers and Electron apps under-report
editability, and wrongly withholding text from a field that would have taken it
is worse than a paste that lands somewhere harmless. Either way the transcript
stays in **Copy Last Transcript** and in `flowctl last`.

## Configuration

`~/Library/Application Support/LocalFlow/config.json`, written on first run.

```json
{
  "asr_backend": "parakeet",
  "polish_enabled": true,
  "polish_model": "mlx-community/Qwen3-4B-Instruct-2507-4bit",
  "aggressive_fillers": false,
  "dictionary": { "lex cloak": "Lex Cloak", "nice f": "NYSCEF" }
}
```

- **`dictionary`** — proper nouns the ASR has never seen. Case-insensitive,
  word-boundary matched. The place to add names, acronyms and jargon as you hit them.
- **`aggressive_fillers`** — off by default. On, it also strips *like*, *you know*,
  *I mean*, *basically*, *actually*. These are content often enough that removing
  them is an edit, not a cleanup, which is why you have to ask for it.
- **`polish_enabled: false`** — rules only, ~200ms end to end.

Restart the daemon after editing: `launchctl kickstart -k gui/$(id -u)/com.cscmsg.localflow.flowd`

## Privacy

- The socket is `0600` in your Application Support directory. No TCP port is opened.
- Audio is never written to disk — it goes from the mic through memory to the model.
- Transcripts are held in daemon **memory** only (last 20, for `flowctl last`).
  Nothing is logged to disk on purpose: dictation is the most sensitive text on
  the machine, and a plaintext log of it is a liability a local-first tool has no
  reason to create.
- The microphone opens on key-down and closes on release, so the macOS mic
  indicator reflects reality rather than being lit all day.

## Debugging

```
make logs                                  # tail the daemon log
make status                                # health + loaded models
$VENV/bin/python daemon/flowctl.py file x.wav        # full pipeline on a file
$VENV/bin/python daemon/flowctl.py file x.wav --raw  # rules only, no LLM
$VENV/bin/python daemon/flowctl.py last              # recent utterances
```

`flowctl file` prints raw ASR and final text separately, plus which guard fired
if the polish pass fell back — that separation is usually enough to tell whether
a bad result came from mishearing or from over-editing.

## Tests

```
make test
```

33 tests, all offline. The interesting ones are negative: that the stutter
collapser leaves `"I had had enough"` alone, that the filler stripper does not
eat `"like"` or `"actually"`, and that each guardrail rejects the specific way a
model has actually been observed to ruin a dictated message.

## Not built

Scoped out of v1 deliberately:

- **Per-app tone** — the frontmost app name is already captured and passed to the
  daemon as `app_hint`; nothing varies the prompt on it yet.
- **Learned dictionary** — corrections are hand-added to `config.json`.

## Declined

**Live transcription preview** (words appearing in the HUD as you speak) — built
in v0.3.0, removed in v0.4.0 after use. It was not a rendering problem that
could be polished away.

A streaming decoder continuously **revises its own hypothesis**, so displayed
text rewrites itself mid-sentence: you cannot tell what is settled from what is
still moving. Same mechanism that made a partial read *"So why I think"* where
the full pass read *"So I think"*. Making it flow like subtitles would require
holding back unstable text, which adds latency to a preview whose whole value
was immediacy. It is also unreadable while speaking, which is the only time it
is on screen. Wispr Flow does not show midway text either.

**Command mode** (select text, hold the key, say *"make this shorter"*) — decided
against on 2026-08-24, not deferred. The operator never used it in Wispr Flow and
prefers to reword deliberately, by hand or with tools better suited to it.

Worth recording because it is not a small omission and will look like one: it is
a headline Wispr Flow Pro feature, and the pieces to build it are mostly already
here. It also cuts against this tool's design. Every guardrail in `polish.py`
enforces *never act on the content, never change meaning* — which is why text can
appear at your cursor without proofreading. Command mode requires the opposite
("make this concise" is a summarisation request that trips the length guard by
design), so it would need a second pipeline with much weaker guards, and it
overwrites text you already wrote rather than filling an empty cursor.

## Verified

- `parakeet-mlx` 0.5.2 (PyPI, 2026-06-05), Apache-2.0
- `mlx-lm` 0.31.3 (PyPI, 2026-04-22)
- `mlx-community/parakeet-tdt-0.6b-v3` — CC-BY-4.0, 2.51 GB, 25 languages
- Built and run on macOS 26.5.1, Xcode 26.6, M4 Pro / 64 GB, 2026-08-24
