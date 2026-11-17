from __future__ import annotations
from src.gameboy.common import NINTENDO_LOGO, CARTRIDGE_TYPES, RAM_SIZES
from dataclasses import dataclass
from pathlib import Path

@dataclass
class CartridgeHeader:
    title: str
    cart_type: int 
    rom_size: int 
    ram_size: int 
    logo_ok: bool
    checksum_ok: bool

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
    return CartridgeHeader(
        title= rom[0x0134: 0x0144].split(b"\x00")[0].decode(
            "ascii", errors="replace"
        ).strip(),
        cart_type=rom[0x0147],
        rom_size=0x8000 << rom[0x0148],
        ram_size=RAM_SIZES.get(rom[0x0149], 0) * 1024,
        logo_ok=rom[0x0104: 0x0134] == NINTENDO_LOGO,
        checksum_ok= checksum == rom[0x014D]
    )


class Cartridge:
    def __init__(self, rom: bytes):
        self.rom = bytes(rom)                       
        self.header = parse_header(self.rom)
        self.ram = bytearray(self.header.ram_size) 

    @classmethod
    def from_file(cls, path: str | Path) -> "Cartridge":
        return cls(Path(path).read_bytes())

    def read(self, addr: int) -> int:
        if addr < 0x8000:                                   # ROM
            return self.rom[addr] if addr < len(self.rom) else 0xFF
        if 0xA000 <= addr < 0xC000:                         # external RAM
            offset = addr - 0xA000
            return self.ram[offset] if offset < len(self.ram) else 0xFF
        raise ValueError(f"Cartridge.read: 0x{addr:04X} is not a cartridge address")

    def write(self, addr: int, value: int) -> None:
        if addr < 0x8000:
            return
        if 0xA000 <= addr < 0xC000:
            offset = addr - 0xA000
            if offset < len(self.ram):
                self.ram[offset] = value
            return
        raise ValueError(f"Cartridge.write: 0x{addr:04X} is not a cartridge address")

    def __repr__(self) -> str:
        h = self.header
        return (f"<Cartridge '{h.title}' {h.type_name} "
                f"ROM={h.rom_size // 1024}KiB({h.rom_banks} banks) RAM={h.ram_size // 1024}KiB>")