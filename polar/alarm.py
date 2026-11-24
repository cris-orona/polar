"""Loud failures: shell out to an alarm command that does not depend on Polar.

POLAR_ALARM_BIN is the command (a path, or a path plus fixed arguments). Unset or missing: the alarm
is only written to the log. The command is run as `<bin> --key K --title T --text X`.
It never raises and never blocks the caller for long.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
import time
from typing import Deque, Optional
from collections import deque

log = logging.getLogger("polar")
TIMEOUT = 20


def alarm(key: str, title: str, text: str = "") -> bool:
    """Raise one alarm. True when the command ran and exited 0. Never raises."""
    raw = (os.environ.get("POLAR_ALARM_BIN") or "").strip()
    if not raw:
        log.warning("alarm (no POLAR_ALARM_BIN): %s [%s]", title, key)
        return False
    try:
        cmd = shlex.split(raw) + ["--key", key, "--title", title]
        if text:
            cmd += ["--text", text[:400]]
        ok = subprocess.run(cmd, capture_output=True, timeout=TIMEOUT).returncode == 0
        if not ok:
            log.warning("alarm command failed: %s [%s]", title, key)
        return ok
    except Exception as exc:  # a broken alarm must not break Polar
        log.error("alarm failed (%s): %s [%s]", type(exc).__name__, title, key)
        return False


class Burst:
    """Count events in a time window. `hit()` is True when `n` events fell inside `seconds`."""

    def __init__(self, n: int = 3, seconds: float = 300.0) -> None:
        self.n, self.seconds = n, seconds
        self.times: Deque[float] = deque()

    def hit(self, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        self.times.append(now)
        while self.times and now - self.times[0] > self.seconds:
            self.times.popleft()
        return len(self.times) >= self.n
