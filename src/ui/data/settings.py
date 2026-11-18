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
SYSTEMS = ("GB", "GBC", "GBA", "NES")


def buttons_for(system: str) -> list[str]:
    """Console buttons that exist on `system` (shoulder buttons only on GBA)."""
    return [b for b in CONSOLE_BUTTONS if system == "GBA" or b not in GBA_ONLY]


# Game Boy / Game Boy Color: d-pad on arrows or WASD, A/B on K/J (or X/Z).
DEFAULT_KEYBOARD_GB: dict[str, list[str]] = {
    "Up": ["Arrow Up", "W"],
    "Down": ["Arrow Down", "S"],
    "Left": ["Arrow Left", "A"],
    "Right": ["Arrow Right", "D"],
    "A": ["K", "X"],
    "B": ["J", "Z"],
    "L": ["", ""],
    "R": ["", ""],
    "Start": ["Enter", ""],
    "Select": ["Backspace", "Shift Right"],
}
# Game Boy Advance: the same, plus the shoulder buttons on Q/E (or 1/3 style: U/I).
DEFAULT_KEYBOARD_GBA: dict[str, list[str]] = {
    **DEFAULT_KEYBOARD_GB,
    "L": ["Q", "U"],
    "R": ["E", "I"],
}
DEFAULT_KEYBOARDS: dict[str, dict[str, list[str]]] = {
    "GB": DEFAULT_KEYBOARD_GB, "GBC": DEFAULT_KEYBOARD_GB, "GBA": DEFAULT_KEYBOARD_GBA,
    "NES": DEFAULT_KEYBOARD_GB,
}
DEFAULT_KEYBOARD = DEFAULT_KEYBOARD_GBA        # legacy name (one layout for every system)


def default_keyboards() -> dict[str, dict[str, list[str]]]:
    return {sysname: {b: list(k) for b, k in m.items()} for sysname, m in DEFAULT_KEYBOARDS.items()}

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
    # not Tab: Tab moves keyboard focus in Flutter and the game would stop hearing keys
    "fast_forward": ("Fast forward (hold)", "F"),
    "rewind": ("Rewind (hold)", "R"),
    "pause": ("Pause / resume", "P"),
    "quick_save": ("Quick save", "F1"),
    "quick_load": ("Quick load", "F4"),
    "screenshot": ("Screenshot", "F12"),
    "record": ("Start / stop recording", "F10"),
    "clip": ("Clip the last 30 s", "F9"),
    "next_slot": ("Next save slot", "]"),
    "prev_slot": ("Previous save slot", "["),
    "fullscreen": ("Toggle fullscreen", "F11"),
}


def data_dir() -> Path:
    base = os.environ.get("FLET_APP_STORAGE_DATA")
    path = Path(base) if base else Path.home() / ".gba_python"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_save_dir() -> Path:
    """Battery saves: ./saves next to main.py when running from source, the app
    data folder's saves/ when packaged."""
    if os.environ.get("FLET_APP_STORAGE_DATA"):
        return data_dir() / "saves"
    return Path(__file__).resolve().parents[3] / "saves"


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
    save_dir: str = ""               # battery saves; "" = default_save_dir()
    # one keyboard layout per system: {"GB": {button: [primary, secondary]}, "GBC": ..., "GBA": ...}
    keyboards: dict[str, dict[str, list[str]]] = field(default_factory=default_keyboards)
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
        if not isinstance(raw, dict):
            return s
        for k, v in raw.items():
            if k == "keyboard":                         # legacy, migrated below
                continue
            if hasattr(s, k) and type(v) is type(getattr(s, k)):
                setattr(s, k, v)
        legacy = raw.get("keyboard")
        if "keyboards" not in raw and isinstance(legacy, dict):
            # one shared layout from older versions: start every system from it
            for sysname in SYSTEMS:
                s.keyboards[sysname] = {b: list(legacy.get(b, d)) for b, d in DEFAULT_KEYBOARDS[sysname].items()}
                if sysname != "GBA":
                    for b in GBA_ONLY:
                        s.keyboards[sysname][b] = ["", ""]
        s.keyboards = s._clean_keyboards(s.keyboards)
        for k, v in HOTKEYS.items():
            s.hotkeys.setdefault(k, v[1])
        if s.hotkeys.get("fast_forward") == "Tab":      # old default stole keyboard focus
            new = HOTKEYS["fast_forward"][1]
            taken = {k.upper() for m in s.keyboards.values() for keys in m.values() for k in keys if k}
            taken |= {k.upper() for h, k in s.hotkeys.items() if k and h != "fast_forward"}
            s.hotkeys["fast_forward"] = "" if new.upper() in taken else new   # never a silent clash
        return s

    @staticmethod
    def _clean_keyboards(kbs) -> dict[str, dict[str, list[str]]]:
        """Every system present, every button a [primary, secondary] pair of strings."""
        out = default_keyboards()
        if not isinstance(kbs, dict):
            return out
        for sysname in SYSTEMS:
            m = kbs.get(sysname)
            if not isinstance(m, dict):
                continue
            for b in CONSOLE_BUTTONS:
                keys = m.get(b)
                if isinstance(keys, list):
                    keys = [k if isinstance(k, str) else "" for k in keys][:2]
                    out[sysname][b] = keys + [""] * (2 - len(keys))
        return out

    def save_folder(self) -> Path:
        """Folder battery saves go to (one subfolder per core, then per game)."""
        return Path(self.save_dir).expanduser() if self.save_dir else default_save_dir()

    def apply_save_dir(self) -> Path | None:
        """Make sure the save folder exists and point every core at it."""
        from src.core.adaptors.saves import set_save_dir
        return set_save_dir(self.save_folder())

    def keyboard_for(self, system: str) -> dict[str, list[str]]:
        """Key layout used while playing `system` ("GB", "GBC", "GBA" or "NES")."""
        m = self.keyboards.get(system) or self.keyboards.get("GBA") or DEFAULT_KEYBOARD_GBA
        allowed = set(buttons_for(system))
        return {b: list(k) for b, k in m.items() if b in allowed}

    @property
    def keyboard(self) -> dict[str, list[str]]:
        """Legacy single layout (the GBA one)."""
        return self.keyboard_for("GBA")

    def save(self) -> None:
        p = self.path()
        tmp = p.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
            os.replace(tmp, p)
        except OSError as e:
            print(f"[ui] could not save settings: {e}")

    def reset_controls(self) -> None:
        self.keyboards = default_keyboards()
        self.hotkeys = {k: v[1] for k, v in HOTKEYS.items()}
