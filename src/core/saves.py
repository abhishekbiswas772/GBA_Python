"""Where battery saves (.sav) live.

By default (``set_save_dir`` never called) a save sits next to its ROM as
``<rom>.sav``, which is what the cores and their tests expect.  The app and
``main.py`` call :func:`set_save_dir` at start-up, and saves then go to
``<save dir>/<core>/<game>/<game>.sav`` (core = "gb", "gbc", "gba" or "nes", game =
the ROM file name without its extension): one folder per game.  A game's
folder is only created when it first writes a save.

Moving to a save folder never loses progress: if a game still has a ``.sav``
next to its ROM and none in the save folder yet, that file is *copied* over the
first time the game loads.  The old file is left untouched.
"""
from __future__ import annotations

import shutil
from pathlib import Path

_SAVE_DIR: Path | None = None
CORES = ("gb", "gbc", "gba", "nes")       # one folder per console


def set_save_dir(path) -> Path | None:
    """Use `path` (created if missing) for every save from now on; None = next to the ROM.
    Returns the folder in use, or None if it could not be created."""
    global _SAVE_DIR
    if path is None or str(path) == "":
        _SAVE_DIR = None
        return None
    p = Path(path).expanduser()
    try:
        p.mkdir(parents=True, exist_ok=True)
        for core in CORES:
            (p / core).mkdir(exist_ok=True)
    except OSError as e:
        print(f"[warning: can't create save folder {p}: {e}; saving next to ROMs]")
        _SAVE_DIR = None
        return None
    _SAVE_DIR = p
    return p


def save_dir() -> Path | None:
    return _SAVE_DIR


def sav_path_for(rom, core: str) -> Path:
    """The .sav file for ROM `rom` of core `core` ("gb", "gbc", "gba", "nes")."""
    rom = Path(rom)
    legacy = rom.with_suffix(".sav")
    if _SAVE_DIR is None:
        return legacy
    target = _SAVE_DIR / core / rom.stem / (rom.stem + ".sav")
    try:
        if not target.exists() and legacy.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, target)          # migrate: copy, never move
    except OSError as e:
        print(f"[warning: save folder unavailable ({e}); using {legacy.name} next to the ROM]")
        return legacy
    return target
