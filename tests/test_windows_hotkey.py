"""The push-to-talk state machine, which is the whole of the interaction logic.

Nothing here needs Windows, which is the point of the module being shaped this
way. The hook that feeds it cannot be tested on a runner at all, so everything
that could be a decision was moved out of it and into here.

The cases the plan asked for are all present and labelled: both latch press
orders, promotion part way through a hold, a hold below the minimum, a latched
session ignoring keystrokes while a hold aborts on them, the second press
winning while the latch modifier is held, the cap firing, and key repeat
changing nothing.
"""
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))

import pytest
from hotkey_state import (
    DEFAULT_LATCH_VK,
    DEFAULT_PRIMARY_VK,
    VK_LSHIFT,
    VK_RCONTROL,
    VK_RSHIFT,
    Action,
    EventKind,
    HotkeyState,
    KeyEvent,
    State,
)

A_KEY = 0x41
C_KEY = 0x43


def down(vk: int, at: float = 0.0, scan: int = 0x1D) -> KeyEvent:
    return KeyEvent(EventKind.DOWN, vk, scan, at)


def up(vk: int, at: float = 0.0, scan: int = 0x1D) -> KeyEvent:
    return KeyEvent(EventKind.UP, vk, scan, at)


# ---- the defaults the plan settled on ------------------------------------

def test_defaults_are_right_control_and_right_shift():
    """Right Alt is AltGr and Left Control is what AltGr fakes. Both are out."""
    machine = HotkeyState()
    assert machine.primary_vk == VK_RCONTROL
    assert machine.latch_vk == VK_RSHIFT
    assert (DEFAULT_PRIMARY_VK, DEFAULT_LATCH_VK) == (0xA3, 0xA1)


def test_a_new_machine_is_idle_and_not_recording():
    machine = HotkeyState()
    assert machine.state is State.IDLE
    assert machine.recording is False
    assert machine.latched is False


# ---- holding -------------------------------------------------------------

def test_hold_begins_on_press_and_ends_on_release():
    machine = HotkeyState()
    assert machine.handle(down(VK_RCONTROL, at=1.0)) == (Action.BEGIN_HOLD,)
    assert machine.state is State.HOLDING
    assert machine.recording is True
    assert machine.latched is False
    assert machine.handle(up(VK_RCONTROL, at=2.0)) == (Action.END,)
    assert machine.state is State.IDLE


def test_hold_below_the_minimum_is_discarded_not_ended():
    """A 0.29s press is the hotkey being struck, not an attempt to dictate."""
    machine = HotkeyState()
    machine.handle(down(VK_RCONTROL, at=10.0))
    assert machine.handle(up(VK_RCONTROL, at=10.29)) == (Action.DISCARD,)
    assert machine.state is State.IDLE


def test_hold_of_exactly_the_minimum_is_kept():
    """The boundary is inclusive, so 0.3 is a dictation and 0.29 is not."""
    machine = HotkeyState()
    machine.handle(down(VK_RCONTROL, at=10.0))
    assert machine.handle(up(VK_RCONTROL, at=10.3)) == (Action.END,)


def test_a_timestamp_that_goes_backwards_discards_rather_than_delivering():
    """A negative hold is a clock adjustment. Nothing usable was captured."""
    machine = HotkeyState()
    machine.handle(down(VK_RCONTROL, at=100.0))
    assert machine.handle(up(VK_RCONTROL, at=99.0)) == (Action.DISCARD,)


def test_key_repeat_during_a_hold_changes_nothing():
    """Windows sends a stream of downs while a key is held."""
    machine = HotkeyState()
    assert machine.handle(down(VK_RCONTROL, at=1.0)) == (Action.BEGIN_HOLD,)
    for repeat in range(5):
        assert machine.handle(down(VK_RCONTROL, at=1.1 + repeat * 0.03)) == ()
    assert machine.state is State.HOLDING
    # The hold is still measured from the first press, not the last repeat.
    assert machine.handle(up(VK_RCONTROL, at=1.31)) == (Action.END,)


def test_release_without_a_press_does_nothing():
    machine = HotkeyState()
    assert machine.handle(up(VK_RCONTROL, at=1.0)) == ()
    assert machine.state is State.IDLE


# ---- aborting ------------------------------------------------------------

def test_a_struck_key_aborts_a_hold():
    """Right Control plus C is a chord. It is not dictation."""
    machine = HotkeyState()
    machine.handle(down(VK_RCONTROL, at=1.0))
    assert machine.handle(down(C_KEY, at=1.1)) == (Action.ABORT,)
    assert machine.state is State.IDLE
    # The key coming back up afterwards must not resurrect anything.
    assert machine.handle(up(VK_RCONTROL, at=2.0)) == ()


def test_a_struck_key_does_not_abort_a_latched_session():
    """A latched session is deliberate, and the user may well type during it."""
    machine = HotkeyState()
    machine.handle(down(VK_RSHIFT, at=1.0))
    machine.handle(down(VK_RCONTROL, at=1.1))
    assert machine.state is State.LATCHED
    assert machine.handle(down(A_KEY, at=2.0)) == ()
    assert machine.handle(up(A_KEY, at=2.1)) == ()
    assert machine.state is State.LATCHED


def test_an_unrelated_modifier_does_not_abort_a_hold():
    """A hand resting on Shift is not a chord, and killing dictation for it
    would be the more annoying half of the trade."""
    machine = HotkeyState()
    machine.handle(down(VK_RCONTROL, at=1.0))
    assert machine.handle(down(VK_LSHIFT, at=1.1)) == ()
    assert machine.state is State.HOLDING
    assert machine.handle(up(VK_RCONTROL, at=1.5)) == (Action.END,)


def test_releasing_another_key_never_aborts():
    """Only a key going down is a chord. A stale release is not."""
    machine = HotkeyState()
    machine.handle(down(VK_RCONTROL, at=1.0))
    assert machine.handle(up(A_KEY, at=1.1)) == ()
    assert machine.state is State.HOLDING


def test_a_struck_key_while_idle_is_ignored():
    machine = HotkeyState()
    assert machine.handle(down(A_KEY, at=1.0)) == ()
    assert machine.state is State.IDLE


# ---- latching, both press orders -----------------------------------------

def test_latch_first_then_primary_begins_latched():
    machine = HotkeyState()
    assert machine.handle(down(VK_RSHIFT, at=1.0)) == ()
    assert machine.handle(down(VK_RCONTROL, at=1.1)) == (Action.BEGIN_LATCHED,)
    assert machine.latched is True


def test_primary_first_then_latch_promotes_the_recording_in_progress():
    """Promotion, not a restart. The audio captured so far is kept, which is
    only true because no BEGIN is emitted a second time."""
    machine = HotkeyState()
    assert machine.handle(down(VK_RCONTROL, at=1.0)) == (Action.BEGIN_HOLD,)
    assert machine.handle(down(VK_RSHIFT, at=1.4)) == (Action.PROMOTE,)
    assert machine.state is State.LATCHED
    # And the hold guard no longer applies, however briefly it ran.
    assert machine.handle(down(VK_RCONTROL, at=1.45)) == (Action.END,)


def test_promotion_happens_once_however_often_the_latch_repeats():
    machine = HotkeyState()
    machine.handle(down(VK_RCONTROL, at=1.0))
    assert machine.handle(down(VK_RSHIFT, at=1.2)) == (Action.PROMOTE,)
    assert machine.handle(down(VK_RSHIFT, at=1.3)) == ()
    assert machine.handle(down(VK_RSHIFT, at=1.4)) == ()


def test_the_latch_modifier_alone_starts_nothing():
    machine = HotkeyState()
    assert machine.handle(down(VK_RSHIFT, at=1.0)) == ()
    assert machine.handle(up(VK_RSHIFT, at=2.0)) == ()
    assert machine.recording is False


def test_releasing_the_primary_key_while_latched_is_ignored():
    machine = HotkeyState()
    machine.handle(down(VK_RSHIFT, at=1.0))
    machine.handle(down(VK_RCONTROL, at=1.1))
    assert machine.handle(up(VK_RCONTROL, at=1.2)) == ()
    assert machine.state is State.LATCHED


def test_second_press_ends_a_latched_session_while_the_latch_is_held():
    """The order the two checks are made in is load bearing: the second press
    has to win even though the latch modifier is down again."""
    machine = HotkeyState()
    machine.handle(down(VK_RCONTROL, at=1.0))
    machine.handle(down(VK_RSHIFT, at=1.1))
    machine.handle(up(VK_RCONTROL, at=1.2))
    machine.handle(up(VK_RSHIFT, at=1.3))
    machine.handle(down(VK_RSHIFT, at=5.0))       # held down again
    assert machine.handle(down(VK_RCONTROL, at=5.1)) == (Action.END,)
    assert machine.state is State.IDLE


def test_a_latched_session_that_ended_does_not_restart_on_the_key_coming_up():
    machine = HotkeyState()
    machine.handle(down(VK_RSHIFT, at=1.0))
    machine.handle(down(VK_RCONTROL, at=1.1))
    machine.handle(down(VK_RCONTROL, at=9.0))
    assert machine.handle(up(VK_RCONTROL, at=9.1)) == ()
    assert machine.state is State.IDLE


def test_the_latch_state_survives_a_session_and_starts_the_next_one_latched():
    """The flag is tracked from the stream, so a modifier still physically
    down after one session is still down at the start of the next."""
    machine = HotkeyState()
    machine.handle(down(VK_RSHIFT, at=1.0))
    machine.handle(down(VK_RCONTROL, at=1.1))
    machine.handle(down(VK_RCONTROL, at=4.0))
    assert machine.handle(down(VK_RCONTROL, at=6.0)) == (Action.BEGIN_LATCHED,)


def test_binding_the_latch_to_the_primary_key_disables_latching(caplog):
    """One key cannot mean both, because the press would be ambiguous."""
    with caplog.at_level(logging.WARNING, logger="lippy.hotkey"):
        machine = HotkeyState(VK_RCONTROL, VK_RCONTROL)
    assert machine.latch_vk is None
    assert "latching disabled" in caplog.text
    assert machine.handle(down(VK_RCONTROL, at=1.0)) == (Action.BEGIN_HOLD,)


def test_latching_can_be_turned_off_entirely():
    machine = HotkeyState(VK_RCONTROL, None)
    assert machine.handle(down(VK_RSHIFT, at=1.0)) == ()
    assert machine.handle(down(VK_RCONTROL, at=1.1)) == (Action.BEGIN_HOLD,)
    # Right Shift is now just another modifier, so it cannot promote or abort.
    assert machine.handle(down(VK_RSHIFT, at=1.2)) == ()
    assert machine.state is State.HOLDING


# ---- the runaway cap -----------------------------------------------------

def test_the_cap_ends_a_forgotten_latched_session():
    machine = HotkeyState(latch_cap=300.0)
    machine.handle(down(VK_RSHIFT, at=0.0))
    machine.handle(down(VK_RCONTROL, at=1.0))
    assert machine.tick(now=300.0) == ()
    assert machine.tick(now=301.0) == (Action.END,)
    assert machine.state is State.IDLE


def test_the_cap_fires_exactly_once():
    machine = HotkeyState(latch_cap=10.0)
    machine.handle(down(VK_RSHIFT, at=0.0))
    machine.handle(down(VK_RCONTROL, at=0.0))
    assert machine.tick(now=10.0) == (Action.END,)
    assert machine.tick(now=11.0) == ()
    assert machine.tick(now=1_000.0) == ()


def test_the_cap_runs_from_the_promotion_not_from_the_first_press():
    """Promotion restarts it, the way startLatchCap does on macOS. A hold that
    ran for a while before being latched still gets the full allowance."""
    machine = HotkeyState(latch_cap=10.0)
    machine.handle(down(VK_RCONTROL, at=0.0))
    machine.handle(down(VK_RSHIFT, at=8.0))
    assert machine.tick(now=15.0) == ()
    assert machine.tick(now=18.0) == (Action.END,)


def test_the_cap_never_touches_a_hold_or_an_idle_machine():
    machine = HotkeyState(latch_cap=1.0)
    assert machine.tick(now=10_000.0) == ()
    machine.handle(down(VK_RCONTROL, at=0.0))
    assert machine.tick(now=10_000.0) == ()
    assert machine.state is State.HOLDING


def test_the_cap_logs_when_it_fires(caplog):
    """A session that ended without the user ending it has to be explicable
    afterwards, because the user will not know what happened."""
    machine = HotkeyState(latch_cap=5.0)
    machine.handle(down(VK_RSHIFT, at=0.0))
    machine.handle(down(VK_RCONTROL, at=0.0))
    with caplog.at_level(logging.WARNING, logger="lippy.hotkey"):
        assert machine.tick(now=5.0) == (Action.END,)
    assert "latch cap reached" in caplog.text


# ---- reset ---------------------------------------------------------------

def test_reset_clears_the_session_and_the_latch_flag():
    """The watchdog reinstalls a hook that vanished, and the events it missed
    may include the key coming up."""
    machine = HotkeyState()
    machine.handle(down(VK_RSHIFT, at=1.0))
    machine.handle(down(VK_RCONTROL, at=1.1))
    machine.reset()
    assert machine.state is State.IDLE
    assert machine.recording is False
    # Latched-ness is gone too, so the next press is an ordinary hold.
    assert machine.handle(down(VK_RCONTROL, at=2.0)) == (Action.BEGIN_HOLD,)


# ---- malformed input -----------------------------------------------------

def test_handle_rejects_anything_that_is_not_a_key_event():
    machine = HotkeyState()
    for rubbish in [None, (EventKind.DOWN, VK_RCONTROL, 0x1D, 1.0), "down", 0xA3]:
        with pytest.raises(TypeError):
            machine.handle(rubbish)


def test_a_virtual_key_code_must_be_an_integer_in_range():
    for bad in ["0xA3", None, 3.0]:
        with pytest.raises(TypeError):
            HotkeyState(bad)
    for out_of_range in [0, -1, 0xFF, 0x100]:
        with pytest.raises(ValueError):
            HotkeyState(out_of_range)


def test_a_boolean_is_not_a_virtual_key_code():
    """True is 1 in Python, and 1 is the left mouse button."""
    with pytest.raises(TypeError):
        HotkeyState(True)


def test_the_guards_reject_values_that_would_disable_them_silently():
    for bad in [float("nan"), float("inf")]:
        with pytest.raises(ValueError):
            HotkeyState(minimum_hold=bad)
    with pytest.raises(ValueError):
        HotkeyState(minimum_hold=-0.1)
    with pytest.raises(ValueError):
        HotkeyState(latch_cap=0)
    with pytest.raises(TypeError):
        HotkeyState(latch_cap="300")


def test_tick_rejects_a_time_that_is_not_a_number():
    machine = HotkeyState()
    for bad in [None, "now", True]:
        with pytest.raises(TypeError):
            machine.tick(bad)


def test_a_minimum_hold_of_zero_is_allowed_and_keeps_everything():
    """Turning the guard off is a legitimate choice, unlike breaking it."""
    machine = HotkeyState(minimum_hold=0.0)
    machine.handle(down(VK_RCONTROL, at=1.0))
    assert machine.handle(up(VK_RCONTROL, at=1.0)) == (Action.END,)
