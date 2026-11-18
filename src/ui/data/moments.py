"""Moments: screenshots, recordings and clips (MP4 with sound, or GIF) from the player.

Files live in <data dir>/moments. The file name carries the game and system so the
gallery can show "Title · GB · 2 h ago" without a database:
    <title>__<SYSTEM>__<unix time>.png | .gif | .mp4
Videos get a poster frame in .posters/<stem>.png for the gallery.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path

from src.ui.data.library import ago
from src.ui.data.settings import data_dir

KINDS = {".png": "SHOT", ".gif": "GIF", ".mp4": "CLIP", ".webm": "CLIP"}


def moments_dir() -> Path:
    d = data_dir() / "moments"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Moment:
    path: str
    title: str
    game: str
    system: str
    kind: str        # SHOT | GIF | CLIP
    created: float

    @property
    def meta(self) -> str:
        return f"{self.game} · {self.system} · {ago(self.created, '')}"

    @property
    def length(self) -> str:
        return {"SHOT": "PNG", "GIF": "GIF", "CLIP": "MP4"}.get(self.kind, self.kind)

    @property
    def image(self) -> str | None:
        """What the gallery can show: the file itself, or a video's poster frame."""
        if self.kind in ("SHOT", "GIF"):
            return self.path
        p = poster_for(self.path)
        return str(p) if p.is_file() else None


def poster_for(path) -> Path:
    p = Path(path)
    return p.parent / ".posters" / f"{p.stem}.png"


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9 \-]+", "", s).strip()[:60] or "Game"


def new_path(game: str, system: str, ext: str = ".png") -> Path:
    return moments_dir() / f"{_safe(game)}__{system}__{int(time.time())}{ext}"


def list_moments() -> list[Moment]:
    out = []
    for p in moments_dir().iterdir():
        kind = KINDS.get(p.suffix.lower())
        if not kind:
            continue
        parts = p.stem.split("__")
        game = parts[0] if parts else p.stem
        system = parts[1] if len(parts) > 1 else "GB"
        try:
            created = float(parts[2]) if len(parts) > 2 else p.stat().st_mtime
        except ValueError:
            created = p.stat().st_mtime
        label = {"SHOT": "Screenshot", "GIF": "GIF", "CLIP": "Video"}[kind]
        out.append(Moment(str(p), f"{label} · {time.strftime('%H:%M', time.localtime(created))}",
                          game, system, kind, created))
    return sorted(out, key=lambda m: -m.created)


def new_count(hours: int = 24) -> int:
    cutoff = time.time() - hours * 3600
    return sum(1 for m in list_moments() if m.created >= cutoff)
