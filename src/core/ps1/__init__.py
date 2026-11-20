"""Experimental PlayStation (PS1) core, written in Rust and loaded through PyO3.

Unlike the other cores (pure Python), the PS1 needs native speed: a 33.8 MHz
MIPS CPU plus a software rasterizer. The Rust crate lives in ``native/``.
Build it once with::

    uv run python -m src.core.ps1.build          # needs Rust (rustup.rs)

That compiles ``native/`` and copies the extension next to this file as
``_ps1.pyd`` (Windows) or ``_ps1.so`` (Linux/macOS). Everything else in the
app works without it; PS1 games are then listed as needing the core.

Status: CPU, memory, DMA, timers, IRQs, GPU, GTE, SPU, CD-ROM and digital pad are
emulated and checked against real hardware (JaCzekanski's ps1-tests). PS-X EXE
programs and homebrew discs run without a BIOS through a small HLE kernel;
commercial discs need a BIOS. Not yet: XA-ADPCM audio, memory cards.
"""
from __future__ import annotations

from pathlib import Path

try:
    from src.core.ps1._ps1 import SAMPLE_RATE, VERSION, Ps1, make_disc_image
    AVAILABLE = True
    IMPORT_ERROR = ""
except ImportError as e:            # not built yet (no Rust toolchain), or wrong platform
    Ps1 = make_disc_image = None
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


DISC_EXTS = {".cue", ".iso"}


def is_disc(path) -> bool:
    """True for a CD image the core opens: a .cue sheet or a 2048/2352-byte-sector .iso."""
    return Path(path).suffix.lower() in DISC_EXTS and Path(path).is_file()


class Ps1Core:
    """The running machine as save states see it: the Rust object plus what it booted
    from. Pickling stores ``Ps1.save_state()`` bytes; the BIOS, EXE and disc id are
    passed by reference (``Machine.state_blobs``), so a state never contains them. A
    disc is stored as its path and reopened on restore."""

    def __init__(self, bios: bytes | None, exe: bytes | None = None, disc: str | None = None):
        self.bios = bios
        self.exe = exe
        self.disc = str(disc) if disc else None
        self.ps1 = Ps1(bios=bios, exe=exe, disc=self.disc)
        self.disc_id = bytes(self.ps1.disc_id()) if self.disc else None

    def __getstate__(self) -> dict:
        return {"bios": self.bios, "exe": self.exe, "disc": self.disc, "disc_id": self.disc_id,
                "state": bytes(self.ps1.save_state())}

    def __setstate__(self, d: dict) -> None:
        self.bios, self.exe = d["bios"], d["exe"]
        self.disc, self.disc_id = d.get("disc"), d.get("disc_id")
        self.ps1 = Ps1(bios=self.bios, exe=self.exe, disc=self.disc)
        self.ps1.load_state(d["state"])
