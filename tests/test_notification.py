"""Tests for desktop notifications."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest
from jeepney import HeaderFields, Message, MessageType

from paswitch import notification


@dataclass
class FakeHeader:
    message_type: MessageType = MessageType.method_return
    fields: dict[object, object] = field(default_factory=dict)


@dataclass
class FakeReply:
    body: tuple[object, ...] = (0,)
    header: FakeHeader = field(default_factory=FakeHeader)


@dataclass
class FakeMessage:
    """Stands in for a jeepney method-call message, exposing its body."""

    body: tuple[object, ...]


class FakeConnection:
    """Records the Notify calls and replies with successive ids."""

    def __init__(self, calls: list[tuple[object, ...]], ids: list[int]) -> None:
        self._calls = calls
        self._ids = iter(ids)

    def send_and_get_reply(self, message: FakeMessage, **_: object) -> FakeReply:
        self._calls.append(message.body)
        return FakeReply(body=(next(self._ids, 0),))

    def close(self) -> None:
        pass


@pytest.fixture
def notify_spy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[], list[tuple[object, ...]]]:
    """Capture Notify call bodies and isolate the id state file."""
    calls: list[tuple[object, ...]] = []
    conn = FakeConnection(calls, ids=[42, 43, 44])

    monkeypatch.setattr(notification, "_state_path", lambda: tmp_path / "id")
    monkeypatch.setattr(
        notification,
        "new_method_call",
        lambda _addr, _method, _sig, body: FakeMessage(body),
    )
    monkeypatch.setattr(notification, "open_dbus_connection", lambda bus, **_: conn)
    return lambda: calls


def _replace_id(body: tuple[object, ...]) -> int:
    """The replace_id argument from a captured Notify body (2nd positional)."""
    return cast(int, body[1])


def _hints(body: tuple[object, ...]) -> dict[str, object]:
    """The hints dict from a captured Notify body (7th positional)."""
    return cast(dict[str, object], body[6])


def test_notification_first_has_no_replace_id(
    notify_spy: Callable[[], list[tuple[object, ...]]],
) -> None:
    notification.send("hello")
    (body,) = notify_spy()
    assert _replace_id(body) == 0  # 0 means "allocate a fresh id"


def test_notification_reuses_previous_id(
    notify_spy: Callable[[], list[tuple[object, ...]]],
) -> None:
    notification.send("first")
    notification.send("second")
    first, second = notify_spy()
    assert _replace_id(first) == 0
    assert _replace_id(second) == 42  # id returned by the first call


def test_notification_ignores_non_numeric_id(
    notify_spy: Callable[[], list[tuple[object, ...]]], tmp_path: Path
) -> None:
    # A garbled but still ASCII state file must not become a bogus replace_id.
    (tmp_path / "id").write_text("garbage", encoding="ascii")
    notification.send("hello")
    (body,) = notify_spy()
    assert _replace_id(body) == 0


def test_notification_clamps_out_of_range_id(
    notify_spy: Callable[[], list[tuple[object, ...]]], tmp_path: Path
) -> None:
    # An id past uint32 (a corrupted state file) would overflow the `u` it is
    # marshalled as, so it degrades to 0 ("allocate a fresh id") instead.
    (tmp_path / "id").write_text(str(2**32), encoding="ascii")
    notification.send("hello")
    (body,) = notify_spy()
    assert _replace_id(body) == 0


def test_notification_ignores_overlong_digit_id(
    notify_spy: Callable[[], list[tuple[object, ...]]], tmp_path: Path
) -> None:
    # A digit string past CPython's 4300-digit limit makes `int()` raise; that
    # must degrade to 0, not propagate and disrupt the switch.
    (tmp_path / "id").write_text("9" * 5000, encoding="ascii")
    notification.send("hello")  # must not raise
    (body,) = notify_spy()
    assert _replace_id(body) == 0


def test_notification_transient_by_default(
    notify_spy: Callable[[], list[tuple[object, ...]]],
) -> None:
    notification.send("hello")
    (body,) = notify_spy()
    assert _hints(body)["transient"] == ("b", True)


def test_notification_non_transient_omits_hint(
    notify_spy: Callable[[], list[tuple[object, ...]]],
) -> None:
    notification.send("oops", transient=False)
    (body,) = notify_spy()
    assert "transient" not in _hints(body)


def test_notification_urgency_hint(
    notify_spy: Callable[[], list[tuple[object, ...]]],
) -> None:
    notification.send("oops", urgency="critical")
    (body,) = notify_spy()
    assert _hints(body)["urgency"] == ("y", 2)


def test_notification_replace_disabled_omits_id_tracking(
    notify_spy: Callable[[], list[tuple[object, ...]]],
) -> None:
    notification.send("one", replace=False)
    notification.send("two", replace=False)
    first, second = notify_spy()
    assert _replace_id(first) == 0
    assert _replace_id(second) == 0  # the first id was never persisted


def test_notification_no_session_bus_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(bus: str, **_: object) -> object:
        raise KeyError("DBUS_SESSION_BUS_ADDRESS")

    monkeypatch.setattr(notification, "open_dbus_connection", boom)
    notification.send("nobody home")  # must not raise


def test_notification_runtime_dir_oserror_skips_id_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If the runtime dir can't be created (non-dir collision, unwritable), the
    # OSError must degrade to "no persistence", not propagate and disrupt the
    # switch: replace_id stays 0 and the call still goes out.
    calls: list[tuple[object, ...]] = []
    conn = FakeConnection(calls, ids=[42])

    def boom() -> Path:
        raise PermissionError("runtime dir not writable")

    monkeypatch.setattr(notification.paths, "runtime_dir", boom)
    monkeypatch.setattr(
        notification, "new_method_call", lambda _a, _m, _s, body: FakeMessage(body)
    )
    monkeypatch.setattr(notification, "open_dbus_connection", lambda bus, **_: conn)
    notification.send("hello")  # must not raise
    (body,) = calls
    assert _replace_id(body) == 0


def test_notification_service_error_is_silent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # The bus is reachable but no daemon answers: an error reply must not raise,
    # and must not overwrite the persisted id with garbage. The error name from
    # the reply header is surfaced in the debug log for diagnosability.
    state = tmp_path / "id"
    state.write_text("7", encoding="ascii")

    class ErroringConnection:
        def send_and_get_reply(self, message: object, **_: object) -> FakeReply:
            return FakeReply(
                header=FakeHeader(
                    MessageType.error,
                    fields={
                        HeaderFields.error_name: "org.freedesktop.DBus.Error.ServiceUnknown"
                    },
                ),
                body=("no such service",),
            )

        def close(self) -> None:
            pass

    monkeypatch.setattr(notification, "_state_path", lambda: state)
    monkeypatch.setattr(notification, "new_method_call", lambda *a: FakeMessage(a[-1]))
    monkeypatch.setattr(
        notification, "open_dbus_connection", lambda bus, **_: ErroringConnection()
    )
    with caplog.at_level("DEBUG", logger=notification.logger.name):
        notification.send("hello")  # must not raise
    assert state.read_text(encoding="ascii") == "7"  # id left intact
    assert any(
        "org.freedesktop.DBus.Error.ServiceUnknown" in r.message for r in caplog.records
    )


def test_notification_no_runtime_dir_skips_id_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no per-user runtime dir, id persistence is skipped: replace_id stays 0
    # across calls, so notifications stack instead of replacing in place.
    calls: list[tuple[object, ...]] = []
    conn = FakeConnection(calls, ids=[99, 100])

    monkeypatch.setattr(notification, "_state_path", lambda: None)
    monkeypatch.setattr(
        notification, "new_method_call", lambda _a, _m, _s, body: FakeMessage(body)
    )
    monkeypatch.setattr(notification, "open_dbus_connection", lambda bus, **_: conn)
    notification.send("one")
    notification.send("two")
    first, second = calls
    assert _replace_id(first) == 0
    assert _replace_id(second) == 0


class _SerializingConnection:
    """Real-marshalling stand-in: serialises the Notify message on the wire.

    Unlike ``FakeConnection`` this takes the actual ``jeepney`` ``Message``
    built by the un-mocked ``new_method_call`` and runs ``serialise`` on it, so
    a wrong body shape (e.g. a hint variant missing its signature tuple) raises
    instead of being silently stored.
    """

    def __init__(self) -> None:
        self.serialised: bytes | None = None

    def send_and_get_reply(self, message: Message, **_: object) -> FakeReply:
        self.serialised = message.serialise(serial=1)
        return FakeReply(body=(42,))

    def close(self) -> None:
        pass


def test_notification_message_serialises_on_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The fakes mock at the new_method_call boundary, so the real jeepney wire
    # format is never exercised. Build the actual Notify message and serialise
    # it: a malformed body (bad variant/dict shape, signature/args mismatch)
    # raises here rather than passing tests and failing in production.
    conn = _SerializingConnection()
    monkeypatch.setattr(notification, "_state_path", lambda: tmp_path / "id")
    # NB: new_method_call is intentionally left real.
    monkeypatch.setattr(notification, "open_dbus_connection", lambda bus, **_: conn)

    notification.send("switched sink", body="to HDMI", urgency="critical")

    assert conn.serialised  # non-empty bytes => the message marshalled cleanly
    assert (tmp_path / "id").read_text(encoding="ascii") == "42"


def test_notification_unexpected_connection_error_is_swallowed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A non-OSError/KeyError from open_dbus_connection (e.g. RuntimeError for
    # an unsupported transport) must not propagate and disrupt the switch.
    def boom(bus: str, **_: object) -> object:
        raise RuntimeError("unsupported transport")

    monkeypatch.setattr(notification, "_state_path", lambda: tmp_path / "id")
    monkeypatch.setattr(notification, "open_dbus_connection", boom)
    with caplog.at_level("WARNING", logger=notification.logger.name):
        notification.send("hello")  # must not raise
    assert any(
        "unexpected error sending notification" in r.message for r in caplog.records
    )
