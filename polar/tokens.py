"""Read the three tokens. Keychain first, then a private file, then an env var. Never print a token.

The file is POLAR_TOKEN_DIR/<name>-token (default ~/.config/polar). Polar uses it only when the
file mode is 0600 and the folder mode is 0700, so no other user can read it."""
from __future__ import annotations

import getpass
import os
import stat
import subprocess
from typing import Optional

TOKENS = {
    "bot": ("polar-bot-token", "POLAR_BOT_TOKEN", "xoxb-"),
    "app": ("polar-app-token", "POLAR_APP_TOKEN", "xapp-"),
    "user": ("polar-user-token", "POLAR_USER_TOKEN", "xoxp-"),
}


def _keychain(service: str) -> Optional[str]:
    account = os.environ.get("USER") or getpass.getuser()
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return value if out.returncode == 0 and value else None


def _file(name: str) -> Optional[str]:
    folder = os.environ.get("POLAR_TOKEN_DIR") or os.path.expanduser("~/.config/polar")
    path = os.path.join(folder, f"{name}-token")
    try:
        if stat.S_IMODE(os.stat(folder).st_mode) != 0o700 or stat.S_IMODE(os.stat(path).st_mode) != 0o600:
            return None
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
    except OSError:
        return None
    return value or None


def get_token(name: str) -> Optional[str]:
    service, env, prefix = TOKENS[name]
    value = _keychain(service) or _file(name) or (os.environ.get(env) or "").strip() or None
    if value and not value.startswith(prefix):
        return None  # wrong shape: treat as missing
    return value
