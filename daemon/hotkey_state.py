"""The push-to-talk state machine, with no platform underneath it.

Ported from `app/Sources/Lippy/HotkeyMonitor.swift`, which is the closest thing
this behaviour has to a specification, together with the two guards that live in
`AppDelegate` on macOS. Windows keeps all of it here for one reason: everything
in this file runs on any machine, and nothing that touches Win32 can run on a
runner at all. What cannot be tested is then a thin adapter with no decisions
left in it.

The class consumes key events and returns actions. It opens no device, holds no
handle, starts no thread, and reads no clock. Time arrives as a field on the
event and as an argument to `tick`, which is what makes the minimum hold and the
runaway latch cap testable without waiting for either of them.

Two ways to capture, both ported unchanged:

  * **Hold** the primary key, default Right Control, and it records while held.
  * **Primary plus the latch modifier**, default Right Shift, and it records
    until the primary key is pressed again. For dictation too long to hold a key
    through.

Because pressing the chord necessarily involves pressing the primary key, press
order is handled explicitly. With the latch already down, capture starts
latched. Adding the latch part way through a hold *promotes* the recording
already in progress rather than starting it over. Either order works, and you
can decide a sentence in that this one is going long.
"""

from __future__ import annotations

import enum
import logging
import math
from dataclasses import dataclass

log = logging.getLogger("lippy.hotkey")

# Virtual key codes, from the Windows list. Plain integers, so naming them here
# costs no import and keeps the whole of the interaction logic in one file.
VK_LSHIFT = 0xA0
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
VK_RCONTROL = 0xA3
VK_LMENU = 0xA4
VK_RMENU = 0xA5

# What a user may bind, by the name the tray menu will show.
#
# Right Alt is bindable and is deliberately not the default: on international
# layouts it is AltGr, it produces characters, and pressing it makes Windows
# generate a simulated Left Control alongside it. Left Control is bindable for
# the same reason and with the same warning. Right Control is inert on its own
# on United States and European layouts, is present on essentially every
# keyboard, and is untouched by AltGr.
KEYS: dict[str, int] = {
    "Right Control": VK_RCONTROL,
    "Left Control": VK_LCONTROL,
    "Right Shift": VK_RSHIFT,
    "Left Shift": VK_LSHIFT,
    "Right Alt": VK_RMENU,
    "Left Alt": VK_LMENU,
}

DEFAULT_PRIMARY_VK = VK_RCONTROL
DEFAULT_LATCH_VK = VK_RSHIFT

# A hold shorter than this is a keystroke that happened to be the hotkey, not
# an attempt to dictate. It applies to holds only, because a latched session is
# deliberate however briefly it ran.
MINIMUM_HOLD_S = 0.3

# A latched session nobody ended is a microphone recording the room. Five
# minutes in, it ends itself and keeps what it has.
LATCH_CAP_S = 5 * 60.0

# Modifiers do not abort a hold. Only a struck key does.
#
# On macOS this falls out of the event types: the abort monitor watches keyDown,
# and modifiers arrive as flagsChanged instead, so they never reach it. A low
# level hook on Windows sees everything, so the same rule has to be stated. It
# matters because the alternative kills dictation whenever a hand rests on Shift.
# Control plus C during a hold still aborts, because C is a struck key.
MODIFIER_VKS = frozenset(
    {
        0x10,  # VK_SHIFT, the undifferentiated one some drivers send
        0x11,  # VK_CONTROL
        0x12,  # VK_MENU
        0x5B,  # VK_LWIN
        0x5C,  # VK_RWIN
        VK_LSHIFT,
        VK_RSHIFT,
        VK_LCONTROL,
        VK_RCONTROL,
        VK_LMENU,
        VK_RMENU,
    }
)


class EventKind(enum.Enum):
    """A key going down or coming up.

    The adapter maps `WM_KEYDOWN` and `WM_SYSKEYDOWN` onto DOWN, and `WM_KEYUP`
    and `WM_SYSKEYUP` onto UP. The SYS variants are not an edge case to skip:
    they are what an Alt key sends.
    """

    DOWN = "down"
    UP = "up"


class Action(enum.Enum):
    """What the worker thread should do about the event it just handed over."""

    BEGIN_HOLD = "begin_hold"
    BEGIN_LATCHED = "begin_latched"
    #: A hold was upgraded part way through. Audio captured so far is kept.
    PROMOTE = "promote"
    #: Capture finished and the audio should be transcribed.
    END = "end"
    #: Another key was struck part way through a hold. That was a chord.
    ABORT = "abort"
    #: The hold was too short to have been speech. Stop, and keep nothing.
    DISCARD = "discard"


class State(enum.Enum):
    IDLE = "idle"
    HOLDING = "holding"
    LATCHED = "latched"


@dataclass(frozen=True)
class KeyEvent:
    """One key transition, as the hook adapter reports it.

    `timestamp` is seconds from a monotonic clock. `scan_code` is carried and
    never read here: it exists for the adapter, which needs it to tell a real
    Left Control from the one Windows synthesises alongside Right Alt. That
    distinction is undocumented, so it belongs next to the hook rather than
    inside the logic it protects.
    """

    kind: EventKind
    vk: int
    scan_code: int
    timestamp: float


def _require_vk(value: object, field: str) -> int:
    # bool is an int in Python, and a True that reaches a key comparison would
    # silently mean VK 0x01, the left mouse button.
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field} must be an int virtual key code, got {value!r}")
    if not 0x01 <= value <= 0xFE:
        raise ValueError(f"{field} must be in 0x01..0xFE, got {value!r}")
    return value


def _require_seconds(value: object, field: str, *, minimum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a number of seconds, got {value!r}")
    seconds = float(value)
    if not math.isfinite(seconds):
        raise ValueError(f"{field} must be finite, got {value!r}")
    if seconds < minimum:
        raise ValueError(f"{field} must be at least {minimum}, got {value!r}")
    return seconds


class HotkeyState:
    """Three states, a separately tracked latch flag, and six actions out.

    The latch flag is tracked from the event stream rather than asked of the
    system on purpose. A low level keyboard callback runs before the
    asynchronous key state is updated, so `GetAsyncKeyState` answers one event
    stale from inside it. Reading the stream is not the simpler option here, it
    is the only correct one.
    """

    def __init__(
        self,
        primary_vk: int = DEFAULT_PRIMARY_VK,
        latch_vk: int | None = DEFAULT_LATCH_VK,
        *,
        minimum_hold: float = MINIMUM_HOLD_S,
        latch_cap: float = LATCH_CAP_S,
        chord_exempt_vks: frozenset[int] = MODIFIER_VKS,
    ) -> None:
        self.primary_vk = _require_vk(primary_vk, "primary_vk")
        if latch_vk is None:
            self.latch_vk: int | None = None
        else:
            latch = _require_vk(latch_vk, "latch_vk")
            # Binding both actions to one key makes a press ambiguous.
            if latch == self.primary_vk:
                log.warning("latch key equals the primary key, latching disabled")
                self.latch_vk = None
            else:
                self.latch_vk = latch
        self.minimum_hold = _require_seconds(minimum_hold, "minimum_hold", minimum=0.0)
        self.latch_cap = _require_seconds(latch_cap, "latch_cap", minimum=0.0)
        if self.latch_cap <= 0:
            raise ValueError(f"latch_cap must be greater than 0, got {latch_cap!r}")
        self.chord_exempt_vks = frozenset(chord_exempt_vks)

        self._state = State.IDLE
        self._latch_down = False
        self._primary_down = False
        self._began_at = 0.0
        self._latched_at = 0.0

    # ---- what the worker can ask -------------------------------------

    @property
    def state(self) -> State:
        return self._state

    @property
    def recording(self) -> bool:
        return self._state is not State.IDLE

    @property
    def latched(self) -> bool:
        return self._state is State.LATCHED

    def reset(self) -> None:
        """Drop everything, for when the hook is torn down and reinstalled.

        The watchdog reinstalls a hook that Windows removed without saying so,
        and the events that went missing while it was gone may well include the
        key coming back up. Starting from idle is the only state that cannot be
        wrong afterwards.

        The primary flag makes this load bearing rather than tidy. A key whose
        release went missing stays recorded as down, and every later press of
        it reads as a repeat, so the hotkey would go quiet for the life of the
        process. The adapter calls this whenever it reinstalls.
        """
        self._state = State.IDLE
        self._latch_down = False
        self._primary_down = False
        self._began_at = 0.0
        self._latched_at = 0.0

    # ---- the machine -------------------------------------------------

    def handle(self, event: KeyEvent) -> tuple[Action, ...]:
        """Feed one key transition in, and get back what it means."""
        if not isinstance(event, KeyEvent):
            raise TypeError(f"expected a KeyEvent, got {event!r}")

        if self.latch_vk is not None and event.vk == self.latch_vk:
            return self._handle_latch(event)
        if event.vk != self.primary_vk:
            return self._handle_other(event)
        return self._handle_primary(event)

    def _handle_latch(self, event: KeyEvent) -> tuple[Action, ...]:
        self._latch_down = event.kind is EventKind.DOWN
        if self._latch_down and self._state is State.HOLDING:
            self._state = State.LATCHED
            self._latched_at = event.timestamp
            log.info("promoted hold to latched")
            return (Action.PROMOTE,)
        return ()

    def _handle_other(self, event: KeyEvent) -> tuple[Action, ...]:
        if event.kind is not EventKind.DOWN:
            return ()
        if self._state is not State.HOLDING:
            # A latched session is deliberate, and the user may well type
            # during it. Only a hold is abandoned this way.
            return ()
        if event.vk in self.chord_exempt_vks:
            return ()
        self._state = State.IDLE
        log.info("capture aborted, another key was struck part way through a hold")
        return (Action.ABORT,)

    def _handle_primary(self, event: KeyEvent) -> tuple[Action, ...]:
        if event.kind is EventKind.DOWN:
            if self._primary_down:
                # Key repeat. Windows sends a stream of downs while a key is
                # physically held, and they mean nothing the first press did
                # not already say.
                #
                # This is tested before the states below rather than inside one
                # of them, because a repeat is not a second press in any state.
                # Reading it as one made a latched session end and restart at
                # the repeat rate for as long as the key stayed down, which
                # running the hook on a real keyboard turned up and which the
                # tests here had missed by feeding the machine one down per
                # intended press.
                return ()
            self._primary_down = True
            if self._state is State.LATCHED:
                # A genuine second press, which by the guard above means the
                # key came up in between. It ends the session even if the latch
                # modifier happens to be held down again.
                self._state = State.IDLE
                log.info("latched capture ended by a second press")
                return (Action.END,)
            self._began_at = event.timestamp
            if self._latch_down:
                self._state = State.LATCHED
                self._latched_at = event.timestamp
                log.info("capture begun, latched")
                return (Action.BEGIN_LATCHED,)
            self._state = State.HOLDING
            log.info("capture begun, held")
            return (Action.BEGIN_HOLD,)

        self._primary_down = False
        if self._state is State.HOLDING:
            self._state = State.IDLE
            held = event.timestamp - self._began_at
            if held < self.minimum_hold:
                log.info("ignored a %.2fs hold, below the %.2fs minimum",
                         held, self.minimum_hold)
                return (Action.DISCARD,)
            log.info("capture ended after %.2fs", held)
            return (Action.END,)
        # Releasing the primary key while latched is deliberately ignored.
        return ()

    def tick(self, now: float) -> tuple[Action, ...]:
        """Call this from the drain loop. It is what enforces the latch cap.

        macOS runs the cap on a Timer. There is no timer here on purpose,
        because a timer is a clock, and a clock is the thing that would make
        every test of this guard take five real minutes.
        """
        if isinstance(now, bool) or not isinstance(now, (int, float)):
            raise TypeError(f"now must be a number of seconds, got {now!r}")
        if self._state is not State.LATCHED:
            return ()
        if float(now) - self._latched_at < self.latch_cap:
            return ()
        self._state = State.IDLE
        log.warning("latch cap reached after %.0fs, ending capture", self.latch_cap)
        return (Action.END,)
