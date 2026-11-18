"""iNES / NES 2.0 cartridges and their mappers.

Every mapper exposes the same flat banking tables, so the CPU and PPU never
call into mapper code on ordinary reads:

* ``prg_banks``: 4 offsets into ``prg`` for the 8 KB windows $8000/$A000/$C000/$E000,
* ``chr_banks``: 8 offsets into ``chr`` for the 1 KB windows $0000-$1FFF,
* ``nt``: 4 offsets into the PPU's nametable RAM for $2000/$2400/$2800/$2C00.

The lists are updated in place on bank switches (callers keep references).
``write(addr, value)`` handles $8000-$FFFF register writes, ``scanline()`` is
the MMC3 IRQ counter clock and ``irq`` is the mapper's IRQ line.

Supported: 0 NROM, 1 MMC1 (SxROM, incl. 512 KB SUROM), 2 UxROM, 3 CNROM,
4 MMC3 (TxROM), 7 AxROM, 11 Color Dreams, 66 GxROM.
"""
from __future__ import annotations

import os
from pathlib import Path

from src.core.saves import sav_path_for

from src.core.nes.common import (
    MIRROR_FOUR, MIRROR_HORIZONTAL, MIRROR_SINGLE0, MIRROR_SINGLE1, MIRROR_VERTICAL,
)

_NT_LAYOUT = {
    MIRROR_HORIZONTAL: (0, 0, 0x400, 0x400),
    MIRROR_VERTICAL: (0, 0x400, 0, 0x400),
    MIRROR_SINGLE0: (0, 0, 0, 0),
    MIRROR_SINGLE1: (0x400, 0x400, 0x400, 0x400),
    MIRROR_FOUR: (0, 0x400, 0x800, 0xC00),
}

MAPPER_NAMES = {0: "NROM", 1: "MMC1", 2: "UxROM", 3: "CNROM", 4: "MMC3", 7: "AxROM",
                11: "Color Dreams", 66: "GxROM"}


class Header:
    def __init__(self, data: bytes):
        if len(data) < 16 or data[:4] != b"NES\x1a":
            raise ValueError("not an iNES ROM (missing NES<EOF> header)")
        f6, f7 = data[6], data[7]
        self.nes2 = (f7 & 0x0C) == 0x08
        self.mapper = (f6 >> 4) | (f7 & 0xF0)
        prg_units, chr_units = data[4], data[5]
        if self.nes2:
            self.mapper |= (data[8] & 0x0F) << 8
            prg_units |= (data[9] & 0x0F) << 8
            chr_units |= (data[9] & 0xF0) << 4
        elif any(data[12:16]):              # garbage-tail ("DiskDude!") headers: ignore upper nibble
            self.mapper &= 0x0F
        self.prg_size = prg_units * 0x4000
        self.chr_size = chr_units * 0x2000
        self.battery = bool(f6 & 0x02)
        self.trainer = bool(f6 & 0x04)
        self.four_screen = bool(f6 & 0x08)
        self.mirroring = (MIRROR_FOUR if self.four_screen
                          else MIRROR_VERTICAL if f6 & 0x01 else MIRROR_HORIZONTAL)

    @property
    def mapper_name(self) -> str:
        return MAPPER_NAMES.get(self.mapper, f"mapper {self.mapper}")


class Mapper:
    def __init__(self, cart: "Cartridge"):
        self.cart = cart
        self.prg = cart.prg
        self.chr = cart.chr
        self.chr_ram = cart.chr_ram
        self.prg_banks = [0, 0x2000, 0x4000, 0x6000]
        self.chr_banks = [i * 0x400 for i in range(8)]
        self.nt = list(_NT_LAYOUT[cart.header.mirroring])
        self.irq = False
        self.on_irq = None                  # callable() when `irq` changes
        self.n_prg8 = max(1, len(self.prg) // 0x2000)
        self.n_chr1 = max(1, len(self.chr) // 0x400)
        self.chr_gen = 0                    # bumps when CHR banking changes (PPU caches)
        self.set_prg16(0, 0)
        self.set_prg16(1, -1)
        self.set_chr8(0)

    # ---------------------------------------------------------------- helpers
    def set_prg8(self, slot: int, bank: int) -> None:
        self.prg_banks[slot] = (bank % self.n_prg8) * 0x2000

    def set_prg16(self, slot: int, bank: int) -> None:
        n = max(1, self.n_prg8 // 2)
        b = bank % n
        self.prg_banks[slot * 2] = b * 0x4000
        self.prg_banks[slot * 2 + 1] = b * 0x4000 + 0x2000

    def set_prg32(self, bank: int) -> None:
        n = max(1, self.n_prg8 // 4)
        b = (bank % n) * 0x8000
        self.prg_banks[:] = [b, b + 0x2000, b + 0x4000, b + 0x6000]

    def set_chr1(self, slot: int, bank: int) -> None:
        off = (bank % self.n_chr1) * 0x400
        if self.chr_banks[slot] != off:
            self.chr_banks[slot] = off
            self.chr_gen += 1

    def set_chr4(self, half: int, bank: int) -> None:
        for i in range(4):
            self.set_chr1(half * 4 + i, bank * 4 + i)

    def set_chr8(self, bank: int) -> None:
        for i in range(8):
            self.set_chr1(i, bank * 8 + i)

    def mirror(self, mode: str) -> None:
        if self.cart.header.four_screen:
            return
        self.nt[:] = _NT_LAYOUT[mode]

    def set_irq(self, on: bool) -> None:
        if on != self.irq:
            self.irq = on
            if self.on_irq is not None:
                self.on_irq()

    # ---------------------------------------------------------------- hooks
    def write(self, addr: int, value: int) -> None:
        pass

    def scanline(self) -> None:
        pass

    @property
    def wants_scanlines(self) -> bool:
        return False


class NROM(Mapper):
    pass


class MMC1(Mapper):
    def __init__(self, cart):
        self.shift = 0x10
        self.control = 0x0C
        self.chr0 = self.chr1 = self.prg_bank = 0
        super().__init__(cart)
        self._apply()

    def write(self, addr: int, value: int) -> None:
        if value & 0x80:
            self.shift = 0x10
            self.control |= 0x0C
            self._apply()
            return
        done = self.shift & 1
        self.shift = (self.shift >> 1) | ((value & 1) << 4)
        if not done:
            return
        v = self.shift
        self.shift = 0x10
        reg = (addr >> 13) & 3
        if reg == 0:
            self.control = v
        elif reg == 1:
            self.chr0 = v
        elif reg == 2:
            self.chr1 = v
        else:
            self.prg_bank = v & 0x0F
            self.cart.prg_ram_enabled = not (v & 0x10)
        self._apply()

    def _apply(self) -> None:
        c = self.control
        self.mirror((MIRROR_SINGLE0, MIRROR_SINGLE1, MIRROR_VERTICAL, MIRROR_HORIZONTAL)[c & 3])
        outer = (self.chr0 & 0x10) if len(self.prg) > 0x40000 else 0   # SUROM 512 KB: 256 KB half
        mode = (c >> 2) & 3
        if mode < 2:
            self.set_prg32((outer | (self.prg_bank & 0x0E)) >> 1)
        elif mode == 2:
            self.set_prg16(0, outer)
            self.set_prg16(1, outer | self.prg_bank)
        else:
            self.set_prg16(0, outer | self.prg_bank)
            self.set_prg16(1, outer | 0x0F)
        if c & 0x10:
            self.set_chr4(0, self.chr0)
            self.set_chr4(1, self.chr1)
        else:
            self.set_chr8(self.chr0 >> 1)


class UxROM(Mapper):
    def write(self, addr: int, value: int) -> None:
        self.set_prg16(0, value & 0x0F if self.n_prg8 <= 32 else value)


class CNROM(Mapper):
    def write(self, addr: int, value: int) -> None:
        self.set_chr8(value & 3 if self.n_chr1 <= 32 else value)


class AxROM(Mapper):
    def __init__(self, cart):
        super().__init__(cart)
        self.set_prg32(0)
        self.mirror(MIRROR_SINGLE0)

    def write(self, addr: int, value: int) -> None:
        self.set_prg32(value & 7)
        self.mirror(MIRROR_SINGLE1 if value & 0x10 else MIRROR_SINGLE0)


class ColorDreams(Mapper):
    def __init__(self, cart):
        super().__init__(cart)
        self.set_prg32(0)

    def write(self, addr: int, value: int) -> None:
        self.set_prg32(value & 3)
        self.set_chr8(value >> 4)


class GxROM(Mapper):
    def __init__(self, cart):
        super().__init__(cart)
        self.set_prg32(0)

    def write(self, addr: int, value: int) -> None:
        self.set_prg32((value >> 4) & 3)
        self.set_chr8(value & 3)


class MMC3(Mapper):
    def __init__(self, cart):
        self.regs = [0, 2, 4, 5, 6, 7, 0, 1]
        self.select = 0
        self.irq_latch = 0
        self.irq_counter = 0
        self.irq_reload = False
        self.irq_enabled = False
        super().__init__(cart)
        self._apply()

    @property
    def wants_scanlines(self) -> bool:
        return True

    def write(self, addr: int, value: int) -> None:
        even = not (addr & 1)
        if addr < 0xA000:
            if even:
                self.select = value
            else:
                self.regs[self.select & 7] = value
            self._apply()
        elif addr < 0xC000:
            if even:
                self.mirror(MIRROR_HORIZONTAL if value & 1 else MIRROR_VERTICAL)
            else:
                self.cart.prg_ram_enabled = bool(value & 0x80)
                self.cart.prg_ram_writable = not (value & 0x40)
        elif addr < 0xE000:
            if even:
                self.irq_latch = value
            else:
                self.irq_counter = 0
                self.irq_reload = True
        else:
            if even:
                self.irq_enabled = False
                self.set_irq(False)
            else:
                self.irq_enabled = True

    def _apply(self) -> None:
        r, s = self.regs, self.select
        if s & 0x40:
            self.set_prg8(0, -2)
            self.set_prg8(2, r[6])
        else:
            self.set_prg8(0, r[6])
            self.set_prg8(2, -2)
        self.set_prg8(1, r[7])
        self.set_prg8(3, -1)
        inv = 4 if s & 0x80 else 0
        self.set_chr1(0 ^ inv, r[0] & 0xFE)
        self.set_chr1(1 ^ inv, r[0] | 1)
        self.set_chr1(2 ^ inv, r[1] & 0xFE)
        self.set_chr1(3 ^ inv, r[1] | 1)
        self.set_chr1(4 ^ inv, r[2])
        self.set_chr1(5 ^ inv, r[3])
        self.set_chr1(6 ^ inv, r[4])
        self.set_chr1(7 ^ inv, r[5])

    def scanline(self) -> None:
        if self.irq_counter == 0 or self.irq_reload:
            self.irq_counter = self.irq_latch
            self.irq_reload = False
        else:
            self.irq_counter -= 1
        if self.irq_counter == 0 and self.irq_enabled:
            self.set_irq(True)


MAPPERS = {0: NROM, 1: MMC1, 2: UxROM, 3: CNROM, 4: MMC3, 7: AxROM, 11: ColorDreams, 66: GxROM}


class Cartridge:
    def __init__(self, data: bytes, path: Path | None = None):
        self.header = h = Header(data)
        off = 16 + (512 if h.trainer else 0)
        prg = data[off:off + h.prg_size]
        if len(prg) < h.prg_size or not prg:
            raise ValueError("ROM file is truncated (PRG data missing)")
        self.prg = bytes(prg)
        chr_data = data[off + h.prg_size:off + h.prg_size + h.chr_size]
        self.chr_ram = h.chr_size == 0
        self.chr = bytearray(chr_data) if not self.chr_ram else bytearray(0x2000)
        self.prg_ram = bytearray(0x2000)
        self.prg_ram_enabled = True
        self.prg_ram_writable = True
        self.ram_dirty = False
        self.path = path
        self.sav_path = sav_path_for(path, "nes") if (path is not None and h.battery) else None
        if h.trainer:
            self.prg_ram[0x1000:0x1200] = data[16:16 + 512]
        cls = MAPPERS.get(h.mapper)
        if cls is None:
            raise ValueError(f"unsupported mapper {h.mapper} ({h.mapper_name}); "
                             f"supported: {', '.join(f'{k} {v}' for k, v in sorted(MAPPER_NAMES.items()))}")
        self.mapper = cls(self)
        self._load_ram()

    @classmethod
    def from_file(cls, path) -> "Cartridge":
        p = Path(path)
        return cls(p.read_bytes(), p)

    @property
    def title(self) -> str:
        return self.path.stem if self.path else "NES ROM"

    def _load_ram(self) -> None:
        if self.sav_path is None or not self.sav_path.is_file():
            return
        try:
            data = self.sav_path.read_bytes()
        except OSError:
            return
        self.prg_ram[:len(data[:0x2000])] = data[:0x2000]

    def save_ram(self) -> bool:
        """Write battery-backed PRG RAM to the .sav file. Returns True if written."""
        if self.sav_path is None or not self.ram_dirty:
            return False
        tmp = self.sav_path.with_name(self.sav_path.name + ".tmp")
        try:
            self.sav_path.parent.mkdir(parents=True, exist_ok=True)     # per-game save folder
            tmp.write_bytes(bytes(self.prg_ram))
            os.replace(tmp, self.sav_path)
        except OSError as e:
            print(f"[warning: could not write {self.sav_path.name}: {e}]")
            return False
        self.ram_dirty = False
        return True

    def __str__(self) -> str:
        h = self.header
        return (f"{self.title} [{h.mapper_name}] PRG {h.prg_size // 1024} KB, "
                f"CHR {'RAM 8' if self.chr_ram else h.chr_size // 1024} KB, "
                f"{'battery' if h.battery else 'no battery'}, mirroring {h.mirroring}")
