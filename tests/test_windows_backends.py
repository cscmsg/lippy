"""Which backend and which polish engine each platform picks.

These assert the *choice*, never a construction: building a real backend loads
gigabytes of model, and the decision is the part that differs by platform.

polish.py imports nothing outside the standard library at module scope, so its
half runs on the bare runner. asr.py imports numpy, so its half asks for numpy
and skips without it rather than failing the lean job.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))

import pytest
import config
import polish


class Spy:
    """Records the arguments it was constructed with, and returns a marker."""

    def __init__(self, label):
        self.label = label
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return f"<{self.label}>"


@pytest.fixture
def asr_module():
    pytest.importorskip("numpy", reason="asr.py imports numpy at module scope")
    import asr
    return asr


@pytest.fixture
def asr_spies(asr_module, monkeypatch):
    spies = {name: Spy(name) for name in ("ParakeetBackend", "SherpaBackend", "WhisperBackend")}
    for name, spy in spies.items():
        monkeypatch.setattr(asr_module, name, spy)
    return spies


# ---- ASR backend selection ----------------------------------------------

@pytest.mark.parametrize("platform,chosen", [
    ("darwin", "ParakeetBackend"),
    ("win32", "SherpaBackend"),
    ("linux", "SherpaBackend"),
])
def test_platform_picks_the_backend_when_none_is_named(
        asr_module, asr_spies, monkeypatch, platform, chosen):
    monkeypatch.setattr(sys, "platform", platform)

    asr_module.build()

    assert asr_spies[chosen].calls == [()]
    for name, spy in asr_spies.items():
        if name != chosen:
            assert spy.calls == [], f"{name} must not be constructed on {platform}"


def test_explicit_backend_overrides_the_platform(asr_module, asr_spies, monkeypatch):
    """Asking for MLX on Windows should fail at the import, not be silently rerouted."""
    monkeypatch.setattr(sys, "platform", "win32")

    asr_module.build("parakeet")

    assert asr_spies["ParakeetBackend"].calls == [()]
    assert asr_spies["SherpaBackend"].calls == []


def test_model_id_reaches_the_backend(asr_module, asr_spies, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    asr_module.build(None, "/models/custom")

    assert asr_spies["SherpaBackend"].calls == [("/models/custom",)]


def test_whisper_is_still_selectable(asr_module, asr_spies, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    asr_module.build("whisper")
    assert asr_spies["WhisperBackend"].calls == [()]


def test_unknown_backend_is_refused(asr_module, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ValueError, match="unknown ASR backend"):
        asr_module.build("sherpa-onnx")


def test_empty_backend_string_falls_back_to_the_platform(asr_module, asr_spies, monkeypatch):
    """"" is falsy, so it must resolve like None rather than raise."""
    monkeypatch.setattr(sys, "platform", "win32")
    asr_module.build("")
    assert asr_spies["SherpaBackend"].calls == [()]


def test_sherpa_names_the_bootstrap_when_the_model_is_absent(asr_module, tmp_path):
    pytest.importorskip("sherpa_onnx", reason="SherpaBackend imports it before anything else")
    with pytest.raises(FileNotFoundError) as caught:
        asr_module.SherpaBackend(str(tmp_path / "absent"))
    assert "models.py" in str(caught.value)


def test_sherpa_rejects_a_half_unpacked_directory(asr_module, tmp_path):
    pytest.importorskip("sherpa_onnx", reason="SherpaBackend imports it before anything else")
    import models
    for name in models.REQUIRED_FILES[:-1]:
        (tmp_path / name).write_bytes(b"x")

    with pytest.raises(FileNotFoundError):
        asr_module.SherpaBackend(str(tmp_path))


# ---- polish engine selection --------------------------------------------

@pytest.fixture
def engine_spies(monkeypatch):
    spies = {name: Spy(name) for name in ("MlxEngine", "OnnxEngine")}
    for name, spy in spies.items():
        monkeypatch.setattr(polish, name, spy)
    return spies


def test_explicit_mlx_wins_anywhere(engine_spies, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    polish.build_engine("mlx-community/Qwen3-4B-Instruct-2507-4bit", "mlx")
    assert engine_spies["MlxEngine"].calls == [("mlx-community/Qwen3-4B-Instruct-2507-4bit",)]


def test_explicit_onnx_wins_anywhere(engine_spies, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    polish.build_engine("/models/qwen-genai", "onnx")
    assert engine_spies["OnnxEngine"].calls == [("/models/qwen-genai",)]


def test_unknown_engine_is_refused(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ValueError, match="unknown polish engine"):
        polish.build_engine("mlx-community/whatever", "onnxruntime")


def test_a_directory_is_an_onnx_model_on_macos(engine_spies, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    polish.build_engine(str(tmp_path))
    assert engine_spies["OnnxEngine"].calls == [(str(tmp_path),)]
    assert engine_spies["MlxEngine"].calls == []


def test_a_directory_is_an_onnx_model_on_windows(engine_spies, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    polish.build_engine(str(tmp_path))
    assert engine_spies["OnnxEngine"].calls == [(str(tmp_path),)]


def test_repo_id_is_an_mlx_model_on_macos(engine_spies, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    polish.build_engine("mlx-community/Qwen3-4B-Instruct-2507-4bit")
    assert engine_spies["MlxEngine"].calls == [("mlx-community/Qwen3-4B-Instruct-2507-4bit",)]


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_repo_id_off_darwin_explains_itself_instead_of_guessing(
        engine_spies, monkeypatch, platform):
    """The old fallback handed an MLX repo id to a runtime that reports it as a
    missing file, which sends the reader hunting for a download."""
    monkeypatch.setattr(sys, "platform", platform)

    with pytest.raises(ValueError) as caught:
        polish.build_engine("mlx-community/Qwen3-4B-Instruct-2507-4bit")

    message = str(caught.value)
    assert "genai-format" in message
    assert "cleanup_level" in message
    assert engine_spies["MlxEngine"].calls == []
    assert engine_spies["OnnxEngine"].calls == []


def test_default_polish_model_is_loadable_on_the_platform_that_defaults_to_polish(monkeypatch):
    """The shipped default must not be a combination that cannot start.

    On Darwin the default level is polish and the default model is an MLX repo
    id, and those agree. Off Darwin the level defaults to clean, so the same
    model id is never reached.
    """
    monkeypatch.setattr(sys, "platform", "darwin")
    assert config.default_cleanup_level() == "polish"
    assert config.Config().polish_model.startswith("mlx-community/")
    assert config.default_polish_engine() == "mlx"

    monkeypatch.setattr(sys, "platform", "win32")
    assert config.default_cleanup_level() != "polish"
