"""mace.cli.config -- credentials as a plain, inspectable .env file, not an
opaque saved blob.

`init` writes one (default ``~/.mace/.env``); `shell` requires ``--api``
pointing at one, every launch. Mandatory, not auto-loaded, on purpose: a
live user hit a stale/wrong saved key and had no easy way to see what was
actually being used -- a plain-text file at a path you chose and can `cat`
is easier to debug than a JSON blob `shell` silently reads on your behalf.

The exact environment variable a given backend's underlying CHIA LLM class
reads for its own credential was not independently verified against every
backend's source for this feature -- ``BACKEND_ENV_VARS`` below is a
best-effort mapping. `init` prints exactly which variable it wrote so a
user whose backend expects something different can see that immediately
rather than have it fail silently three commands later.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

CONFIG_DIR = Path.home() / ".mace"
DEFAULT_ENV_PATH = CONFIG_DIR / ".env"

# Best-effort per-backend env var name -- see module docstring.
BACKEND_ENV_VARS: dict[str, str] = {
    "opencode": "OPENCODE_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "antigravity": "ANTIGRAVITY_API_KEY",
    "vertex": "GOOGLE_APPLICATION_CREDENTIALS",
}


def write_env_file(backend: str, api_key: str, path: Path = DEFAULT_ENV_PATH) -> Path:
    """Write ``<VAR>=<api_key>`` for *backend*'s env var to *path*, owner-only
    permissions. Returns the path written."""
    env_var = BACKEND_ENV_VARS.get(backend, "MACE_LLM_API_KEY")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{env_var}={api_key}\n")
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def load_env_file(path: str | Path) -> dict[str, str]:
    """Parse a plain ``KEY=VALUE``-per-line file. Blank lines and lines
    starting with ``#`` are skipped; a value may be wrapped in matching
    quotes. Raises FileNotFoundError (with the path in the message) if
    *path* doesn't exist -- the caller reports that, this stays pure."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"no .env file at {p}")
    env: dict[str, str] = {}
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            env[key] = value
    return env


def apply_env_to_environment(env: dict[str, str]) -> None:
    """Set every key in *env* into ``os.environ``, verbatim."""
    os.environ.update(env)
