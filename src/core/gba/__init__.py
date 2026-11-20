"""Game Boy Advance core: a native (Rust + PyO3) machine, managed from Python.

The emulation itself (ARM7TDMI, bus, HLE BIOS, timers, DMA, PPU, APU, saves,
RTC) lives in ``native/`` and is built once with::

    uv run python -m src.core.gba.build          # needs Rust (rustup.rs)

which copies the extension next to this file as ``_gba.pyd`` / ``_gba.so``.

Python is the "linker": :class:`GbaCore` loads the ROM, BIOS and ``.sav`` file,
hands them to the native machine once, and then calls it once per frame. It also
does the management the machine doesn't: battery saves to disk (every 600
frames and on exit), the RTC's local-time offset, and pickling for save states.
"""
from __future__ import annotations

import os
import time
from array import array
from dataclasses import dataclass
from pathlib import Path

from src.core.adaptors.saves import sav_path_for

try:
    from src.core.gba._gba import SAMPLE_RATE, VERSION, Gba
    AVAILABLE = True
    IMPORT_ERROR = ""
except ImportError as e:            # not built yet (no Rust toolchain), or wrong platform
    Gba = None
    VERSION = ""
    SAMPLE_RATE = 44_100
    AVAILABLE = False
    IMPORT_ERROR = str(e)

CPU_HZ = 16_777_216
WIDTH, HEIGHT = 240, 160
FRAME_CYCLES = 1232 * 228
AUTOSAVE_FRAMES = 600
BUTTON_NAMES = {"a": 0, "b": 1, "select": 2, "start": 3, "right": 4, "left": 5,
                "up": 6, "down": 7, "r": 8, "l": 9}


@dataclass
class Header:
    title: str
    game_code: str
    maker: str
    version: int
    checksum_ok: bool


def parse_header(rom: bytes) -> Header:
    if len(rom) < 0xC0:
        raise ValueError(f"File is too small ({len(rom)} bytes) - not a GBA ROM")
    chk = 0
    for b in rom[0xA0:0xBD]:
        chk = (chk - b) & 0xFF
    chk = (chk - 0x19) & 0xFF

    def text(a: int, b: int) -> str:
        return rom[a:b].split(b"\x00")[0].decode("ascii", errors="replace").strip()
    return Header(title=text(0xA0, 0xAC), game_code=text(0xAC, 0xB0),
                  maker=text(0xB0, 0xB2), version=rom[0xBC], checksum_ok=chk == rom[0xBD])


def require_core() -> None:
    if not AVAILABLE:
        raise RuntimeError("The GBA core isn't built. Run: uv run python -m src.core.gba.build "
                           f"({IMPORT_ERROR})")


class Cart:
    """The cartridge as the app sees it: header, save type and its ``.sav`` file."""

    def __init__(self, core: "GbaCore", rom: bytes, path: Path | None):
        self._core = core
        self.header = parse_header(rom)
        self.path = path
        self.sav_path = sav_path_for(path, "gba") if path is not None else None
        self.size = len(rom)

    @property
    def save_kind(self) -> str:
        return self._core.gba.save_kind

    @property
    def rtc(self) -> bool:
        return self._core.gba.has_rtc

    def save_ram(self) -> bool:
        """Write the backup memory to the .sav file if it changed (atomic)."""
        g = self._core.gba
        data = g.save_data()
        if data is None or self.sav_path is None or not g.save_dirty:
            return False
        tmp = self.sav_path.with_suffix(".sav.tmp")
        try:                            # a crash mid-write never truncates the .sav
            self.sav_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, self.sav_path)
        except OSError as e:
            print(f"[warning: could not write {self.sav_path.name}: {e}]")
            return False
        g.clear_save_dirty()
        return True

    def __str__(self) -> str:
        h = self.header
        return (f"{h.title} [{h.game_code}] maker {h.maker} v{h.version} - "
                f"{self.size // 1024} KB, save: {self.save_kind}"
                + (", RTC" if self.rtc else "")
                + ("" if h.checksum_ok else " (bad header checksum)"))


class GbaCore:
    """One GBA. ``rom``: a path or raw bytes; ``bios``: a path, raw bytes or None (HLE BIOS).

    Pickling stores the native ``save_state()`` bytes; ROM and BIOS are passed by
    reference (``Machine.state_blobs``), so a state never contains them."""

    def __init__(self, rom: str | Path | bytes, bios: str | Path | bytes | None = None):
        require_core()
        path = None if isinstance(rom, (bytes, bytearray)) else Path(rom)
        self.rom = bytes(rom) if path is None else path.read_bytes()
        self.bios = Path(bios).read_bytes() if isinstance(bios, (str, Path)) else bios
        self.cart = Cart(self, self.rom, path)
        sav = self.cart.sav_path
        save = sav.read_bytes() if sav is not None and sav.exists() else None
        self.gba = Gba(self.rom, self.bios, save)
        self._frames_since_save = 0
        self._set_clock()

    def _set_clock(self) -> None:
        self.gba.set_rtc_offset(time.localtime().tm_gmtoff or 0)

    # ------------------------------------------------------------ pickling
    def __getstate__(self) -> dict:
        return {"rom": self.rom, "bios": self.bios, "cart": self.cart,
                "frames_since_save": self._frames_since_save, "state": bytes(self.gba.save_state())}

    def __setstate__(self, d: dict) -> None:
        require_core()
        self.rom, self.bios, self.cart = d["rom"], d["bios"], d["cart"]
        self._frames_since_save = d.get("frames_since_save", 0)
        self.gba = Gba(self.rom, self.bios)
        self.gba.load_state(d["state"])
        self._set_clock()

    # ------------------------------------------------------------ input
    def press(self, button: str | int) -> None:
        self.gba.set_key(BUTTON_NAMES[button] if isinstance(button, str) else button, True)

    def release(self, button: str | int) -> None:
        self.gba.set_key(BUTTON_NAMES[button] if isinstance(button, str) else button, False)

    # ------------------------------------------------------------ running
    def run_frame(self, render: bool = True) -> None:
        self.gba.run_frame(render)
        self._frames_since_save += 1
        if self._frames_since_save >= AUTOSAVE_FRAMES:
            self._frames_since_save = 0
            self.cart.save_ram()

    def run_frames(self, n: int) -> None:
        for _ in range(n):
            self.run_frame()

    def save(self) -> bool:
        return self.cart.save_ram()

    @property
    def fb(self) -> bytes:
        """240x160 little-endian BGR555 framebuffer."""
        return self.gba.frame_bgr555()

    @property
    def frame(self) -> int:
        return self.gba.frame

    @property
    def now(self) -> int:
        return self.gba.now

    @property
    def seconds(self) -> float:
        return self.gba.now / CPU_HZ

    @property
    def apu(self) -> "Audio":
        return Audio(self)

    # ------------------------------------------------------------ debug views (tests, tools)
    @property
    def bus(self) -> "BusView":
        return BusView(self.gba)

    @property
    def cpu(self) -> "CpuView":
        return CpuView(self.gba)

    @property
    def ppu(self) -> "PpuView":
        return PpuView(self.gba)


class Audio:
    """The ``apu`` contract: ``sampling`` and ``take_samples()`` -> array('h') (int16 stereo).
    Reads ``core.gba`` each time, so it follows a machine replaced by a state load."""

    def __init__(self, core: GbaCore):
        self._core = core

    @property
    def sampling(self) -> bool:
        return self._core.gba.sampling

    @sampling.setter
    def sampling(self, on: bool) -> None:
        self._core.gba.sampling = bool(on)

    def take_samples(self) -> array:
        a = array("h")
        a.frombytes(self._core.gba.take_samples())
        return a


class _Mem:
    """One plain memory of the native machine, indexable like a bytearray."""

    def __init__(self, gba, name: str):
        self._g, self._name = gba, name

    def __len__(self) -> int:
        return len(self._g.memory(self._name))

    def __bytes__(self) -> bytes:
        return bytes(self._g.memory(self._name))

    def __getitem__(self, i):
        return self._g.memory(self._name)[i]

    def __setitem__(self, i, value) -> None:
        if isinstance(i, slice):
            start, stop, step = i.indices(len(self))
            if step != 1:
                raise ValueError("step slices aren't supported")
            data = bytes(value)
            if len(data) != stop - start:
                raise ValueError("slice assignment can't change the size")
            self._g.poke(self._name, start, data)
        else:
            self._g.poke(self._name, i % len(self), bytes([value]))


class BusView:
    def __init__(self, gba):
        self._g = gba
        for name in ("ewram", "iwram", "io", "pal", "vram", "oam"):
            setattr(self, name, _Mem(gba, name))

    def read8(self, a: int) -> int:
        return self._g.read8(a & 0xFFFFFFFF)

    def read16(self, a: int) -> int:
        return self._g.read16(a & 0xFFFFFFFF)

    def read32(self, a: int) -> int:
        return self._g.read32(a & 0xFFFFFFFF)

    def write8(self, a: int, v: int) -> None:
        self._g.write8(a & 0xFFFFFFFF, v & 0xFF)

    def write16(self, a: int, v: int) -> None:
        self._g.write16(a & 0xFFFFFFFF, v & 0xFFFF)

    def write32(self, a: int, v: int) -> None:
        self._g.write32(a & 0xFFFFFFFF, v & 0xFFFFFFFF)


class _Regs:
    def __init__(self, gba):
        self._g = gba

    def __getitem__(self, i: int) -> int:
        return self._g.regs[i]

    def __setitem__(self, i: int, v: int) -> None:
        self._g.set_reg(i, v & 0xFFFFFFFF)

    def __iter__(self):
        return iter(self._g.regs)

    def __len__(self) -> int:
        return 16


class CpuView:
    def __init__(self, gba):
        self._g = gba
        self.r = _Regs(gba)

    def swi(self, n: int) -> int:
        """Run a BIOS call directly (HLE BIOS)."""
        return self._g.swi(n)

    def step(self) -> int:
        return self._g.step()

    def __getattr__(self, name):          # pc, thumb, now, halted, intrwait, cpsr
        if name in ("pc", "thumb", "now", "halted", "intrwait", "cpsr"):
            return getattr(self._g, name)
        raise AttributeError(name)


class PpuView:
    def __init__(self, gba):
        self._g = gba

    @property
    def fb(self) -> bytes:
        return self._g.frame_bgr555()

    @property
    def frame(self) -> int:
        return self._g.frame

    def render_line(self, y: int) -> None:
        self._g.render_line(y)

    def latch_ref(self, bg: int) -> None:
        self._g.latch_ref(bg)
