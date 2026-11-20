"""Experimental PlayStation (PS1) core, written in Rust and loaded through PyO3.

Unlike the other cores (pure Python), the PS1 needs native speed: a 33.8 MHz
MIPS CPU plus a software rasterizer. The Rust crate lives in ``native/``.
Build it once with::

    uv run python -m src.core.ps1.build          # needs Rust (rustup.rs)

That compiles ``native/`` and copies the extension next to this file as
``_ps1.pyd`` (Windows) or ``_ps1.so`` (Linux/macOS). Everything else in the
app works without it; PS1 games are then listed as needing the core.

Status (milestone 1): CPU, memory, DMA, timers, IRQs, GPU and digital pad are
emulated and checked against real hardware (JaCzekanski's ps1-tests). PS-X EXE
programs run without a BIOS through a small HLE kernel. Next: CD-ROM (games),
GTE (3D), SPU (sound), memory cards, MDEC (video), save states.
"""
from __future__ import annotations

try:
    from src.core.ps1._ps1 import SAMPLE_RATE, VERSION, Ps1
    AVAILABLE = True
    IMPORT_ERROR = ""
except ImportError as e:            # not built yet (no Rust toolchain), or wrong platform
    Ps1 = None
    VERSION = ""
    SAMPLE_RATE = 44_100
    AVAILABLE = False
    IMPORT_ERROR = str(e)

EXE_MAGIC = b"PS-X EXE"


def is_exe(path) -> bool:
    """True when `path` is a PS-X EXE (checks the 8-byte magic, not the extension)."""
    try:
        with open(path, "rb") as f:
            return f.read(8) == EXE_MAGIC
    except OSError:
        return False


class Ps1Core:
    """The running machine as save states see it: the Rust object plus the images it
    booted from. Pickling stores ``Ps1.save_state()`` bytes; the BIOS and EXE are
    passed by reference (``Machine.state_blobs``), so a state never contains them."""

    def __init__(self, bios: bytes | None, exe: bytes):
        self.bios = bios
        self.exe = exe
        self.ps1 = Ps1(bios=bios, exe=exe)

    def __getstate__(self) -> dict:
        return {"bios": self.bios, "exe": self.exe, "state": bytes(self.ps1.save_state())}

    def __setstate__(self, d: dict) -> None:
        self.bios, self.exe = d["bios"], d["exe"]
        self.ps1 = Ps1(bios=self.bios, exe=self.exe)
        self.ps1.load_state(d["state"])
