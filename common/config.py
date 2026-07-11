import os
from pathlib import Path
import yaml


def load_config(path: str | Path) -> dict:
    path = Path(path)
    with path.open() as f:
        cfg = yaml.safe_load(f) or {}
    _resolve_env_refs(cfg)
    return cfg


def resolve_settings_path(start: Path | None = None) -> Path:
    """Return the best available settings YAML path.

    Looks for `config/settings.yaml` (real creds, gitignored) first; falls back
    to `config/settings.example.yaml` (committed template). If neither exists,
    raises FileNotFoundError."""
    if start is None:
        # Default to the repo root (two levels up from this file).
        start = Path(__file__).resolve().parents[1]
    real = start / "config" / "settings.yaml"
    example = start / "config" / "settings.example.yaml"
    if real.exists():
        return real
    if example.exists():
        return example
    raise FileNotFoundError(
        f"no config found at {real.parent} (looked for settings.yaml then settings.example.yaml)"
    )


def _resolve_env_refs(cfg: dict) -> None:
    for section in cfg.values():
        if not isinstance(section, dict):
            continue
        for key in list(section.keys()):
            if key.endswith("_env"):
                env_var = section.pop(key)
                actual_key = key[:-4]
                section[actual_key] = os.environ.get(env_var, "")
