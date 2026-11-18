"""NES (NTSC) adapter around :class:`src.core.nes.nes.NES`.

Video: the PPU draws straight into ``ppu.rgb``, a 256x240 RGB888 buffer, so
``frame_rgb()`` is a plain copy.  The NES pad has no shoulder buttons; L / R
are accepted and ignored so the UI can use one button set for every system.
"""
from __future__ import annotations

from src.bridge.base import Machine, normalize_button
from src.core.nes.common import FPS, HEIGHT, WIDTH
from src.core.nes.nes import NES

_NO_OP = frozenset(("L", "R"))


class NESMachine(Machine):
    """``Machine`` adapter for the NES core."""

    system = "NES"
    width = WIDTH
    height = HEIGHT
    fps = FPS

    def __init__(self, rom):
        """``rom`` is a path to an iNES / NES 2.0 file or its raw bytes."""
        self._rom = rom
        self.core = NES(rom)

    @property
    def title_info(self) -> str:
        return str(self.core.cart)

    def run_frame(self, render: bool = True) -> None:
        ppu = self.core.ppu
        ppu.skip_render = not render
        try:
            self.core.run_frame()
        finally:
            ppu.skip_render = False

    def press(self, button: str) -> None:
        name = normalize_button(button)
        if name not in _NO_OP:
            self.core.press(name)

    def release(self, button: str) -> None:
        name = normalize_button(button)
        if name not in _NO_OP:
            self.core.release(name)

    def frame_rgb(self) -> bytes:
        return bytes(self.core.ppu.rgb)

    @property
    def apu(self):
        return self.core.apu

    def save(self) -> bool:
        return self.core.save()

    def reset(self) -> None:
        self.core.reset()
