"""Audio backend abstraction and sink-switching logic.

The switching logic is kept separate from the sound-server binding so it can be
exercised with a fake backend in tests. `PactlBackend` is the real
implementation: it shells out to `pactl` (the PulseAudio control utility,
provided by `pipewire-pulse` on modern systems) and parses its JSON output,
so no fragile text scraping is involved.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from .exclude import Exclude

logger = logging.getLogger(__name__)


class Direction(StrEnum):
    """Cycling direction for the default sink."""

    NEXT = "next"
    PREV = "prev"


@dataclass(frozen=True, order=True)
class Sink:
    """A playback (output) sink.

    `index` is the sink's stable identity, so only it takes part in ordering
    and equality (the other fields are `compare=False`). `sorted(sinks)`
    thus yields the stable, backend-independent order used for cycling. Callers
    sort explicitly; backends may return sinks in any order.
    """

    index: int
    name: str = field(compare=False)
    description: str = field(compare=False)
    # Whether the sink's active port is usable. Unusable sinks (e.g. an HDMI
    # output with nothing plugged in) are skipped when cycling, since the server
    # would refuse them. Defaults to `True` when the backend gives no port info.
    is_available: bool = field(compare=False, default=True)

    @property
    def label(self) -> str:
        """A human-friendly label, falling back to the name."""
        return self.description or self.name


class AudioError(Exception):
    """Base class for every error raised by this module."""


class BackendError(AudioError):
    """Raised when the underlying sound server cannot be reached or queried."""


class NothingToSwitchError(AudioError):
    """Raised when there are not enough sinks to cycle through."""


class SinkNotFoundError(AudioError):
    """Raised when a requested sink cannot be resolved unambiguously."""


class SwitchRefusedError(AudioError):
    """Raised when the server silently kept the old default after a switch."""


class AudioBackend(Protocol):
    """Minimal backend surface required to switch sinks."""

    def list_sinks(self) -> list[Sink]: ...
    def default_sink_name(self) -> str: ...
    def set_default(self, sink: Sink) -> None: ...


class PactlBackend:
    """Audio backend backed by the `pactl` command-line utility.

    `pactl` talks to the PulseAudio server, or to the PulseAudio-compatible
    interface exposed by `pipewire-pulse` on PipeWire systems.
    """

    def __init__(self, pactl: str = "pactl") -> None:
        self._pactl = pactl

    def _run(self, *args: str) -> str:
        """Run `pactl` with `args` and return its stdout."""
        argv = [self._pactl, *args]
        logger.debug("running %s", argv)
        try:
            proc = subprocess.run(
                argv,
                check=True,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError as exc:
            raise BackendError(f"{self._pactl!r} not found in PATH") from exc
        except OSError as exc:
            # Covers e.g. a pactl that exists but is not executable, or another
            # low-level failure to spawn it — surface it as a backend error
            # (exit 2) rather than an uncaught traceback.
            raise BackendError(f"could not run {self._pactl!r}: {exc}") from exc
        except subprocess.CalledProcessError as exc:
            detail = exc.stderr.strip() or exc.stdout.strip() or "no output"
            raise BackendError(
                f"{' '.join(argv)} failed (exit {exc.returncode}): {detail}"
            ) from exc
        return proc.stdout

    def _run_json(self, *args: str) -> object:
        out = self._run("--format=json", *args)
        try:
            return json.loads(out)
        except json.JSONDecodeError as exc:
            raise BackendError(f"could not parse pactl JSON output: {exc}") from exc

    def list_sinks(self) -> list[Sink]:
        data = self._run_json("list", "sinks")
        if not isinstance(data, list):
            raise BackendError("unexpected pactl output: expected a list of sinks")
        # Skip a malformed entry rather than failing the whole listing: one
        # exotic sink should not stop the user switching between the others.
        sinks = []
        for entry in data:
            try:
                sinks.append(self._parse_sink(entry))
            except BackendError as exc:
                logger.error("skipping malformed sink: %s", exc)
        return sinks

    @staticmethod
    def _parse_sink(entry: object) -> Sink:
        """Build a `Sink` from one `pactl list sinks` JSON object."""
        if not isinstance(entry, dict):
            raise BackendError("unexpected pactl output: sink is not an object")
        index = entry.get("index")
        name = entry.get("name")
        description = entry.get("description")
        if not isinstance(index, int) or not isinstance(name, str):
            raise BackendError("unexpected pactl output: malformed sink fields")
        return Sink(
            index=index,
            name=name,
            description=description if isinstance(description, str) else "",
            is_available=PactlBackend._port_available(entry),
        )

    @staticmethod
    def _port_available(entry: object) -> bool:
        """Whether the sink's active port is usable, from a `list sinks` object.

        `pactl` reports each port's `availability` as `"available"`,
        `"not available"` or `"availability unknown"`. Only an explicit
        `"not available"` means the server will refuse the sink; anything else
        (including a missing `active_port` or ports list) is treated as usable so
        we never hide a sink the server would happily accept.
        """
        if not isinstance(entry, dict):
            return True
        active = entry.get("active_port")
        ports = entry.get("ports")
        if not isinstance(active, str) or not isinstance(ports, list):
            return True
        for port in ports:
            if isinstance(port, dict) and port.get("name") == active:
                return port.get("availability") != "not available"
        return True

    def default_sink_name(self) -> str:
        return self._run("get-default-sink").strip()

    def set_default(self, sink: Sink) -> None:
        self._run("set-default-sink", sink.name)


def pick_sink(
    sinks: Sequence[Sink], current_name: str, direction: Direction = Direction.NEXT
) -> Sink:
    """Return the sink following `current_name` in `direction`.

    Wraps around the list. If `current_name` is not among `sinks` (e.g. the
    current default was excluded from the candidates), the first (next) or last
    (prev) sink is returned. Raises `NothingToSwitchError` when switching
    would be a no-op: there are no sinks, or the only one is already current.
    """
    if not sinks:
        raise NothingToSwitchError("no sinks available")
    names = [s.name for s in sinks]
    step = 1 if direction is Direction.NEXT else -1
    try:
        idx = names.index(current_name)
    except ValueError:
        # The current default is not a candidate, so any sink is a real move.
        # Virtual "before first" position so next -> first, prev -> last.
        idx = -1 if direction is Direction.NEXT else 0
    else:
        if len(sinks) == 1:
            raise NothingToSwitchError("only the current sink is available")
    return sinks[(idx + step) % len(sinks)]


def find_sink(sinks: Sequence[Sink], query: str) -> Sink:
    """Resolve `query` to a single sink.

    Tries an exact match on the sink *name* (its stable technical id) first,
    then a unique case-insensitive substring match against the name or the
    description. Raises `SinkNotFoundError` on no or ambiguous matches.
    """
    for sink in sinks:
        if sink.name == query:
            return sink
    needle = query.lower()
    matches = [
        sink
        for sink in sinks
        if needle in sink.name.lower() or needle in sink.description.lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SinkNotFoundError(f"no sink matching {query!r}")
    names = ", ".join(s.name for s in matches)
    raise SinkNotFoundError(f"ambiguous query {query!r} matches: {names}")


def _set_default_confirmed(backend: AudioBackend, target: Sink) -> Sink:
    """Promote `target` and verify the server actually adopted it.

    `set-default-sink` exits 0 even when the server refuses the sink and keeps
    the old default (see `SwitchRefusedError`), so we re-read the default
    afterwards and raise rather than report a switch that never happened.
    """
    backend.set_default(target)
    if backend.default_sink_name() != target.name:
        raise SwitchRefusedError(
            f"the server kept the previous default instead of {target.name!r} "
            f"(its active port may be unavailable)"
        )
    return target


def cycle_default(
    backend: AudioBackend,
    direction: Direction = Direction.NEXT,
    *,
    exclude: Exclude = Exclude(),
) -> Sink:
    """Cycle the default sink in `direction` and return the new default.

    Sinks are sorted (by `index`) so the cycling order is deterministic and
    independent of the order the backend happens to list them in. Sinks matching
    any glob in `exclude` are skipped (see `Exclude.match`), as are sinks
    whose active port is unavailable (e.g. an HDMI output with nothing plugged
    in): the server would refuse them and leave the default unchanged, so cycling
    onto them would wedge. Both only affect cycling, not `switch_to`.
    """
    sinks = sorted(backend.list_sinks())
    candidates = [s for s in sinks if s.is_available and not exclude.match(s)]
    target = pick_sink(candidates, backend.default_sink_name(), direction)
    return _set_default_confirmed(backend, target)


def switch_to(backend: AudioBackend, query: str) -> Sink:
    """Set the sink matching `query` as the default and return it.

    Raises `SwitchRefusedError` if the server silently keeps the old
    default — e.g. `set` onto an unavailable port that `cycle_default` would
    have skipped but an explicit request still reaches.
    """
    target = find_sink(backend.list_sinks(), query)
    return _set_default_confirmed(backend, target)
