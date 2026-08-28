"""Sample rate conversion, and specifically the branch that is easy to lose.

The integer branch averages each group of input samples and the non integer
branch interpolates. Porting only the second one would look correct, pass a
smoke test, and quietly alias every sibilant on the 48 kHz hardware that almost
everybody has. So the expected values here are written out by hand from the
arithmetic rather than computed, and the aliasing case has a test of its own.
"""
import array
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))

import pytest
import audio


def f32(values) -> array.array:
    return array.array("f", values)


# ---- the integer branch, which is the common one -------------------------

def test_48k_averages_each_group_of_three():
    """(0+1+2)/3 and (3+4+5)/3, not sample 0 and sample 3."""
    out = audio.resample(f32([0, 1, 2, 3, 4, 5]), 48_000)
    assert list(out) == pytest.approx([1.0, 4.0])


def test_32k_averages_each_pair():
    out = audio.resample(f32([0, 2, 4, 6]), 32_000)
    assert list(out) == pytest.approx([1.0, 5.0])


def test_averaging_is_what_stops_the_aliasing():
    """The reason this branch exists at all.

    A signal alternating every sample is at the input Nyquist frequency. Taking
    every third sample would keep it at full amplitude and fold it back into the
    speech band. Averaging the group drops it to a third, which is a crude low
    pass doing its job.
    """
    out = audio.resample(f32([1, -1, 1, -1, 1, -1]), 48_000)
    assert list(out) == pytest.approx([1 / 3, -1 / 3])
    assert max(abs(value) for value in out) < 0.4


def test_a_partial_final_group_is_dropped():
    """Seven samples at 48 kHz are two output samples and one remainder."""
    out = audio.resample(f32([0, 1, 2, 3, 4, 5, 9]), 48_000)
    assert list(out) == pytest.approx([1.0, 4.0])


def test_fewer_samples_than_the_factor_yields_nothing():
    assert list(audio.resample(f32([1.0, 2.0]), 48_000)) == []


def test_a_full_second_of_48k_is_exactly_16000_samples():
    out = audio.resample(f32([0.5] * 48_000), 48_000)
    assert len(out) == 16_000
    assert set(out) == {0.5}


# ---- the non integer branch ----------------------------------------------

def test_44k1_interpolates_between_neighbours():
    """A ramp interpolates to its own position, so the expected values are the
    input positions: 0, 2.75625, 5.5125, 8.26875 at a ratio of 44100/16000."""
    out = audio.resample(f32(range(12)), 44_100)
    assert list(out) == pytest.approx([0.0, 2.75625, 5.5125, 8.26875], rel=1e-6)


def test_the_last_sample_is_held_rather_than_extrapolated():
    """Upsampling walks off the end of the buffer, and the clamp is what keeps
    the final output sample from reading past it."""
    out = audio.resample(f32([0, 1, 2, 3]), 8_000)
    assert list(out) == pytest.approx([0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.0])


def test_a_ratio_of_two_and_a_half_takes_the_interpolating_branch():
    """40 kHz is not an integer multiple of 16 kHz, and rounding it to two
    would silently resample to 20 kHz instead."""
    out = audio.resample(f32(range(10)), 40_000)
    assert list(out) == pytest.approx([0.0, 2.5, 5.0, 7.5], rel=1e-6)


def test_too_few_samples_to_interpolate_yields_nothing():
    assert list(audio.resample(f32([1.0, 2.0]), 44_100)) == []


# ---- the paths that must not be silent -----------------------------------

def test_a_rate_of_zero_returns_nothing_and_says_so(caplog):
    """The device was queried before it was open. The silent version of this
    is a recording that captures nothing while everything looks fine."""
    with caplog.at_level(logging.WARNING, logger="lippy.audio"):
        out = audio.resample(f32([1, 2, 3]), 0)
    assert list(out) == []
    assert "no input rate" in caplog.text


def test_a_negative_rate_returns_nothing_and_says_so(caplog):
    with caplog.at_level(logging.WARNING, logger="lippy.audio"):
        assert list(audio.resample(f32([1, 2, 3]), -48_000)) == []
    assert "no input rate" in caplog.text


def test_no_samples_returns_no_samples():
    assert list(audio.resample(f32([]), 48_000)) == []


# ---- what the caller gets back -------------------------------------------

def test_the_source_buffer_is_never_modified():
    source = f32([0, 1, 2, 3, 4, 5])
    audio.resample(source, 48_000)
    assert list(source) == [0, 1, 2, 3, 4, 5]


def test_a_rate_that_needs_no_conversion_still_returns_a_separate_buffer():
    """The caller keeps its own buffer, so handing back the same object would
    let one side of the pipeline write into the other's audio."""
    source = f32([0.25, 0.5])
    out = audio.resample(source, 16_000)
    assert list(out) == [0.25, 0.5]
    assert out is not source
    out[0] = 0.75
    assert source[0] == 0.25


def test_the_result_is_float32_which_is_what_the_models_take():
    out = audio.resample(f32([0, 1, 2]), 48_000)
    assert isinstance(out, array.array)
    assert out.typecode == "f"


def test_a_plain_list_is_accepted_as_well_as_an_array():
    assert list(audio.resample([0, 1, 2, 3, 4, 5], 48_000)) == pytest.approx([1.0, 4.0])


def test_an_explicit_target_rate_is_honoured():
    """8 kHz from 48 kHz is a factor of six, not the default three."""
    out = audio.resample(f32(range(12)), 48_000, 8_000)
    assert list(out) == pytest.approx([2.5, 8.5])


# ---- malformed input -----------------------------------------------------

def test_a_target_rate_of_zero_is_a_caller_error_not_an_empty_buffer():
    with pytest.raises(ValueError):
        audio.resample(f32([1, 2, 3]), 48_000, 0)


def test_a_non_integer_target_rate_is_rejected():
    for bad in ["16000", 16_000.0, None, True]:
        with pytest.raises(ValueError):
            audio.resample(f32([1, 2, 3]), 48_000, bad)


def test_a_source_rate_that_is_not_a_number_is_rejected():
    for bad in ["48000", None, True]:
        with pytest.raises(TypeError):
            audio.resample(f32([1, 2, 3]), bad)


def test_samples_that_are_not_numbers_are_rejected():
    with pytest.raises(TypeError):
        audio.resample(["loud", "quiet"], 48_000)
