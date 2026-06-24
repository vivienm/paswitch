"""Filesystem locations owned by paswitch.

Centralises where paswitch keeps its per-user files so every module agrees on
the layout (e.g. `$XDG_RUNTIME_DIR/paswitch/`) instead of scattering ad-hoc
path joins around the codebase.
"""

from __future__ import annotations

import os
from pathlib import Path

_APP_NAME = "paswitch"


def runtime_dir() -> Path | None:
    """Return paswitch's per-user runtime directory, or `None` if none exists."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not Path(runtime).is_dir():
        return None
    path = Path(runtime) / _APP_NAME
    path.mkdir(exist_ok=True)  # base is guaranteed to exist; only make the leaf
    return path
