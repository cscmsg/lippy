# Lippy on Windows, Part 1 of 2: the client

*Part 1 of 2. A work brief, not documentation of something that exists. Written
2026-08-25, before any Windows code.*

## Goal

Get Lippy running on Windows from source: hold a key, speak, release, and clean
text appears at the cursor. Ship at `cleanup_level: "clean"` so there is no
model download beyond ASR and no LLM dependency. Polish stays opt-in and is
Part 2's problem at the earliest.

**Windows is a single process.** The macOS daemon/app split exists only because
macOS binds TCC permissions to a signed `.app`, which a venv interpreter cannot
hold across rebuilds. Windows has no equivalent constraint, so there is no
socket, no `lippyd`, no `protocol.py`. One Python process owns the tray, the
hotkey, the microphone, the models and the paste. Do not port the daemon.

## Read first

- `daemon/rules.py`: the entire cleanup product below `polish`. Platform-neutral.
- `daemon/config.py`: `CLEANUP_LEVELS`, `rule_config()`, and the macOS-bound
  `SUPPORT_DIR` that needs a Windows branch (`%LOCALAPPDATA%`).
- `daemon/asr.py`: `SherpaBackend` is already written and verified on macOS.
- `daemon/polish.py`: `MlxEngine` / `OnnxEngine` split behind one `Polisher`.
- `app/Sources/Lippy/AppDelegate.swift`: the state machine to mirror: hold,
  latch, promote-mid-hold, abort-on-keypress, minimum-hold, latch cap.
- `app/Sources/Lippy/TextInjector.swift`: clipboard save, paste, restore. The
  approach ports. The API does not.
- `.github/workflows/release.yml`: the existing macOS release job, for the
  shape a Windows equivalent should take.

## Recent-state recon (read before Phase 1)

**Streaming was deliberately removed, and sherpa-onnx offers it.**
Commit `7c61f0a` (v0.4.0) removed a live transcription preview after it was
used in earnest. The reason is in the README's *Declined* section: a streaming
decoder continuously revises its own hypothesis, so displayed text rewrites
itself mid-sentence and is unreadable while speaking. `sherpa-onnx` exposes an
attractive streaming API. **Do not re-add it.** This is Chesterton's Fence with
the note still nailed to it.

## Pre-flight (cross-cutting, touches 3 of 10 surfaces)

- **Runtime dependencies**: adds `sherpa-onnx`, `sounddevice`, a tray library
  and Win32 bindings. Note `llama-cpp-python` was ruled out: zero Windows
  wheels on PyPI.
- **CI/CD workflows**: a new Windows job. This is the only place the code can
  execute during development.
- **External integrations**: Microsoft Store is Part 2, but the package
  identity decision starts here.

**Environments that will run this code:** Windows on a GitHub Actions runner
(headless, no audio device, no interactive desktop), and the author's own
Windows laptop, which has real audio and a real keyboard. The interactive layer
is testable. Use it early rather than at the end.

**Rollback:** the Windows client is additive. Nothing in it can break the macOS
build if `asr.py` and `polish.py` backend selection stays keyed off the platform
rather than replaced.

## Phase 1, declare and wire the ONNX backends

**Landed.** Every item below is done.

- ~~Declare the ONNX backends.~~ **Done before this plan started.**
  `sherpa-onnx==1.13.6` and `onnxruntime-genai==0.15.2` are now pinned in
  `daemon/requirements.txt`. What remains is splitting the macOS-only MLX pins
  out so a Windows install does not try to build them.
- Make backend selection platform-aware in `asr.py`'s `build()` and
  `polish.py`'s `build_engine()`: default to `sherpa` / `onnx` off Darwin,
  `parakeet` / `mlx` on it. Keep both importable everywhere so the macOS tests
  still exercise the shared code.
- Give `config.py` a Windows support directory (`%LOCALAPPDATA%\Lippy`) without
  disturbing the macOS path, and keep the config migration path working.
- Add the ONNX model bootstrap: download and unpack
  `sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8` (640 MB) into a cache directory,
  resumable, with `LIPPY_ONNX_MODEL_DIR` as an override.

## Phase 2. The Windows shell

**Not started.** This is all that is left of Part 1.

- Tray icon with the same menu as macOS: the four cleanup levels, hotkey
  binding, latch modifier, Copy Last Transcript, open config, open log, quit.
- **Hotkey via a `WH_KEYBOARD_LL` low-level hook.** `RegisterHotKey` cannot
  bind a bare modifier, and bare-modifier hold is the entire interaction.
  Reproduce the full state machine from `AppDelegate.swift`: hold, latch chord,
  promote-mid-hold keeping audio already captured, abort on another keypress
  during a hold only, minimum-hold guard, latch cap.
- **Do not default to Right Alt.** On international layouts it is AltGr and
  produces characters. Pick a default that is inert on a US and a European
  layout, and make it rebindable.
- Audio capture via `sounddevice` at the device's native rate, resampled once on
  stop. Port the group-averaging decimation from `AudioRecorder.swift`, not
  naive sample-dropping: plain decimation aliases sibilants into the speech band.
- Paste via clipboard save, `SendInput` Ctrl+V, restore after a delay. Mirror
  `TextInjector.swift`'s reasoning, including why synthesized typing was
  rejected.
- **Port the separator decision.** Dictation arrives one utterance at a time, so
  without it a second sentence lands hard against the first
  ("Ship it Tuesday.The bug is fixed"). `Separator.needed(after:inserting:)` is
  pure and ports as-is, and `--selftest-separator` covers it. What does not port
  is the reading of the character before the cursor: macOS uses the
  accessibility tree, and Windows needs its own route (UI Automation
  `TextPattern`, or a fallback that declines to guess). Keep the fail-closed
  behaviour: when the app will not say what precedes the cursor, add nothing. A
  missing space is one keystroke to fix, a spurious leading space appears on the
  first dictation into every such app.
- Port the recovery panel: when no editable field has focus, hold the text and
  offer a copy button rather than pasting into nothing. Carry the fail-open rule
  with it, and `--selftest-destination` covers that rule the same way: an
  unreadable answer from the accessibility layer is *unknown*, never "not
  editable". Reading an error as a confident no is what made the macOS build
  refuse to paste into windows that would have taken the text.
- Keep both models resident for the life of the process. There is no daemon to
  hold them, so the tray app is the warm process.

## Phase 3, CI

**Landed**, with one caveat: the Windows job has never run. It was written
on a Mac and its first execution will be the pull request that adds it.

- A Windows job on `windows-2025-vs2026` that installs the requirements, runs
  the full test suite, and exercises a file-through-the-pipeline smoke test
  (the equivalent of `--selftest`) with a committed WAV fixture.
- The suite must pass on both platforms. `rules.py` and the polish guards are
  the shared contract and must not fork.

## Testing requirements

Every new or modified module gets tests in the same phase as the code, meeting
all six criteria: strict assertions (no `assertTrue`-shaped checks), no logic
mirroring (hard-coded golden data), at least two sad paths per happy path,
boundary and type stress, side-effect verification, and mock integrity
including malformed responses.

Put Windows tests in `tests/test_windows_*.py`, not appended to existing files.

**What cannot be tested in CI, and must be labelled as such:** the hotkey hook,
the tray, the paste, and real audio capture. A headless runner has no
interactive desktop. Write these so the logic is unit-testable with the Win32
calls behind a seam, and state plainly in the PR which paths have only ever run
on a developer's assertion.

## Deliverables

1. Lippy runs on Windows from source and inserts text at the cursor.
2. `requirements-windows.txt` with verified pins. The undeclared-dependency bug
   is closed on both platforms.
3. Platform-aware backend selection with the macOS path unchanged and its tests
   still green.
4. A Windows CI job that runs the suite and a pipeline smoke test.
5. A README section covering Windows install from source, including the hotkey
   default and why it differs from macOS.

## Constraints

- Do not port the daemon, the socket, or `protocol.py`.
- Do not re-add streaming (see recon above).
- Do not fork `rules.py` or the polish guards. If Windows needs different
  behaviour, it needs a parameter, not a copy.
- Do not touch the macOS app target.
- Write the logging first, not after the first mystery. The macOS build only
  became debuggable once the app wrote its own log to disk, and every one of its
  early failures was silent: a converter that returned zero frames without
  erroring, an entitlement refused before the permission system was consulted,
  an exception swallowed by the UI framework. Assume the Windows equivalents
  exist and instrument for them from the first commit.
