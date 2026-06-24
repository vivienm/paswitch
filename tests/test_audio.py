"""Tests for paswitch audio logic and the pactl backend."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence

import pytest

from paswitch.audio import (
    BackendError,
    Direction,
    NothingToSwitchError,
    PactlBackend,
    SinkNotFoundError,
    SwitchRefusedError,
    cycle_default,
    find_sink,
    pick_sink,
    switch_to,
)
from paswitch.exclude import Exclude

from .helpers import FakeBackend, RefusingBackend, sink

# --- pure logic -------------------------------------------------------------


def test_pick_sink_next_cycles_and_wraps() -> None:
    sinks = [sink("a"), sink("b"), sink("c")]
    assert pick_sink(sinks, "a").name == "b"
    assert pick_sink(sinks, "b").name == "c"
    assert pick_sink(sinks, "c").name == "a"  # wraps around


def test_pick_sink_prev_cycles_and_wraps() -> None:
    sinks = [sink("a"), sink("b"), sink("c")]
    assert pick_sink(sinks, "a", Direction.PREV).name == "c"  # wraps to last
    assert pick_sink(sinks, "b", Direction.PREV).name == "a"
    assert pick_sink(sinks, "c", Direction.PREV).name == "b"


def test_pick_sink_current_not_found() -> None:
    sinks = [sink("a"), sink("b")]
    assert pick_sink(sinks, "missing").name == "a"  # next -> first
    assert pick_sink(sinks, "missing", Direction.PREV).name == "b"  # prev -> last


def test_pick_sink_single_sink_raises() -> None:
    with pytest.raises(NothingToSwitchError):
        pick_sink([sink("a")], "a")
    with pytest.raises(NothingToSwitchError):
        pick_sink([], "a")


def test_pick_sink_single_sink_not_current_returns_it() -> None:
    # A lone candidate that is not the current default is a real move (the
    # current default was filtered out as unavailable/excluded): return it
    # rather than raising NothingToSwitchError.
    assert pick_sink([sink("b")], "a").name == "b"
    assert pick_sink([sink("b")], "a", Direction.PREV).name == "b"


def test_find_sink_exact_then_substring() -> None:
    sinks = [
        sink("alsa_output.analog", "Built-in Analog"),
        sink("bluez_sink.a2dp", "Headphones"),
    ]
    assert find_sink(sinks, "bluez_sink.a2dp").name == "bluez_sink.a2dp"  # exact
    assert find_sink(sinks, "Headphones").name == "bluez_sink.a2dp"  # description
    assert find_sink(sinks, "analog").name == "alsa_output.analog"


def test_find_sink_ambiguous_and_missing() -> None:
    sinks = [sink("alsa_output.analog"), sink("bluez_sink.analog")]
    with pytest.raises(SinkNotFoundError):
        find_sink(sinks, "analog")  # matches both
    with pytest.raises(SinkNotFoundError):
        find_sink(sinks, "nope")


def test_sink_label_falls_back_to_name() -> None:
    assert sink("a", "Speakers").label == "Speakers"
    assert sink("a", "").label == "a"


# --- orchestration ----------------------------------------------------------


def test_cycle_default_sets_next() -> None:
    backend = FakeBackend([sink("a", "A"), sink("b", "B")], default="a")
    target = cycle_default(backend, Direction.NEXT)
    assert target.name == "b"
    assert backend.default_set_to is not None
    assert backend.default_set_to.name == "b"


def test_cycle_default_orders_by_index() -> None:
    # Listed out of index order; cycling must follow index, not list order.
    backend = FakeBackend(
        [sink("b", "B", index=1), sink("a", "A", index=0)], default="a"
    )
    target = cycle_default(backend, Direction.NEXT)
    assert target.name == "b"  # index 0 -> index 1


def test_cycle_default_single_sink_raises() -> None:
    backend = FakeBackend([sink("a")], default="a")
    with pytest.raises(NothingToSwitchError):
        cycle_default(backend)


def test_cycle_default_skips_excluded() -> None:
    backend = FakeBackend(
        [sink("a", index=0), sink("hdmi", index=1), sink("b", index=2)], default="a"
    )
    target = cycle_default(backend, Direction.NEXT, exclude=Exclude.parse("hdmi*"))
    assert target.name == "b"  # the excluded sink between a and b is skipped


def test_cycle_default_from_excluded_default() -> None:
    # When the current default is itself excluded, cycling falls onto the
    # first remaining candidate rather than getting stuck.
    backend = FakeBackend([sink("a", index=0), sink("hdmi", index=1)], default="hdmi")
    target = cycle_default(backend, Direction.NEXT, exclude=Exclude.parse("hdmi*"))
    assert target.name == "a"


def test_cycle_default_all_excluded_raises() -> None:
    backend = FakeBackend([sink("hdmi1"), sink("hdmi2")], default="hdmi1")
    with pytest.raises(NothingToSwitchError):
        cycle_default(backend, exclude=Exclude.parse("hdmi*"))


def test_cycle_default_skips_unavailable() -> None:
    # A sink whose active port is unavailable (e.g. an unplugged HDMI output)
    # is skipped: the server would refuse it and leave the default unchanged.
    backend = FakeBackend(
        [
            sink("a", index=0),
            sink("hdmi", index=1, available=False),
            sink("b", index=2),
        ],
        default="a",
    )
    target = cycle_default(backend, Direction.NEXT)
    assert target.name == "b"  # the unavailable sink is skipped


def test_cycle_default_all_unavailable_raises() -> None:
    backend = FakeBackend(
        [sink("a", index=0, available=False), sink("b", index=1, available=False)],
        default="a",
    )
    with pytest.raises(NothingToSwitchError):
        cycle_default(backend)


def test_switch_to_sets_default() -> None:
    backend = FakeBackend([sink("a"), sink("b", "Headphones")], default="a")
    target = switch_to(backend, "Headphones")
    assert target.name == "b"
    assert backend.default_set_to is not None
    assert backend.default_set_to.name == "b"


def test_switch_to_refused_raises() -> None:
    # `set` reaches a sink the server then silently declines (exit 0, old
    # default kept): the post-switch confirmation must surface it.
    backend = RefusingBackend([sink("a"), sink("b", "Headphones")], default="a")
    with pytest.raises(SwitchRefusedError):
        switch_to(backend, "Headphones")


def test_cycle_default_refused_raises() -> None:
    backend = RefusingBackend([sink("a", index=0), sink("b", index=1)], default="a")
    with pytest.raises(SwitchRefusedError):
        cycle_default(backend, Direction.NEXT)


# --- pactl backend ----------------------------------------------------------


def test_pactl_backend_parses_sinks(monkeypatch: pytest.MonkeyPatch) -> None:
    # list_sinks parses fields and preserves pactl's order (callers sort).
    payload = (
        '[{"index": 6, "name": "bluez.hp"},'
        ' {"index": 5, "name": "alsa.spk", "description": "Speakers"}]'
    )

    def fake_run(self: PactlBackend, *args: str) -> str:
        assert args == ("--format=json", "list", "sinks")
        return payload

    monkeypatch.setattr(PactlBackend, "_run", fake_run)
    sinks = PactlBackend().list_sinks()
    # Compare on attributes: Sink equality only looks at index now.
    assert [(s.index, s.name, s.description) for s in sinks] == [
        (6, "bluez.hp", ""),
        (5, "alsa.spk", "Speakers"),
    ]


def test_pactl_backend_parses_port_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The active port's availability drives Sink.is_available: an explicit
    # "not available" marks the sink unusable; everything else stays usable.
    payload = json.dumps(
        [
            {
                "index": 0,
                "name": "hdmi",
                "active_port": "[Out] HDMI3",
                "ports": [{"name": "[Out] HDMI3", "availability": "not available"}],
            },
            {
                "index": 1,
                "name": "spk",
                "active_port": "[Out] Speaker",
                "ports": [
                    {"name": "[Out] Speaker", "availability": "availability unknown"}
                ],
            },
            {"index": 2, "name": "noports"},
        ]
    )
    monkeypatch.setattr(PactlBackend, "_run", lambda self, *a: payload)
    sinks = PactlBackend().list_sinks()
    assert [(s.name, s.is_available) for s in sinks] == [
        ("hdmi", False),
        ("spk", True),
        ("noports", True),  # no port info -> assumed usable
    ]


def test_pactl_backend_skips_malformed_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    # One malformed entry (missing name) must not sink the whole listing: it is
    # skipped so the user can still switch between the well-formed sinks.
    payload = json.dumps(
        [
            {"index": 0, "name": "good"},
            {"index": 1},  # no name -> malformed
            {"index": 2, "name": "also_good"},
        ]
    )
    monkeypatch.setattr(PactlBackend, "_run", lambda self, *a: payload)
    sinks = PactlBackend().list_sinks()
    assert [s.name for s in sinks] == ["good", "also_good"]


def test_pactl_backend_rejects_bad_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(PactlBackend, "_run", lambda self, *a: "not json")
    with pytest.raises(BackendError):
        PactlBackend().list_sinks()


def test_pactl_backend_null_description_falls_back_to_node_nick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pactl's JSON encoder turns non-ASCII descriptions into the literal
    # "(null)" (older pa_json_escape rejects bytes > 0x7F). The properties
    # block survives for ASCII values, so `node.nick` stands in for the label —
    # no extra pactl call, no text scraping.
    payload = json.dumps(
        [
            {
                "index": 68,
                "name": "alsa_output.pci-0000_00_1f.3.iec958-stereo",
                "description": "(null)",
                "properties": {
                    "node.nick": "ALCS1200A Digital",
                    "device.description": "Audio interne",
                },
            },
            {
                "index": 118,
                "name": "alsa_output.pci-0000_03_00.1.hdmi-stereo-extra1",
                "description": "Navi 31 HDMI/DP Audio Digital Stereo (HDMI 2)",
                "properties": {"node.nick": "DELL U2719DC"},
            },
        ]
    )
    monkeypatch.setattr(PactlBackend, "_run", lambda self, *a: payload)
    sinks = PactlBackend().list_sinks()
    assert [(s.index, s.description) for s in sinks] == [
        (68, "ALCS1200A Digital"),
        (118, "Navi 31 HDMI/DP Audio Digital Stereo (HDMI 2)"),
    ]


def test_pactl_backend_null_description_falls_back_to_device_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # If `node.nick` is itself missing or "(null)", `device.description` is the
    # next stop; only then does `Sink.label` fall back to the name.
    payload = json.dumps(
        [
            {
                "index": 0,
                "name": "a",
                "description": "(null)",
                "properties": {"device.description": "Speakers"},
            },
            {
                "index": 1,
                "name": "b",
                "description": "(null)",
                "properties": {
                    "node.nick": "(null)",
                    "device.description": "(null)",
                },
            },
        ]
    )
    monkeypatch.setattr(PactlBackend, "_run", lambda self, *a: payload)
    sinks = PactlBackend().list_sinks()
    assert [(s.name, s.description, s.label) for s in sinks] == [
        ("a", "Speakers", "Speakers"),
        ("b", "", "b"),  # nothing usable -> name fallback
    ]


def test_pactl_backend_no_extra_call_when_descriptions_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The common case (ASCII descriptions, or a pactl without the encoder bug)
    # stays a single `pactl list sinks` invocation.
    payload = json.dumps([{"index": 0, "name": "a", "description": "Speakers"}])
    calls: list[tuple[str, ...]] = []

    def fake_run(self: PactlBackend, *args: str) -> str:
        calls.append(args)
        return payload

    monkeypatch.setattr(PactlBackend, "_run", fake_run)
    sinks = PactlBackend().list_sinks()
    assert [s.description for s in sinks] == ["Speakers"]
    assert calls == [("--format=json", "list", "sinks")]


def test_pactl_backend_missing_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object, **__: object) -> object:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(BackendError, match="not found in PATH"):
        PactlBackend().default_sink_name()


def test_pactl_backend_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(argv: Sequence[str], **_: object) -> object:
        raise subprocess.CalledProcessError(
            returncode=1, cmd=list(argv), output="", stderr="connection refused"
        )

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(BackendError, match=r"exit 1.*connection refused"):
        PactlBackend().default_sink_name()


def test_pactl_backend_oserror_besides_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A spawn failure that is not FileNotFoundError (e.g. pactl present but not
    # executable -> PermissionError) still surfaces as a BackendError, not an
    # uncaught traceback.
    def boom(argv: Sequence[str], **_: object) -> object:
        raise PermissionError("not executable")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(BackendError, match="could not run"):
        PactlBackend().default_sink_name()
