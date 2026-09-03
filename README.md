# Lippy

Speak up instead of typing. Hold a key, talk, let go, and clean text appears at
your cursor in whatever app you are using.

Everything runs on your Mac. The audio stays on your machine, there is no
account, and there is nothing to subscribe to.

## How it works

Two pieces, on purpose:

| | |
|---|---|
| **`Lippy.app`** | Swift menu-bar app. Owns the hotkey, the microphone, the HUD, and the paste. |
| **`lippyd`** | Python daemon holding both models resident, reachable only over a `0600` unix socket. |

They are split because loading Parakeet plus a 4B LLM takes ~25 seconds. Keeping
them warm in a daemon turns that into ~1.1s per utterance. The app is native
because macOS binds Accessibility and Microphone grants to a code signature. A
signed `.app` keeps its permissions across rebuilds. A Python process launched
from a venv does not.

### The pipeline

```
hold Right Option
   ↓  AVAudioEngine → 16 kHz mono Float32
   ↓  unix socket (newline-delimited JSON, base64 PCM)
   ↓  Parakeet TDT 0.6B v3          ~200ms   speech → text
   ↓  deterministic rules             <1ms   fillers, stutters, terms, addresses, numbers
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

Parakeet TDT 0.6B v3, NVIDIA, CC-BY-4.0, 25 languages, 2.51 GB, ~1.9% WER on
LibriSpeech clean, and ~40× realtime on an M4 Pro.

### Why the LLM pass is on a leash

A small instruct model told to "clean this up" will sometimes answer the
question you dictated, summarise a long passage, or quietly drop a clause. Those
failures are fluent and plausible. You notice when the message you already sent
says something you did not say.

So the model's output is a *proposal*. Four guards run before it is used, and
anything that fails falls back to the deterministic rule-cleaned text:

1. **Length**: grew >1.3× (added content) or shrank <0.55× (summarised).
2. **Content retention**: <70% of input content words survived. Apostrophe-blind,
   because restoring `whats` → `what's` is the job, not a lost word.
3. **Interrogative**: a dictated question that came back as a statement. This
   catches the dangerous near-miss the first two guards miss: *"what is the
   capital of france"* → *"The capital of France is Paris."* is the same length
   and keeps every content word, and is still an answer rather than a cleanup.
4. **Meta-commentary**: the model describing the task instead of doing it.

When a guard fires it is logged with the reason, so you can see which one caught
it rather than guessing.

## Requirements (macOS)

- **Apple Silicon Mac** (M1 or later). MLX is Metal and unified-memory based, so
  there is no Intel path. Both the installer and the daemon refuse to run rather
  than failing obscurely.
- **macOS 14** or later.
- **Python 3.12 or newer**: Homebrew (`brew install python@3.12`) or
  python.org. Only used to build a private environment under Application
  Support. Nothing is installed into your system Python.
- **About 5 GB of disk** for the model weights, downloaded once.

## Install

**From a release**

1. Download the latest `Lippy-x.y.z.dmg` from
   [Releases](https://github.com/cscmsg/lippy/releases) and drag the app to
   Applications.
2. Launch it, then choose **Run First-Time Setup…** from its menu bar item. That
   builds the Python environment, downloads the models (~4.5 GB, once) and
   installs the background service. It is safe to re-run.
3. Grant the two permissions below.

Or with Homebrew:

```
brew install --cask cscmsg/tap/lippy
```

**From source**

```
make bootstrap        # venv + ~4.5 GB of model weights
make install-daemon   # lippyd as a LaunchAgent (survives reboot)
make install          # build, sign, install to ~/Applications
make dmg              # optional: build a distributable disk image
```

Then grant two permissions. macOS will prompt, or you can set them by hand in
**System Settings → Privacy and Security**:

- **Microphone**: to hear you.
- **Accessibility**: to see the hotkey and to paste. Without it macOS silently
  delivers no global key events, so the app looks broken rather than blocked.

Confirm it is alive:

```
make status                                    # daemon + loaded models
~/Applications/Lippy.app/Contents/MacOS/Lippy --selftest sample.wav
```

## Windows

Partly built. The pipeline runs from source, transcribes a file and cleans the
text. The tray icon, the hotkey and the paste are not written yet, so there is
no hold-to-talk on Windows and little reason to install this unless you are
working on it. What is left is tracked in
[docs/plans/windows-client.md](docs/plans/windows-client.md).

There is no daemon on Windows and there will not be one. The two-process split
on macOS exists because macOS binds Microphone and Accessibility permission to
a signed app bundle, which a virtual environment cannot hold across rebuilds.
Windows has no equivalent constraint, so the finished client is one process.

**Requirements (Windows)**

- Windows 10 or later on x64. Both runtime wheels also publish arm64 builds,
  which nobody has run here.
- Python 3.12 or newer.
- About 1 GB of disk. The speech model is 487 MB compressed and 643 MB
  unpacked, and no language model is downloaded at all.

**From source**

```
python -m venv .venv
.venv\Scripts\pip install -r daemon\requirements-windows.txt
.venv\Scripts\python daemon\models.py
```

The last line fetches the speech model into `%USERPROFILE%\.cache\lippy-onnx`.
It resumes if the connection drops, so running it again after a failure picks
up where it stopped rather than starting the 487 MB over. Set
`LIPPY_ONNX_MODEL_DIR` to keep the model elsewhere, or to point at a copy you
already have.

Then run a recording through the pipeline:

```
.venv\Scripts\python daemon\selftest.py tests\fixtures\hello-lippy.wav
```

It prints the raw transcript and the final text separately, which is usually
enough to tell a mishearing from an over-edit.

**What differs from macOS, and why**

- **The speech backend is sherpa-onnx rather than MLX.** MLX is Metal based and
  has no Windows build at all. Same model family, different runtime, and the
  int8 export is 643 MB against MLX's 2.51 GB.
- **Cleanup ships at `clean` rather than `polish`.** The dial still has four
  positions and everything below `polish` is the same deterministic regex on
  both platforms. Reaching `polish` here means supplying a genai-format model
  directory yourself, so it is not the default, and the code says exactly that
  instead of failing at load with a message about a missing file.
- **Config and logs live in `%LOCALAPPDATA%\Lippy`** rather than under
  Application Support. Your dictation is still never written to disk on either
  platform.
- **There is no hotkey yet.** When there is, it will not default to Right Alt.
  On international layouts that key is AltGr and produces characters, so a
  default that is inert on a US and a European layout is the one worth picking.

## Use

Two ways to capture:

- **Hold Right Option**, speak, release. Push-to-talk, for a sentence or two.
- **Right Option + Right Shift** latches: recording continues with no key held,
  until you press Right Option again. For dictation too long to hold a key for.

Pressing the chord necessarily involves pressing the primary key, so press order
is handled explicitly. With Shift already down, capture starts latched. Adding
Shift *mid-hold* promotes the recording already in progress and keeps the audio
so far, so either order works, and you can decide a sentence in that this one is
going long.

- Holding under 0.3s is a fumbled keypress and is ignored. This does not apply to
  a latched session, which is deliberate however briefly it ran.
- Pressing any other key mid-hold cancels, because that was a chord, not dictation. A
  latched session is not cancelled this way, because you may well type during one.
- A latched session stops itself after 5 minutes, so a forgotten one does not
  record the room for hours. The HUD says "latched" the whole time.
- Both keys are rebindable in the menu, and the same key cannot be bound to both
  (a press would be ambiguous). Check your own machine before rebinding: other
  apps commonly claim Fn and a Right Control double-tap.
- The menu bar toggles **Polished** vs **Verbatim** (rules only, ~200ms, no LLM),
  and rebinds the key (Right/Left Option, Right Command, Right Control, Right
  Shift, Fn).
- **Copy Last Transcript** recovers text if a paste went somewhere unexpected.
- **Start at Login** registers the app as a login item (`SMAppService`), so
  dictation is available the moment you log in. The daemon already starts on its
  own (it is a LaunchAgent with `RunAtLoad`) so this is the missing half. The
  system is the source of truth, not a preference: if you switch it off in
  System Settings the menu says so rather than showing a tick for something that
  is disabled.
- **Mute Other Audio While Dictating** (off by default) silences the default
  output device for the duration of the recording and restores it afterwards.
  Music, a video or a call playing through speakers bleeds into the microphone
  and contaminates the transcript. It is opt-in because muting the machine is a
  side effect that should be asked for. Sometimes you dictate a note while
  deliberately listening to something. If the output was already muted, it is
  left alone rather than helpfully unmuted afterwards. Devices with no mute
  control (HDMI, some external DACs) fall back to zeroing per-channel volume.

### When there is nowhere to paste

Dictating into a window with no editable field would otherwise lose the text
silently: ⌘V goes to whatever has focus, and if that is a file list or a web
page, nothing happens and the words are gone. Two guards:

- **At capture start**, if nothing editable has focus, the HUD dot turns amber
  and reads "⚠︎ no text field focused". Better to learn that before speaking for
  a minute than after.
- **At delivery**, the check runs again (focus can move while you talk) and if
  there is still no target the text is held and a panel appears with a **Copy**
  button. It stays until dismissed, because the case it exists for is finishing a
  long dictation and only then noticing, which is exactly when you may have
  looked away.

Detection uses the accessibility tree, and it deliberately **fails open**: only a
confident "not editable" holds text back. Browsers and Electron apps under-report
editability, and wrongly withholding text from a field that would have taken it
is worse than a paste that lands somewhere harmless. An unreadable answer counts
as no answer, not as a no. Either way the transcript stays in **Copy Last
Transcript** and in `lippyctl last`.

Electron apps need one step before any of that works. They leave the
accessibility tree unbuilt until something asks for it, so an Electron window
reports nothing focused while the cursor is blinking in a text box. At the start
of each capture Lippy asks the frontmost app to publish its tree, using the
`AXManualAccessibility` attribute Electron documents for assistive apps, which is
the same switch a screen reader flips. Apps that do not recognise the request
ignore it. The tree is built asynchronously, which is why the request goes out
when the key goes down rather than when it comes up.

## Configuration

`~/Library/Application Support/Lippy/config.json`, written on first run.

```json
{
  "asr_backend": "parakeet",
  "cleanup_level": "polish",
  "polish_model": "mlx-community/Qwen3-4B-Instruct-2507-4bit",
  "aggressive_fillers": false,
  "dictionary": { "nice f": "NYSCEF" },
  "protected_terms": ["Lex Cloak", "Monty Home"],
  "fuzzy_threshold": 0.80,
  "spoken_urls": true,
  "spoken_numbers": true,
  "number_word_max": 12,
  "digit_triggers": ["/session start", "/session end"]
}
```

- **`dictionary`**: proper nouns the ASR has never seen, one exact spelling at a
  time. Case-insensitive, word-boundary matched. The place for a mis-hearing you
  have already seen and can name.
- **`protected_terms`**: the same problem when you cannot name the mis-hearing.
  An invented name comes back differently every time it is spoken, so listing
  every variant is a losing race. Write the name once in the form you want and
  anything close enough is snapped onto it. *Lexiclook*, *lex clock*, *lexi
  cloak* and *legs cloak* all become **Lex Cloak**.

  Safety here is a property of the term, not of the setting. A distinctive name
  collides with almost nothing. A short one that looks like ordinary English
  collides with a great deal, and no threshold repairs it. Measure before you
  trust it:

  ```
  lippyctl terms --check "Lex Cloak"
  ```

  Against the 235,976 word system dictionary, `Lex Cloak` captures two real
  words and `Paddle` captures sixteen. The second is a dictionary entry, not a
  protected term.

  A window is only scored when its length is within 25% of the term's, which is
  what stops an ordinary short word being rewritten. *cloak* scores 0.75 against
  *lexcloak* and never reaches the threshold, because it is rejected on length
  first. Tightening the threshold could not have done this.
  Cost scales with utterance length times the number of terms, because every
  window is compared against every term. Measured with three terms: 0.27ms on a
  60 character utterance, 0.66ms on a 180 character one, and 3.65ms on an
  atypical 820 character block. The deterministic pass stays under a millisecond
  for anything dictated in one breath.
- **`fuzzy_threshold`**: how close is close enough, 0 to 1, default `0.80`.
  Host names are matched 0.10 looser, because nothing inside an address is
  ordinary English and the prose bar costs real corrections there.
- **`spoken_urls`**: on by default. Joins a dictated address into a written one,
  so *"lex cloak dot app"* arrives as `lexcloak.app` rather than as four words.
  A protected term is absorbed into the host, which is what lets a two word name
  become one label. Only a known suffix counts, and a closed class word before
  the *dot* is refused, so *"the dot com bubble"* is left alone.

  Addresses are also held out of every other rule. Before this, a `dictionary`
  key matching a host rewrote it into the display form and turned a correctly
  heard `lexcloak.com` into `Lex Cloak.com`, because a full stop satisfies a
  word boundary.

  **What this cannot fix:** when the speech model drops the dot entirely, as it
  does with *"lexcloak.app"* about half the time, the words that survive are
  *"Lex Cloak app"*, which is also an ordinary English phrase for the
  application itself. Nothing downstream can tell those apart, so nothing tries.
- **`spoken_numbers`**: off by default, because this is a policy rather than a
  correction and a wrongly written number still reads as a number. The speech
  model already writes digits when the words around them make the purpose
  obvious, but that inference is fragile. Changing one word of context was
  enough to lose it:

  ```
  "session start ten fifty one"  ->  "Session start 1051"
  "session end ten fifty one"    ->  "Session and ten fifty one"
  ```

  A run of number words is read three ways, decided entirely by what surrounds it.

  | Reading | When | Result |
  |---|---|---|
  | identifier | after a `digit_triggers` phrase, alone in the utterance, or read out in pieces | `ten fifty one` becomes `1051` |
  | clock | a preposition in front, or a meridiem behind | `at nine thirty` becomes `9:30` |
  | quantity | everything else | `twenty three` becomes `23`, `three` stays `three` |

  "Read out in pieces" is the part that makes this survive a misheard trigger.
  `ten fifty one` is two numbers side by side, which is not how any English
  sentence counts, so it is treated as an identifier wherever it appears.

  A trigger outranks the clock, which is the ambiguity worth knowing about:
  `ten fifteen` is both a valid time and a valid session number, so
  `/session start ten fifteen` is `1015` while `meet at ten fifteen` is `10:15`.
  With no cue either way a bare `ten fifteen` is `1015`, on the grounds that the
  number said many times a day should not have to fight the clock for it.
- **`number_word_max`**: the largest quantity still spelled out, default `12`.
  Above it becomes digits. The carve-out usefully covers exactly the words that
  are most dangerous to touch, since *one*, *two* and the rest of the small ones
  appear constantly in prose meaning something other than a count.
- **`digit_triggers`**: phrases after which a number is an identifier rather than
  a quantity or a time. Runs after the dictionary, so a trigger the dictionary
  just repaired still counts.
- **`aggressive_fillers`**: off by default. On, it also strips *like*, *you know*,
  *I mean*, *basically*, *actually*. These are content often enough that removing
  them is an edit, not a cleanup, which is why you have to ask for it.
- **`cleanup_level`**: how hard to work. Measured on the same 10s recording:

  | Level | Round trip | What it does |
  |---|---|---|
  | `raw` | 190ms | exactly what the speech model heard |
  | `fillers` | 210ms | drops um / uh / er |
  | `clean` | 221ms | + stutters, false starts, punctuation, capitals, dictionary, terms, addresses, numbers |
  | `polish` | 1057ms | + an LLM pass over the result |

  Only `polish` needs a language model. Everything below it is deterministic
  regex, sub-millisecond, and it ports to any platform as plain logic. On that
  sample the LLM's whole contribution was joining one sentence fragment, which
  is why `clean` is the right default anywhere a 4B model is a stretch.

Restart the daemon after editing: `launchctl kickstart -k gui/$(id -u)/com.cscmsg.lippy.lippyd`

## Privacy

- The socket is `0600` in your Application Support directory. No TCP port is opened.
- Audio is never written to disk. It goes from the mic through memory to the model.
- Transcripts are held in daemon **memory** only (last 20, for `lippyctl last`).
  Nothing is logged to disk on purpose: dictation is the most sensitive text on
  the machine, and a plaintext log of it is a liability a local-first tool has no
  reason to create.
- The microphone opens on key-down and closes on release, so the macOS mic
  indicator reflects reality rather than being lit all day.

## Debugging

```
make logs                                  # tail the daemon log
make status                                # health + loaded models
$VENV/bin/python daemon/lippyctl.py file x.wav        # full pipeline on a file
$VENV/bin/python daemon/lippyctl.py file x.wav --raw  # rules only, no LLM
$VENV/bin/python daemon/lippyctl.py last              # recent utterances
$VENV/bin/python daemon/lippyctl.py terms             # audit protected terms
```

`lippyctl file` prints raw ASR and final text separately, plus which guard fired
if the polish pass fell back. That separation is usually enough to tell whether
a bad result came from mishearing or from over-editing.

`lippyctl terms` needs no daemon. It reports how many real words each protected
term would rewrite, and says so plainly where the platform has no word list to
check against rather than reporting a clean bill of health it cannot support.

## Releasing

Tag a version and the workflow builds, signs, notarises, staples and publishes:

```
git tag v0.9.0 && git push origin v0.9.0
```

That needs six repository secrets. `./scripts/set-release-secrets.sh <owner/repo>`
sets them: values go from your keychain export and password manager straight
into `gh`, piped rather than passed as arguments, so they never reach your shell
history or the process list. Without them the workflow still builds, but ad-hoc
signs, Gatekeeper rejects the result, so it is not distributable.

For local builds, `make_app.sh` will not guess between multiple signing
identities: order in `security find-identity` is not meaningful, and picking the
first one silently signed a release here with a legacy certificate. Record your
choice once in a gitignored file:

```
echo <identity-hash> > .signing-identity
```

## Tests

```
make test
```

104 tests, all offline. Ten of them need numpy and skip without it, which is
how the lean test job stays lean. The interesting ones are negative: that the stutter
collapser leaves `"I had had enough"` alone, that the filler stripper does not
eat `"like"` or `"actually"`, and that each guardrail rejects the specific way a
model has actually been observed to ruin a dictated message.

## Not built

Scoped out of v1 deliberately:

- **Per-app tone**: the frontmost app name is already captured and passed to the
  daemon as `app_hint`, and nothing varies the prompt on it yet.
- **Learned dictionary**: corrections are hand-added to `config.json`.

## Declined

**A system-wide mobile version**, iOS or Android, decided against on
2026-08-26 after a feasibility study. Not on effort: the speech recognition
ports cleanly and the deterministic cleanup is a few hundred lines of regex.

The blocker is a platform rule, not a missing library. **Mobile operating
systems forbid one app from putting text into another app's field.** The input
method is the only sanctioned bridge, which is why the sole one-tap path on
Android is to ship a full keyboard, and why iOS charges a switch away and back
on every use. A minimal input method that is only a microphone button collapses
back into that same friction.

This tool exists on macOS precisely because desktop platforms grant what mobile
withholds. Accessibility on macOS, and SendInput on Windows, are sanctioned
cross-app text injection. That affordance was deliberately removed from phones.

**What this does not decline:** dictation inside an application you control.
When the destination field belongs to your own app there is no cross-app
boundary to cross, so none of the above applies. The cleanup rules and the
guards in this repository are plain logic and port to any platform. They are
the reusable part.

**Live transcription preview** (words appearing in the HUD as you speak), built
in v0.3.0, removed in v0.4.0 after use. It was not a rendering problem that
could be polished away.

A streaming decoder continuously **revises its own hypothesis**, so displayed
text rewrites itself mid-sentence: you cannot tell what is settled from what is
still moving. Same mechanism that made a partial read *"So why I think"* where
the full pass read *"So I think"*. Making it flow like subtitles would require
holding back unstable text, which adds latency to a preview whose whole value
was immediacy. It is also unreadable while speaking, which is the only time it
is on screen.

**Command mode** (select text, hold the key, say *"make this shorter"*), decided
against, not deferred. Rewording is something better done deliberately, by hand
or with a tool suited to it.

Worth recording because it is not a small omission and will look like one: paid
dictation tools treat it as a headline feature, and the pieces to build it are
mostly already here. It also cuts against this tool's design. Every guardrail in `polish.py`
enforces *never act on the content, never change meaning*, which is why text can
appear at your cursor without proofreading. Command mode requires the opposite
("make this concise" is a summarisation request that trips the length guard by
design), so it would need a second pipeline with much weaker guards, and it
overwrites text you already wrote rather than filling an empty cursor.

## Verified

- `parakeet-mlx` 0.5.2 (PyPI, 2026-06-05), Apache-2.0
- `mlx-lm` 0.31.3 (PyPI, 2026-04-22)
- `mlx-community/parakeet-tdt-0.6b-v3`, CC-BY-4.0, 2.51 GB, 25 languages
- Built and run on macOS 26.5.1, Xcode 26.6, M4 Pro / 64 GB, 2026-08-24
- `sherpa-onnx` 1.13.6 and `onnxruntime-genai` 0.15.2, cp312 win_amd64 wheels
  confirmed on PyPI, 2026-08-26
- `sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8`, 487 MB compressed, from the
  k2-fsa/sherpa-onnx `asr-models` release, 2026-08-26
- `windows-2025-vs2026` confirmed a current runner label against the
  actions/runner-images README, 2026-08-26
