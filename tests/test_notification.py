"""Tests for desktop notifications."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from paswitch import notification


@dataclass
class FakeProc:
    returncode: int = 0
    stdout: str = ""


@pytest.fixture
def notify_spy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Callable[[], list[list[str]]]:
    """Capture notify-send invocations and isolate the id state file."""
    calls: list[list[str]] = []
    ids = iter(["42", "43", "44"])

    def fake_run(args: Sequence[str], **_: object) -> FakeProc:
        calls.append(list(args))
        return FakeProc(returncode=0, stdout=next(ids, ""))

    monkeypatch.setattr(notification, "_state_path", lambda: tmp_path / "id")
    monkeypatch.setattr(notification.subprocess, "run", fake_run)
    return lambda: calls


def test_notification_first_has_no_replace_id(
    notify_spy: Callable[[], list[list[str]]],
) -> None:
    notification.send("hello")
    (argv,) = notify_spy()
    assert "--print-id" in argv
    assert not any(a.startswith("--replace-id") for a in argv)


def test_notification_reuses_previous_id(
    notify_spy: Callable[[], list[list[str]]],
) -> None:
    notification.send("first")
    notification.send("second")
    first, second = notify_spy()
    assert not any(a.startswith("--replace-id") for a in first)
    assert "--replace-id=42" in second  # id printed by the first call


def test_notification_ignores_non_numeric_id(
    notify_spy: Callable[[], list[list[str]]], tmp_path: Path
) -> None:
    # A garbled but still ASCII state file must not become a bogus --replace-id.
    (tmp_path / "id").write_text("garbage", encoding="ascii")
    notification.send("hello")
    (argv,) = notify_spy()
    assert not any(a.startswith("--replace-id") for a in argv)


def test_notification_transient_by_default(
    notify_spy: Callable[[], list[list[str]]],
) -> None:
    notification.send("hello")
    (argv,) = notify_spy()
    assert "--hint=int:transient:1" in argv


def test_notification_non_transient_omits_hint(
    notify_spy: Callable[[], list[list[str]]],
) -> None:
    notification.send("oops", transient=False)
    (argv,) = notify_spy()
    assert not any("transient" in a for a in argv)


def test_notification_replace_disabled_omits_id_tracking(
    notify_spy: Callable[[], list[list[str]]],
) -> None:
    notification.send("one", replace=False)
    notification.send("two", replace=False)
    first, second = notify_spy()
    assert "--print-id" not in first
    assert not any(a.startswith("--replace-id") for a in second)


def test_notification_missing_binary_is_silent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*_: object, **__: object) -> object:
        raise FileNotFoundError

    monkeypatch.setattr(notification.subprocess, "run", boom)
    notification.send("nobody home")  # must not raise


def test_notification_no_runtime_dir_skips_id_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With no per-user runtime dir, id persistence is skipped: no --print-id and
    # no --replace-id, so notifications stack instead of replacing in place.
    calls: list[list[str]] = []

    def fake_run(args: Sequence[str], **_: object) -> FakeProc:
        calls.append(list(args))
        return FakeProc(returncode=0, stdout="99")

    monkeypatch.setattr(notification, "_state_path", lambda: None)
    monkeypatch.setattr(notification.subprocess, "run", fake_run)
    notification.send("one")
    notification.send("two")
    first, second = calls
    assert "--print-id" not in first
    assert not any(a.startswith("--replace-id") for a in second)
