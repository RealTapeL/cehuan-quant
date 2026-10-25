from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "configs" / "default.toml"


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = Path(path) if path else DEFAULT_CONFIG
    with config_path.open("rb") as handle:
        return tomllib.load(handle)


def overlay_strategy(config: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    """Return an isolated config copy with non-None strategy overrides."""
    copied = {key: dict(value) if isinstance(value, dict) else value for key, value in config.items()}
    for section in ("strategy", "market", "costs", "plan"):
        if section in copied:
            copied[section] = dict(copied[section])
    for key, value in overrides.items():
        if value is None:
            continue
        for section in ("strategy", "market", "costs", "plan"):
            if key in copied.get(section, {}):
                copied[section][key] = value
                break
    return copied
