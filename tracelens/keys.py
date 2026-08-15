"""API key resolution.

Three sources, highest precedence first: `--api-key`, `ANTHROPIC_API_KEY` in the
environment, then `ANTHROPIC_API_KEY` in a gitignored `.env` at the repo root.

An earlier version of this file was 153 lines — it could write and clear the
`.env`, and printed per-shell instructions for four shells. That is product
polish on a take-home. Setting an environment variable is not a feature anyone
needs help with; a key never being *printed* is.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "ANTHROPIC_API_KEY"
DOTENV = Path(__file__).resolve().parent.parent / ".env"


@dataclass(frozen=True)
class KeyStatus:
    key: str | None
    source: str

    @property
    def present(self) -> bool:
        return bool(self.key)

    @property
    def masked(self) -> str:
        """Enough to recognise which key it is, not enough to use it."""
        if not self.key:
            return "not set"
        return f"{self.key[:7]}...{self.key[-4:]} ({len(self.key)} chars)"


def read_dotenv(path: Path | None = None) -> dict[str, str]:
    """Minimal parser — no dependency, no shell evaluation, no surprises.

    The default resolves at call time rather than binding at import: a
    module-level default freezes the path and silently ignores a later override,
    which is a bug I actually shipped here once.
    """
    path = path or DOTENV
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            values[name.strip()] = value.strip().strip("\"'")
    return values


def resolve(explicit: str | None = None) -> KeyStatus:
    if explicit:
        return KeyStatus(explicit, "flag")
    if os.environ.get(ENV_VAR):
        return KeyStatus(os.environ[ENV_VAR], "environment")
    if read_dotenv().get(ENV_VAR):
        return KeyStatus(read_dotenv()[ENV_VAR], ".env")
    return KeyStatus(None, "none")


def sdk_available() -> bool:
    """True if the optional extra is installed.

    `importlib.util.find_spec` raises rather than returning None when a parent
    package is missing, so this imports and catches instead.
    """
    try:
        import anthropic  # noqa: F401
    except Exception:
        return False
    return True
