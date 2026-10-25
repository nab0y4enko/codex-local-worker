"""Best-effort macOS notifications; callers redact user/job data before invoking."""
from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Callable


_SCRIPT = "display notification (item 2 of argv) with title (item 1 of argv)"
_MAX_TEXT = 200
_log = logging.getLogger(__name__)


def _bounded(value: Any) -> str:
    text = " ".join(str(value).replace("\x00", " ").split())
    return text[:_MAX_TEXT]


def notify(kind: str, title: str, message: str, *,
           run: Callable[..., Any] = subprocess.run) -> bool:
    """Post a notification without shell interpolation; never make job success depend on it."""
    if os.environ.get("LOCAL_WORKER_NOTIFY", "1").strip() == "0":
        return False
    # ``kind`` is intentionally not interpolated into the AppleScript. It is a caller-side
    # category, not content to execute or display.
    del kind
    argv = ["/usr/bin/osascript", "-e", "on run argv", "-e", _SCRIPT, "-e", "end run",
            _bounded(title), _bounded(message)]
    try:
        result = run(argv, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     timeout=5)
        if getattr(result, "returncode", 1) == 0:
            return True
    except Exception:  # Notification failures are advisory and must never fail a job.
        pass
    _log.warning("macOS notification failed")
    return False
