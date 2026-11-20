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
    """The `apu` contract: `sampling` and `take_samples()` -> array('h') (int16 stereo).
    Reads ``machine.core`` each time, so it follows a core replaced by a state load."""

    def __init__(self, machine):
        self._m = machine

    @property
    def sampling(self) -> bool:
        return self._m.core.ps1.sampling

    @sampling.setter
    def sampling(self, on: bool) -> None:
        self._m.core.ps1.sampling = bool(on)

    def take_samples(self) -> array:
        a = array("h")
        a.frombytes(self._m.core.ps1.take_samples())
        return a


class PS1Machine(Machine):
    """``Machine`` adapter for the PS1 core. ``rom``: a PS-X EXE path; ``bios``: optional BIOS path.

    ``self.core`` is a :class:`src.core.ps1.Ps1Core` (picklable, for save states and rewind);
    the Rust machine itself is ``self.core.ps1``."""

    system = "PS1"
    width, height = CANVAS

    def __init__(self, rom, bios=None):
        if not core.AVAILABLE:
            raise RuntimeError("The PS1 core isn't built. Run: uv run python -m src.core.ps1.build "
                               f"({core.IMPORT_ERROR})")
        self.path = Path(rom)
        data = self.path.read_bytes()
        if not data.startswith(core.EXE_MAGIC):
            raise ValueError("Only PS-X EXE programs run for now; CD images (.cue/.bin) come with "
                             "the CD-ROM milestone.")
        self.core = core.Ps1Core(Path(bios).read_bytes() if bios else None, data)
        self.fps = self.core.ps1.fps
        self._buttons = 0
        self._apu = _Audio(self)
        self.tty = bytearray()

    @property
    def title_info(self) -> str:
        return f"PS-X EXE · {'BIOS' if self.core.bios else 'HLE kernel'} · core {core.VERSION}"

    def run_frame(self, render: bool = True) -> None:
        ps1 = self.core.ps1
        ps1.set_buttons(self._buttons)     # also right after a state load swapped the machine
        ps1.run_frame()
        self.tty += ps1.take_tty()
        if len(self.tty) > 65536:
            del self.tty[:-32768]

    # ------------------------------------------------------------ input
    def _set(self, name: str, down: bool) -> None:
        bit = 1 << PAD_BITS[name]
        self._buttons = (self._buttons | bit) if down else (self._buttons & ~bit)
        self.core.ps1.set_buttons(self._buttons)

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
        return self.core.ps1.frame_canvas(*CANVAS)

    @property
    def apu(self):
        return self._apu

    # ------------------------------------------------------------ lifecycle
    def save(self) -> bool:
        return False                 # memory cards come in a later milestone

    def reset(self) -> None:
        self.core = core.Ps1Core(self.core.bios, self.core.exe)

    def state_blobs(self) -> list:
        """The EXE (first: it identifies the game) and the BIOS, kept out of every state."""
        return [b for b in (self.core.exe, self.core.bios) if b is not None]
