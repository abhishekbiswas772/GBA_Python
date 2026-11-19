"""PlayStation adapter around the Rust core (:mod:`src.core.ps1`).

The PS1 switches resolution at run time (256 to 640 pixels wide, 240 or 480
lines). The UI, previews and recorder expect one size per session, so frames
are scaled (nearest) onto a fixed 320×240 canvas.

Buttons: the shared button set maps onto the pad as A→✕ (Cross), B→○ (Circle),
L→L1, R→R1. △, □, L2 and R2 can be pressed with :meth:`press_ps1`.
"""
from __future__ import annotations

from array import array
from pathlib import Path

from src.bridge.base import Machine, normalize_button
from src.core import ps1 as core

CANVAS = (320, 240)

# PS1 pad bits (as sent over SIO): Select L3 R3 Start Up Right Down Left L2 R2 L1 R1 △ ○ ✕ □
PAD_BITS = {
    "Select": 0, "L3": 1, "R3": 2, "Start": 3, "Up": 4, "Right": 5, "Down": 6, "Left": 7,
    "L2": 8, "R2": 9, "L1": 10, "R1": 11, "Triangle": 12, "Circle": 13, "Cross": 14, "Square": 15,
}
_FROM_SHARED = {"A": "Cross", "B": "Circle", "L": "L1", "R": "R1"}


class _Audio:
    """The `apu` contract: `sampling` and `take_samples()` -> array('h') (int16 stereo)."""

    def __init__(self, ps1):
        self._ps1 = ps1

    @property
    def sampling(self) -> bool:
        return self._ps1.sampling

    @sampling.setter
    def sampling(self, on: bool) -> None:
        self._ps1.sampling = bool(on)

    def take_samples(self) -> array:
        a = array("h")
        a.frombytes(self._ps1.take_samples())
        return a


class PS1Machine(Machine):
    """``Machine`` adapter for the PS1 core. ``rom``: a PS-X EXE path; ``bios``: optional BIOS path."""

    system = "PS1"
    width, height = CANVAS
    supports_states = False          # save states / rewind: a later milestone

    def __init__(self, rom, bios=None):
        if not core.AVAILABLE:
            raise RuntimeError("The PS1 core isn't built. Run: uv run python -m src.core.ps1.build "
                               f"({core.IMPORT_ERROR})")
        self.path = Path(rom)
        data = self.path.read_bytes()
        if not data.startswith(core.EXE_MAGIC):
            raise ValueError("Only PS-X EXE programs run for now; CD images (.cue/.bin) come with "
                             "the CD-ROM milestone.")
        bios_bytes = Path(bios).read_bytes() if bios else None
        self._bios = bios_bytes
        self._exe = data
        self.core = core.Ps1(bios=bios_bytes, exe=data)
        self.fps = self.core.fps
        self._buttons = 0
        self._apu = _Audio(self.core)
        self.tty = bytearray()

    @property
    def title_info(self) -> str:
        return f"PS-X EXE · {'BIOS' if self._bios else 'HLE kernel'} · core {core.VERSION}"

    def run_frame(self, render: bool = True) -> None:
        self.core.run_frame()
        self.tty += self.core.take_tty()
        if len(self.tty) > 65536:
            del self.tty[:-32768]

    # ------------------------------------------------------------ input
    def _set(self, name: str, down: bool) -> None:
        bit = 1 << PAD_BITS[name]
        self._buttons = (self._buttons | bit) if down else (self._buttons & ~bit)
        self.core.set_buttons(self._buttons)

    def press(self, button: str) -> None:
        name = normalize_button(button)
        self._set(_FROM_SHARED.get(name, name), True)

    def release(self, button: str) -> None:
        name = normalize_button(button)
        self._set(_FROM_SHARED.get(name, name), False)

    def press_ps1(self, button: str, down: bool = True) -> None:
        """Any PS1 pad button by its own name ("Triangle", "Square", "L2", ...)."""
        self._set(button, down)

    # ------------------------------------------------------------ video / audio
    def frame_rgb(self) -> bytes:
        return self.core.frame_canvas(*CANVAS)

    @property
    def apu(self):
        return self._apu

    # ------------------------------------------------------------ lifecycle
    def save(self) -> bool:
        return False                 # memory cards come in a later milestone

    def reset(self) -> None:
        self.core = core.Ps1(bios=self._bios, exe=self._exe)
        self._apu = _Audio(self.core)
        self.core.set_buttons(self._buttons)

    def state_blobs(self) -> list:
        raise NotImplementedError("Save states aren't available for the PS1 core yet.")

    def __reduce__(self):
        raise NotImplementedError("Save states aren't available for the PS1 core yet.")

