"""Platform branches: support directory, backend defaults, cleanup level.

macOS is asserted beside Windows in nearly every case here. The risk in this
change was never that Windows picks wrong -- that fails loudly on the first
run -- it is that macOS shifts underneath a change nobody was testing it for.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))

import json
import logging

import pytest
import config


# ---- support directory --------------------------------------------------

def test_windows_support_dir_uses_localappdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\ada\AppData\Local")
    assert config._support_dir() == pathlib.Path(r"C:\Users\ada\AppData\Local") / "Lippy"


def test_windows_support_dir_falls_back_when_localappdata_absent(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: pathlib.Path("/home/ada")))
    assert config._support_dir() == pathlib.Path("/home/ada/AppData/Local/Lippy")


def test_windows_support_dir_treats_empty_localappdata_as_absent(monkeypatch):
    """An empty variable is set-but-useless, and joining onto it yields C:\\Lippy."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: pathlib.Path("/home/ada")))
    assert config._support_dir() == pathlib.Path("/home/ada/AppData/Local/Lippy")


def test_macos_support_dir_unchanged(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: pathlib.Path("/Users/ada")))
    assert config._support_dir() == pathlib.Path(
        "/Users/ada/Library/Application Support/Lippy")


def test_macos_support_dir_ignores_windows_variables(monkeypatch):
    """A LOCALAPPDATA inherited from a shell must not reroute the macOS path."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\ada\AppData\Local")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: pathlib.Path("/Users/ada")))
    assert config._support_dir() == pathlib.Path(
        "/Users/ada/Library/Application Support/Lippy")


def test_linux_support_dir_honours_xdg(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/var/data")
    assert config._support_dir() == pathlib.Path("/var/data/lippy")


def test_linux_support_dir_without_xdg(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: pathlib.Path("/home/ada")))
    assert config._support_dir() == pathlib.Path("/home/ada/.local/share/lippy")


# ---- backend and level defaults -----------------------------------------

@pytest.mark.parametrize("platform,asr,engine,level", [
    ("darwin", "parakeet", "mlx", "polish"),
    ("win32", "sherpa", "onnx", "clean"),
    ("linux", "sherpa", "onnx", "clean"),
])
def test_platform_defaults(monkeypatch, platform, asr, engine, level):
    monkeypatch.setattr(sys, "platform", platform)
    assert config.default_asr_backend() == asr
    assert config.default_polish_engine() == engine
    assert config.default_cleanup_level() == level


def test_config_instance_follows_platform(monkeypatch):
    """The dataclass defaults are factories, so they resolve per construction."""
    monkeypatch.setattr(sys, "platform", "win32")
    windows = config.Config()
    monkeypatch.setattr(sys, "platform", "darwin")
    macos = config.Config()

    assert (windows.asr_backend, windows.cleanup_level) == ("sherpa", "clean")
    assert (macos.asr_backend, macos.cleanup_level) == ("parakeet", "polish")


def test_every_default_level_is_a_real_level(monkeypatch):
    for platform in ("darwin", "win32", "linux", "freebsd13"):
        monkeypatch.setattr(sys, "platform", platform)
        assert config.default_cleanup_level() in config.CLEANUP_LEVELS


# ---- load, migrate, save ------------------------------------------------

def test_unknown_level_falls_back_to_platform_default(monkeypatch, tmp_path, caplog):
    """The old code hardcoded 'polish' here, which is unreachable on Windows."""
    monkeypatch.setattr(sys, "platform", "win32")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"cleanup_level": "immaculate"}))

    with caplog.at_level("WARNING"):
        loaded = config.Config.load(path)

    assert loaded.cleanup_level == "clean"
    assert "immaculate" in caplog.text


def test_unknown_level_still_falls_back_to_polish_on_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"cleanup_level": "immaculate"}))
    assert config.Config.load(path).cleanup_level == "polish"


def test_migration_survives_on_windows(monkeypatch, tmp_path):
    """polish_enabled predates every platform branch and must still translate."""
    monkeypatch.setattr(sys, "platform", "win32")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"polish_enabled": False, "asr_backend": "sherpa"}))

    loaded = config.Config.load(path)

    assert loaded.cleanup_level == "clean"
    assert not hasattr(loaded, "polish_enabled")


def test_explicit_backend_in_file_beats_platform_default(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"asr_backend": "whisper"}))
    assert config.Config.load(path).asr_backend == "whisper"


def test_missing_file_returns_platform_defaults(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    loaded = config.Config.load(tmp_path / "absent.json")
    assert (loaded.asr_backend, loaded.cleanup_level) == ("sherpa", "clean")


def test_save_round_trips_a_windows_config(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    path = tmp_path / "nested" / "config.json"
    config.Config().save(path)

    assert path.is_file(), "save must create the parent directory"
    written = json.loads(path.read_text())
    assert written["asr_backend"] == "sherpa"
    assert written["cleanup_level"] == "clean"
    assert config.Config.load(path).asr_backend == "sherpa"


# ---- the Windows hotkey binding ------------------------------------------

def test_the_default_binding_is_right_control_and_right_shift():
    """Right Alt is AltGr and Left Control is what AltGr fakes, so the default
    is the one modifier that is inert on its own and untouched by either."""
    assert config.Config().hotkey_vks() == (0xA3, 0xA1)


def test_a_configured_binding_resolves_to_its_virtual_key_codes():
    cfg = config.Config(hotkey="Left Alt", latch_key="Left Shift")
    assert cfg.hotkey_vks() == (0xA4, 0xA0)


def test_an_empty_latch_key_turns_latching_off():
    assert config.Config(latch_key="").hotkey_vks() == (0xA3, None)


def test_an_unknown_hotkey_warns_and_falls_back_rather_than_refusing(caplog):
    """Refusing to start over one misspelled setting would take the whole
    hotkey with it, which is worse than starting on the default and saying so."""
    cfg = config.Config(hotkey="Super Key")
    with caplog.at_level(logging.WARNING, logger="lippy.config"):
        assert cfg.hotkey_vks() == (0xA3, 0xA1)
    assert "unknown hotkey" in caplog.text
    assert "Right Control" in caplog.text


def test_an_unknown_latch_key_falls_back_on_its_own(caplog):
    cfg = config.Config(hotkey="Left Alt", latch_key="Middle Shift")
    with caplog.at_level(logging.WARNING, logger="lippy.config"):
        assert cfg.hotkey_vks() == (0xA4, 0xA1)
    assert "unknown latch_key" in caplog.text


def test_the_binding_survives_a_save_and_load(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    path = tmp_path / "config.json"
    config.Config(hotkey="Left Control", latch_key="").save(path)
    loaded = config.Config.load(path)
    assert (loaded.hotkey, loaded.latch_key) == ("Left Control", "")
    assert loaded.hotkey_vks() == (0xA2, None)


def test_every_bindable_key_resolves():
    """A name the tray menu can offer that config cannot resolve would be a
    setting the user can choose and the hotkey cannot honour."""
    import hotkey_state

    for name in hotkey_state.KEYS:
        primary, _ = config.Config(hotkey=name).hotkey_vks()
        assert primary == hotkey_state.KEYS[name], name
