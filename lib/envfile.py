from __future__ import annotations

from pathlib import Path


def load_env_file(path: Path) -> dict[str, str]:
    """
    Minimal .env loader.

    - Supports KEY=VALUE lines
    - Ignores blank lines and lines starting with '#'
    - Does not perform shell expansion
    """
    if not path.exists():
        return {}

    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip("'").strip('"')
        if k:
            out[k] = v
    return out


def apply_env(overrides: dict[str, str], *, into: dict[str, str]) -> None:
    """
    Apply env vars only when missing/blank.
    """
    for k, v in overrides.items():
        if not v:
            continue
        cur = into.get(k, "").strip()
        if not cur:
            into[k] = v

