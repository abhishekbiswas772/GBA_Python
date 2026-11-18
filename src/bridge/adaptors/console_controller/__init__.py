"""Gamepad adaptor (Xbox / PlayStation / Switch Pro / generic) via SDL GameController.

Uses pygame's SDL2 game-controller API, which gives every supported pad the
same standard layout and works without a pygame window (the Flet app has
none). Polled: call poll() once per emulated frame; it reports the full held
set of every connected pad to the router. Hot-plug is checked periodically.

Layout (matches the desktop app's Controls screen):
  D-pad / left stick -> D-pad, A -> A, B -> B, LB / LT -> L, RB / RT -> R,
  Menu (Start) -> Start, View (Back) -> Select.
"""
from __future__ import annotations

from src.bridge.adaptors.router import InputRouter

# SDL_GameControllerButton values (stable across SDL2 versions)
BTN_A, BTN_B, BTN_X, BTN_Y = 0, 1, 2, 3
BTN_BACK, BTN_GUIDE, BTN_START = 4, 5, 6
BTN_LSTICK, BTN_RSTICK, BTN_LB, BTN_RB = 7, 8, 9, 10
BTN_UP, BTN_DOWN, BTN_LEFT, BTN_RIGHT = 11, 12, 13, 14
AXIS_LX, AXIS_LY, AXIS_RX, AXIS_RY, AXIS_LT, AXIS_RT = 0, 1, 2, 3, 4, 5
AXIS_MAX = 32767

DEFAULT_PAD_MAP: dict[int, str] = {
    BTN_A: "A", BTN_B: "B", BTN_LB: "L", BTN_RB: "R",
    BTN_START: "Start", BTN_BACK: "Select",
    BTN_UP: "Up", BTN_DOWN: "Down", BTN_LEFT: "Left", BTN_RIGHT: "Right",
}


def held_from_state(buttons: dict[int, bool], axes: dict[int, int], mapping=None,
                    deadzone: float = 0.5) -> set[str]:
    """Pure mapping from a pad snapshot to console buttons (testable without SDL)."""
    mapping = DEFAULT_PAD_MAP if mapping is None else mapping
    held = {name for btn, name in mapping.items() if buttons.get(btn)}
    dz = int(deadzone * AXIS_MAX)
    lx, ly = axes.get(AXIS_LX, 0), axes.get(AXIS_LY, 0)
    if lx <= -dz:
        held.add("Left")
    elif lx >= dz:
        held.add("Right")
    if ly <= -dz:
        held.add("Up")
    elif ly >= dz:
        held.add("Down")
    if axes.get(AXIS_LT, 0) >= dz:
        held.add("L")
    if axes.get(AXIS_RT, 0) >= dz:
        held.add("R")
    return held


class GamepadAdaptor:
    HOTPLUG_EVERY = 120          # polls between controller re-scans (~2 s)

    def __init__(self, router: InputRouter, mapping: dict[int, str] | None = None,
                 deadzone: float = 0.5, source: str = "gamepad"):
        self.router = router
        self.mapping = dict(DEFAULT_PAD_MAP if mapping is None else mapping)
        self.deadzone = deadzone
        self.source = source
        self.available = False
        self.error: str | None = None
        self._mod = None
        self._pads: list = []
        self._polls = 0
        self._init()

    def _init(self) -> None:
        try:
            import pygame
            import pygame._sdl2.controller as controller
            pygame.init()
            controller.init()
            self._pygame = pygame
            self._mod = controller
            self.available = True
            self._scan()
        except Exception as e:           # no pygame / SDL build without controllers
            self.available = False
            self.error = str(e)

    def _scan(self) -> None:
        c = self._mod
        pads = []
        try:
            for i in range(c.get_count()):
                if c.is_controller(i):
                    pads.append(c.Controller(i))
        except Exception as e:
            self.error = str(e)
        self._pads = pads

    @property
    def names(self) -> list[str]:
        out = []
        for p in self._pads:
            try:
                out.append(p.name)
            except Exception:
                pass
        return out

    @property
    def connected(self) -> bool:
        return bool(self._pads)

    def poll(self) -> set[str]:
        """Read every pad and report the merged held set to the router."""
        if not self.available:
            return set()
        self._polls += 1
        if self._polls % self.HOTPLUG_EVERY == 1:
            self._scan()
        try:
            self._pygame.event.pump()
            self._mod.update()
        except Exception:
            pass
        held: set[str] = set()
        alive = []
        for pad in self._pads:
            try:
                if not pad.attached():
                    continue
                buttons = {b: bool(pad.get_button(b)) for b in self.mapping}
                axes = {a: pad.get_axis(a) for a in (AXIS_LX, AXIS_LY, AXIS_LT, AXIS_RT)}
            except Exception:
                continue
            alive.append(pad)
            held |= held_from_state(buttons, axes, self.mapping, self.deadzone)
        self._pads = alive
        self.router.set_held(self.source, held)
        return held

    def close(self) -> None:
        self.router.release_all(self.source)
        self._pads = []
