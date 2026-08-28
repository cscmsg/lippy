"""The hook adapter, minus the hook.

Step 2b is the first one that only runs on a real desktop, so the module is
arranged to keep the untestable part down to the ctypes plumbing. What is left
over is tested here and runs everywhere: the translation from a raw hook record
to a key event, the watchdog's judgement about silence, the drain loop, and the
structure layout, which is the one thing that would otherwise be verified by
whether Windows crashed.

What is NOT covered here, and cannot be: SetWindowsHookEx, the message loop, the
callback trampoline, and the reinstall actually reinstalling anything.
"""
import ctypes
import logging
import pathlib
import queue
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))

import pytest
import windows_hook as hook
from hotkey_state import Action, EventKind, HotkeyState, KeyEvent

VK_RCONTROL = 0xA3
VK_RSHIFT = 0xA1
VK_LCONTROL = 0xA2
A_KEY = 0x41


def record(message=hook.WM_KEYDOWN, vk=VK_RCONTROL, scan=0x1D, flags=0,
           extra=0, at=1.0):
    """One raw hook record, in the order the callback pushes them."""
    return (message, vk, scan, flags, extra, at)


# ---- the structure, which Windows would otherwise verify by crashing ------

def test_the_hook_structure_matches_the_win64_layout():
    """Four 32-bit fields and then a pointer. Borrowing wintypes.DWORD would
    give eight-byte fields everywhere except Windows, so this assertion would
    have been checking the wrong thing on the only machines that can run it."""
    offsets = [(name, getattr(hook.KBDLLHOOKSTRUCT, name).offset,
                getattr(hook.KBDLLHOOKSTRUCT, name).size)
               for name, _ in hook.KBDLLHOOKSTRUCT._fields_]
    assert offsets == [
        ("vkCode", 0, 4),
        ("scanCode", 4, 4),
        ("flags", 8, 4),
        ("time", 12, 4),
        ("dwExtraInfo", 16, 8),
    ]
    assert ctypes.sizeof(hook.KBDLLHOOKSTRUCT) == 24


def test_the_pointer_sized_types_are_pointer_sized():
    """A 32-bit LRESULT builds and runs on Win64 and truncates every return."""
    pointer = ctypes.sizeof(ctypes.c_void_p)
    for name in ["ULONG_PTR", "WPARAM", "LPARAM", "LRESULT"]:
        assert ctypes.sizeof(getattr(hook, name)) == pointer, name


def test_the_synthetic_tag_fits_where_it_is_written():
    assert 0 < hook.SYNTHETIC_TAG <= 0xFFFFFFFF


def test_every_key_message_maps_including_the_sys_variants():
    """The SYS variants are what an Alt key sends, and Alt is bindable."""
    assert hook.KIND_BY_MESSAGE == {
        hook.WM_KEYDOWN: EventKind.DOWN,
        hook.WM_SYSKEYDOWN: EventKind.DOWN,
        hook.WM_KEYUP: EventKind.UP,
        hook.WM_SYSKEYUP: EventKind.UP,
    }


# ---- translate -----------------------------------------------------------

def test_a_key_down_becomes_a_down_event_with_its_fields_intact():
    event = hook.translate(*record(vk=VK_RCONTROL, scan=0x1D, at=12.5))
    assert event == KeyEvent(kind=EventKind.DOWN, vk=VK_RCONTROL,
                             scan_code=0x1D, timestamp=12.5)


def test_a_key_up_becomes_an_up_event():
    event = hook.translate(*record(message=hook.WM_KEYUP, at=13.0))
    assert event.kind is EventKind.UP
    assert event.timestamp == 13.0


@pytest.mark.parametrize("message,kind", [
    (hook.WM_SYSKEYDOWN, EventKind.DOWN),
    (hook.WM_SYSKEYUP, EventKind.UP),
])
def test_the_sys_messages_are_not_skipped(message, kind):
    assert hook.translate(*record(message=message)).kind is kind


def test_a_message_this_does_not_handle_is_ignored():
    """The hook only receives key messages, but a wrong constant somewhere
    should produce nothing rather than an event with a guessed kind."""
    for message in [0x0200, 0x0000, hook.WM_TIMER, hook.WM_QUIT]:
        assert hook.translate(*record(message=message)) is None


def test_our_own_paste_chord_is_dropped(caplog):
    """SendInput tags what it injects so this can recognise it coming back.
    Without the tag, the paste in 2d would feed its own Control press into the
    state machine while the user is still holding the hotkey."""
    with caplog.at_level(logging.DEBUG, logger="lippy.hook"):
        assert hook.translate(*record(extra=hook.SYNTHETIC_TAG)) is None
    assert "our own synthetic input" in caplog.text


def test_another_application_tag_is_not_ours_and_is_kept():
    """Plenty of software writes dwExtraInfo. Only our own value is ours."""
    assert hook.translate(*record(extra=hook.SYNTHETIC_TAG + 1)) is not None
    assert hook.translate(*record(extra=0xDEADBEEF)) is not None


def test_injected_input_that_is_not_ours_is_kept():
    """The on-screen keyboard, remote sessions and assistive tools all inject.
    Dropping every injected event would mute all of them to solve a problem the
    tag already solves."""
    event = hook.translate(*record(flags=hook.LLKHF_INJECTED))
    assert event is not None
    assert event.vk == VK_RCONTROL


def test_the_left_control_that_altgr_synthesises_is_dropped(caplog):
    with caplog.at_level(logging.DEBUG, logger="lippy.hook"):
        dropped = hook.translate(*record(vk=VK_LCONTROL,
                                         scan=hook.ALTGR_LCONTROL_SCAN))
    assert dropped is None
    assert "AltGr" in caplog.text


def test_a_real_left_control_is_not_dropped():
    """The filter is the whole risk in this file. Eating real Left Control
    would break the key for anyone who binds it, which is the exact user the
    filter exists to serve."""
    event = hook.translate(*record(vk=VK_LCONTROL, scan=0x1D))
    assert event is not None
    assert event.vk == VK_LCONTROL


def test_the_altgr_filter_is_scoped_to_left_control():
    """Some other key reporting that scan code is not AltGr's doing."""
    event = hook.translate(*record(vk=A_KEY, scan=hook.ALTGR_LCONTROL_SCAN))
    assert event is not None
    assert event.vk == A_KEY


def test_translate_does_not_care_about_the_extended_flag():
    """Right Control is extended and Left Control is not, but the virtual key
    codes already distinguish them, so nothing here reads the flag."""
    left = hook.translate(*record(vk=VK_LCONTROL, scan=0x1D, flags=0))
    right = hook.translate(*record(vk=VK_RCONTROL, scan=0x1D,
                                   flags=hook.LLKHF_EXTENDED))
    assert (left.vk, right.vk) == (VK_LCONTROL, VK_RCONTROL)


# ---- the watchdog --------------------------------------------------------

def test_the_clock_starts_at_installation_not_at_the_first_key():
    """Otherwise a hook installed onto an idle machine looks dead immediately."""
    dog = hook.Watchdog(silence=30.0)
    dog.note_installed(100.0)
    assert dog.due(129.0) is False
    assert dog.quiet_for(129.0) == 29.0


def test_silence_of_exactly_the_threshold_is_due():
    dog = hook.Watchdog(silence=30.0)
    dog.note_installed(100.0)
    assert dog.due(130.0) is True


def test_activity_restarts_the_clock():
    dog = hook.Watchdog(silence=30.0)
    dog.note_installed(100.0)
    dog.note_activity(125.0)
    assert dog.due(130.0) is False
    assert dog.due(155.0) is True


def test_a_reinstall_restarts_the_clock_without_counting_as_proof_of_life():
    """Reinstalling proves nothing about whether anyone is typing, so the
    counter climbs and the log gets quieter rather than repeating an alarm."""
    dog = hook.Watchdog(silence=30.0)
    dog.note_installed(0.0)
    assert dog.reinstalls_since_event == 0
    dog.note_reinstalled(30.0)
    assert dog.reinstalls_since_event == 1
    assert dog.due(59.0) is False
    dog.note_reinstalled(60.0)
    assert dog.reinstalls_since_event == 2


def test_real_activity_resets_the_counter_so_the_next_alarm_is_loud_again():
    dog = hook.Watchdog(silence=30.0)
    dog.note_installed(0.0)
    dog.note_reinstalled(30.0)
    dog.note_reinstalled(60.0)
    dog.note_activity(65.0)
    assert dog.reinstalls_since_event == 0


def test_last_activity_reports_the_most_recent_of_the_three():
    dog = hook.Watchdog()
    dog.note_installed(1.0)
    assert dog.last_activity == 1.0
    dog.note_activity(2.0)
    assert dog.last_activity == 2.0
    dog.note_reinstalled(3.0)
    assert dog.last_activity == 3.0


def test_a_watchdog_that_could_never_fire_is_rejected():
    for bad in [0, -1, -0.5]:
        with pytest.raises(ValueError):
            hook.Watchdog(silence=bad)
    for bad in ["30", None, True]:
        with pytest.raises(TypeError):
            hook.Watchdog(silence=bad)


# ---- the drain loop ------------------------------------------------------

class Clock:
    def __init__(self, now=0.0):
        self.now = now

    def __call__(self):
        return self.now


def dispatcher(machine=None, clock=None, on_raw=None, installs=None):
    events: queue.SimpleQueue = queue.SimpleQueue()
    seen = []
    pump = hook.Dispatcher(machine or HotkeyState(VK_RCONTROL, VK_RSHIFT),
                           events, seen.append, clock or Clock(), on_raw,
                           installs)
    return pump, events, seen


class Installs:
    """Stands in for the hook's install counter, which the drain loop reads to
    find out that events went missing under it."""

    def __init__(self, count=1):
        self.count = count

    def __call__(self):
        return self.count


def test_an_empty_queue_emits_nothing():
    pump, _, seen = dispatcher()
    assert pump.pump(timeout=0.0) == 0
    assert seen == []


def test_a_hold_arrives_as_two_actions():
    pump, events, seen = dispatcher()
    events.put(record(message=hook.WM_KEYDOWN, at=1.0))
    events.put(record(message=hook.WM_KEYUP, at=2.0))
    assert pump.pump(timeout=0.0) == 2
    assert seen == [Action.BEGIN_HOLD, Action.END]


def test_everything_already_queued_drains_in_one_pump():
    """The hook fills the queue faster than the worker empties it, and a pump
    that took one record per call would fall behind while the user speaks."""
    pump, events, seen = dispatcher()
    for at in [1.0, 1.1, 1.2, 1.3]:
        events.put(record(message=hook.WM_KEYDOWN, at=at))    # repeat
    events.put(record(message=hook.WM_KEYUP, at=2.0))
    pump.pump(timeout=0.0)
    assert seen == [Action.BEGIN_HOLD, Action.END]
    assert events.empty()


def test_a_dropped_record_does_not_stop_the_drain():
    """A record translate throws away sits between two that matter."""
    pump, events, seen = dispatcher()
    events.put(record(message=hook.WM_KEYDOWN, at=1.0))
    events.put(record(vk=VK_LCONTROL, scan=hook.ALTGR_LCONTROL_SCAN, at=1.1))
    events.put(record(message=hook.WM_KEYUP, at=2.0))
    assert pump.pump(timeout=0.0) == 2
    assert seen == [Action.BEGIN_HOLD, Action.END]


def test_the_latch_cap_fires_from_the_pump_with_no_events_at_all():
    """The cap is the only action that arrives without a keystroke, so a pump
    that only ran when something was queued would never deliver it."""
    clock = Clock(0.0)
    pump, events, seen = dispatcher(HotkeyState(VK_RCONTROL, VK_RSHIFT,
                                                latch_cap=10.0), clock)
    events.put(record(vk=VK_RSHIFT, at=0.0))
    events.put(record(vk=VK_RCONTROL, at=0.0))
    pump.pump(timeout=0.0)
    assert seen == [Action.BEGIN_LATCHED]

    clock.now = 5.0
    assert pump.pump(timeout=0.0) == 0
    clock.now = 10.0
    assert pump.pump(timeout=0.0) == 1
    assert seen == [Action.BEGIN_LATCHED, Action.END]


def test_the_pump_uses_the_clock_it_was_given():
    """Not time.monotonic, or the cap could not be tested without waiting."""
    clock = Clock(0.0)
    pump, events, _ = dispatcher(HotkeyState(VK_RCONTROL, VK_RSHIFT,
                                             latch_cap=10.0), clock)
    events.put(record(vk=VK_RSHIFT, at=0.0))
    events.put(record(vk=VK_RCONTROL, at=0.0))
    pump.pump(timeout=0.0)
    clock.now = 10_000.0
    assert pump.pump(timeout=0.0) == 1


def test_raw_records_reach_the_observer_including_the_dropped_ones():
    """The point of --raw is seeing what translate refuses, because that is
    what settles the AltGr scan code on a real keyboard."""
    raw = []
    pump, events, _ = dispatcher(on_raw=lambda *fields: raw.append(fields))
    events.put(record(message=hook.WM_KEYDOWN, at=1.0))
    events.put(record(vk=VK_LCONTROL, scan=hook.ALTGR_LCONTROL_SCAN, at=1.1))
    pump.pump(timeout=0.0)
    assert len(raw) == 2
    assert raw[1][1] == VK_LCONTROL


def test_a_malformed_record_is_not_swallowed():
    """A record of the wrong shape is a bug in the callback, and a drain loop
    that quietly skipped it would hide the one place that cannot be tested."""
    pump, events, _ = dispatcher()
    events.put((hook.WM_KEYDOWN, VK_RCONTROL))
    with pytest.raises(TypeError):
        pump.pump(timeout=0.0)


def test_a_failing_handler_is_not_swallowed():
    events: queue.SimpleQueue = queue.SimpleQueue()

    def explode(action):
        raise RuntimeError("the paste failed")

    pump = hook.Dispatcher(HotkeyState(VK_RCONTROL, VK_RSHIFT), events,
                           explode, Clock())
    events.put(record(message=hook.WM_KEYDOWN, at=1.0))
    with pytest.raises(RuntimeError):
        pump.pump(timeout=0.0)


# ---- the platform guard --------------------------------------------------

def test_the_win32_loader_refuses_elsewhere_and_says_what_is_testable(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(RuntimeError) as caught:
        hook._user32()
    assert "needs Windows" in str(caught.value)
    assert "translate" in str(caught.value)


def test_describe_reports_the_flags_a_person_needs_to_read(capsys):
    line = hook.describe(hook.WM_KEYDOWN, VK_LCONTROL, 0x1D,
                         hook.LLKHF_EXTENDED | hook.LLKHF_INJECTED, 0, 1.0)
    printed = capsys.readouterr().out
    assert "vk=0xA2" in line and "scan=0x1D" in line
    assert "extended" in line and "injected" in line
    assert line in printed


def test_describe_marks_our_own_input_as_ours():
    line = hook.describe(hook.WM_KEYDOWN, VK_RCONTROL, 0x1D, 0,
                         hook.SYNTHETIC_TAG, 1.0)
    assert "ours" in line


# ---- resetting the machine when the hook is reinstalled under it ----------
#
# The watchdog reinstalls a hook that Windows may have removed silently. Events
# went missing for as long as it was gone, and one of them may be the key
# coming up, which the machine would otherwise go on believing is held.


def test_a_reinstall_between_pumps_clears_a_capture_in_progress(caplog):
    machine = HotkeyState(VK_RCONTROL, VK_RSHIFT)
    installs = Installs(1)
    pump, events, seen = dispatcher(machine=machine, installs=installs)
    events.put(record(message=hook.WM_KEYDOWN, at=1.0))
    pump.pump(timeout=0.0)
    assert machine.recording is True

    installs.count = 2
    with caplog.at_level(logging.INFO, logger="lippy.hook"):
        pump.pump(timeout=0.0)
    assert machine.recording is False
    assert "reinstalled during a capture" in caplog.text


def test_a_reinstall_lets_the_next_press_work_after_a_release_went_missing():
    """The wedge this exists to prevent. Without the reset the key stays
    recorded as down and every later press reads as a repeat."""
    machine = HotkeyState(VK_RCONTROL, VK_RSHIFT)
    installs = Installs(1)
    pump, events, seen = dispatcher(machine=machine, installs=installs)
    events.put(record(message=hook.WM_KEYDOWN, at=1.0))
    pump.pump(timeout=0.0)

    installs.count = 2
    pump.pump(timeout=0.0)
    events.put(record(message=hook.WM_KEYDOWN, at=9.0))
    events.put(record(message=hook.WM_KEYUP, at=10.0))
    pump.pump(timeout=0.0)
    assert seen == [Action.BEGIN_HOLD, Action.BEGIN_HOLD, Action.END]


def test_no_reinstall_leaves_a_capture_alone():
    machine = HotkeyState(VK_RCONTROL, VK_RSHIFT)
    pump, events, seen = dispatcher(machine=machine, installs=Installs(4))
    events.put(record(message=hook.WM_KEYDOWN, at=1.0))
    pump.pump(timeout=0.0)
    pump.pump(timeout=0.0)
    pump.pump(timeout=0.0)
    assert machine.recording is True
    assert seen == [Action.BEGIN_HOLD]


def test_a_reinstall_while_idle_says_nothing(caplog):
    machine = HotkeyState(VK_RCONTROL, VK_RSHIFT)
    installs = Installs(1)
    pump, _, seen = dispatcher(machine=machine, installs=installs)
    installs.count = 2
    with caplog.at_level(logging.INFO, logger="lippy.hook"):
        assert pump.pump(timeout=0.0) == 0
    assert "reinstalled during a capture" not in caplog.text
    assert seen == []


def test_the_counter_is_read_once_at_construction_so_the_first_pump_is_quiet():
    """A dispatcher built after the hook installed must not treat that first
    install as a reinstall, which would reset a machine nobody had used yet."""
    machine = HotkeyState(VK_RCONTROL, VK_RSHIFT)
    pump, events, seen = dispatcher(machine=machine, installs=Installs(7))
    events.put(record(message=hook.WM_KEYDOWN, at=1.0))
    events.put(record(message=hook.WM_KEYUP, at=2.0))
    assert pump.pump(timeout=0.0) == 2
    assert seen == [Action.BEGIN_HOLD, Action.END]


def test_a_dispatcher_with_no_install_counter_never_resets():
    """The counter is optional, because the state machine tests drive a
    dispatcher that has no hook behind it at all."""
    machine = HotkeyState(VK_RCONTROL, VK_RSHIFT)
    pump, events, _ = dispatcher(machine=machine)
    events.put(record(message=hook.WM_KEYDOWN, at=1.0))
    pump.pump(timeout=0.0)
    pump.pump(timeout=0.0)
    assert machine.recording is True


def test_an_install_counter_that_goes_backwards_still_counts_as_a_change():
    """Nothing promises the count only rises. A change of any direction means
    the hook is not the one the machine was tracking."""
    machine = HotkeyState(VK_RCONTROL, VK_RSHIFT)
    installs = Installs(5)
    pump, events, _ = dispatcher(machine=machine, installs=installs)
    events.put(record(message=hook.WM_KEYDOWN, at=1.0))
    pump.pump(timeout=0.0)
    installs.count = 2
    pump.pump(timeout=0.0)
    assert machine.recording is False
