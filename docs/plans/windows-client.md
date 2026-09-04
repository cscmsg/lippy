# Lippy on Windows, Part 1 of 2: the client

*A work brief, not documentation of something that exists. Originally written
2026-08-25 covering three phases. Phases 1 and 3 shipped on 2026-08-26, and step
2a of the shell shipped on 2026-08-27. Their sections have been removed, because
a plan describing something that already ships has outlived its purpose. What is
left here is the part of the Windows shell that touches Windows. Re-scoped
2026-08-27.*

## Already landed, so you can skip it

The shared layer and the CI job are done, in cscmsg/lippy#3 and #4. Backend
selection follows the platform, `%LOCALAPPDATA%\Lippy` is the Windows support
directory, `daemon/models.py` fetches and unpacks the ONNX speech model
resumably, `requirements-windows.txt` installs without touching an Apple-only
wheel, and a `windows-2025-vs2026` job runs the suite plus one recording through
the pipeline on every pull request. The README's Windows section covers install
from source. **The pipeline works on Windows today.** What it does not have is a
way to start it with your voice or put the result anywhere.

The pure layer is done too, in cscmsg/lippy#6, which was step 2a. Three modules
and 110 tests, all of which run on the lean job as well as the Windows one:

- `daemon/hotkey_state.py` is the state machine, the two guards included. Feed
  it `KeyEvent`s and it returns `Action`s. Call `tick(now)` from the drain loop,
  because that is what enforces the latch cap.
- `daemon/audio.py` is `resample`, both branches, standard library only.
- `daemon/separator.py` is `needed(after, inserting)` and `prepare`, carrying the
  sixteen cases from `Separator.runSelfTest` as golden data.

One rule was decided there rather than here, so do not re-litigate it in the
adapter: **modifiers do not abort a hold, struck keys do.** On macOS that falls
out of the event types. A low level hook sees everything, so it had to be said
out loud. It is the `chord_exempt_vks` parameter.

`onnxruntime` is pinned at 1.29.0 in the same pull request, which closes the
last item this plan carried against shipping a build. Note for anyone reading
the old wording: it arrives through `onnxruntime-genai`, not `sherpa-onnx`,
whose wheel carries its own runtime.

The hook adapter is written, in cscmsg/lippy#7, and **it has now run**, on a
real Windows laptop on 2026-08-28. `daemon/windows_hook.py` holds the hook
thread, the queue-push callback, the watchdog and the drain loop, and
`config.py` carries `hotkey` and `latch_key` as names resolved through
`hotkey_state.KEYS`.

What the run established, so that 2c can be built on it:

- The hook installs and stays installed. Right Control produced `begin_hold`
  and `end`, the 0.30s guard rejected a 0.14s tap, and a struck key aborted a
  hold.
- Nothing is swallowed. A sentence typed into Notepad with the diagnostic
  running arrived in full, and Control plus C reached the console underneath.
- The watchdog reinstalled once after 30s of silence and then stayed quiet.
- `ALTGR_LCONTROL_SCAN` is **correct**, which trap 3 now records with the
  measurement rather than the doubt.

It also turned up one defect the tests could not have caught, because three of
them shared the mistake. Key repeat was being read as a second press, so a
latched session ended and restarted at the repeat rate for as long as the key
stayed down. Fixed in `hotkey_state.py`, along with the missing-release wedge
that the fix would otherwise have introduced. **The latch path is the one thing
the run did not re-exercise afterwards**, so the first minute of the next
session at a Windows keyboard belongs to it.

## Goal

Hold a key, speak, release, and cleaned text appears at the cursor, on Windows.
One process. Ship at `cleanup_level: "clean"`, which is already the default off
Darwin, so no language model and no second download.

**Windows is a single process.** The macOS daemon and app split exists only
because macOS binds TCC permissions to a signed `.app`, which a venv interpreter
cannot hold across rebuilds. Windows has no equivalent constraint, so there is no
socket, no `lippyd`, and no `protocol.py`. Do not port the daemon.

## Read first

Four macOS files and one Python one, and what each is for. The reasoning ports
even where no line of the code does.

- `daemon/hotkey_state.py`: the state machine, already ported and already
  tested. The adapter's whole job is to feed it. Read it before writing the
  hook, and read `HotkeyMonitor.swift` alongside it only if you want the
  original, which is where its comments point.
- `app/Sources/Lippy/AudioRecorder.swift`: capture at the hardware rate and
  convert once on stop. The conversion itself is now `daemon/audio.py`, so what
  is left to take from here is the tap arrangement and the buffer counting in
  `stop`, which exists because of the silent failure recorded in trap 6.
- `app/Sources/Lippy/TextInjector.swift`: clipboard save, paste, restore, and the
  250ms delay before restoring. Also why synthesised typing was rejected.
- `app/Sources/Lippy/Separator.swift`: `characterBeforeCursor()`, which is the
  half that did not port. The decision it feeds is `daemon/separator.py`.
- `app/Sources/Lippy/TextDestination.swift`: the three-way `Availability` answer
  and the rule that only a confident "no" holds text back.

## The shape: three threads and one seam

This is the decision the rest of the plan depends on, and it is forced by the
first trap below rather than chosen for elegance.

- **Main thread.** Tray icon, its menu, and a message loop. Owns nothing
  time-critical.
- **Hook thread, dedicated.** `SetWindowsHookEx(WH_KEYBOARD_LL, ...)` and its own
  message loop. The callback timestamps the event, pushes it onto a queue, calls
  `CallNextHookEx`, and returns. It does nothing else, ever.
- **Worker thread.** Drains the queue, drives the state machine, and owns every
  consequence: starting and stopping capture, running ASR, reading focus, and
  pasting.

**The seam is the state machine itself**, and it exists now. `HotkeyState`
consumes `KeyEvent(kind, vk, scan_code, timestamp)` and returns actions. It
imports nothing from Win32 and holds no handles, so the whole of the interaction
logic, both guards included, is already tested on macOS, on Linux, and in the
existing CI job. What remains is a thin adapter, and the measure of whether it
is thin enough is whether it contains a decision that could have been tested and
was not.

## Traps found while scoping

Six, and the first two are documented Windows behaviour rather than
speculation. Both are silent, which is the failure mode this repository already
knows it has a weakness for.

### 1. A slow hook is removed without telling you

From the `LowLevelKeyboardProc` documentation: the callback must finish inside
the `LowLevelHooksTimeout` value under `HKEY_CURRENT_USER\Control Panel\Desktop`,
and *"on Windows 7 and later, the hook is silently removed without being called.
There is no way for the application to know whether the hook is removed."* Since
Windows 10 version 1709 the system caps that timeout at 1000ms and commonly
defaults far lower.

So the hotkey can simply stop working, mid-session, with no exception, no log
line, and no error code, because something on the worker path once took too long
on a busy machine. Microsoft's own advice in the same document is the
architecture above: *"run the hooks on a dedicated thread that passes the work
off to a worker thread and then immediately returns."*

Two consequences. The callback does queue-push and nothing else, including no
logging call that touches a file. And because loss is undetectable by asking,
the app needs a **watchdog**: record the time of the last hook event, and if a
long interval passes with none, tear down and re-install the hook and write that
to the log. A user reporting "it just stopped working" must land in a log line
that already knows why.

The same document notes the thread that installs the hook must have a message
loop, which is why the hook thread has its own.

### 2. `GetAsyncKeyState` lies inside the callback

Same document: *"the callback function is called before the asynchronous state of
the key is updated. Consequently, the asynchronous state of the key cannot be
determined by calling GetAsyncKeyState from within the callback function."*

The latch modifier's up or down state therefore has to be tracked from the event
stream itself, which is exactly what `HotkeyMonitor` does with its own
`latchDown` field rather than querying the system. Do not be tempted to simplify
by asking Windows, because it will answer, and the answer will be one event
stale.

### 3. AltGr presses Left Control for you

The brief already said not to default to Right Alt, because on international
layouts it is AltGr and produces characters. Scoping turned up the other half:
on a European layout, pressing AltGr makes Windows generate a **simulated
`VK_LCONTROL` down alongside `VK_RMENU`**. It is distinguishable in a low level
hook by its scan code, `0x21D` rather than `0x1D`, but that behaviour is
undocumented and the scan code never reaches an ordinary `WM_KEYDOWN`. A default
that rests on an undocumented workaround is not a good default.

So **Right Alt and Left Control are both out**, and the default is **Right
Control**, with **Right Shift** as the latch, both rebindable. Right Control is
inert on its own on United States and European layouts, is present on
essentially every keyboard, and is untouched by AltGr. Note that the abort rule
then does the right thing for free, because Control plus C during a hold is a
chord and not dictation.

If a user rebinds the primary key to Left Control, the scan code check is the
only mitigation available. It belongs in the adapter rather than the state
machine, and it should be commented as undocumented behaviour so that a future
reader knows it can lapse without warning.

That check exists as `ALTGR_LCONTROL_SCAN` in `daemon/windows_hook.py`, and it
was the weakest line in this whole plan until 2026-08-28, when it was measured
on a real keyboard with a United Kingdom layout added. **The number is right.**
Holding AltGr produces, repeatedly:

```
raw message=0x0104 vk=0xA2 scan=0x21D flags=0x20 extra=0x0
raw message=0x0104 vk=0xA5 scan=0x38 flags=0x21 extra=0x0  extended
```

So Windows does put `0x200` into the hook's `scanCode` field for the synthetic
Left Control, even though the field and the `lParam` encoding had no obligation
to agree. Note that the synthetic event is **not** flagged extended and carries
`LLKHF_ALTDOWN` instead, which distinguishes it from both a real Left Control
(`scan=0x1D flags=0x00`) and a real Right Control (`scan=0x1D flags=0x01`).

The behaviour is still undocumented and can still lapse, so leave the comment
saying so. What has changed is that the number is now observed rather than
borrowed.

### 4. `SendInput` has no private modifier state

`TextInjector` creates its event source with `CGEventSource(stateID:
.privateState)` for a specific reason recorded in its comments: without it, a
Shift still physically held turns the synthetic Command plus V into Command plus
Shift plus V, which is paste-and-match-style in some apps and nothing at all in
others.

**Windows has no equivalent.** Synthetic input from `SendInput` merges with the
real physical key state. This is not hypothetical here, because in latched mode
the paste happens while the user may well be holding the primary key down, and
the latch modifier is Right Shift.

Before sending Control plus V, synthesise key-up for every modifier currently
recorded as down, send the paste, and then leave the physical state alone, since
the user's own key-up will arrive through the hook normally. Test it with the
hotkey deliberately held.

### 5. UI Automation can block for seconds

A cross-process UI Automation call against a busy or unresponsive application can
take a long time to return. The paste path must never wait on one. Give the focus
check and the character-before-cursor read a hard timeout, and treat expiry as
`unknown`, which is already the fail-open answer that `TextDestination` defines.
Never call UI Automation from the hook thread.

This preserves the existing rule rather than inventing one. Only a confident
"not editable" holds text back. An unreadable answer pastes, because wrongly
withholding text from a field that would have taken it is the worse failure, and
this module has already made that mistake once.

### 6. A capture that records nothing looks exactly like one that works

The resampler half of this trap is closed: `daemon/audio.py` has both branches
and the golden data, and `asr.load_wav`'s linear-at-every-ratio version stays
where it is, for files. Call `audio.resample` from the capture path and the
aliasing question is settled. What is left is the half that only bites on a real
microphone.

From the macOS history in `AudioRecorder.swift`: an earlier design converted
every buffer as it arrived and captured **nothing**, silently, because the
resampler needed several buffers before it could emit any. The Windows shape of
that failure is a `sounddevice` stream whose callback never fires or whose dtype
is wrong. Assert a non-zero frame count on every stop and log buffer counts the
way `AudioRecorder` already does.

## Dependencies, and two packages to avoid

Verified on PyPI 2026-08-26.

- **`keyboard` 0.13.5, last released 2020-03-23.** Six years stale. It also
  abstracts away the left and right distinction and the scan code that trap 3
  needs. Do not use it.
- **`pystray` 0.19.5, last released 2023-09-17.** Nearly three years stale, and a
  message loop already exists for the tray to live on.

Proposed additions, which is two:

- **`sounddevice` 0.5.6** (2026-08-17, actively maintained, pure Python over
  PortAudio) for capture. Already named in the original brief.
- **`comtypes` 1.4.16** (2026-03-02) for the narrow slice of UI Automation this
  needs. Preferred over `uiautomation` 2.0.29 (2025-08-05), which wraps far more
  than is wanted here and is the staler of the two.

Everything else is **`ctypes` against Win32 directly**, with no new dependency:
the hook, `SendInput`, the clipboard, and `Shell_NotifyIcon` for the tray. That
is deliberate. These are the parts needing exact control over flags and scan
codes, they are the parts a wrapper would hide, and Part 2 has to put all of this
inside an MSIX package where a smaller dependency surface is worth having.
`pywin32` 312 (2026-06-04) is current and is a reasonable fallback if the ctypes
tray proves tedious, but start without it.

## Work, in order

Ordered so each step is verifiable before the next one depends on it, and so the
riskiest logic is the part that needs no Windows at all. That part was 2a and it
has shipped, which is why the list now starts part way through. The lettering is
left alone rather than closed up, because the pull requests and the commits refer
to it.

Everything from here runs only on the laptop. None of it can be covered on a
runner, so each step below says what it owes the reader instead.

**2b-verify is done**, on 2026-08-28. What it found is above. One thing it did
not cover, because the latch was only exercised before the repeat fix and not
after: **hold Right Control, add Right Shift to latch, release both, then press
Right Control once to end it.** Expect `begin_latched`, then `promote` if you
came in from a hold, then a single `end`. Anything that ends and restarts while
a key is down means the guard has regressed. Do that first, then carry on.

**2c. Capture.** `sounddevice` at the device's native rate, converted once on
stop through the ported resampler, with the non-zero frame assertion.

**2d. Paste.** Clipboard save, set, `SendInput` Control plus V, restore after a
delay, and the modifier-clearing guard from trap 4. Verify with the hotkey held.

**2e. Focus and separator.** UI Automation behind a timeout, mapping onto the
existing three-way answer. Fail open.

**2f. Tray, menu, recovery panel, warm process.** The four cleanup levels, hotkey
binding, latch modifier, Copy Last Transcript, open config, open log, quit. Both
models stay resident for the life of the process, because there is no daemon to
hold them and the tray app is the warm process. When nothing editable has focus,
hold the text and offer a copy button rather than pasting into nothing.

## Testing requirements

Every new or modified module gets tests in the same phase as the code, meeting
all six criteria: strict assertions, no logic mirroring, at least two sad paths
per happy path, boundary and type stress, side-effect verification, and mock
integrity including malformed responses. Put them in `tests/test_windows_*.py`.

The state machine's own list is done and lives in `tests/test_windows_hotkey.py`.
Do not repeat those cases against the adapter. The adapter's tests are about
whether events reach the machine and whether actions leave it, which is a
different question from whether the machine is right.

**What cannot be tested in CI, and must be labelled as such in the pull request:**
the hook, the tray, the paste, real audio capture, and every UI Automation read.
A headless runner has no interactive desktop. State plainly which paths have only
ever run on a developer's assertion.

## Deliverables

1. Lippy runs on Windows and inserts dictated text at the cursor.
2. A hotkey that defaults to Right Control, is rebindable, and does not fire on
   AltGr.
3. A tray icon with the same menu as macOS.
4. README instructions covering the hotkey, the default, and how to change it.

Delivered already: the state machine as a platform-neutral, fully tested module.

## Constraints

- Do not port the daemon, the socket, or `protocol.py`.
- Do not re-add streaming. A decoder that revises its own hypothesis is
  unreadable while you speak, it was built and removed once already, and the
  reasoning is in the README's *Declined* section.
- Do not fork `rules.py` or the polish guards. If Windows needs different
  behaviour it needs a parameter, not a copy. The same now applies to
  `separator.py`, `audio.py` and `hotkey_state.py`, which is why the abort
  exemption and both guards are constructor arguments.
- Do not touch the macOS app target.
- Do not do work inside the hook callback. See trap 1.
- Do not put a decision in the adapter. If the hook, the tray or the paste path
  grows a rule about what a key means, it belongs in `hotkey_state.py` where it
  can be tested, and the adapter should be passing it a parameter instead.
- Write the logging first, not after the first mystery. Every early macOS failure
  was silent, and traps 1 and 6 say the Windows ones will be too.

## Verified

- `LowLevelKeyboardProc` timeout, silent removal, the message loop requirement,
  and the `GetAsyncKeyState` caveat: Microsoft Learn, retrieved 2026-08-26.
- AltGr generating a simulated `VK_LCONTROL` with scan code `0x21D`, and that
  distinction being undocumented and absent from `WM_KEYDOWN`: keymanapp pull
  request 14909, read directly 2026-08-26.
- Package versions and release dates: PyPI JSON API, 2026-08-26. `sounddevice`
  0.5.6, `comtypes` 1.4.16, `pywin32` 312, `uiautomation` 2.0.29, `pystray`
  0.19.5, `keyboard` 0.13.5.
- AltGr's synthetic Left Control arriving with `scanCode` `0x21D`, not extended,
  and carrying `LLKHF_ALTDOWN`: measured directly with
  `python daemon/windows_hook.py --raw` on Windows with a United Kingdom layout
  added, 2026-08-28. This supersedes the doubt the plan carried about the
  Keyman reading, and confirms it.
- `onnxruntime` 1.29.0 is the current release, published 2026-08-17, with cp312
  wheels for `win_amd64` and `macosx_14_0_arm64`: PyPI JSON API, 2026-08-27. That
  it reaches the tree through `onnxruntime-genai` rather than `sherpa-onnx` was
  read from the installed package metadata the same day.
