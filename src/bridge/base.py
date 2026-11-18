"""Unified ``Machine`` interface implemented by every emulator core adapter.

A :class:`Machine` wraps one running console (Game Boy, Game Boy Color or Game
Boy Advance) behind a single API so the UI and headless tools never need to
know which core is underneath:

* ``run_frame()`` / ``run_frames(n)`` advance emulation by whole video frames,
* ``press`` / ``release`` take UI button names (``"A"``, ``"L"``, ...) and map
  them to whatever the core expects,
* ``frame_rgb()`` returns one packed RGB888 frame (``width * height * 3``
  bytes, row-major),
* ``save`` / ``reset`` handle battery RAM and restarting the core,
* ``apu`` exposes the core's audio unit, normalised to the contract described
  in :mod:`src.bridge.audio` (int16 stereo interleaved at ``SAMPLE_RATE``).

Nothing in this package imports a UI framework, so it works headless and from
``main.py``.
"""
from __future__ import annotations

from array import array

# Canonical button names shared by every system.  L/R only exist on GBA; the
# Game Boy adapters treat them as no-ops so the UI can use one mapping.
BUTTONS: tuple[str, ...] = (
    "A", "B", "Start", "Select", "Up", "Down", "Left", "Right", "L", "R",
)

_ALIASES = {name.lower(): name for name in BUTTONS}


def normalize_button(button: str) -> str:
    """Return the canonical name for a button ("a" / "START" -> "A" / "Start").

    Raises ``ValueError`` for anything that is not a known button.
    """
    if not isinstance(button, str):
        raise ValueError(f"button must be a string, got {button!r}")
    name = _ALIASES.get(button.strip().lower())
    if name is None:
        raise ValueError(f"unknown button {button!r}; expected one of {BUTTONS}")
    return name


class Machine:
    """Base class for emulator core adapters.

    Subclasses set the class attributes (or override the properties) and
    implement the methods.  The base implementations only document the
    contract.
    """

    #: "GB", "GBC", "GBA" or "NES"
    system: str = "?"
    #: native screen width in pixels
    width: int = 0
    #: native screen height in pixels
    height: int = 0
    #: target video refresh rate in Hz (59.7275 for every system here)
    fps: float = 60.0

    # ------------------------------------------------------------------ running
    def run_frame(self, render: bool = True) -> None:
        """Emulate exactly one video frame.

        ``render=False`` is frameskip: the game runs exactly the same (timing,
        interrupts, audio), only the framebuffer is not drawn, so
        ``frame_rgb()`` keeps showing the last drawn frame."""
        raise NotImplementedError

    def run_frames(self, n: int, render_last: bool = True) -> None:
        """Emulate ``n`` video frames (only the last one drawn when ``render_last``)."""
        for i in range(n):
            self.run_frame(render=render_last and i == n - 1)

    # ------------------------------------------------------------------ input
    def press(self, button: str) -> None:
        """Press a button (see :data:`BUTTONS` for the accepted names)."""
        raise NotImplementedError

    def release(self, button: str) -> None:
        """Release a button (see :data:`BUTTONS` for the accepted names)."""
        raise NotImplementedError

    # ------------------------------------------------------------------ video
    def frame_rgb(self) -> bytes:
        """The most recent frame as packed RGB888 (``width*height*3`` bytes)."""
        raise NotImplementedError

    @property
    def frame_size(self) -> tuple[int, int]:
        """``(width, height)`` of the native screen."""
        return self.width, self.height

    # ------------------------------------------------------------------ audio
    @property
    def apu(self):
        """The core's audio unit (see :mod:`src.bridge.audio` for the contract:
        ``take_samples() -> array('h')`` of interleaved stereo int16 at
        ``SAMPLE_RATE`` Hz, with a settable ``sampling`` flag)."""
        raise NotImplementedError

    # ------------------------------------------------------------------ save states
    def state_blobs(self) -> list:
        """Big read-only buffers (ROM, BIOS) left out of save states and passed
        by reference; the first one identifies the game."""
        return []

    def adopt_core(self, core) -> None:
        """Replace the running core with one restored from a save-state file."""
        self.core = core

    # ------------------------------------------------------------------ lifecycle
    def save(self) -> bool:
        """Flush battery RAM to disk; returns True if a save was written."""
        raise NotImplementedError

    def reset(self) -> None:
        """Restart the console from power-on (battery RAM is preserved)."""
        raise NotImplementedError

    # ------------------------------------------------------------------ info
    @property
    def title_info(self) -> str:
        """Short human-readable cartridge description (title / mapper / ...)."""
        return self.system

    @property
    def info(self) -> str:
        """One-line description: system, screen size and cartridge info."""
        return f"{self.system} {self.width}x{self.height} - {self.title_info}"


def take_samples(machine: "Machine") -> array:
    """Pull pending audio samples from a machine's APU (int16 stereo)."""
    from src.bridge.audio import take_samples as _take
    return _take(machine.apu)
