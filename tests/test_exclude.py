"""Tests for the Exclude exclusion-glob type."""

from __future__ import annotations

from paswitch.exclude import Exclude

from .helpers import sink

# --- parse ------------------------------------------------------------------


def test_exclude_parse_splits_on_colon() -> None:
    assert Exclude.parse("*hdmi*:*digital*").patterns == ("*hdmi*", "*digital*")


def test_exclude_parse_strips_and_lowercases() -> None:
    assert Exclude.parse("  Foo : Bar ").patterns == ("foo", "bar")


def test_exclude_parse_drops_empty_pieces() -> None:
    assert Exclude.parse("::").patterns == ()
    assert Exclude.parse("*hdmi*:").patterns == ("*hdmi*",)
    assert Exclude.parse("").patterns == ()


# --- match ------------------------------------------------------------------


def test_exclude_match_name_and_description() -> None:
    hdmi = sink("alsa.hdmi", "Built-in Audio Digital (HDMI)")
    assert Exclude.parse("*hdmi*").match(hdmi)  # name
    assert Exclude.parse("*digital*").match(hdmi)  # description
    assert not Exclude.parse("bluez*").match(hdmi)


def test_exclude_match_is_case_insensitive_end_to_end() -> None:
    # parse lowercases the patterns and match lowercases the sink fields, so an
    # upper-case glob matches a mixed-case sink.
    assert Exclude.parse("*HDMI*").match(sink("ALSA.HDMI", "HDMI Output"))


def test_exclude_match_patterns_matched_as_given() -> None:
    # match() lowercases the sink fields but not the patterns (fnmatchcase), so
    # a pattern that was not lowered by parse does not match — the contract is
    # "patterns must be lowercased", which parse() enforces.
    assert not Exclude(("*HDMI*",)).match(sink("alsa.hdmi"))


def test_exclude_match_empty_patterns() -> None:
    assert not Exclude.parse("").match(sink("anything"))
    assert not Exclude(()).match(sink("anything"))
