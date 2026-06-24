"""Tests for paswitch filesystem locations."""

from __future__ import annotations

from pathlib import Path

import pytest

from paswitch import paths


def test_runtime_dir_uses_xdg_runtime_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    path = paths.runtime_dir()
    assert path == tmp_path / "paswitch"
    assert path is not None and path.is_dir()  # created on demand


def test_runtime_dir_no_xdg_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # No $XDG_RUNTIME_DIR: no per-user runtime dir. We do not fall back to /tmp
    # (wrong lifetime/perms); the sole consumer degrades gracefully without it.
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert paths.runtime_dir() is None


def test_runtime_dir_stale_xdg_returns_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A stale $XDG_RUNTIME_DIR pointing at a missing path yields None rather
    # than being silently created (wrong ownership/perms) or falling back to /tmp.
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "does-not-exist"))
    assert paths.runtime_dir() is None
    assert not (tmp_path / "does-not-exist").exists()  # parent tree not created
