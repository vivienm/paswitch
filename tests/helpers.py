"""Shared test doubles and builders.

Lives in a module (not ``conftest.py``) because these are imported by name
rather than injected as fixtures. ``tests`` is a package so the import works
under pytest's ``importlib`` mode.
"""

from __future__ import annotations

from collections.abc import Sequence

from paswitch.audio import Sink


def sink(name: str, desc: str = "", *, index: int = 0, available: bool = True) -> Sink:
    """Build a :class:`Sink`, defaulting the fields tests rarely care about."""
    return Sink(index=index, name=name, description=desc, is_available=available)


class FakeBackend:
    """In-memory AudioBackend for tests, recording side effects."""

    def __init__(self, sinks: Sequence[Sink], default: str) -> None:
        self._sinks = list(sinks)
        self._default = default
        self.default_set_to: Sink | None = None

    def list_sinks(self) -> list[Sink]:
        # Return sinks in insertion order: the AudioBackend contract makes no
        # ordering promise, so callers are responsible for sorting.
        return list(self._sinks)

    def default_sink_name(self) -> str:
        return self._default

    def set_default(self, sink: Sink) -> None:
        self.default_set_to = sink
        self._default = sink.name


class RefusingBackend(FakeBackend):
    """A backend that records the request but silently keeps the old default.

    Mirrors ``pactl set-default-sink`` exiting 0 while the server declines the
    sink (e.g. an unavailable active port), used to exercise the post-switch
    confirmation that turns this into a :class:`~paswitch.audio.SwitchRefusedError`.
    """

    def set_default(self, sink: Sink) -> None:
        self.default_set_to = sink  # the request is recorded, but ignored
