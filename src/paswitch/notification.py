"""Desktop notifications through the freedesktop `notify-send` utility.

This lives outside `paswitch.audio` because sending a notification has
nothing to do with sound. Notifications are best-effort: anything that goes
wrong here is logged and swallowed so it never disrupts an audio switch.

To avoid stacking a fresh popup every time the user taps the switch key, the
id of the last notification we sent is remembered (under
`$XDG_RUNTIME_DIR/paswitch/`) and passed back via `--replace-id`, so the
next notification supersedes the previous one in place instead of piling up.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Literal

from . import paths

logger = logging.getLogger(__name__)

# Notification urgency levels defined by the freedesktop notification spec.
Urgency = Literal["low", "normal", "critical"]

_STATE_FILENAME = "notification-id"


def _state_path() -> Path | None:
    """Where to remember the id of the last notification, or `None`.

    `None` when no per-user runtime directory is available (no usable
    `$XDG_RUNTIME_DIR`): id persistence is then skipped and notifications
    stack instead of replacing in place.
    """
    runtime = paths.runtime_dir()
    return runtime / _STATE_FILENAME if runtime is not None else None


def _read_last_id(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        # ValueError covers a corrupted, non-ASCII state file (UnicodeDecodeError).
        return None
    # notify-send ids are positive integers; reject anything else so a garbled
    # (but still ASCII) state file never becomes a bogus --replace-id argument.
    return text if text.isdigit() else None


def _write_last_id(path: Path, notification_id: str) -> None:
    try:
        path.write_text(notification_id, encoding="ascii")
    except OSError as exc:
        logger.debug("could not persist notification id: %s", exc)


def send(
    summary: str,
    *,
    body: str | None = None,
    urgency: Urgency = "normal",
    expire_ms: int = 2000,
    replace: bool = True,
    transient: bool = True,
) -> None:
    """Send a desktop notification via `notify-send`.

    When `replace` is true, reuse the id of the previous notification so the
    new one overwrites it in place rather than stacking another popup. Silently
    does nothing when `notify-send` is unavailable, keeping the tool usable
    headless.

    `transient` controls the freedesktop `transient` hint: transient
    notifications bypass the persistent log (right for routine switches), while
    non-transient ones survive there so an error is not lost when it expires.
    """
    args = [
        "notify-send",
        f"--urgency={urgency}",
        f"--expire-time={expire_ms}",
    ]
    if transient:
        args.append("--hint=int:transient:1")
    # Reuse the previous notification's id so the new one overwrites it in place
    # rather than stacking another popup. Skipped when `replace` is false or
    # there is no runtime dir to persist the id in (notifications then just
    # stack, which is the graceful degradation we want over a /tmp state file).
    # `_state_path` is resolved once and threaded to the read/write helpers so
    # we don't repeat the env lookup + stat + mkdir several times per call.
    state_path = _state_path() if replace else None
    if state_path is not None:
        args.append("--print-id")
        last_id = _read_last_id(state_path)
        if last_id is not None:
            args.append(f"--replace-id={last_id}")
    # Use `--` so a summary/body starting with `-` is never mistaken for
    # an option by notify-send's argument parser.
    args.append("--")
    args.append(summary)
    if body:
        args.append(body)
    try:
        proc = subprocess.run(
            args,
            check=False,
            stdout=subprocess.PIPE if state_path is not None else subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        logger.debug("notify-send not found, skipping notification")
        return
    if state_path is not None and proc.returncode == 0:
        new_id = proc.stdout.strip()
        if new_id:
            _write_last_id(state_path, new_id)
