"""Configuration. One JSON file, no schema library, sensible defaults."""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
from dataclasses import asdict, dataclass, field

log = logging.getLogger("lippy.config")

def _support_dir() -> pathlib.Path:
    """Where config, logs and (on macOS) the socket live.

    macOS keeps the location it has always used. Windows uses %LOCALAPPDATA%
    rather than the roaming half of AppData deliberately: a log and a socket
    path describe one machine, and roaming them onto a second machine would
    carry claims that are not true there.
    """
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        base = pathlib.Path(local) if local else pathlib.Path.home() / "AppData" / "Local"
        return base / "Lippy"
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "Lippy"
    xdg = os.environ.get("XDG_DATA_HOME")
    root = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "share"
    return root / "lippy"


SUPPORT_DIR = _support_dir()
CONFIG_PATH = SUPPORT_DIR / "config.json"
SOCKET_PATH = SUPPORT_DIR / "lippyd.sock"
LOG_PATH = SUPPORT_DIR / "lippyd.log"

# Empty by design. This is where proper nouns the speech model has never seen
# get corrected -- names, acronyms, jargon, product names. Add your own to
# `dictionary` in config.json as you catch mistakes:
#
#     "dictionary": { "nice f": "NYSCEF", "cue three": "Q3" }
#
# Matching is case-insensitive and on word boundaries, longest key first.
DEFAULT_DICTIONARY: dict[str, str] = {}


# Also empty by design, and the companion to the dictionary above. A dictionary
# entry fixes one spelling you have already seen. A protected term is written
# once in the form you want, and anything close enough to it is snapped onto
# that form, which is what an invented name needs when it comes back differently
# every time it is spoken:
#
#     "protected_terms": ["Ravenscroft", "Bellhaven Group"]
#
# How safe this is depends on the term. A distinctive name collides with almost
# nothing. A short one that looks like ordinary English collides with a great
# deal, and no threshold setting repairs that. Run `lippyctl terms` before
# trusting an entry: it reports which real words the entry would rewrite.
DEFAULT_PROTECTED_TERMS: list[str] = []


# Cleanup is a dial, not a switch. Each step costs more than the last, and the
# top one is the only step that needs a language model at all -- which is what
# makes the lower steps viable on hardware where a 4B model is not.
#
#   raw      what the speech model heard, untouched
#   fillers  drop um / uh / er
#   clean    + stutters, false starts, punctuation, capitals, your dictionary
#   polish   + an LLM pass over the result
#
# Everything below "polish" is deterministic regex: sub-millisecond, no model,
# and it ports to any language or platform as plain logic.
CLEANUP_LEVELS = ("raw", "fillers", "clean", "polish")


# Backends follow the platform, because the wrong one does not fail gracefully:
# MLX has no Windows build at all, and the ONNX runtime cannot load the MLX
# export the macOS install already has on disk. Both stay importable
# everywhere so the shared code keeps one set of tests rather than a fork.
def default_asr_backend() -> str:
    return "parakeet" if sys.platform == "darwin" else "sherpa"


def default_polish_engine() -> str:
    return "mlx" if sys.platform == "darwin" else "onnx"


def default_cleanup_level() -> str:
    """Off Darwin the dial ships at "clean": deterministic, and no second model.

    Polish is not refused there, it is only not the default, because reaching it
    means supplying a genai-format model directory that no install step provides
    yet. Defaulting to a level that cannot load is the silent-failure shape this
    repo keeps paying for elsewhere.
    """
    return "polish" if sys.platform == "darwin" else "clean"


@dataclass
class Config:
    asr_backend: str = field(default_factory=default_asr_backend)
    asr_model: str | None = None

    cleanup_level: str = field(default_factory=default_cleanup_level)
    polish_model: str = "mlx-community/Qwen3-4B-Instruct-2507-4bit"

    strip_fillers: bool = True
    aggressive_fillers: bool = False
    collapse_stutters: bool = True
    spoken_commands: bool = True

    # Join a dictated address into a written one, so "example dot com" arrives
    # as "example.com" rather than as three words.
    spoken_urls: bool = True

    # Join "<name> at <host>" into an address. Off by default: it can only fire
    # when the speech model got the local part right, and when it did not the
    # result still reads as a valid address. A plausible wrong address is worse
    # than visibly unfinished text, so this is opt-in. An email cue word is
    # required in the utterance, which is what keeps "look at example.com" and
    # "the docs are at example.com" as prose.
    spoken_emails: bool = False

    dictionary: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DICTIONARY))
    protected_terms: list[str] = field(
        default_factory=lambda: list(DEFAULT_PROTECTED_TERMS))

    # Write dictated numbers as digits. Off by default because it is a policy
    # rather than a correction, and a wrong one is a changed number.
    #
    # A run of number words is read three ways depending on what surrounds it.
    # After a phrase in `digit_triggers`, or standing alone, or read out in
    # pieces, it is an identifier and the digits run together: "ten fifty one"
    # becomes 1051. With a preposition in front or a meridiem behind it is a
    # clock reading: "at nine thirty" becomes 9:30. Otherwise it is a quantity,
    # and only values above `number_word_max` become digits.
    spoken_numbers: bool = False
    number_word_max: int = 12
    digit_triggers: list[str] = field(default_factory=list)

    # How close a span must be to a protected term before it is rewritten, from
    # 0 to 1. Raising it accepts fewer mis-hearings, lowering it risks ordinary
    # words. The default was chosen against a 235,976 word system dictionary.
    fuzzy_threshold: float = 0.80

    # Utterances kept in memory so a failed paste is recoverable via
    # `lippyctl last`. Deliberately never written to disk: dictation is the most
    # sensitive text on the machine, and a plaintext log of it is a liability
    # that a local-first tool has no reason to create.
    history_size: int = 20

    # The Windows hotkey, by the name the tray menu shows. macOS does not read
    # these: there the Swift app owns the hotkey and keeps its choice in
    # UserDefaults, because the app rather than the Python holds the keyboard.
    # Two stores while that is true is more honest than one store that only one
    # platform obeys.
    #
    # Right Control is the default for the reason recorded in hotkey_state: Right
    # Alt is AltGr on international layouts, and AltGr also synthesises a Left
    # Control, so both of those are out. An empty latch_key turns latching off.
    hotkey: str = "Right Control"
    latch_key: str = "Right Shift"

    @classmethod
    def load(cls, path: pathlib.Path = CONFIG_PATH) -> "Config":
        if not path.exists():
            return cls()
        data = cls._migrate(json.loads(path.read_text()))

        # Warn rather than refuse. Rejecting unknown keys catches typos, but it
        # also means any renamed setting stops the daemon dead on an existing
        # install -- which is exactly what happened when polish_enabled became
        # cleanup_level. A tool that will not start is a worse failure than a
        # setting silently ignored, and the warning is still there to find.
        known = set(cls.__dataclass_fields__)
        for key in sorted(set(data) - known):
            log.warning("ignoring unknown config key %r in %s", key, path)
            data.pop(key)

        level = data.get("cleanup_level")
        if level is not None and level not in CLEANUP_LEVELS:
            fallback = default_cleanup_level()
            log.warning("unknown cleanup_level %r; falling back to %r", level, fallback)
            data["cleanup_level"] = fallback

        return cls(**data)

    @staticmethod
    def _migrate(data: dict) -> dict:
        """Translate settings from older versions in place."""
        # polish_enabled (bool) became one step on the cleanup dial.
        if "polish_enabled" in data:
            if "cleanup_level" not in data:
                data["cleanup_level"] = "polish" if data["polish_enabled"] else "clean"
            data.pop("polish_enabled")
        return data

    def save(self, path: pathlib.Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")

    def rule_config(self, level: str | None = None):
        """Rules shaped by the dial. "raw" is genuinely raw -- no rules run."""
        from rules import RuleConfig

        level = level or self.cleanup_level
        if level == "fillers":
            return RuleConfig(
                strip_fillers=self.strip_fillers,
                aggressive_fillers=self.aggressive_fillers,
                collapse_stutters=False,
                spoken_commands=False,
                spoken_urls=False,
                spoken_emails=False,
                dictionary={},
                protected_terms=[],
                spoken_numbers=False,
            )
        return RuleConfig(
            strip_fillers=self.strip_fillers,
            aggressive_fillers=self.aggressive_fillers,
            collapse_stutters=self.collapse_stutters,
            spoken_commands=self.spoken_commands,
            spoken_urls=self.spoken_urls,
            spoken_emails=self.spoken_emails,
            dictionary=self.dictionary,
            protected_terms=self.protected_terms,
            fuzzy_threshold=self.fuzzy_threshold,
            spoken_numbers=self.spoken_numbers,
            number_word_max=self.number_word_max,
            digit_triggers=self.digit_triggers,
        )


    def hotkey_vks(self) -> tuple[int, int | None]:
        """The configured hotkey as virtual key codes, falling back loudly.

        Warns and uses the default rather than refusing to start, the same way
        an unknown cleanup_level does. A tool that will not launch because one
        setting is misspelled is a worse failure than one that launches on the
        default and says so, and here the failure would take the whole hotkey
        with it.
        """
        from hotkey_state import DEFAULT_LATCH_VK, DEFAULT_PRIMARY_VK, KEYS

        primary = KEYS.get(self.hotkey)
        if primary is None:
            log.warning("unknown hotkey %r; falling back to Right Control. "
                        "Known keys: %s", self.hotkey, ", ".join(KEYS))
            primary = DEFAULT_PRIMARY_VK

        if not self.latch_key:
            return primary, None

        latch = KEYS.get(self.latch_key)
        if latch is None:
            log.warning("unknown latch_key %r; falling back to Right Shift. "
                        "Known keys: %s", self.latch_key, ", ".join(KEYS))
            latch = DEFAULT_LATCH_VK
        return primary, latch


def socket_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("LIPPY_SOCKET", SOCKET_PATH))
