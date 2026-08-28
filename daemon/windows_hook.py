"""The low level keyboard hook, and the watchdog for it vanishing.

Step 2b of `docs/plans/windows-client.md`. The hook itself cannot be tested on a
runner, so this file is arranged to make that surface as small as it can be:
everything that decides anything is a plain function or a plain class at the top,
and the part that talks to Win32 is at the bottom and contains no decisions.

Three threads, and this file owns two of them.

  * **The hook thread** installs `WH_KEYBOARD_LL` and runs its own message loop,
    which `SetWindowsHookEx` requires. Its callback timestamps the event, stores
    that timestamp, pushes the raw fields onto a queue, calls `CallNextHookEx`
    and returns. It does nothing else, ever, and in particular it does not
    translate, does not log, and does not touch a file.
  * **The worker** drains the queue, translates, and drives the state machine.
    Consequences hang off the actions it emits.

The reason for that division is the first trap in the plan. A callback that
overruns `LowLevelHooksTimeout` is removed by Windows silently, with no call, no
error and no way to ask whether it is still installed. So the callback stays
trivial, and because loss cannot be detected by asking, a watchdog reinstalls the
hook when events stop arriving.

**Silence is not proof of loss.** A user who is not typing produces exactly the
same evidence as a hook that Windows removed. The watchdog reinstalls anyway,
because reinstalling costs almost nothing and being deaf costs the whole
feature, but the log says which one it knows and which one it is guessing.
"""

from __future__ import annotations

import ctypes
import logging
import queue
import sys
import threading
import time
from ctypes import wintypes

from hotkey_state import Action, EventKind, HotkeyState, KeyEvent

log = logging.getLogger("lippy.hook")

# ---- Win32 constants ------------------------------------------------------

WH_KEYBOARD_LL = 13
HC_ACTION = 0

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
WM_TIMER = 0x0113

# The SYS variants are not an edge case to skip. They are what an Alt key sends,
# and Alt is bindable.
KIND_BY_MESSAGE = {
    WM_KEYDOWN: EventKind.DOWN,
    WM_SYSKEYDOWN: EventKind.DOWN,
    WM_KEYUP: EventKind.UP,
    WM_SYSKEYUP: EventKind.UP,
}

LLKHF_EXTENDED = 0x01
LLKHF_INJECTED = 0x10

VK_LCONTROL = 0xA2

# The scan code the plan recorded for the Left Control that Windows synthesises
# alongside Right Alt on layouts where Right Alt is AltGr. Real Left Control
# reports 0x1D.
#
# This one constant is the least trustworthy line in the file. The behaviour is
# undocumented, it was read from a Keyman pull request rather than from
# Microsoft, and that code was reading the `lParam` encoding of an ordinary key
# message rather than the `scanCode` field of a hook structure, which are not
# obliged to agree. It is implemented as scoped and it is logged on every drop,
# and `--diagnose` prints the raw fields precisely so that AltGr can be
# characterised on a real keyboard and this number corrected.
#
# The blast radius is small while it is wrong in either direction. Right Control
# is the default primary and is untouched by AltGr, and Left Control does not
# abort a hold because it is a modifier. This matters only to somebody who binds
# Left Control deliberately.
ALTGR_LCONTROL_SCAN = 0x21D

# Our own paste chord, tagged so the hook can recognise it coming back.
#
# `SendInput` in 2d sets this on `dwExtraInfo` and the hook drops it here.
# Without the tag the only way to ignore our own synthetic Control plus V would
# be to drop every injected event, which would also mute the on-screen keyboard,
# remote sessions and assistive tools. Their input is real input as far as this
# is concerned, so it is kept.
SYNTHETIC_TAG = 0x4C495050  # "LIPP"


# ---- the structure, which is easy to get subtly wrong ---------------------

# Widths stated outright rather than borrowed, for two different reasons.
#
# `dwExtraInfo` is a `ULONG_PTR` and has to be pointer-sized, because declaring
# it 32 bits wide builds and runs perfectly on Win64 while reading half of the
# tag above and half of nothing.
#
# The four `DWORD`s are `c_uint32` rather than `wintypes.DWORD` because
# `wintypes.DWORD` is `c_ulong`, which is four bytes on Windows and eight
# everywhere else. Borrowing it would leave the structure correct on Windows and
# wrong on every machine the tests run on, so a test of the layout would be
# asserting something other than what ships. Stating the width makes the layout
# identical on all three platforms, which is what makes it testable at all.
ULONG_PTR = ctypes.c_size_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
LRESULT = ctypes.c_ssize_t


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", ctypes.c_uint32),
        ("scanCode", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ULONG_PTR),
    ]


# ---- the decisions, none of which need Windows ----------------------------

def translate(
    message: int,
    vk: int,
    scan_code: int,
    flags: int,
    extra_info: int,
    timestamp: float,
) -> KeyEvent | None:
    """One raw hook record to one `KeyEvent`, or None to ignore it.

    Runs on the worker rather than in the callback, which is the whole reason
    the callback pushes raw fields instead of events.

    Returns None for three things: a message this does not handle, our own
    synthetic paste chord coming back through the hook, and the Left Control
    that AltGr generates on international layouts.
    """
    kind = KIND_BY_MESSAGE.get(message)
    if kind is None:
        return None
    if extra_info == SYNTHETIC_TAG:
        log.debug("ignoring our own synthetic input, vk=0x%02X", vk)
        return None
    if vk == VK_LCONTROL and scan_code == ALTGR_LCONTROL_SCAN:
        log.debug("ignoring the Left Control that AltGr synthesised, scan=0x%X", scan_code)
        return None
    return KeyEvent(kind=kind, vk=vk, scan_code=scan_code, timestamp=timestamp)


class Watchdog:
    """Reinstalls the hook when it stops hearing anything.

    Holds no handle and installs nothing. It answers one question, which is
    whether enough quiet has passed to be worth acting on, and it counts how
    many times it has said yes since it last heard a real event so that the
    caller can log the first reinstall differently from the two hundredth on an
    idle machine.
    """

    def __init__(self, silence: float = 30.0) -> None:
        if isinstance(silence, bool) or not isinstance(silence, (int, float)):
            raise TypeError(f"silence must be a number of seconds, got {silence!r}")
        if silence <= 0:
            raise ValueError(f"silence must be greater than 0, got {silence!r}")
        self.silence = float(silence)
        self._last_activity = 0.0
        self._reinstalls_since_event = 0

    @property
    def last_activity(self) -> float:
        return self._last_activity

    def note_installed(self, now: float) -> None:
        """The clock starts at installation, not at the first key."""
        self._last_activity = float(now)
        self._reinstalls_since_event = 0

    def note_activity(self, now: float) -> None:
        """A real hook event arrived, which is the only proof the hook is alive."""
        self._last_activity = float(now)
        self._reinstalls_since_event = 0

    def quiet_for(self, now: float) -> float:
        return float(now) - self._last_activity

    def due(self, now: float) -> bool:
        return self.quiet_for(now) >= self.silence

    def note_reinstalled(self, now: float) -> None:
        """Reinstalling restarts the clock without counting as proof of life.

        The counter is what keeps an idle machine from writing the same alarming
        line every thirty seconds all night.
        """
        self._last_activity = float(now)
        self._reinstalls_since_event += 1

    @property
    def reinstalls_since_event(self) -> int:
        """0 when the next reinstall would be the first since real activity,
        which is the one worth an INFO line. Higher means an idle machine."""
        return self._reinstalls_since_event


class Dispatcher:
    """Drains raw hook records into the state machine and hands out actions.

    The clock is an argument so that the latch cap can be tested without waiting
    five minutes for it, the same way the state machine takes its time from the
    event.
    """

    def __init__(
        self,
        machine: HotkeyState,
        events: queue.SimpleQueue,
        on_action,
        clock=time.monotonic,
        on_raw=None,
    ) -> None:
        self.machine = machine
        self.events = events
        self.on_action = on_action
        self.clock = clock
        # `--diagnose --raw` uses this to print records that translate throws
        # away, which is the only way to see what AltGr actually sends.
        self.on_raw = on_raw

    def pump(self, timeout: float = 0.1) -> int:
        """Wait briefly for one record, drain any others already waiting, tick.

        Returns the number of actions emitted, which is what the diagnostic
        counts and what a test can assert on.
        """
        emitted = 0
        try:
            raw = self.events.get(timeout=timeout)
        except queue.Empty:
            raw = None

        while raw is not None:
            emitted += self._handle(raw)
            try:
                raw = self.events.get_nowait()
            except queue.Empty:
                raw = None

        # The cap is the only thing that fires without a key event, so it needs
        # a pump that runs even when nothing arrived.
        for action in self.machine.tick(self.clock()):
            self.on_action(action)
            emitted += 1
        return emitted

    def _handle(self, raw) -> int:
        if self.on_raw is not None:
            self.on_raw(*raw)
        event = translate(*raw)
        if event is None:
            return 0
        emitted = 0
        for action in self.machine.handle(event):
            self.on_action(action)
            emitted += 1
        return emitted


# ---- the part that only runs on Windows -----------------------------------

def _user32():
    """Load user32 with argument types declared, or say why it cannot be.

    Everything above this line imports and runs anywhere. Everything below it
    needs a Windows Python, because `WinDLL` and `WINFUNCTYPE` do not exist
    elsewhere and the tests would import a module that raises at load.
    """
    if sys.platform != "win32":
        raise RuntimeError(
            "the keyboard hook needs Windows. Everything it decides is in "
            "translate, Watchdog and Dispatcher, which do not.")

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, ctypes.c_void_p, wintypes.HINSTANCE, wintypes.DWORD]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, WPARAM, LPARAM]
    user32.CallNextHookEx.restype = LRESULT
    # GetMessageW returns -1 on error, which a BOOL restype would hide.
    user32.GetMessageW.argtypes = [
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
    user32.GetMessageW.restype = ctypes.c_int
    user32.PostThreadMessageW.argtypes = [
        wintypes.DWORD, wintypes.UINT, WPARAM, LPARAM]
    user32.PostThreadMessageW.restype = wintypes.BOOL
    user32.SetTimer.argtypes = [
        wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
    user32.SetTimer.restype = ctypes.c_size_t
    user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
    user32.KillTimer.restype = wintypes.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    return user32, kernel32


class KeyboardHook:
    """The hook, its thread, its message loop and its watchdog.

    Nothing here decides anything. If a rule about what a key means turns up in
    this class it is in the wrong file, and `hotkey_state` is the right one.
    """

    def __init__(self, events: queue.SimpleQueue, watchdog: Watchdog | None = None,
                 timer_ms: int = 1000) -> None:
        self.events = events
        self.watchdog = watchdog or Watchdog()
        self.timer_ms = timer_ms
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._hook = None
        self._timer = 0
        # The ctypes trampoline is held here on purpose. Letting it be collected
        # leaves Windows calling into freed memory on the next keystroke, which
        # is a crash rather than a bug report.
        self._proc = None
        self._installs = 0
        self._error: BaseException | None = None
        self._user32 = None
        self._kernel32 = None
        # Written by the callback and read by the watchdog tick. One float, so
        # the callback does a store rather than taking a lock on the clock
        # Windows is timing it against.
        self._last_event = 0.0

    # -- lifecycle ---------------------------------------------------------

    def start(self, timeout: float = 5.0) -> None:
        """Start the hook thread and wait until the hook is actually installed.

        Waiting matters. Returning before installation would let the caller
        report a working hotkey and then drop every key pressed in the next
        few milliseconds, which is the silent shape of failure this file exists
        to avoid.
        """
        if self._thread is not None:
            raise RuntimeError("already started")
        self._thread = threading.Thread(target=self._run, name="lippy-hook", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError(f"the hook thread did not install within {timeout}s")
        if self._error is not None:
            raise self._error

    def stop(self, timeout: float = 5.0) -> None:
        if self._thread is None:
            return
        user32 = self._user32 or _user32()[0]
        user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        self._thread.join(timeout)
        if self._thread.is_alive():
            log.warning("the hook thread did not stop within %.1fs", timeout)
        self._thread = None

    @property
    def installs(self) -> int:
        """How many times the hook has been installed, reinstalls included."""
        return self._installs

    # -- the thread --------------------------------------------------------

    def _run(self) -> None:
        try:
            user32, kernel32 = _user32()
            self._user32, self._kernel32 = user32, kernel32
            self._thread_id = kernel32.GetCurrentThreadId()
            self._proc = self._build_proc(user32)
            self._install(user32, kernel32)
            # A NULL window means WM_TIMER lands on this thread's queue, which
            # is what wakes GetMessageW up often enough to check the watchdog.
            self._timer = user32.SetTimer(None, 0, self.timer_ms, None)
            self._ready.set()
            self._loop(user32, kernel32)
        except BaseException as exc:  # reported through start(), never swallowed
            self._error = exc
            log.exception("the hook thread failed")
            self._ready.set()
        finally:
            self._teardown()

    def _build_proc(self, user32):
        call_next = user32.CallNextHookEx
        events = self.events
        monotonic = time.monotonic
        hook_self = self

        def _callback(n_code, w_param, l_param):
            # Everything in here is on the clock that Windows measures against
            # LowLevelHooksTimeout. One cast, one store, one queue push. No
            # translation, no logging, no allocation that can block, and no try
            # block, because SimpleQueue.put on an unbounded queue does not
            # fail and a handler here would be more code on the same clock.
            if n_code == HC_ACTION:
                info = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                now = monotonic()
                hook_self._last_event = now
                events.put((w_param, info.vkCode, info.scanCode, info.flags,
                            info.dwExtraInfo, now))
            return call_next(None, n_code, w_param, l_param)

        return ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)(_callback)

    def _install(self, user32, kernel32) -> None:
        module = kernel32.GetModuleHandleW(None)
        handle = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL, ctypes.cast(self._proc, ctypes.c_void_p), module, 0)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self._hook = handle
        self._installs += 1
        self._last_event = time.monotonic()
        self.watchdog.note_installed(self._last_event)

    def _loop(self, user32, kernel32) -> None:
        message = wintypes.MSG()
        while True:
            result = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
            if result == 0:          # WM_QUIT
                log.info("hook thread asked to stop")
                return
            if result == -1:
                raise ctypes.WinError(ctypes.get_last_error())
            if message.message == WM_TIMER:
                self._check(user32, kernel32)
                continue
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

    def _check(self, user32, kernel32) -> None:
        """The watchdog tick. Runs on the hook thread, because the hook belongs
        to it and a reinstall from anywhere else would install nothing useful."""
        now = time.monotonic()
        # The callback records when it last ran. Anything newer than what the
        # watchdog knows about is proof the hook is still installed.
        if self._last_event > self.watchdog.last_activity:
            self.watchdog.note_activity(self._last_event)
        if not self.watchdog.due(now):
            return
        quiet = self.watchdog.quiet_for(now)
        if self.watchdog.reinstalls_since_event == 0:
            log.info("no hook events for %.0fs, reinstalling. Either the hook was "
                     "removed without telling us or nobody is typing, and there "
                     "is no way to ask which", quiet)
        else:
            log.debug("still quiet after %.0fs, reinstalling again", quiet)
        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None
        self._install(user32, kernel32)
        self.watchdog.note_reinstalled(now)

    def _teardown(self) -> None:
        user32 = self._user32
        if user32 is None:
            return
        try:
            if self._timer:
                user32.KillTimer(None, self._timer)
                self._timer = 0
            if self._hook:
                user32.UnhookWindowsHookEx(self._hook)
                self._hook = None
        except Exception:
            log.exception("failed to tear the hook down cleanly")


# ---- the diagnostic -------------------------------------------------------

def describe(message: int, vk: int, scan_code: int, flags: int,
             extra_info: int, timestamp: float) -> str:
    """One raw hook record as a line a person can read.

    Printed by `--raw`, and the reason it exists: AltGr on a real keyboard is
    the only thing that can settle what ALTGR_LCONTROL_SCAN should be.
    """
    marks = []
    if flags & LLKHF_EXTENDED:
        marks.append("extended")
    if flags & LLKHF_INJECTED:
        marks.append("injected")
    if extra_info == SYNTHETIC_TAG:
        marks.append("ours")
    line = (f"raw message=0x{message:04X} vk=0x{vk:02X} scan=0x{scan_code:X} "
            f"flags=0x{flags:02X} extra=0x{extra_info:X}")
    if marks:
        line += "  " + " ".join(marks)
    print(line)
    return line


def main() -> int:
    """`python daemon/windows_hook.py` prints what the hook sees and decides.

    This is how 2b is verified at all, since no runner has an interactive
    desktop. Press the hotkey and watch the actions. Press AltGr and read the
    raw fields, which is what settles ALTGR_LCONTROL_SCAN one way or the other.
    """
    import argparse

    parser = argparse.ArgumentParser(description=main.__doc__.splitlines()[0])
    parser.add_argument("--raw", action="store_true",
                        help="print every hook record, not only the ones that mean something")
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="stop after this long (default: until interrupted)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.raw else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if sys.platform != "win32":
        print("this diagnostic needs Windows. The logic it exercises is tested "
              "everywhere, in tests/test_windows_hook.py", file=sys.stderr)
        return 2

    import config as config_mod

    cfg = config_mod.Config.load()
    primary, latch = cfg.hotkey_vks()
    machine = HotkeyState(primary, latch)
    events: queue.SimpleQueue = queue.SimpleQueue()

    def show(action: Action) -> None:
        print(f"  -> {action.value}")

    hook = KeyboardHook(events)
    dispatcher = Dispatcher(machine, events, show,
                            on_raw=describe if args.raw else None)
    hook.start()
    print(f"listening. hold {cfg.hotkey} to dictate, "
          f"{cfg.latch_key or 'nothing'} latches. Control plus C to stop.")
    started = time.monotonic()
    try:
        while not args.seconds or time.monotonic() - started < args.seconds:
            dispatcher.pump(0.1)
    except KeyboardInterrupt:
        print()
    finally:
        hook.stop()
    print(f"hook installed {hook.installs} time(s)")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
    raise SystemExit(main())
