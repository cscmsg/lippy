"""Configuration. One JSON file, no schema library, sensible defaults."""

from __future__ import annotations

import json
import logging
import os
import pathlib
from dataclasses import asdict, dataclass, field

log = logging.getLogger("localflow.config")

SUPPORT_DIR = pathlib.Path.home() / "Library" / "Application Support" / "LocalFlow"
CONFIG_PATH = SUPPORT_DIR / "config.json"
SOCKET_PATH = SUPPORT_DIR / "flowd.sock"
LOG_PATH = SUPPORT_DIR / "flowd.log"

# Empty by design. This is where proper nouns the speech model has never seen
# get corrected -- names, acronyms, jargon, product names. Add your own to
# `dictionary` in config.json as you catch mistakes:
#
#     "dictionary": { "nice f": "NYSCEF", "cue three": "Q3" }
#
# Matching is case-insensitive and on word boundaries, longest key first.
DEFAULT_DICTIONARY: dict[str, str] = {}


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


@dataclass
class Config:
    asr_backend: str = "parakeet"
    asr_model: str | None = None

    cleanup_level: str = "polish"
    polish_model: str = "mlx-community/Qwen3-4B-Instruct-2507-4bit"

    strip_fillers: bool = True
    aggressive_fillers: bool = False
    collapse_stutters: bool = True
    spoken_commands: bool = True
    dictionary: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_DICTIONARY))

    # Utterances kept in memory so a failed paste is recoverable via
    # `flowctl last`. Deliberately never written to disk: dictation is the most
    # sensitive text on the machine, and a plaintext log of it is a liability
    # that a local-first tool has no reason to create.
    history_size: int = 20

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
            log.warning("unknown cleanup_level %r; falling back to 'polish'", level)
            data["cleanup_level"] = "polish"

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
                dictionary={},
            )
        return RuleConfig(
            strip_fillers=self.strip_fillers,
            aggressive_fillers=self.aggressive_fillers,
            collapse_stutters=self.collapse_stutters,
            spoken_commands=self.spoken_commands,
            dictionary=self.dictionary,
        )


def socket_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("LOCALFLOW_SOCKET", SOCKET_PATH))
