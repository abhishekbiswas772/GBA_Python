"""Keyboard adaptor: key names -> console buttons.

Accepts key names from both front ends and normalises them to one form
(upper case, Flet spelling): Flet sends "Arrow Up", "K", "Enter"; pygame's
key.name() gives "up", "k", "return". So one mapping serves the desktop app
and the CLI window.
"""
from __future__ import annotations

from src.bridge.adaptors.router import InputRouter
from src.bridge.base import normalize_button

# default layout of the desktop app (matches src.ui.data.settings.DEFAULT_KEYBOARD)
PRESET_APP: dict[str, list[str]] = {
    "Up": ["W", "Arrow Up"], "Down": ["S", "Arrow Down"],
    "Left": ["A", "Arrow Left"], "Right": ["D", "Arrow Right"],
    "A": ["K"], "B": ["J"], "L": ["Q"], "R": ["E"],
    "Start": ["Enter"], "Select": ["Backspace"],
}
# layout of the CLI pygame window (also listed in `python main.py --help`)
PRESET_CLASSIC: dict[str, list[str]] = {
    "Up": ["Arrow Up"], "Down": ["Arrow Down"],
    "Left": ["Arrow Left"], "Right": ["Arrow Right"],
    "A": ["X"], "B": ["Z"], "L": ["A"], "R": ["S"],
    "Start": ["Enter"], "Select": ["Shift Right", "Backspace"],
}

_ALIASES = {
    # pygame key.name() spellings -> Flet spellings
    "UP": "ARROW UP", "DOWN": "ARROW DOWN", "LEFT": "ARROW LEFT", "RIGHT": "ARROW RIGHT",
    "RETURN": "ENTER", "KEYPAD ENTER": "ENTER", "NUMPAD ENTER": "ENTER", "ENTER": "ENTER",
    "RIGHT SHIFT": "SHIFT RIGHT", "LEFT SHIFT": "SHIFT LEFT",
    "RIGHT CTRL": "CONTROL RIGHT", "LEFT CTRL": "CONTROL LEFT",
    "RIGHT ALT": "ALT RIGHT", "LEFT ALT": "ALT LEFT",
    "SPACE": " ", "ESC": "ESCAPE",
    "BRACKET RIGHT": "]", "BRACKET LEFT": "[",
}


def normalize_key(key: str) -> str:
    k = (key or "").strip().upper() if key != " " else " "
    return _ALIASES.get(k, k)


class KeyboardAdaptor:
    def __init__(self, router: InputRouter, mapping: dict[str, list[str]] | None = None,
                 source: str = "keyboard"):
        self.router = router
        self.source = source
        self.keymap: dict[str, str] = {}
        self.set_mapping(mapping or PRESET_APP)

    def set_mapping(self, mapping: dict[str, list[str]]) -> None:
        """mapping: button -> list of key names (empty strings ignored)."""
        km = {}
        for button, keys in mapping.items():
            name = normalize_button(button)
            for k in keys:
                if k:
                    km[normalize_key(k)] = name
        if km != getattr(self, "keymap", None):
            self.router.release_all(self.source)      # a remap must not leave keys stuck
            self.keymap = km

    def button_for(self, key: str) -> str | None:
        return self.keymap.get(normalize_key(key))

    def key_down(self, key: str) -> bool:
        """Returns True when the key is mapped to a button (event consumed)."""
        b = self.button_for(key)
        if b is None:
            return False
        self.router.press(self.source, b)
        return True

    def key_up(self, key: str) -> bool:
        b = self.button_for(key)
        if b is None:
            return False
        self.router.release(self.source, b)
        return True

    # ------------------------------------------------------------ pygame helper
    def handle_pygame(self, event) -> bool:
        """Feed a pygame KEYDOWN / KEYUP event (key repeat is ignored)."""
        import pygame
        name = pygame.key.name(event.key)
        if event.type == pygame.KEYDOWN:
            if getattr(event, "repeat", False):
                return self.button_for(name) is not None
            return self.key_down(name)
        if event.type == pygame.KEYUP:
            return self.key_up(name)
        return False
