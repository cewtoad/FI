"""Filesystem anchors that work both from source and from a frozen/bundled run.

When packaged with PyInstaller, ``__file__`` points inside ``_internal/``, so
mutable user data (.env, sessions/, downloaded models) must NOT be anchored
there. ``app_root()`` returns the directory the user actually sees:

    source run      -> the project folder (next to this file)
    PyInstaller     -> the folder containing the .exe
    bundled run     -> honour PROJECT_ROOT if the launcher sets it

Everything user-facing (``.env``, ``sessions/``, ``stt_lib/``, ``stt_models/``)
should hang off ``app_root()``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """Directory for user data: next to the exe when frozen, else this folder."""
    override = os.environ.get("F1TR_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_root() -> Path:
    """Directory for bundled read-only resources.

    PyInstaller (onedir) unpacks data under ``sys._MEIPASS``; from source it is
    the same as ``app_root()``.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return app_root()


def subdir(name: str) -> Path:
    """Return ``app_root()/name`` (creating it if it is a data directory)."""
    return app_root() / name
