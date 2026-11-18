"""Input adaptors: turn device input into console button presses.

    from src.bridge.adaptors import InputRouter, KeyboardAdaptor, GamepadAdaptor

    router = InputRouter(machine)            # sink: anything with press/release
    keys = KeyboardAdaptor(router)           # key_down("K") -> press("A")
    pads = GamepadAdaptor(router)            # pads.poll() once per frame
    touch = TouchAdaptor(router, gba_layout(w, h))

All adaptors report through one InputRouter, so a button held by two devices
stays held until both let go. Nothing here imports a UI framework.
"""
from src.bridge.adaptors.console_controller import DEFAULT_PAD_MAP, GamepadAdaptor, held_from_state
from src.bridge.adaptors.keyboard import PRESET_APP, PRESET_CLASSIC, KeyboardAdaptor, normalize_key
from src.bridge.adaptors.mobile import DPad, TouchAdaptor, TouchButton, TouchLayout, gba_layout
from src.bridge.adaptors.router import InputRouter

__all__ = [
    "InputRouter", "KeyboardAdaptor", "GamepadAdaptor", "TouchAdaptor",
    "DPad", "TouchButton", "TouchLayout", "gba_layout", "held_from_state",
    "normalize_key", "PRESET_APP", "PRESET_CLASSIC", "DEFAULT_PAD_MAP",
]
