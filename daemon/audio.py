"""Sample rate conversion for live capture, and nothing else.

Ported from `AudioRecorder.resample` in `app/Sources/Lippy/AudioRecorder.swift`,
including the branch that looks redundant and is not.

`asr.load_wav` already resamples, and this is deliberately not that function.
That one interpolates linearly at every ratio, which is fine for a file read
once. Live capture is usually 48 kHz, which is the integer case, and there this
**averages each group of input samples** rather than picking one of them. The
average is a crude low pass, and it matters: plain decimation folds everything
above 8 kHz back into the speech band as aliasing, and sibilants are exactly
what lands up there.

Standard library only, no numpy, which is why the lean test job covers it. The
cost of that choice was measured before it was made: a full five minute latched
session at 48 kHz converts in about 0.3 seconds, once, at the moment the user
stops speaking and a speech model is about to take considerably longer.

Buffers are `array` of float32, the same width the callback delivers and the
same width the model wants, so the capture layer can fill one with `frombytes`
and hand it straight over without touching a sample in Python.
"""

from __future__ import annotations

import array
import logging
from collections.abc import Sequence

log = logging.getLogger("lippy.audio")

TARGET_SAMPLE_RATE = 16_000


def _as_float32(samples: Sequence[float]) -> array.array:
    if isinstance(samples, array.array) and samples.typecode == "f":
        return samples
    return array.array("f", samples)


def resample(
    samples: Sequence[float],
    from_rate: float,
    to_rate: int = TARGET_SAMPLE_RATE,
) -> array.array:
    """Convert mono float32 audio to `to_rate`, defaulting to what the models want.

    Returns a new buffer every time, including when no conversion is needed.
    The caller owns the one it passed in and is entitled to keep using it.

    A rate of zero or less returns nothing and says so in the log. It means the
    device was queried before it was open, and the silent version of that
    failure is a recording that captures nothing while the interface reports
    that everything is fine.
    """
    if not isinstance(to_rate, int) or isinstance(to_rate, bool) or to_rate <= 0:
        raise ValueError(f"to_rate must be a positive integer, got {to_rate!r}")
    if isinstance(from_rate, bool) or not isinstance(from_rate, (int, float)):
        raise TypeError(f"from_rate must be a number, got {from_rate!r}")

    if from_rate <= 0:
        log.warning("no input rate (%r), returning no audio", from_rate)
        return array.array("f")

    source = _as_float32(samples)
    if not source:
        log.debug("nothing captured, returning no audio")
        return array.array("f")
    if from_rate == to_rate:
        return array.array("f", source)

    ratio = from_rate / to_rate
    rounded = round(ratio)

    if rounded >= 2 and abs(ratio - rounded) < 1e-9:
        return _decimate_averaging(source, int(rounded))
    return _interpolate(source, ratio)


def _decimate_averaging(source: array.array, factor: int) -> array.array:
    """The integer case, which is every common microphone: 48k/3, 32k/2.

    Averaging the group rather than taking one sample of it is the whole
    difference between this and plain decimation. See the module docstring.
    """
    count = len(source) // factor
    if count == 0:
        log.debug("only %d samples for a factor of %d, returning no audio",
                  len(source), factor)
        return array.array("f")

    # Strided slices and one zip, because the alternative is a Python level
    # loop over several million samples. The trailing samples that do not fill
    # a whole group are dropped, which is at most two of them.
    used = count * factor
    groups = [source[offset:used:factor] for offset in range(factor)]
    return array.array("f", [sum(values) / factor for values in zip(*groups)])


def _interpolate(source: array.array, ratio: float) -> array.array:
    """The non integer case, which in practice means 44.1 kHz hardware."""
    count = int(len(source) / ratio)
    if count == 0:
        log.debug("only %d samples at a ratio of %.4f, returning no audio",
                  len(source), ratio)
        return array.array("f")

    out = array.array("f", bytes(4 * count))
    last = len(source) - 1
    for i in range(count):
        position = i * ratio
        index = int(position)
        if index > last:
            index = last
        following = index + 1 if index < last else last
        fraction = position - index
        out[i] = source[index] + (source[following] - source[index]) * fraction
    return out
