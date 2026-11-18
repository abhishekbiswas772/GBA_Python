"""Game Boy Advance adapter around :class:`src.core.gba.gba.GBA`.

Video: the GBA PPU draws into ``ppu.fb``, a 240x160 framebuffer of
little-endian 15-bit BGR555 pixels (``0BBBBBGGGGGRRRRR``, 2 bytes per pixel).
:func:`bgr555_to_rgb888` converts it to packed RGB888 with the standard
5-to-8 bit expansion (``v << 3 | v >> 2``, so 31 maps to 255).  The
conversion is done with ``bytes.translate`` + big-int arithmetic instead of a
per-pixel Python loop: the two green halves (low 3 bits live in the low byte,
high 2 bits in the high byte) are added as parallel byte arrays, which is
carry-free because each byte position sums to at most 31.

Buttons map to the GBA keypad names (lowercase); L / R are supported here.
"""
from __future__ import annotations

from src.bridge.base import Machine, normalize_button
from src.core.gba.common import CPU_HZ, FRAME_CYCLES, HEIGHT, WIDTH
from src.core.gba.gba import GBA

FPS = CPU_HZ / FRAME_CYCLES                      # 16_777_216 / (1232 * 228) Hz


def _exp5(v: int) -> int:
    """Expand a 5-bit colour channel to 8 bits (31 -> 255)."""
    return (v << 3) | (v >> 2)


# byte-lookup tables (indexed by the low or the high byte of the pixel)
_R_T = bytes(_exp5(v & 0x1F) for v in range(256))             # lo byte -> red
_B_T = bytes(_exp5((v >> 2) & 0x1F) for v in range(256))      # hi byte -> blue
_GL_T = bytes(v >> 5 for v in range(256))                     # lo byte -> green bits 0-2
_GH_T = bytes((v & 3) << 3 for v in range(256))               # hi byte -> green bits 3-4
_GX_T = bytes(_exp5(v) if v < 32 else 0 for v in range(256))  # 5-bit green -> 8-bit


def bgr555_to_rgb888(data) -> bytes:
    """Convert little-endian BGR555 pixels (2 bytes each) to packed RGB888."""
    raw = bytes(data)
    n = len(raw) >> 1
    if n == 0:
        return b""
    lo, hi = raw[0::2], raw[1::2]
    r = lo.translate(_R_T)
    b = hi.translate(_B_T)
    # green = (lo >> 5) | ((hi & 3) << 3); per-byte sums stay < 256 so the
    # big-int addition is exactly a byte-wise add without carries
    g5 = (int.from_bytes(lo.translate(_GL_T), "big")
          + int.from_bytes(hi.translate(_GH_T), "big")).to_bytes(n, "big")
    g = g5.translate(_GX_T)
    out = bytearray(n * 3)
    out[0::3], out[1::3], out[2::3] = r, g, b
    return bytes(out)


class GBAMachine(Machine):
    """``Machine`` adapter for the GBA core."""

    system = "GBA"
    width = WIDTH
    height = HEIGHT
    fps = FPS

    def __init__(self, rom, bios=None):
        """``rom`` is a path or raw ROM bytes; ``bios`` is an optional path to
        a real GBA BIOS image (default: the core's HLE BIOS)."""
        self._rom = rom
        self._bios = bios
        self.core = GBA(rom, bios=bios)

    # ------------------------------------------------------------------ info
    @property
    def title_info(self) -> str:
        h = self.core.cart.header
        return f"{h.title} [{h.game_code}]"

    def state_blobs(self) -> list:
        core = self.core
        return [core.cart.rom, core.bus.rom, core.bus.bios]

    # ------------------------------------------------------------------ running
    def run_frame(self, render: bool = True) -> None:
        ppu = self.core.ppu
        ppu.skip_render = not render
        try:
            self.core.run_frame()
        finally:
            ppu.skip_render = False

    # ------------------------------------------------------------------ input
    def press(self, button: str) -> None:
        self.core.press(normalize_button(button).lower())

    def release(self, button: str) -> None:
        self.core.release(normalize_button(button).lower())

    # ------------------------------------------------------------------ video
    @property
    def bgr555_frame(self) -> bytes:
        """Raw framebuffer: 240x160 little-endian BGR555 pixels."""
        return bytes(self.core.ppu.fb)

    def frame_rgb(self) -> bytes:
        """RGB888 frame (240*160*3 bytes)."""
        return bgr555_to_rgb888(self.core.ppu.fb)

    # ------------------------------------------------------------------ audio
    @property
    def apu(self):
        return self.core.apu

    # ------------------------------------------------------------------ lifecycle
    def save(self) -> bool:
        return self.core.save()

    def reset(self) -> None:
        self.core.save()
        self.core = GBA(self._rom, bios=self._bios)
