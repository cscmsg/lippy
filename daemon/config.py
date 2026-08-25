"""Configuration. One JSON file, no schema library, sensible defaults."""

from __future__ import annotations

import json
import os
import pathlib
from dataclasses import asdict, dataclass, field

SUPPORT_DIR = pathlib.Path.home() / "Library" / "Application Support" / "LocalFlow"
CONFIG_PATH = SUPPORT_DIR / "config.json"
SOCKET_PATH = SUPPORT_DIR / "flowd.sock"
LOG_PATH = SUPPORT_DIR / "flowd.log"

# Proper nouns Parakeet has no reason to know. Extend this in the config file;
# these are seeded so the first run already knows the words used daily here.
DEFAULT_DICTIONARY = {
    "lex cloak": "Lex Cloak",
    "lexcloak": "Lex Cloak",
    "lex clock": "Lex Cloak",
    "lex cloke": "Lex Cloak",
    "monty home": "Monty Home",
    "montyhome": "Monty Home",
    "nice f": "NYSCEF",
    "nyscef": "NYSCEF",
    "i app": "IAPP",
    "sipped": "CIPT",
    "kanban": "Kanban",
}


@dataclass
class Config:
    asr_backend: str = "parakeet"
    asr_model: str | None = None

    # Live preview in the HUD while you speak. Costs a little GPU during
    # recording and changes nothing about the delivered text.
    streaming_preview: bool = True

    polish_enabled: bool = True
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
        data = json.loads(path.read_text())
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"unknown config keys in {path}: {sorted(unknown)}")
        return cls(**data)

    def save(self, path: pathlib.Path = CONFIG_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n")

    def rule_config(self):
        from rules import RuleConfig
        return RuleConfig(
            strip_fillers=self.strip_fillers,
            aggressive_fillers=self.aggressive_fillers,
            collapse_stutters=self.collapse_stutters,
            spoken_commands=self.spoken_commands,
            dictionary=self.dictionary,
        )


def socket_path() -> pathlib.Path:
    return pathlib.Path(os.environ.get("LOCALFLOW_SOCKET", SOCKET_PATH))
