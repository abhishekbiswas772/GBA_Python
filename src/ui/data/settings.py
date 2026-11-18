"""User settings and play history, persisted as JSON.

Stored in Flet's per-app storage dir when packaged (desktop, mobile), else in
~/.gba_python. Everything the UI lets you change that should survive a restart
lives here; transient view state lives in `src.ui.state.UIState`.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

# Console buttons in the order the Controls table shows them.
CONSOLE_BUTTONS = ["Up", "Down", "Left", "Right", "A", "B", "L", "R", "Start", "Select"]
GBA_ONLY = {"L", "R"}

DEFAULT_KEYBOARD: dict[str, list[str]] = {
    "Up": ["W", "Arrow Up"],
    "Down": ["S", "Arrow Down"],
    "Left": ["A", "Arrow Left"],
    "Right": ["D", "Arrow Right"],
    "A": ["K", ""],
    "B": ["J", ""],
    "L": ["Q", ""],
    "R": ["E", ""],
    "Start": ["Enter", ""],
    "Select": ["Backspace", ""],
}

GAMEPAD_LAYOUT: dict[str, list[str]] = {
    "Up": ["D-pad ↑", "L-stick ↑"],
    "Down": ["D-pad ↓", "L-stick ↓"],
    "Left": ["D-pad ←", "L-stick ←"],
    "Right": ["D-pad →", "L-stick →"],
    "A": ["Button A", ""],
    "B": ["Button B", ""],
    "L": ["LB", ""],
    "R": ["RB", ""],
    "Start": ["Menu", ""],
    "Select": ["View", ""],
}

# id -> (label, default key)
HOTKEYS: dict[str, tuple[str, str]] = {
    "fast_forward": ("Fast forward (hold)", "Tab"),
    "rewind": ("Rewind (hold)", "R"),
    "pause": ("Pause / resume", "P"),
    "quick_save": ("Quick save", "F1"),
    "quick_load": ("Quick load", "F4"),
    "screenshot": ("Screenshot", "F12"),
    "next_slot": ("Next save slot", "]"),
    "prev_slot": ("Previous save slot", "["),
    "fullscreen": ("Toggle fullscreen", "F11"),
}


def data_dir() -> Path:
    base = os.environ.get("FLET_APP_STORAGE_DATA")
    path = Path(base) if base else Path.home() / ".gba_python"
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Settings:
    theme: str = "daylight"
    match_console: bool = False
    follow_system: bool = False
    shader: str = "lcd"
    gb_palette: str = "pea"
    lcd_colors: bool = True
    integer_scale: bool = True
    frame_blend: bool = False
    volume: int = 70
    replay_buffer: bool = True
    rom_dirs: list[str] = field(default_factory=lambda: [str(Path.home() / "Games" / "ROMs")])
    extra_roms: list[str] = field(default_factory=list)
    keyboard: dict[str, list[str]] = field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_KEYBOARD.items()})
    hotkeys: dict[str, str] = field(default_factory=lambda: {k: v[1] for k, v in HOTKEYS.items()})
    favorites: list[str] = field(default_factory=list)
    # rom path -> {"last": epoch seconds, "seconds": total play time}
    history: dict[str, dict] = field(default_factory=dict)
    # Game Boy printer, webcam camera, rumble & tilt
    printer: bool = True
    webcam: bool = False
    rumble: bool = True

    @classmethod
    def path(cls) -> Path:
        return data_dir() / "settings.json"

    @classmethod
    def load(cls) -> "Settings":
        s = cls()
        try:
            raw = json.loads(cls.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return s
        for k, v in raw.items():
            if hasattr(s, k) and type(v) is type(getattr(s, k)):
                setattr(s, k, v)
        for k, v in DEFAULT_KEYBOARD.items():           # new buttons added later
            s.keyboard.setdefault(k, list(v))
        for k, v in HOTKEYS.items():
            s.hotkeys.setdefault(k, v[1])
        return s

    def save(self) -> None:
        p = self.path()
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            print(f"[ui] could not save settings: {e}")

    def reset_controls(self) -> None:
        self.keyboard = {k: list(v) for k, v in DEFAULT_KEYBOARD.items()}
        self.hotkeys = {k: v[1] for k, v in HOTKEYS.items()}
