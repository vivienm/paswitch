"""Exclusion globs for skipping sinks when cycling.

A single `--exclude` value (or the `PASWITCH_EXCLUDE` environment variable)
is a colon-separated list of shell-style globs, matched case-insensitively
against a sink's name and description. `Exclude` bundles the parsed
patterns with the matching logic, and `Exclude.parse` doubles as the
Typer `parser` for the `--exclude` option, so the command line and the env
var share one parsing path.
"""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    # `Sink` is needed only for the `match` annotation; importing it at
    # runtime would cycle (audio imports Exclude for cycle_default).
    from .audio import Sink

# Separator between patterns in an `--exclude` value / `PASWITCH_EXCLUDE`.
EXCLUDE_SEP = ":"


@dataclass(frozen=True)
class Exclude:
    """A set of lowercased exclusion globs.

    Patterns are matched as given (case-sensitively, via `fnmatchcase`)
    against the lowercased sink fields, so they must already be lowercased —
    `parse` does this once on entry.
    """

    patterns: tuple[str, ...] = ()

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse a colon-separated value into an `Exclude`.

        Splits on `EXCLUDE_SEP`, strips and lowercases each pattern, and drops
        empty pieces (e.g. a trailing `:`); an empty value yields no patterns.

        >>> Exclude.parse("*hdmi*:*digital*").patterns
        ('*hdmi*', '*digital*')
        >>> Exclude.parse("  Foo : : bar ").patterns
        ('foo', 'bar')
        >>> Exclude.parse("").patterns
        ()
        """
        return cls(
            tuple(
                pattern.strip().lower()
                for pattern in value.split(EXCLUDE_SEP)
                if pattern.strip()
            )
        )

    def match(self, sink: Sink) -> bool:
        """Return whether `sink` matches any of these globs.

        Each pattern is tested against both the sink name and its description;
        the sink fields are lowercased here, the patterns are matched as given.

        >>> from paswitch.audio import Sink
        >>> hdmi = Sink(index=0, name="alsa.hdmi", description="HDMI Output")
        >>> Exclude.parse("*hdmi*").match(hdmi)
        True
        >>> Exclude.parse("bluez*").match(hdmi)
        False
        """
        haystacks = (sink.name.lower(), sink.description.lower())
        return any(
            fnmatchcase(text, pattern)
            for pattern in self.patterns
            for text in haystacks
        )
