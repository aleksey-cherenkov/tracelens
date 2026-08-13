"""API key resolution.

Three sources, highest precedence first:

1. --api-key on the command line   (one-off, never persisted)
2. ANTHROPIC_API_KEY in the environment
3. ANTHROPIC_API_KEY in a .env file at the repo root

The .env file exists so "add a key" and "clear a key" are both a single visible
action on a single file, rather than a shell incantation whose effect depends on
which terminal you happen to be in. It is gitignored. Keys are never printed in
full and never written to examples/ or any other output.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ENV_VAR = "ANTHROPIC_API_KEY"
REPO_ROOT = Path(__file__).resolve().parent.parent
DOTENV = REPO_ROOT / ".env"


@dataclass(frozen=True)
class KeyStatus:
    key: str | None
    source: str
    """'flag', 'environment', '.env', or 'none'."""

    @property
    def present(self) -> bool:
        return bool(self.key)

    @property
    def masked(self) -> str:
        """Enough to recognise which key it is, not enough to use it."""
        if not self.key:
            return "not set"
        if len(self.key) <= 12:
            return f"…{self.key[-4:]} ({len(self.key)} chars)"
        return f"{self.key[:7]}…{self.key[-4:]} ({len(self.key)} chars)"

    @property
    def looks_valid(self) -> bool:
        return bool(self.key) and self.key.startswith("sk-ant-") and len(self.key) > 40


def read_dotenv(path: Path | None = None) -> dict[str, str]:
    """Minimal parser -- no dependency, no shell evaluation, no surprises.

    The default is resolved at call time, not bound at import time: a module-level
    default would freeze the path and quietly ignore any later override.
    """
    path = path or DOTENV
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        values[name.strip()] = value.strip().strip("'\"")
    return values


def resolve(explicit: str | None = None) -> KeyStatus:
    if explicit:
        return KeyStatus(explicit.strip(), "flag")

    from_env = os.environ.get(ENV_VAR, "").strip()
    if from_env:
        return KeyStatus(from_env, "environment")

    from_file = read_dotenv().get(ENV_VAR, "").strip()
    if from_file:
        return KeyStatus(from_file, ".env")

    return KeyStatus(None, "none")


def write_dotenv_key(key: str, path: Path | None = None) -> Path:
    """Set the key in .env, preserving any other keys already in the file."""
    path = path or DOTENV
    values = read_dotenv(path)
    values[ENV_VAR] = key.strip()
    body = "\n".join(f"{name}={value}" for name, value in sorted(values.items()))
    path.write_text(
        "# Local secrets. Gitignored -- do not commit.\n" + body + "\n", encoding="utf-8"
    )
    try:
        path.chmod(0o600)  # best effort; a no-op on Windows
    except OSError:
        pass
    return path


def clear_dotenv_key(path: Path | None = None) -> bool:
    """Remove the key from .env. Deletes the file if nothing else is left."""
    path = path or DOTENV
    values = read_dotenv(path)
    if ENV_VAR not in values:
        return False
    del values[ENV_VAR]
    if values:
        body = "\n".join(f"{name}={value}" for name, value in sorted(values.items()))
        path.write_text(body + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return True


def sdk_available() -> bool:
    """Is the optional anthropic SDK importable?

    Checks sys.modules first: find_spec raises ValueError for a module that is
    already imported but carries no __spec__, which is a real condition for
    namespace packages and injected test doubles alike. A probe for an optional
    dependency should never be the thing that raises.
    """
    import sys

    if "anthropic" in sys.modules:
        return True
    try:
        from importlib.util import find_spec

        return find_spec("anthropic") is not None
    except (ImportError, ValueError):
        return False


SHELL_HELP = {
    "PowerShell (this session only)": [
        '$env:ANTHROPIC_API_KEY = "sk-ant-..."      # set',
        "$env:ANTHROPIC_API_KEY = $null              # clear",
    ],
    "PowerShell (persistent, new terminals)": [
        '[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", "sk-ant-...", "User")',
        '[Environment]::SetEnvironmentVariable("ANTHROPIC_API_KEY", $null, "User")',
    ],
    "cmd.exe": [
        "set ANTHROPIC_API_KEY=sk-ant-...            # this session",
        "setx ANTHROPIC_API_KEY sk-ant-...           # persistent",
        "set ANTHROPIC_API_KEY=                      # clear this session",
    ],
    "bash / zsh": [
        'export ANTHROPIC_API_KEY="sk-ant-..."       # set',
        "unset ANTHROPIC_API_KEY                     # clear",
    ],
}
