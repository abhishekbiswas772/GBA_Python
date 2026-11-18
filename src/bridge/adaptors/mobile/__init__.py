"""Touch adaptor for phones / tablets / touch screens.

Geometry is in the coordinates of the on-screen pad control (pixels from its
top-left). Each finger (pointer id) holds the buttons under it and keeps
tracking while it slides: a thumb rolling across the d-pad changes direction
without lifting, and covering a diagonal holds two directions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from src.bridge.adaptors.router import InputRouter

_SECTORS = (               # 8 sectors of 45 degrees, starting at "right", counter-clockwise
    ("Right",), ("Up", "Right"), ("Up",), ("Up", "Left"),
    ("Left",), ("Down", "Left"), ("Down",), ("Down", "Right"),
)


@dataclass
class DPad:
    cx: float
    cy: float
    radius: float
    deadzone: float = 0.22             # fraction of radius with no direction
    diagonals: bool = True

    def buttons_at(self, x: float, y: float) -> tuple[str, ...]:
        dx, dy = x - self.cx, self.cy - y            # screen y grows downwards
        dist = math.hypot(dx, dy)
        if dist < self.deadzone * self.radius or dist > self.radius * 1.35:
            return ()
        ang = math.degrees(math.atan2(dy, dx)) % 360
        if self.diagonals:
            return _SECTORS[int((ang + 22.5) // 45) % 8]
        return _SECTORS[2 * (int((ang + 45) // 90) % 4)]


@dataclass
class TouchButton:
    name: str
    cx: float
    cy: float
    radius: float

    def hit(self, x: float, y: float) -> bool:
        return math.hypot(x - self.cx, y - self.cy) <= self.radius


@dataclass
class TouchLayout:
    dpad: DPad | None = None
    buttons: list[TouchButton] = field(default_factory=list)

    def buttons_at(self, x: float, y: float) -> set[str]:
        held = set(self.dpad.buttons_at(x, y)) if self.dpad else set()
        for b in self.buttons:
            if b.hit(x, y):
                held.add(b.name)
        return held


def gba_layout(width: float, height: float, shoulders: bool = True) -> TouchLayout:
    """A default landscape layout scaled to a width x height pad area."""
    r = min(width, height)
    layout = TouchLayout(
        dpad=DPad(cx=r * 0.30, cy=height * 0.55, radius=r * 0.26),
        buttons=[
            TouchButton("A", width - r * 0.14, height * 0.45, r * 0.11),
            TouchButton("B", width - r * 0.38, height * 0.60, r * 0.11),
            TouchButton("Select", width * 0.42, height * 0.92, r * 0.07),
            TouchButton("Start", width * 0.58, height * 0.92, r * 0.07),
        ])
    if shoulders:
        layout.buttons += [TouchButton("L", r * 0.12, r * 0.08, r * 0.10),
                           TouchButton("R", width - r * 0.12, r * 0.08, r * 0.10)]
    return layout


class TouchAdaptor:
    def __init__(self, router: InputRouter, layout: TouchLayout, source: str = "touch"):
        self.router = router
        self.layout = layout
        self.source = source
        self._fingers: dict[int, set[str]] = {}

    def down(self, pointer: int, x: float, y: float) -> None:
        self._fingers[pointer] = self.layout.buttons_at(x, y)
        self._sync()

    def move(self, pointer: int, x: float, y: float) -> None:
        if pointer in self._fingers:
            self._fingers[pointer] = self.layout.buttons_at(x, y)
            self._sync()

    def up(self, pointer: int) -> None:
        if self._fingers.pop(pointer, None) is not None:
            self._sync()

    def cancel(self) -> None:
        self._fingers.clear()
        self._sync()

    def held(self) -> set[str]:
        out: set[str] = set()
        for s in self._fingers.values():
            out |= s
        return out

    def _sync(self) -> None:
        self.router.set_held(self.source, self.held())
