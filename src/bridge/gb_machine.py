"""Game Boy / Game Boy Color adapter around :class:`src.core.gameboy.gameboy.GameBoy`.

Video output:

* CGB mode uses the core's RGB888 buffer (``ppu.rgb``, 160x144x3 bytes),
* DMG mode exposes the raw shade-index framebuffer (``ppu.framebuffer``,
  values 0-3) and colours it through a 4-shade palette.  The palette may be
  passed to the constructor or to every ``frame_rgb()`` call, so the UI can
  keep applying its own GB_PALETTES theming; a classic grayscale default is
  used when none is given.

Buttons map to the core's joypad names (lowercase).  "L" / "R" do not exist
on Game Boy hardware and are accepted as no-ops.
"""
from __future__ import annotations

from src.bridge.base import Machine, normalize_button
from src.core.gameboy.common import (
    CYCLES_PER_SECOND, DMG_PALETTE, FRAME_DOTS, HEIGHT, WIDTH,
)
from src.core.gameboy.gameboy import GameBoy

# UI button -> GameBoy.joypad name (the canonical name lowercased).
_GB_BUTTONS = {"A", "B", "Start", "Select", "Up", "Down", "Left", "Right"}

FPS = CYCLES_PER_SECOND / FRAME_DOTS              # 4_194_304 / 70_224 Hz


def _palette_tables(palette) -> tuple[bytes, bytes, bytes]:
    return tuple(bytes(palette[i & 3][ch] for i in range(256)) for ch in range(3))


def dmg_rgb(framebuffer, palette=DMG_PALETTE) -> bytes:
    """Colourise a DMG shade-index framebuffer (values 0-3) to RGB888."""
    src = bytes(framebuffer)
    r, g, b = _palette_tables(tuple(tuple(c) for c in palette))
    n = len(src)
    out = bytearray(n * 3)
    out[0::3] = src.translate(r)
    out[1::3] = src.translate(g)
    out[2::3] = src.translate(b)
    return bytes(out)


class GameBoyMachine(Machine):
    """``Machine`` adapter for the Game Boy core (DMG and CGB)."""

    width = WIDTH
    height = HEIGHT
    fps = FPS

    def __init__(self, path, cgb: bool | None = None, palette=None):
        """``cgb=None`` auto-detects from the header; ``palette`` is an
        optional 4-entry sequence of (r, g, b) triples used for DMG output."""
        self._path = path
        self._cgb_opt = cgb
        self._palette = tuple(tuple(c) for c in palette) if palette is not None else None
        self.core = GameBoy(path, cgb=cgb)

    # ------------------------------------------------------------------ info
    @property
    def system(self) -> str:
        return "GBC" if self.core.cgb else "GB"

    @property
    def cgb(self) -> bool:
        return self.core.cgb

    @property
    def title_info(self) -> str:
        h = self.core.cart.header
        return f"{h.title} [{h.type_name}]"

    # ------------------------------------------------------------------ running
    def run_frame(self) -> None:
        self.core.run_frame()

    # ------------------------------------------------------------------ input
    def press(self, button: str) -> None:
        name = normalize_button(button)
        if name in _GB_BUTTONS:
            self.core.joypad.press(name.lower())

    def release(self, button: str) -> None:
        name = normalize_button(button)
        if name in _GB_BUTTONS:
            self.core.joypad.release(name.lower())

    # ------------------------------------------------------------------ video
    @property
    def dmg_frame(self) -> bytes:
        """Raw DMG framebuffer: one shade index (0-3) per pixel."""
        return bytes(self.core.ppu.framebuffer)

    def frame_rgb(self, palette=None) -> bytes:
        """RGB888 frame.  ``palette`` (4 (r,g,b) triples) overrides the
        constructor palette for DMG output; ignored in CGB mode."""
        if self.core.cgb:
            return bytes(self.core.ppu.rgb)
        pal = palette if palette is not None else self._palette
        return dmg_rgb(self.core.ppu.framebuffer, pal) if pal is not None \
            else dmg_rgb(self.core.ppu.framebuffer)

    # ------------------------------------------------------------------ audio
    @property
    def apu(self):
        return self.core.apu

    # ------------------------------------------------------------------ lifecycle
    def save(self) -> bool:
        return self.core.save()

    def reset(self) -> None:
        self.core.save()
        self.core = GameBoy(self._path, cgb=self._cgb_opt)
