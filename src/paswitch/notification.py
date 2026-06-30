"""Desktop notifications through the freedesktop D-Bus notification service.

This lives outside `paswitch.audio` because sending a notification has
nothing to do with sound. Notifications are best-effort: anything that goes
wrong here (no session bus, no notification daemon, a D-Bus error) is logged
and swallowed so it never disrupts an audio switch.

We talk to `org.freedesktop.Notifications` directly over D-Bus (via `jeepney`,
a pure-Python client) rather than shelling out to `notify-send`: the `Notify`
method returns the new notification's id, and accepts a `replace_id`, both part
of the stable freedesktop spec. paswitch is a one-shot process, so to replace
the previous popup in place (instead of stacking a fresh one every time the
user taps the switch key) that id is remembered between runs under
`$XDG_RUNTIME_DIR/paswitch/` and passed back as `replace_id`.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Literal

from jeepney import DBusAddress, HeaderFields, MessageType, new_method_call
from jeepney.io.blocking import open_dbus_connection

from . import paths

logger = logging.getLogger(__name__)

# Notification urgency levels defined by the freedesktop notification spec,
# mapped to the byte value the `urgency` hint expects.
Urgency = Literal["low", "normal", "critical"]
_URGENCY_VALUES: dict[Urgency, int] = {"low": 0, "normal": 1, "critical": 2}

_STATE_FILENAME = "notification-id"

# `replace_id` is marshalled as a uint32; cap the persisted id to that range.
_MAX_ID = 2**32 - 1

# Best-effort timeouts (seconds) to keep a stuck bus from freezing an audio
# switch: one for the auth handshake, one for the reply to `Notify`. Kept
# separate rather than sharing a single value, which would conflate two distinct
# waits. Note these don't cover the `Hello` call jeepney makes between the two
# (when registering on the bus): in practice a bus that finished auth answers it
# at once, so it's left unbounded rather than wrapped in a watchdog.
_AUTH_TIMEOUT = 1.0
_REPLY_TIMEOUT = 2.0

# The freedesktop notification service on the session bus.
_NOTIFICATIONS = DBusAddress(
    "/org/freedesktop/Notifications",
    bus_name="org.freedesktop.Notifications",
    interface="org.freedesktop.Notifications",
)


def _state_path() -> Path | None:
    """Where to remember the id of the last notification, or `None`.

    `None` when no per-user runtime directory is available (no usable
    `$XDG_RUNTIME_DIR`, or its creation failed): id persistence is then
    skipped and notifications stack instead of replacing in place.
    """
    try:
        runtime = paths.runtime_dir()
    except OSError as exc:
        # `runtime_dir` may `mkdir` the leaf; a non-dir collision or an
        # unwritable runtime dir must degrade to "no persistence", never
        # propagate and disrupt an audio switch.
        logger.debug("no usable runtime dir, skipping id tracking: %s", exc)
        return None
    return runtime / _STATE_FILENAME if runtime is not None else None


def _read_last_id(path: Path) -> int:
    """Read the last notification id, or `0` (the "no replacement" sentinel).

    `Notify`'s `replace_id` treats `0` as "allocate a fresh id", so a missing,
    unreadable or garbled state file degrades gracefully to a new popup.
    """
    try:
        text = path.read_text(encoding="ascii").strip()
        # Reject anything but a positive integer so a garbled (but still ASCII)
        # state file never becomes a bogus replace_id. `int()` stays inside the
        # `try`: a digit string past CPython's 4300-digit limit raises here too.
        value = int(text) if text.isdigit() else 0
    except (OSError, ValueError):
        # ValueError covers a non-ASCII state file (UnicodeDecodeError) or a
        # digit string too long for `int()` to parse.
        return 0
    # Clamp out-of-range values so an id can't overflow the uint32 it is
    # marshalled as.
    return value if value <= _MAX_ID else 0


def _write_last_id(path: Path, notification_id: int) -> None:
    try:
        path.write_text(str(notification_id), encoding="ascii")
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
    """Send a desktop notification over D-Bus.

    When `replace` is true, reuse the id of the previous notification so the
    new one overwrites it in place rather than stacking another popup. Silently
    does nothing when no notification service can be reached, keeping the tool
    usable headless.

    `transient` controls the freedesktop `transient` hint: transient
    notifications bypass the persistent log (right for routine switches), while
    non-transient ones survive there so an error is not lost when it expires.
    """
    # Reuse the previous notification's id so the new one overwrites it in place
    # rather than stacking another popup. Skipped when `replace` is false or
    # there is no runtime dir to persist the id in (notifications then just
    # stack, which is the graceful degradation we want over a /tmp state file).
    # `_state_path` is resolved once and threaded to the read/write helpers so
    # we don't repeat the env lookup + stat + mkdir several times per call.
    state_path = _state_path() if replace else None
    replace_id = _read_last_id(state_path) if state_path is not None else 0

    hints: dict[str, tuple[str, object]] = {
        "urgency": ("y", _URGENCY_VALUES[urgency]),
    }
    if transient:
        hints["transient"] = ("b", True)

    # Notify(app_name, replace_id, app_icon, summary, body, actions, hints,
    #        expire_timeout) -> uint32 id. Signature: susssasa{sv}i.
    call = new_method_call(
        _NOTIFICATIONS,
        "Notify",
        "susssasa{sv}i",
        # `expire_timeout` is a signed int32 (`i`); -1 means "server default"
        # and 0 "never expire", so pass the caller's value through unchanged.
        ("paswitch", replace_id, "", summary, body or "", [], hints, expire_ms),
    )

    # Open the bus, send the call and close the connection under one best-effort
    # policy: an expected unavailability (no session bus, dead socket, a failed
    # call) is logged at debug and swallowed, while anything else is surfaced at
    # warning — but neither is ever allowed to disrupt an audio switch.
    conn = None
    try:
        conn = open_dbus_connection(bus="SESSION", auth_timeout=_AUTH_TIMEOUT)
        reply = conn.send_and_get_reply(call, timeout=_REPLY_TIMEOUT)
    except (OSError, KeyError) as exc:
        # KeyError if DBUS_SESSION_BUS_ADDRESS is unset, OSError if the socket
        # is gone or the handshake/reply times out (TimeoutError is an OSError)
        # — e.g. running headless. A daemon-side failure comes back as an error
        # reply (handled below), not as an exception here. Skip silently.
        logger.debug("no session bus, skipping notification: %s", exc)
        return
    except Exception as exc:
        # Unexpected (bus refused the handshake, unsupported transport, a
        # marshalling bug, …). Likely points at a real problem rather than a
        # missing daemon, so surface it — but still skip.
        logger.warning("unexpected error sending notification: %s", exc)
        return
    finally:
        if conn is not None:
            # Closing a connection left in a degraded state (failed handshake,
            # timed-out reply) can itself raise; suppress so it never escapes
            # the finally and disrupts an audio switch.
            with contextlib.suppress(Exception):
                conn.close()

    if reply.header.message_type == MessageType.error:
        # No notification daemon registered on the bus, or it refused the call.
        # The error name (e.g. org.freedesktop.DBus.Error.ServiceUnknown)
        # travels in the header; the body holds the daemon's textual detail.
        error_name = reply.header.fields.get(HeaderFields.error_name, "?")
        detail = reply.body[0] if reply.body else ""
        logger.debug("notification service error %s: %s", error_name, detail)
        return
    # A conformant Notify reply carries the new id as a single uint32; guard the
    # body shape so a misbehaving daemon can't turn the unpack into a raise.
    if state_path is not None and reply.body:
        _write_last_id(state_path, reply.body[0])
