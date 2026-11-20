"""Game Boy Advance adapter around the native core (:class:`src.core.gba.GbaCore`).

Video: the PPU draws a 240x160 framebuffer of little-endian 15-bit BGR555
pixels; the core converts it to RGB888 (5-to-8 bit: ``v << 3 | v >> 2``).

Buttons map to the GBA keypad names (lowercase); L / R are supported here.
"""
from __future__ import annotations

from src.bridge.base import Machine, normalize_button
from src.core.gba import CPU_HZ, FRAME_CYCLES, HEIGHT, WIDTH, Audio, GbaCore

FPS = CPU_HZ / FRAME_CYCLES                      # 16_777_216 / (1232 * 228) Hz


def _exp5(v: int) -> int:
    """Expand a 5-bit colour channel to 8 bits (31 -> 255)."""
    return (v << 3) | (v >> 2)


_R_T = bytes(_exp5(v & 0x1F) for v in range(256))             # lo byte -> red
_B_T = bytes(_exp5((v >> 2) & 0x1F) for v in range(256))      # hi byte -> blue
_GL_T = bytes(v >> 5 for v in range(256))                     # lo byte -> green bits 0-2
_GH_T = bytes((v & 3) << 3 for v in range(256))               # hi byte -> green bits 3-4
_GX_T = bytes(_exp5(v) if v < 32 else 0 for v in range(256))  # 5-bit green -> 8-bit


def bgr555_to_rgb888(data) -> bytes:
    """Convert little-endian BGR555 pixels (2 bytes each) to packed RGB888 (pure Python;
    the core does the same natively for its own frames)."""
    raw = bytes(data)
    n = len(raw) >> 1
    if n == 0:
        return b""
    lo, hi = raw[0::2], raw[1::2]
    # green = (lo >> 5) | ((hi & 3) << 3); per-byte sums stay < 256, so the big-int
    # addition is exactly a byte-wise add without carries
    g5 = (int.from_bytes(lo.translate(_GL_T), "big")
          + int.from_bytes(hi.translate(_GH_T), "big")).to_bytes(n, "big")
    out = bytearray(n * 3)
    out[0::3], out[1::3], out[2::3] = lo.translate(_R_T), g5.translate(_GX_T), hi.translate(_B_T)
    return bytes(out)


class _MachineAudio(Audio):
    """`apu` that follows ``machine.core`` (replaced by reset or a state load)."""

    def __init__(self, machine):
        self._m = machine

    @property
    def _core(self):
        return self._m.core


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
        self.core = GbaCore(rom, bios=bios)
        self._apu = _MachineAudio(self)

    # ------------------------------------------------------------------ info
    @property
    def title_info(self) -> str:
        h = self.core.cart.header
        return f"{h.title} [{h.game_code}]"

    def state_blobs(self) -> list:
        """The ROM (first: it identifies the game) and the BIOS, kept out of every state."""
        core = self.core
        return [core.rom] + ([core.bios] if core.bios is not None else [])

    # ------------------------------------------------------------------ running
    def run_frame(self, render: bool = True) -> None:
        self.core.run_frame(render)

    # ------------------------------------------------------------------ input
    def press(self, button: str) -> None:
        self.core.press(normalize_button(button).lower())

    def release(self, button: str) -> None:
        self.core.release(normalize_button(button).lower())

    # ------------------------------------------------------------------ video
    @property
    def bgr555_frame(self) -> bytes:
        """Raw framebuffer: 240x160 little-endian BGR555 pixels."""
        return self.core.fb

    def frame_rgb(self) -> bytes:
        """RGB888 frame (240*160*3 bytes)."""
        return self.core.gba.frame_rgb()

    # ------------------------------------------------------------------ audio
    @property
    def apu(self):
        return self._apu

    # ------------------------------------------------------------------ lifecycle
    def save(self) -> bool:
        return self.core.save()

    def reset(self) -> None:
        self.core.save()
        self.core = GbaCore(self._rom, bios=self._bios)
