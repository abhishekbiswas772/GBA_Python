"""Transient per-session view state (filters, selected tabs...). Not persisted."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UIState:
    # library
    lib_filter: str = "all"          # all | GB | GBC | GBA | fav
    lib_sort: str = "recent"         # recent | title | system
    lib_search: str = ""
    # player
    player_tab: str = "saves"        # saves | cheats | link
    cheats: dict[str, bool] = field(default_factory=lambda: {"a": True, "b": False, "c": False, "d": True})
    recording: bool = False
    toast: str | None = None         # path of the moment just saved
    # settings
    settings_section: str = "Controls"
    settings_device: str = "kb"      # kb | pad
    settings_system: str = "GBA"     # GB | GBC | GBA | NES: which keyboard layout is being edited
    listening: tuple[str, str] | None = None   # ("button", "Up", col) / ("hotkey", id)
    draft_keyboards: dict[str, dict[str, list[str]]] | None = None   # system -> button -> keys
    draft_hotkeys: dict[str, str] | None = None
    # moments
    moments_filter: str = "all"      # all | CLIP | GIF | SHOT
    moments_sel: str | None = None   # path
    moments_fmt: str = "MP4"
    moments_size: str = "4x"
    moments_inputs: bool = True
    moments_frame: bool = False
    moments_audio: bool = True
    # achievements & link
    together_tab: str = "all"        # all | won | locked
    hardcore: bool = True
    link_mode: str = "host"          # host | join
