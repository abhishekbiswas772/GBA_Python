from __future__ import annotations
import os
import struct
import time
from dataclasses import dataclass
from pathlib import Path
from src.core.gameboy.common import (
    NINTENDO_LOGO, CARTRIDGE_TYPES, RAM_SIZES,
    BATTERY_TYPES, RTC_TYPES,
)

@dataclass
class CartridgeHeader:
    title: str
    cart_type: int 
    rom_size: int 
    ram_size: int 
    logo_ok: bool
    checksum_ok: bool
    cgb_flag: int = 0              # 0x0143: 0x80 = CGB-enhanced, 0xC0 = CGB-only

    @property
    def supports_cgb(self) -> bool:
        return bool(self.cgb_flag & 0x80)

    @property
    def type_name(self) -> str:
        return CARTRIDGE_TYPES.get(self.cart_type, f"UNKNOWN (0x{self.cart_type:02X})")


    @property
    def rom_banks(self) -> int:
        return self.rom_size // 0x4000


def parse_header(rom: bytes) -> CartridgeHeader:
    if len(rom) < 0x0150:
        raise ValueError(f"File is too Small ({len(rom)} bytes) - not a Game Boy ROM")

    checksum = 0
    for addr in range(0x0134, 0x014D):
        checksum = (checksum - rom[addr] - 1) & 0xFF
    size_code = rom[0x0148]
    if size_code in (0x52, 0x53, 0x54):
        rom_size = {0x52: 72, 0x53: 80, 0x54: 96}[size_code] * 0x4000
    elif size_code <= 8:
        rom_size = 0x8000 << size_code
    else:
        rom_size = len(rom)
    return CartridgeHeader(
        title= rom[0x0134: 0x0143 if rom[0x0143] & 0x80 else 0x0144].split(b"\x00")[0].decode(
            "ascii", errors="replace"
        ).strip(),
        cart_type=rom[0x0147],
        rom_size=rom_size,
        ram_size=RAM_SIZES.get(rom[0x0149], 0) * 1024,
        logo_ok=rom[0x0104: 0x0134] == NINTENDO_LOGO,
        checksum_ok= checksum == rom[0x014D],
        cgb_flag=rom[0x0143],
    )


class RTC:
    """MBC3 real-time clock. Counters advance lazily from the wall clock."""
    MASKS = (0x3F, 0x3F, 0x1F, 0xFF, 0xC1)

    def __init__(self):
        self.regs = [0] * 5            # sec, min, hr, day-low, day-high
        self.latch = [0] * 5
        self.last = time.time()

    def advance(self) -> None:
        now = time.time()
        dt = int(now - self.last)
        if dt <= 0:
            return
        self.last += dt
        if self.regs[4] & 0x40:        # halted: elapsed time discarded
            return
        s = self.regs[0] + dt
        self.regs[0] = s % 60
        m = self.regs[1] + s // 60
        self.regs[1] = m % 60
        h = self.regs[2] + m // 60
        self.regs[2] = h % 24
        d = (self.regs[3] | ((self.regs[4] & 1) << 8)) + h // 24
        if d > 0x1FF:
            self.regs[4] |= 0x80      # day carry
        self.regs[3] = d & 0xFF
        self.regs[4] = (self.regs[4] & 0xFE) | ((d >> 8) & 1)

    def read(self, reg: int) -> int:
        return self.latch[reg]

    def write(self, reg: int, value: int) -> bool:
        # Writable at any time on real MBC3 (games are told to halt first, not forced to).
        self.advance()
        if reg == 0:
            self.last = time.time()    # writing seconds resets the sub-second phase
        value &= self.MASKS[reg]
        changed = self.regs[reg] != value
        self.regs[reg] = value
        return changed

    def latch_clock(self) -> None:
        self.advance()
        self.latch = list(self.regs)

    def to_bytes(self) -> bytes:
        self.advance()
        return (struct.pack("<5I", *self.regs) + struct.pack("<5I", *self.latch)
                + struct.pack("<Q", int(self.last)))

    def from_bytes(self, data: bytes) -> None:
        """Load the VBA/BGB RTC footer: 48 bytes (64-bit timestamp) or 44 (32-bit)."""
        cur = struct.unpack("<5I", data[:20])
        lat = struct.unpack("<5I", data[20:40])
        (ts,) = struct.unpack("<Q", data[40:48]) if len(data) >= 48 else struct.unpack("<I", data[40:44])
        self.regs = [cur[i] & self.MASKS[i] for i in range(5)]
        self.latch = [lat[i] & self.MASKS[i] for i in range(5)]
        self.last = float(ts)


class Cartridge:
    """ROM-only cartridge (also ROM+RAM types 0x08/0x09). Base for the MBCs."""
    rtc = None

    def __new__(cls, rom: bytes, path: Path | str | None = None):
        if cls is Cartridge:           # factory dispatch on header 0x0147
            cls = _mapper_class(parse_header(rom))
        return super().__new__(cls)

    def __init__(self, rom: bytes, path: Path | str | None = None):
        data = bytes(rom)
        self.header = parse_header(data)
        need = max(self.header.rom_size, 0x8000)
        if len(data) < need <= 0x800000:
            data += b"\xFF" * (need - len(data))
        self.rom = data
        self.ram = bytearray(self._ram_size())
        self.ram_dirty = False
        self.sav_path = Path(path).with_suffix(".sav") if path is not None else None
        self.has_battery = self.header.cart_type in BATTERY_TYPES
        self._load_save()

    def _ram_size(self) -> int:
        return self.header.ram_size

    @classmethod
    def from_file(cls, path: str | Path) -> "Cartridge":
        p = Path(path)
        return cls(p.read_bytes(), p)

    def read(self, addr: int) -> int:
        if addr < 0x8000:                                   # ROM
            return self.rom[addr]
        if 0xA000 <= addr < 0xC000:                         # external RAM
            offset = addr - 0xA000
            return self.ram[offset] if offset < len(self.ram) else 0xFF
        raise ValueError(f"Cartridge.read: 0x{addr:04X} is not a cartridge address")

    def write(self, addr: int, value: int) -> None:
        if addr < 0x8000:
            return
        if 0xA000 <= addr < 0xC000:
            self._ram_write(addr, value)
            return
        raise ValueError(f"Cartridge.write: 0x{addr:04X} is not a cartridge address")

    def _ram_write(self, addr: int, value: int) -> None:
        offset = addr - 0xA000
        if offset < len(self.ram) and self.ram[offset] != value:
            self.ram[offset] = value
            self.ram_dirty = True

    def save_ram(self, force: bool = False) -> bool:
        """Write external RAM (and RTC state) to <rom>.sav. Returns True if written."""
        if not (self.has_battery and self.sav_path):
            return False
        if not (self.ram_dirty or force):
            return False
        extra = self.rtc.to_bytes() if self.rtc is not None else b""
        tmp = self.sav_path.with_name(self.sav_path.name + ".tmp")
        try:                           # atomic: a crash mid-write never truncates the .sav
            tmp.write_bytes(bytes(self.ram) + extra)
            os.replace(tmp, self.sav_path)
        except OSError as e:
            print(f"[warning: could not write {self.sav_path.name}: {e}]")
            return False
        self.ram_dirty = False
        return True

    def _load_save(self) -> None:
        if not (self.has_battery and self.sav_path and self.sav_path.exists()):
            return
        data = self.sav_path.read_bytes()
        n = min(len(self.ram), len(data))
        self.ram[:n] = data[:n]
        if self.rtc is not None and len(data) - n in (44, 48):
            self.rtc.from_bytes(data[n:])

    def __repr__(self) -> str:
        h = self.header
        return (f"<{type(self).__name__} '{h.title}' {h.type_name} "
                f"ROM={h.rom_size // 1024}KiB({h.rom_banks} banks) RAM={h.ram_size // 1024}KiB>")


class MBC(Cartridge):
    """Shared RAM banking for the memory bank controllers."""
    def __init__(self, rom: bytes, path: Path | str | None = None):
        super().__init__(rom, path)
        self.ram_enabled = False
        self.rom0_base = 0
        self.romx_base = 0x4000       # bank 1
        self.ram_base = 0

    def _ram_read(self, addr: int) -> int:
        if self.ram_enabled:
            offset = self.ram_base + addr - 0xA000
            if offset < len(self.ram):
                return self.ram[offset]
        return 0xFF

    def _ram_write(self, addr: int, value: int) -> None:
        if self.ram_enabled:
            offset = self.ram_base + addr - 0xA000
            if offset < len(self.ram) and self.ram[offset] != value:
                self.ram[offset] = value
                self.ram_dirty = True


class MBC1(MBC):
    def __init__(self, rom: bytes, path: Path | str | None = None):
        super().__init__(rom, path)
        self.b1 = 1                   # 5-bit ROM bank register
        self.b2 = 0                   # 2-bit bank register
        self.mode = 0
        self._update()

    def _update(self) -> None:
        banks = self.header.rom_banks
        self.romx_base = (((self.b2 << 5) | (self.b1 or 1)) % banks) * 0x4000
        self.rom0_base = ((self.b2 << 5) % banks if self.mode else 0) * 0x4000
        ram_banks = max(len(self.ram) // 0x2000, 1)
        self.ram_base = (self.b2 % ram_banks) * 0x2000 if self.mode else 0

    def read(self, addr: int) -> int:
        if addr < 0x4000:
            return self.rom[self.rom0_base + addr]
        if addr < 0x8000:
            return self.rom[self.romx_base + addr - 0x4000]
        if 0xA000 <= addr < 0xC000:
            return self._ram_read(addr)
        raise ValueError(f"Cartridge.read: 0x{addr:04X} is not a cartridge address")

    def write(self, addr: int, value: int) -> None:
        if addr < 0x2000:
            self.ram_enabled = value & 0x0F == 0x0A
        elif addr < 0x4000:
            self.b1 = value & 0x1F
            self._update()
        elif addr < 0x6000:
            self.b2 = value & 0x03
            self._update()
        elif addr < 0x8000:
            self.mode = value & 0x01
            self._update()
        elif 0xA000 <= addr < 0xC000:
            self._ram_write(addr, value)
        else:
            raise ValueError(f"Cartridge.write: 0x{addr:04X} is not a cartridge address")


class MBC2(MBC):
    """MBC2: 512 x 4-bit built-in RAM; address bit 8 selects RAM-enable vs ROM bank."""
    def _ram_size(self) -> int:
        return 512

    def __init__(self, rom: bytes, path: Path | str | None = None):
        super().__init__(rom, path)
        self.rom_bank = 1

    def read(self, addr: int) -> int:
        if addr < 0x4000:
            return self.rom[addr]
        if addr < 0x8000:
            return self.rom[self.romx_base + addr - 0x4000]
        if 0xA000 <= addr < 0xC000:
            if self.ram_enabled:
                return 0xF0 | self.ram[addr & 0x1FF]
            return 0xFF
        raise ValueError(f"Cartridge.read: 0x{addr:04X} is not a cartridge address")

    def write(self, addr: int, value: int) -> None:
        if addr < 0x4000:
            if addr & 0x0100:
                self.rom_bank = (value & 0x0F) or 1
                self.romx_base = (self.rom_bank % self.header.rom_banks) * 0x4000
            else:
                self.ram_enabled = value & 0x0F == 0x0A
        elif 0xA000 <= addr < 0xC000:
            if self.ram_enabled:
                v = value & 0x0F
                if self.ram[addr & 0x1FF] != v:
                    self.ram[addr & 0x1FF] = v
                    self.ram_dirty = True
        else:
            raise ValueError(f"Cartridge.write: 0x{addr:04X} is not a cartridge address")


class MBC3(MBC):
    def __init__(self, rom: bytes, path: Path | str | None = None):
        self.rtc = RTC() if parse_header(rom).cart_type in RTC_TYPES else None
        super().__init__(rom, path)
        self.rom_bank = 1
        self.ram_sel = 0               # 0-3: RAM bank, 8-0x0C: RTC register
        self._latch_prev = 0xFF

    def read(self, addr: int) -> int:
        if addr < 0x4000:
            return self.rom[addr]
        if addr < 0x8000:
            return self.rom[self.romx_base + addr - 0x4000]
        if 0xA000 <= addr < 0xC000:
            if self.ram_enabled:
                sel = self.ram_sel
                if sel <= 3:
                    return self._ram_read(addr)
                if self.rtc is not None and 0x08 <= sel <= 0x0C:
                    return self.rtc.read(sel - 0x08)
            return 0xFF
        raise ValueError(f"Cartridge.read: 0x{addr:04X} is not a cartridge address")

    def write(self, addr: int, value: int) -> None:
        if addr < 0x2000:
            self.ram_enabled = value & 0x0F == 0x0A
        elif addr < 0x4000:
            self.rom_bank = value & 0x7F or 1
            self.romx_base = (self.rom_bank % self.header.rom_banks) * 0x4000
        elif addr < 0x6000:
            self.ram_sel = value & 0x0F
            if self.ram_sel <= 3:
                ram_banks = max(len(self.ram) // 0x2000, 1)
                self.ram_base = (self.ram_sel % ram_banks) * 0x2000
        elif addr < 0x8000:
            if self._latch_prev == 0x00 and value == 0x01 and self.rtc is not None:
                self.rtc.latch_clock()
            self._latch_prev = value
        elif 0xA000 <= addr < 0xC000:
            if self.ram_enabled:
                sel = self.ram_sel
                if sel <= 3:
                    self._ram_write(addr, value)
                elif (self.rtc is not None and 0x08 <= sel <= 0x0C
                      and self.rtc.write(sel - 0x08, value)):
                    self.ram_dirty = True
        else:
            raise ValueError(f"Cartridge.write: 0x{addr:04X} is not a cartridge address")


class MBC5(MBC):
    def __init__(self, rom: bytes, path: Path | str | None = None):
        super().__init__(rom, path)
        self.rom_bank = 1

    def read(self, addr: int) -> int:
        if addr < 0x4000:
            return self.rom[addr]
        if addr < 0x8000:
            return self.rom[self.romx_base + addr - 0x4000]
        if 0xA000 <= addr < 0xC000:
            return self._ram_read(addr)
        raise ValueError(f"Cartridge.read: 0x{addr:04X} is not a cartridge address")

    def write(self, addr: int, value: int) -> None:
        if addr < 0x2000:
            self.ram_enabled = value & 0x0F == 0x0A
        elif addr < 0x3000:
            self.rom_bank = (self.rom_bank & 0x100) | value
            self.romx_base = (self.rom_bank % self.header.rom_banks) * 0x4000
        elif addr < 0x4000:
            self.rom_bank = (self.rom_bank & 0xFF) | ((value & 1) << 8)
            self.romx_base = (self.rom_bank % self.header.rom_banks) * 0x4000
        elif addr < 0x6000:
            ram_banks = max(len(self.ram) // 0x2000, 1)
            mask = 0x07 if self.header.cart_type in (0x1C, 0x1D, 0x1E) else 0x0F  # bit 3 = rumble motor
            self.ram_base = ((value & mask) % ram_banks) * 0x2000
        elif addr < 0x8000:
            pass
        elif 0xA000 <= addr < 0xC000:
            self._ram_write(addr, value)
        else:
            raise ValueError(f"Cartridge.write: 0x{addr:04X} is not a cartridge address")


def _mapper_class(header: CartridgeHeader):
    t = header.cart_type
    if t in (0x00, 0x08, 0x09):
        return Cartridge
    if t in (0x01, 0x02, 0x03):
        return MBC1
    if t in (0x05, 0x06):
        return MBC2
    if t in (0x0F, 0x10, 0x11, 0x12, 0x13):
        return MBC3
    if t in (0x19, 0x1A, 0x1B, 0x1C, 0x1D, 0x1E):
        return MBC5
    raise ValueError(f"unsupported mapper: {header.type_name}")
