"""ROM library: finds .gb / .gbc / .gba / .nes / PS1 (.exe, .cue, .iso) files and reports their system and play history."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from src.ui.data.settings import Settings

ROM_EXTS = {".gb": "GB", ".gbc": "GBC", ".cgb": "GBC", ".gba": "GBA", ".agb": "GBA", ".nes": "NES",
            ".exe": "PS1", ".psx": "PS1", ".cue": "PS1", ".iso": "PS1"}

# Consoles the bundled core can actually run.
PLAYABLE = {"GB", "GBC", "GBA", "NES", "PS1"}

# Native screen size per system.
NATIVE_SIZE = {"GB": (160, 144), "GBC": (160, 144), "GBA": (240, 160), "NES": (256, 240), "PS1": (320, 240)}


@dataclass
class Game:
    path: str
    title: str
    system: str          # GB | GBC | GBA | NES
    size: int            # bytes
    last_played: float   # epoch, 0 = never
    seconds: float       # total play time

    @property
    def filename(self) -> str:
        return Path(self.path).name

    @property
    def playable(self) -> bool:
        return self.system in PLAYABLE


def _title_from_name(stem: str) -> str:
    stem = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", stem)        # drop (USA), [!] tags
    stem = stem.replace("_", " ").strip()
    return stem or "Untitled"


def _detect_system(path: Path) -> str | None:
    system = ROM_EXTS.get(path.suffix.lower())
    if system == "PS1" and path.suffix.lower() in (".cue", ".iso"):
        return system
    if system == "PS1":
        # only real PS-X EXEs: a ROM folder may well contain Windows .exe files
        from src.core.ps1 import is_exe
        return system if is_exe(path) else None
    if system == "GB":
        try:                       # header CGB flag: 0x80 = enhanced, 0xC0 = CGB only
            with path.open("rb") as f:
                f.seek(0x143)
                flag = f.read(1)
            if flag and flag[0] & 0x80:
                system = "GBC"
        except OSError:
            pass
    return system


def scan(settings: Settings, extra_dirs=(), extra_files=()) -> list[Game]:
    """ROMs under settings.rom_dirs + extra_roms, plus session-only extra dirs/files
    (from the command line; not written to settings)."""
    found: dict[str, Path] = {}
    for d in list(settings.rom_dirs) + list(extra_dirs):
        root = Path(d).expanduser()
        if not root.is_dir():
            continue
        try:
            for p in root.rglob("*"):
                if p.suffix.lower() in ROM_EXTS and p.is_file():
                    found[str(p.resolve())] = p
        except OSError:
            continue
    for f in list(settings.extra_roms) + list(extra_files):
        p = Path(f)
        if p.is_file():
            found[str(p.resolve())] = p

    games = []
    for key, p in found.items():
        system = _detect_system(p)
        if not system:
            continue
        h = settings.history.get(key, {})
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        games.append(Game(key, _title_from_name(p.stem), system, size,
                          float(h.get("last", 0)), float(h.get("seconds", 0))))
    return games


def sort_games(games: list[Game], mode: str) -> list[Game]:
    if mode == "title":
        return sorted(games, key=lambda g: g.title.lower())
    if mode == "system":
        order = {"GB": 0, "GBC": 1, "GBA": 2, "NES": 3, "PS1": 4}
        return sorted(games, key=lambda g: (order.get(g.system, 9), g.title.lower()))
    return sorted(games, key=lambda g: (-g.last_played, g.title.lower()))


def ago(ts: float, never: str = "Never played") -> str:
    if not ts:
        return never
    d = time.time() - ts
    if d < 60:
        return "Just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    if d < 86400:
        return f"{int(d // 3600)} h ago"
    if d < 2 * 86400:
        return "Yesterday"
    if d < 7 * 86400:
        return f"{int(d // 86400)} days ago"
    if d < 14 * 86400:
        return "Last week"
    if d < 31 * 86400:
        return f"{int(d // (7 * 86400))} weeks ago"
    if d < 62 * 86400:
        return "Last month"
    return time.strftime("%d %b %Y", time.localtime(ts))


def played(seconds: float) -> str:
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60} m played" if m >= 60 else f"{m} m played"


def human_size(n: int) -> str:
    if n >= 1 << 20:
        return f"{n / (1 << 20):.0f} MB"
    return f"{max(1, n // 1024)} KB"
