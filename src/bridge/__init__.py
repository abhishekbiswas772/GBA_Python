"""Bridge between the UI and the emulator cores.

Public API::

    from src.bridge import load

    machine = load("rom.gba")          # -> GBAMachine / GameBoyMachine
    machine.run_frame()
    rgb = machine.frame_rgb()          # width*height*3 bytes, RGB888
    machine.press("A"); machine.release("A")
    machine.save()

``load()`` picks the right core from the file extension and header:

* ``.gba`` / ``.agb``            -> GBA (:class:`~src.bridge.gba_machine.GBAMachine`)
* ``.gbc`` / ``.cgb``            -> Game Boy Color (:class:`~src.bridge.gb_machine.GameBoyMachine`)
* ``.gb``                        -> auto: CGB when the header says so, else DMG

Options: ``cgb`` (force GB/DMG vs CGB), ``bios`` (GBA BIOS image path),
``palette`` (DMG 4-shade palette of (r,g,b) triples).
"""
from __future__ import annotations

from pathlib import Path

from src.bridge.audio import SAMPLE_RATE, take_samples
from src.bridge.base import BUTTONS, Machine, normalize_button
from src.bridge.gb_machine import GameBoyMachine
from src.bridge.gba_machine import GBAMachine, bgr555_to_rgb888

__all__ = [
    "BUTTONS", "SAMPLE_RATE", "Machine", "GameBoyMachine", "GBAMachine",
    "bgr555_to_rgb888", "detect", "load", "normalize_button", "take_samples",
]

_ROM_EXTS = {".gba": "GBA", ".agb": "GBA", ".gbc": "GBC", ".cgb": "GBC", ".gb": "GB"}


def detect(path) -> str:
    """Detect the system for a ROM file: "GB", "GBC" or "GBA".

    Uses the file extension, then the cartridge header for ``.gb`` files
    (the CGB flag at 0x0143).  Raises ``ValueError`` for unknown extensions
    and ``FileNotFoundError`` for missing files.
    """
    p = Path(path)
    system = _ROM_EXTS.get(p.suffix.lower())
    if system is None:
        raise ValueError(
            f"unrecognised ROM extension {p.suffix!r} (expected .gb, .gbc, .cgb, .gba or .agb)")
    if system == "GB":
        try:                       # header CGB flag: 0x80 = enhanced, 0xC0 = CGB only
            with p.open("rb") as f:
                f.seek(0x143)
                flag = f.read(1)
        except OSError as e:
            raise FileNotFoundError(f"can't read ROM header: {p}") from e
        if flag and flag[0] & 0x80:
            system = "GBC"
    return system


def load(path, *, cgb: bool | None = None, bios=None, palette=None) -> Machine:
    """Load a ROM and return a :class:`Machine` for the right system.

    ``cgb`` and ``palette`` apply to Game Boy ROMs (``cgb=None`` auto-detects
    from the header), ``bios`` to GBA ROMs.
    """
    system = detect(path)
    if system == "GBA":
        return GBAMachine(path, bios=bios)
    return GameBoyMachine(path, cgb=cgb, palette=palette)
