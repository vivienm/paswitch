"""Tests for the paswitch command-line interface."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

import pytest
from typer.testing import CliRunner

import paswitch.__main__ as cli
from paswitch.audio import BackendError, Sink

from .helpers import FakeBackend, RefusingBackend, sink

runner = CliRunner()


@dataclass
class CliDoubles:
    """Test doubles installed into the CLI module."""

    install: Callable[[FakeBackend], FakeBackend]
    notified: list[str] = field(default_factory=list)


@pytest.fixture
def patched_cli(monkeypatch: pytest.MonkeyPatch) -> CliDoubles:
    """Replace the real backend and notifier with in-memory doubles."""
    notified: list[str] = []

    def fake_notify(summary: str, **_: object) -> None:
        notified.append(summary)

    def install(backend: FakeBackend) -> FakeBackend:
        monkeypatch.setattr(cli, "_backend", lambda: backend)
        monkeypatch.setattr(cli.notification, "send", fake_notify)
        return backend

    return CliDoubles(install=install, notified=notified)


def test_cli_no_command_lists(patched_cli: CliDoubles) -> None:
    # With no subcommand, paswitch lists the sinks; it does not switch.
    backend = patched_cli.install(
        FakeBackend(
            [sink("a", "Speakers", index=0), sink("b", "Headphones", index=1)],
            default="a",
        )
    )
    result = runner.invoke(cli.app, [])
    assert result.exit_code == 0, result.output
    assert "Speakers" in result.output and "Headphones" in result.output
    assert backend.default_set_to is None  # nothing switched
    assert patched_cli.notified == []


def test_cli_prev_routes_to_previous(patched_cli: CliDoubles) -> None:
    # The wrap-around itself is covered by test_pick_sink_prev_*; here we only
    # check that the `prev` command routes to Direction.PREV (a -> last, not b).
    backend = patched_cli.install(
        FakeBackend(
            [sink("a", "Speakers", index=0), sink("b", "Headphones", index=1)],
            default="a",
        )
    )
    result = runner.invoke(cli.app, ["prev"])
    assert result.exit_code == 0, result.output
    assert backend.default_set_to is not None
    assert backend.default_set_to.name == "b"  # prev from first wraps to last


def test_cli_no_notify_flag(patched_cli: CliDoubles) -> None:
    patched_cli.install(FakeBackend([sink("a", "A"), sink("b", "B")], default="a"))
    result = runner.invoke(cli.app, ["--no-notify", "next"])
    assert result.exit_code == 0, result.output
    assert patched_cli.notified == []


def test_cli_no_notify_via_env(patched_cli: CliDoubles) -> None:
    patched_cli.install(FakeBackend([sink("a", "A"), sink("b", "B")], default="a"))
    result = runner.invoke(cli.app, ["next"], env={"PASWITCH_NOTIFY": "0"})
    assert result.exit_code == 0, result.output
    assert patched_cli.notified == []


def test_cli_notify_flag_overrides_env(patched_cli: CliDoubles) -> None:
    patched_cli.install(FakeBackend([sink("a", "A"), sink("b", "B")], default="a"))
    result = runner.invoke(cli.app, ["--notify", "next"], env={"PASWITCH_NOTIFY": "0"})
    assert result.exit_code == 0, result.output
    assert patched_cli.notified == ["Audio output: B"]


def test_format_sink_marks_default_and_states() -> None:
    # typer.style only emits ANSI when colours are forced; the CliRunner path
    # strips them, so assert on the styling and the plain text here directly.
    default = cli._format_sink(
        sink("a", "Speakers", index=0), default=True, excluded=False
    )
    excluded = cli._format_sink(sink("x", "X", index=1), default=False, excluded=True)
    unavailable = cli._format_sink(
        sink("u", "U", index=2, available=False), default=False, excluded=False
    )
    both = cli._format_sink(
        sink("b", "B", index=3, available=False), default=False, excluded=True
    )
    plain = cli._format_sink(sink("p", "Other", index=4), default=False, excluded=False)
    assert default.lstrip().startswith("\x1b")  # bold marker, not a bare space
    assert "(excluded)" in excluded
    assert "(unavailable)" in unavailable
    assert "(excluded, unavailable)" in both  # combined into a single tag
    # The description leads on the first line; the bracketed name follows on an
    # indented second line.
    first, second = plain.splitlines()
    assert "Other" in first and "(" not in first  # description, no status tag
    assert "[p]" in second and "[p]" not in first  # bracketed name on line two


def test_format_sink_default_unavailable_keeps_bold_not_yellow() -> None:
    # An unavailable *default* still carries the tag, but stays bold rather than
    # turning yellow: yellow means "skipped when cycling" and the default never is.
    import typer

    yellow_seq = typer.style("x", fg=typer.colors.YELLOW).split("x")[0]  # ANSI prefix
    default = cli._format_sink(
        sink("d", "Default", index=0, available=False), default=True, excluded=False
    )
    nondefault = cli._format_sink(
        sink("u", "Other", index=1, available=False), default=False, excluded=False
    )
    default_first = default.splitlines()[0]
    assert "(unavailable)" in default_first  # the tag is still informative
    assert yellow_seq not in default_first  # but the line is not coloured yellow
    assert yellow_seq in nondefault.splitlines()[0]  # a non-default one is yellow


def test_cli_list_marks_unavailable(patched_cli: CliDoubles) -> None:
    patched_cli.install(
        FakeBackend(
            [
                sink("a", "Speakers", index=0),
                sink("hdmi", "HDMI Output", index=1, available=False),
            ],
            default="a",
        )
    )
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0, result.output
    # Each sink spans two lines; find the unavailable one's first (tag) line.
    tag_line = next(
        line for line in result.output.splitlines() if "(unavailable)" in line
    )
    assert "HDMI Output" in tag_line  # the tag rides the description line
    assert "(unavailable)" not in result.output.split("Speakers")[0]  # not the default


def test_cli_list_marks_default(patched_cli: CliDoubles) -> None:
    patched_cli.install(
        FakeBackend(
            [sink("a", "Speakers", index=0), sink("b", "Headphones", index=1)],
            default="b",
        )
    )
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    # Sinks sort by index: "a" (lines 0-1), default "b" (lines 2-3).
    assert lines[0].startswith("  ") and "Speakers" in lines[0]
    assert lines[2].startswith("* ") and "Headphones" in lines[2]


def test_cli_set_by_substring(patched_cli: CliDoubles) -> None:
    backend = patched_cli.install(
        FakeBackend(
            [sink("a", "Speakers", index=0), sink("b", "Headphones", index=1)],
            default="a",
        )
    )
    result = runner.invoke(cli.app, ["set", "Headphones"])
    assert result.exit_code == 0, result.output
    assert backend.default_set_to is not None
    assert backend.default_set_to.name == "b"


def test_cli_exclude_flag_skips_sink(patched_cli: CliDoubles) -> None:
    backend = patched_cli.install(
        FakeBackend(
            [
                sink("a", "A", index=0),
                sink("hdmi", "HDMI", index=1),
                sink("b", "B", index=2),
            ],
            default="a",
        )
    )
    result = runner.invoke(cli.app, ["--exclude", "hdmi*", "next"])
    assert result.exit_code == 0, result.output
    assert backend.default_set_to is not None
    assert backend.default_set_to.name == "b"  # hdmi skipped


def test_cli_exclude_via_env(patched_cli: CliDoubles) -> None:
    backend = patched_cli.install(
        FakeBackend(
            [sink("a", index=0), sink("hdmi", index=1), sink("b", index=2)],
            default="a",
        )
    )
    result = runner.invoke(cli.app, ["next"], env={"PASWITCH_EXCLUDE": "hdmi*:foo*"})
    assert result.exit_code == 0, result.output
    assert backend.default_set_to is not None
    assert backend.default_set_to.name == "b"


def test_cli_exclude_splits_on_colon(patched_cli: CliDoubles) -> None:
    # A single --exclude value is split on ':' (same as PASWITCH_EXCLUDE), so
    # 'hdmi:b' excludes both the hdmi and b sinks, leaving nothing to switch to.
    backend = patched_cli.install(
        FakeBackend(
            [sink("a", index=0), sink("hdmi", index=1), sink("b", index=2)],
            default="a",
        )
    )
    result = runner.invoke(cli.app, ["--exclude", "hdmi:b", "next"])
    assert result.exit_code == 1, result.output
    assert "Nothing to switch" in result.output
    assert backend.default_set_to is None


def test_cli_exclude_is_case_insensitive(patched_cli: CliDoubles) -> None:
    # The CLI lowercases patterns when parsing --exclude, so an upper-case glob
    # still matches a lower-case sink (and its description): exclusion is
    # case-insensitive end to end.
    backend = patched_cli.install(
        FakeBackend(
            [
                sink("a", "A", index=0),
                sink("hdmi", "HDMI Output", index=1),
                sink("b", "B", index=2),
            ],
            default="a",
        )
    )
    result = runner.invoke(cli.app, ["--exclude", "*HDMI*", "next"])
    assert result.exit_code == 0, result.output
    assert backend.default_set_to is not None
    assert backend.default_set_to.name == "b"  # HDMI sink skipped despite the case


def test_cli_exclude_flag_overrides_env(patched_cli: CliDoubles) -> None:
    # Explicit --exclude replaces the env var entirely, so hdmi is no longer
    # excluded and cycling lands on it.
    backend = patched_cli.install(
        FakeBackend(
            [sink("a", index=0), sink("hdmi", index=1), sink("b", index=2)],
            default="a",
        )
    )
    result = runner.invoke(
        cli.app, ["--exclude", "nomatch*", "next"], env={"PASWITCH_EXCLUDE": "hdmi*"}
    )
    assert result.exit_code == 0, result.output
    assert backend.default_set_to is not None
    assert backend.default_set_to.name == "hdmi"


def test_cli_set_ignores_exclusion(patched_cli: CliDoubles) -> None:
    # An excluded sink stays reachable explicitly via `set`.
    backend = patched_cli.install(
        FakeBackend(
            [sink("a", "A", index=0), sink("hdmi", "HDMI", index=1)], default="a"
        )
    )
    result = runner.invoke(cli.app, ["--exclude", "hdmi*", "set", "hdmi"])
    assert result.exit_code == 0, result.output
    assert backend.default_set_to is not None
    assert backend.default_set_to.name == "hdmi"


def test_cli_list_marks_excluded(patched_cli: CliDoubles) -> None:
    patched_cli.install(
        FakeBackend(
            [sink("a", "Speakers", index=0), sink("hdmi", "HDMI Output", index=1)],
            default="a",
        )
    )
    result = runner.invoke(cli.app, ["--exclude", "hdmi*", "list"])
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    # Default "a" spans lines 0-1; excluded "hdmi" spans lines 2-3.
    assert lines[0].startswith("* ") and "Speakers" in lines[0]
    assert "(excluded)" in lines[2] and "HDMI" in lines[2]


def test_cli_set_not_found(patched_cli: CliDoubles) -> None:
    patched_cli.install(
        FakeBackend([sink("a", "Speakers"), sink("b", "Headphones")], default="a")
    )
    result = runner.invoke(cli.app, ["set", "nope"])
    assert result.exit_code == 1
    assert "Sink not found" in result.output
    assert patched_cli.notified == []  # benign exit 1: no notification


class FailingBackend(FakeBackend):
    """A backend whose queries fail, to exercise error paths."""

    def list_sinks(self) -> list[Sink]:
        raise BackendError("connection refused")


def test_cli_set_backend_error(patched_cli: CliDoubles) -> None:
    # A backend failure on the `set` path reports the error and exits 2,
    # mirroring the cycle/list paths.
    patched_cli.install(FailingBackend([sink("a", "A")], default="a"))
    result = runner.invoke(cli.app, ["set", "anything"])
    assert result.exit_code == 2
    assert "Audio backend error" in result.output


def test_cli_set_switch_refused(patched_cli: CliDoubles) -> None:
    # The server accepts the command but keeps the old default (exit 0, no
    # change): paswitch must report it as a backend-level failure, not success.
    backend = patched_cli.install(
        RefusingBackend([sink("a", "A"), sink("b", "B")], default="a")
    )
    result = runner.invoke(cli.app, ["set", "B"])
    # Code 3 (not 2): the backend worked, the server just declined the switch.
    assert result.exit_code == 3, result.output
    assert "Switch refused" in result.output
    assert backend.default_sink_name() == "a"  # really unchanged
    # A genuine failure (exit 3) still pops a notification, unlike exit 1.
    assert patched_cli.notified == [
        "Switch refused: the server kept the previous default instead of 'b' (its active port may be unavailable)"
    ]


def test_cli_nothing_to_switch(patched_cli: CliDoubles) -> None:
    patched_cli.install(FakeBackend([sink("a", "Only")], default="a"))
    result = runner.invoke(cli.app, ["next"])
    assert result.exit_code == 1
    assert "Nothing to switch" in result.output
    # Benign dead-end (exit 1): no popup — it's a no-op behind a keybinding,
    # not a fault worth interrupting the user for. See test_cli_set_switch_refused
    # / test_cli_set_backend_error for the failures that *do* notify.
    assert patched_cli.notified == []
