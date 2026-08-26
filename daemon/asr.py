"""Speech-to-text backends.

Parakeet TDT 0.6B v3 is the default and the reason this feels instant: it is a
0.6B transducer that runs far faster than realtime on Apple Silicon, emits its
own punctuation and capitalisation, and -- critically for push-to-talk --
returns an empty string for silence instead of inventing text. Whisper
large-v3-turbo is kept as a fallback for the languages Parakeet does not cover.

Both backends take PCM already in memory. Nothing is written to disk, which is
half the point of building this locally in the first place.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import numpy as np

import config
import models

log = logging.getLogger("lippy.asr")

SAMPLE_RATE = 16_000

# Below these, there is nothing to transcribe and running a model on it only
# invites hallucination. RMS of true silence is ~1e-5; speech is ~1e-2.
MIN_DURATION_S = 0.25
MIN_RMS = 1e-3


@dataclass
class Transcript:
    text: str
    duration_s: float
    compute_s: float

    @property
    def realtime_factor(self) -> float:
        return self.duration_s / self.compute_s if self.compute_s else 0.0


class ParakeetBackend:
    """NVIDIA Parakeet TDT via MLX. English-strong, 25 languages, CC-BY-4.0."""

    name = "parakeet"

    def __init__(self, model_id: str = "mlx-community/parakeet-tdt-0.6b-v3") -> None:
        from parakeet_mlx import from_pretrained

        self.model_id = model_id
        t0 = time.perf_counter()
        self._model = from_pretrained(model_id)
        log.info("loaded %s in %.1fs", model_id, time.perf_counter() - t0)

    def warm_up(self) -> None:
        """Force Metal kernel compilation now, not on the user's first word.

        The first generate() call costs ~1s of shader compilation. Paying it at
        daemon start makes every real utterance fast.
        """
        self._transcribe_array(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32))

    def _transcribe_array(self, pcm: np.ndarray) -> str:
        import mlx.core as mx
        from parakeet_mlx.audio import get_logmel

        mel = get_logmel(mx.array(pcm), self._model.preprocessor_config)
        results = self._model.generate(mel)
        return results[0].text.strip() if results else ""

    def transcribe(self, pcm: np.ndarray) -> Transcript:
        duration = len(pcm) / SAMPLE_RATE
        t0 = time.perf_counter()
        text = "" if _is_silence(pcm, duration) else self._transcribe_array(pcm)
        return Transcript(text, duration, time.perf_counter() - t0)


class SherpaBackend:
    """Parakeet via sherpa-onnx / ONNX Runtime. The cross-platform backend.

    Same model family as ParakeetBackend, different runtime. MLX is Apple-only,
    so Windows and Linux run the ONNX export instead. int8-quantised, which is
    643 MB against MLX's 2.51 GB -- a real difference when the whole thing has
    to fit in a Store package.

    Kept in the same file as the MLX backend on purpose: they must produce
    comparable text, and that is easier to keep true when they are read side by
    side.
    """

    name = "sherpa"

    def __init__(self, model_dir: str | None = None) -> None:
        import sherpa_onnx

        # Resolved by models.py so the place this loads from and the place the
        # bootstrap writes to cannot drift apart.
        directory = models.model_dir(model_dir)
        if not models.is_complete(directory):
            # is_complete rather than is_dir: a download killed part-way leaves
            # the directory behind, and loading from it fails inside the
            # decoder with nothing pointing back at the real cause.
            raise FileNotFoundError(
                f"ONNX model not ready at {directory}. Fetch it with "
                f"`python daemon/models.py`, or set LIPPY_ONNX_MODEL_DIR to an "
                f"existing copy."
            )

        t0 = time.perf_counter()
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=str(directory / "encoder.int8.onnx"),
            decoder=str(directory / "decoder.int8.onnx"),
            joiner=str(directory / "joiner.int8.onnx"),
            tokens=str(directory / "tokens.txt"),
            # NeMo transducers use a different blank/label convention to the
            # k2 ones sherpa defaults to; the wrong model_type decodes to
            # confident nonsense rather than failing.
            model_type="nemo_transducer",
            decoding_method="greedy_search",
            num_threads=max(2, (os.cpu_count() or 4) // 2),
        )
        log.info("loaded %s in %.1fs", directory.name, time.perf_counter() - t0)

    def warm_up(self) -> None:
        self._transcribe_array(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32))

    def _transcribe_array(self, pcm: np.ndarray) -> str:
        stream = self._recognizer.create_stream()
        stream.accept_waveform(SAMPLE_RATE, pcm)
        self._recognizer.decode_stream(stream)
        return stream.result.text.strip()

    def transcribe(self, pcm: np.ndarray) -> Transcript:
        duration = len(pcm) / SAMPLE_RATE
        t0 = time.perf_counter()
        text = "" if _is_silence(pcm, duration) else self._transcribe_array(pcm)
        return Transcript(text, duration, time.perf_counter() - t0)


class WhisperBackend:
    """OpenAI Whisper large-v3-turbo via MLX. Broader language coverage.

    Note the tradeoff that made it the fallback rather than the default:
    Whisper hallucinates confident text over silence and background noise,
    which is exactly what the leading and trailing edges of a push-to-talk
    recording contain.
    """

    name = "whisper"

    def __init__(self, model_id: str = "mlx-community/whisper-large-v3-turbo") -> None:
        self.model_id = model_id
        import mlx_whisper  # noqa: F401  (import cost paid at construction)

    def warm_up(self) -> None:
        self._transcribe_array(np.zeros(int(SAMPLE_RATE * 0.5), dtype=np.float32))

    def _transcribe_array(self, pcm: np.ndarray) -> str:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            pcm,
            path_or_hf_repo=self.model_id,
            # condition_on_previous_text=False cuts Whisper's habit of looping
            # a phrase once it starts repeating itself.
            condition_on_previous_text=False,
            temperature=0.0,
        )
        return result["text"].strip()

    def transcribe(self, pcm: np.ndarray) -> Transcript:
        duration = len(pcm) / SAMPLE_RATE
        t0 = time.perf_counter()
        text = "" if _is_silence(pcm, duration) else self._transcribe_array(pcm)
        return Transcript(text, duration, time.perf_counter() - t0)


def _is_silence(pcm: np.ndarray, duration: float) -> bool:
    if duration < MIN_DURATION_S:
        return True
    rms = float(np.sqrt(np.mean(np.square(pcm, dtype=np.float64))))
    if rms < MIN_RMS:
        log.info("rejected as silence (rms=%.2e, %.2fs)", rms, duration)
        return True
    return False


def build(backend: str | None = None, model_id: str | None = None):
    """Construct a backend. None means "whatever this platform runs".

    The platform default is not cosmetic: MLX has no Windows build, so asking
    for "parakeet" there fails at import with a message about a missing module
    rather than about a backend choice.
    """
    backend = backend or config.default_asr_backend()
    if backend == "parakeet":
        return ParakeetBackend(model_id) if model_id else ParakeetBackend()
    if backend == "sherpa":
        return SherpaBackend(model_id) if model_id else SherpaBackend()
    if backend == "whisper":
        return WhisperBackend(model_id) if model_id else WhisperBackend()
    raise ValueError(f"unknown ASR backend: {backend!r}")
