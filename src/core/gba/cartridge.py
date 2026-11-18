"""GBA cartridge: ROM header, save-type detection and backup memory.

The save type is detected from the Nintendo SDK library ID string in the ROM
(EEPROM_V / SRAM_V / FLASH_V / FLASH512_V / FLASH1M_V). Backup memory persists
to `<rom>.sav` (raw bytes; written via a temp file + rename).
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from src.core.gba.rtc import RTC
from src.core.gba.common import (
    RTC_SIGNATURE,
    SAVE_NONE, SAVE_SRAM, SAVE_FLASH64, SAVE_FLASH128, SAVE_EEPROM, SAVE_SIGNATURES,
    SRAM_SIZE, FLASH_BANK, FLASH_ID_64, FLASH_ID_128, EEPROM_SMALL, EEPROM_LARGE,
)


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
    text = lambda a, b: rom[a:b].split(b"\x00")[0].decode("ascii", errors="replace").strip()
    return Header(title=text(0xA0, 0xAC), game_code=text(0xAC, 0xB0),
                  maker=text(0xB0, 0xB2), version=rom[0xBC], checksum_ok=chk == rom[0xBD])


def detect_save(rom: bytes) -> str:
    for sig, kind in SAVE_SIGNATURES:
        if rom.find(sig) >= 0:
            return kind
    return SAVE_NONE


# ------------------------------------------------------------------ backup memory
class SRAM:
    kind = SAVE_SRAM

    def __init__(self):
        self.data = bytearray(b"\xff" * SRAM_SIZE)
        self.dirty = False

    def read8(self, a: int) -> int:
        return self.data[a & (SRAM_SIZE - 1)]

    def write8(self, a: int, v: int) -> None:
        self.data[a & (SRAM_SIZE - 1)] = v
        self.dirty = True


class Flash:
    def __init__(self, size: int):
        self.kind = SAVE_FLASH128 if size > FLASH_BANK else SAVE_FLASH64
        self.data = bytearray(b"\xff" * size)
        self.ident = FLASH_ID_128 if size > FLASH_BANK else FLASH_ID_64
        self.bank = 0
        self.state = 0               # command handshake progress
        self.id_mode = False
        self.erase_armed = False
        self.write_next = False
        self.bank_next = False
        self.dirty = False

    def read8(self, a: int) -> int:
        a &= 0xFFFF
        if self.id_mode and a < 2:
            return self.ident[a]
        return self.data[self.bank * FLASH_BANK + a]

    def write8(self, a: int, v: int) -> None:
        a &= 0xFFFF
        if self.write_next:
            self.data[self.bank * FLASH_BANK + a] &= v
            self.write_next = False
            self.dirty = True
            return
        if self.bank_next:
            if a == 0:
                self.bank = v & (len(self.data) // FLASH_BANK - 1)
            self.bank_next = False
            return
        if self.state == 0 and a == 0x5555 and v == 0xAA:
            self.state = 1
        elif self.state == 1 and a == 0x2AAA and v == 0x55:
            self.state = 2
        elif self.state == 2:
            self.state = 0
            if self.erase_armed:
                self.erase_armed = False
                if a == 0x5555 and v == 0x10:
                    self.data[:] = b"\xff" * len(self.data)
                    self.dirty = True
                elif v == 0x30:
                    start = self.bank * FLASH_BANK + (a & 0xF000)
                    self.data[start:start + 0x1000] = b"\xff" * 0x1000
                    self.dirty = True
                return
            if a != 0x5555:
                return
            if v == 0x90:
                self.id_mode = True
            elif v == 0xF0:
                self.id_mode = False
            elif v == 0x80:
                self.erase_armed = True
            elif v == 0xA0:
                self.write_next = True
            elif v == 0xB0 and len(self.data) > FLASH_BANK:
                self.bank_next = True
        else:
            self.state = 0
            if v == 0xF0:
                self.id_mode = False


class EEPROM:
    """Serial EEPROM on the ROM bus (bit 0 of 16-bit accesses, normally via DMA3).

    Requests: '11' + addr + '0' (read, then 68 bits: 4 junk + 64 data) or
    '10' + addr + 64 data bits + '0' (write). Address width (6 or 14 bits) is
    inferred from the DMA length of the first request.
    """
    kind = SAVE_EEPROM

    def __init__(self):
        self.data = bytearray(b"\xff" * EEPROM_LARGE)
        self.size = 0                       # 0 = not yet known
        self.bits: list[int] = []
        self.expect = 0                     # bit count of the current request (from DMA)
        self.out: list[int] = []
        self.dirty = False

    @property
    def addr_bits(self) -> int:
        return 6 if self.size == EEPROM_SMALL else 14

    def begin(self, count: int) -> None:
        """DMA to EEPROM of `count` halfwords is starting."""
        if self.size == 0:
            if count in (9, 73):
                self.size = EEPROM_SMALL
                del self.data[EEPROM_SMALL:]
            elif count in (17, 81):
                self.size = EEPROM_LARGE
        self.expect = count
        self.bits = []

    def write_bit(self, v: int) -> None:
        self.bits.append(v & 1)
        n = len(self.bits)
        ab = self.addr_bits
        if n < 2:
            return
        cmd = (self.bits[0] << 1) | self.bits[1]
        if cmd == 3 and n == 2 + ab + 1:
            addr = self._addr(ab)
            self.out = [0, 0, 0, 0]
            block = self.data[addr * 8: addr * 8 + 8]
            for byte in block:
                self.out += [(byte >> s) & 1 for s in range(7, -1, -1)]
            self.bits = []
        elif cmd == 2 and n == 2 + ab + 64 + 1:
            addr = self._addr(ab)
            payload = self.bits[2 + ab: 2 + ab + 64]
            for k in range(8):
                byte = 0
                for bit in payload[k * 8: k * 8 + 8]:
                    byte = (byte << 1) | bit
                if addr * 8 + k < len(self.data):
                    self.data[addr * 8 + k] = byte
            self.dirty = True
            self.out = []
            self.bits = []
        elif cmd in (0, 1):
            self.bits = []

    def _addr(self, ab: int) -> int:
        a = 0
        for bit in self.bits[2:2 + ab]:
            a = (a << 1) | bit
        return a & ((len(self.data) // 8) - 1)

    def read_bit(self) -> int:
        if self.out:
            return self.out.pop(0)
        return 1                            # ready

    def read8(self, a: int) -> int:
        return 0xFF

    def write8(self, a: int, v: int) -> None:
        pass


def make_save(kind: str):
    if kind == SAVE_SRAM:
        return SRAM()
    if kind == SAVE_FLASH64:
        return Flash(FLASH_BANK)
    if kind == SAVE_FLASH128:
        return Flash(FLASH_BANK * 2)
    if kind == SAVE_EEPROM:
        return EEPROM()
    return None


class Cartridge:
    def __init__(self, rom: bytes, path: Path | None = None):
        self.rom = bytes(rom)
        self.header = parse_header(self.rom)
        self.save_kind = detect_save(self.rom)
        self.save = make_save(self.save_kind)
        self.rtc = RTC() if RTC_SIGNATURE in self.rom else None
        self.path = path
        self.sav_path = path.with_suffix(".sav") if path is not None else None
        if self.save is not None and self.sav_path is not None and self.sav_path.exists():
            self._load(self.sav_path.read_bytes())

    @classmethod
    def from_file(cls, path: str | Path) -> "Cartridge":
        path = Path(path)
        return cls(path.read_bytes(), path)

    def _load(self, data: bytes) -> None:
        s = self.save
        if isinstance(s, EEPROM):
            if len(data) <= EEPROM_SMALL:
                s.size = EEPROM_SMALL
                s.data = bytearray(data[:EEPROM_SMALL].ljust(EEPROM_SMALL, b"\xff"))
            else:
                s.size = EEPROM_LARGE
                s.data[:] = data[:EEPROM_LARGE].ljust(EEPROM_LARGE, b"\xff")
            return
        n = min(len(data), len(s.data))
        s.data[:n] = data[:n]

    def save_ram(self) -> bool:
        s = self.save
        if s is None or self.sav_path is None or not s.dirty:
            return False
        tmp = self.sav_path.with_suffix(".sav.tmp")
        tmp.write_bytes(bytes(s.data))
        os.replace(tmp, self.sav_path)
        s.dirty = False
        return True

    def __str__(self) -> str:
        h = self.header
        return (f"{h.title} [{h.game_code}] maker {h.maker} v{h.version} - "
                f"{len(self.rom) // 1024} KB, save: {self.save_kind}"
                + (", RTC" if self.rtc is not None else "")
                + ("" if h.checksum_ok else " (bad header checksum)"))
