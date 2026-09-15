"""mace.cli.config -- where `init` puts the API key so later commands (and
later shell sessions) don't have to ask again.

Stored as plain JSON under ``~/.mace/config.json``, permissioned owner-only
(chmod 0600) immediately after writing -- not OS-keychain-backed. That's a
real, deliberate tradeoff for a hackathon timeline, not an oversight: the
``keyring`` package would be the stronger answer if this ever needs to be
more than a local dev tool. Documented here so it isn't a silent gap.

The exact environment variable a given backend's underlying CHIA LLM class
reads for its own credential was not independently verified against every
backend's source for this feature -- ``BACKEND_ENV_VARS`` below is a
best-effort mapping, applied by setting the env var before any LLM call, and
`init` prints exactly which variable it set so a user whose backend expects
something different can see that immediately rather than have it fail
silently three commands later.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

CONFIG_DIR = Path.home() / ".mace"
CONFIG_PATH = CONFIG_DIR / "config.json"

# Best-effort per-backend env var name -- see module docstring.
BACKEND_ENV_VARS: dict[str, str] = {
    "opencode": "OPENCODE_API_KEY",
    "claude": "ANTHROPIC_API_KEY",
    "antigravity": "ANTIGRAVITY_API_KEY",
    "vertex": "GOOGLE_APPLICATION_CREDENTIALS",
}


def save_config(backend: str, api_key: str) -> Path:
    """Write ``{backend, api_key}`` to :data:`CONFIG_PATH`, owner-only
    permissions. Returns the path written."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps({"backend": backend, "api_key": api_key}))
    os.chmod(CONFIG_PATH, stat.S_IRUSR | stat.S_IWUSR)
    return CONFIG_PATH


def load_config() -> dict | None:
    """The saved ``{backend, api_key}``, or ``None`` if `init` has never run."""
    if not CONFIG_PATH.exists():
        return None
    return json.loads(CONFIG_PATH.read_text())


def apply_config_to_environment(config: dict) -> str:
    """Set the right env var for *config*'s backend from its api_key.

    Returns the env var name that was set, so the caller can tell the user
    exactly what happened (see module docstring on why this matters).
    """
    backend = config["backend"]
    env_var = BACKEND_ENV_VARS.get(backend, "MACE_LLM_API_KEY")
    os.environ[env_var] = config["api_key"]
    os.environ.setdefault("MACE_LLM", backend)
    return env_var
