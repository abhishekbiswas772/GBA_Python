"""MBC mapper + battery save tests (fast, in-memory crafted headers)."""
import os
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import time
from pathlib import Path

import pygame
import pytest

from src.gameboy.cartridge import Cartridge, MBC1, MBC2, MBC3, MBC5
from src.gameboy.display import GRAY_PALETTE, framebuffer_surface
from src.gameboy.gameboy import GameBoy

ROM_SIZE_CODE = {2: 0, 4: 1, 8: 2, 16: 3, 32: 4, 64: 5, 128: 6, 256: 7, 512: 8}
RAM_SIZE_CODE = {0: 0, 8: 2, 32: 3, 128: 4, 64: 5}


def make_cart_rom(cart_type: int, banks: int = 4, ram_kb: int = 0) -> bytes:
    rom = bytearray(banks * 0x4000)
    rom[0x100:0x104] = b"\x00\xC3\x50\x01"
    rom[0x0147] = cart_type
    rom[0x0148] = ROM_SIZE_CODE[banks]
    rom[0x0149] = RAM_SIZE_CODE[ram_kb]
    for b in range(banks):
        rom[b * 0x4000 + 0x1234] = b & 0xFF
        rom[b * 0x4000 + 0x1235] = (b >> 8) & 0xFF
    return bytes(rom)


def bank_at(cart, base: int = 0x4000) -> int:
    """Bank id mapped at region base 0x0000/0x4000 (reads make_cart_rom's marker)."""
    addr = base + 0x1234
    return cart.read(addr) | (cart.read(addr + 1) << 8)


# --- factory dispatch -------------------------------------------------------

def test_factory_dispatch():
    assert type(Cartridge(make_cart_rom(0x00))) is Cartridge
    assert type(Cartridge(make_cart_rom(0x03))) is MBC1
    assert type(Cartridge(make_cart_rom(0x06))) is MBC2
    assert type(Cartridge(make_cart_rom(0x13))) is MBC3
    assert type(Cartridge(make_cart_rom(0x1B))) is MBC5
    with pytest.raises(ValueError, match="unsupported mapper"):
        Cartridge(make_cart_rom(0x0B))       # MMM01


# --- MBC1 -------------------------------------------------------------------

def test_mbc1_bank0_maps_to_1():
    cart = Cartridge(make_cart_rom(0x01, banks=32))
    assert bank_at(cart, 0x4000) == 1
    cart.write(0x2000, 0x00)                 # B1 of 0 behaves as 1
    assert bank_at(cart, 0x4000) == 1
    cart.write(0x2000, 0x07)
    assert bank_at(cart, 0x4000) == 7


def test_mbc1_b1_b2_composition():
    cart = Cartridge(make_cart_rom(0x01, banks=64))
    cart.write(0x2000, 3)
    cart.write(0x4000, 1)
    assert bank_at(cart, 0x4000) == (1 << 5) | 3
    cart.write(0x4000, 0)
    assert bank_at(cart, 0x4000) == 3
    cart.write(0x2000, 0)
    assert bank_at(cart, 0x4000) == (0 << 5) | 1


def test_mbc1_mode1_ram_banking():
    cart = Cartridge(make_cart_rom(0x03, banks=64, ram_kb=32))
    cart.write(0x0000, 0x0A)                 # enable RAM
    cart.write(0x6000, 1)                    # mode 1
    for bank in range(4):
        cart.write(0x4000, bank)
        cart.write(0xA000, 0x40 + bank)
    for bank in range(4):
        cart.write(0x4000, bank)
        assert cart.read(0xA000) == 0x40 + bank
    cart.write(0x4000, 0)
    assert cart.read(0xA000) == 0x40


def test_mbc1_mode1_bank0_region():
    cart = Cartridge(make_cart_rom(0x01, banks=128))
    assert bank_at(cart, 0x0000) == 0        # mode 0: fixed bank 0
    cart.write(0x6000, 1)                    # mode 1: (B2 << 5)
    assert bank_at(cart, 0x0000) == 0
    cart.write(0x4000, 1)
    assert bank_at(cart, 0x0000) == 0x20
    cart.write(0x4000, 3)
    assert bank_at(cart, 0x0000) == 0x60
    cart.write(0x6000, 0)                    # back to mode 0
    assert bank_at(cart, 0x0000) == 0


def test_mbc1_ram_enable_disable():
    cart = Cartridge(make_cart_rom(0x03, banks=4, ram_kb=8))
    assert cart.read(0xA000) == 0xFF         # disabled
    cart.write(0xA000, 0x55)                 # ignored
    cart.write(0x0000, 0x0A)
    assert cart.read(0xA000) == 0x00
    cart.write(0xA000, 0x55)
    assert cart.read(0xA000) == 0x55
    cart.write(0x0000, 0x00)                 # disable
    assert cart.read(0xA000) == 0xFF
    cart.write(0xA000, 0x77)                 # ignored
    cart.write(0x0000, 0x0A)
    assert cart.read(0xA000) == 0x55


def test_mbc1_bank_wrap():
    cart = Cartridge(make_cart_rom(0x01, banks=4))
    cart.write(0x2000, 5)
    assert bank_at(cart, 0x4000) == 5 % 4
    cart.write(0x2000, 6)
    assert bank_at(cart, 0x4000) == 6 % 4
    cart.write(0x2000, 3)
    assert bank_at(cart, 0x4000) == 3


# --- MBC3 -------------------------------------------------------------------

def test_mbc3_rom_bank():
    cart = Cartridge(make_cart_rom(0x11, banks=128))
    assert bank_at(cart, 0x4000) == 1
    cart.write(0x2000, 0x00)                 # 0 -> 1
    assert bank_at(cart, 0x4000) == 1
    cart.write(0x2000, 0x2A)
    assert bank_at(cart, 0x4000) == 0x2A
    cart.write(0x2000, 0x85)                 # 7-bit mask
    assert bank_at(cart, 0x4000) == 5


def test_mbc3_ram_banks():
    cart = Cartridge(make_cart_rom(0x13, banks=16, ram_kb=32))
    cart.write(0x0000, 0x0A)
    for bank in range(4):
        cart.write(0x4000, bank)
        cart.write(0xA000, 0xC0 + bank)
        cart.write(0xA001, bank)
    for bank in range(4):
        cart.write(0x4000, bank)
        assert cart.read(0xA000) == 0xC0 + bank
        assert cart.read(0xA001) == bank
    cart.write(0x0000, 0x00)
    assert cart.read(0xA000) == 0xFF


def mbc3_rtc():
    cart = Cartridge(make_cart_rom(0x10, banks=16, ram_kb=32))
    cart.write(0x0000, 0x0A)                 # RAM/RTC enable
    return cart


def rtc_select(cart, reg):
    cart.write(0x4000, reg)


def latch(cart):
    cart.write(0x6000, 0x00)
    cart.write(0x6000, 0x01)


def set_halt(cart, halted=True):
    rtc_select(cart, 0x0C)
    cart.write(0xA000, 0x40 if halted else 0x00)


def test_mbc3_rtc_latch():
    cart = mbc3_rtc()
    set_halt(cart)
    rtc_select(cart, 0x08)
    cart.write(0xA000, 30)
    assert cart.read(0xA000) == 0            # not latched yet
    latch(cart)
    assert cart.read(0xA000) == 30
    cart.write(0xA000, 45)                   # live regs advance
    assert cart.read(0xA000) == 30           # ...but latch holds
    cart.write(0x6000, 0x01)                 # 0x01 twice: no re-latch
    assert cart.read(0xA000) == 30
    latch(cart)
    assert cart.read(0xA000) == 45


def test_mbc3_rtc_halt_and_writes():
    cart = mbc3_rtc()
    rtc_select(cart, 0x08)
    cart.write(0xA000, 30)                   # running: ignored
    assert cart.rtc.regs[0] == 0
    set_halt(cart)
    rtc_select(cart, 0x08)
    cart.write(0xA000, 30)
    assert cart.rtc.regs[0] == 30
    cart.rtc.last = time.time() - 100        # halted: time discarded
    cart.write(0xA000, 30)                   # triggers lazy advance()
    assert cart.rtc.regs[0] == 30
    set_halt(cart, False)                    # running again
    cart.rtc.last = time.time() - 100
    latch(cart)
    assert cart.rtc.regs[0] in (10, 11)      # (30 + ~100s) % 60


def test_mbc3_rtc_registers():
    cart = mbc3_rtc()
    set_halt(cart)
    for reg, val in ((0x08, 11), (0x09, 22), (0x0A, 3), (0x0B, 0x34), (0x0C, 0x41)):
        rtc_select(cart, reg)
        cart.write(0xA000, val)
    latch(cart)
    for reg, val in ((0x08, 11), (0x09, 22), (0x0A, 3), (0x0B, 0x34), (0x0C, 0x41)):
        rtc_select(cart, reg)
        assert cart.read(0xA000) == val
    assert cart.rtc.regs[4] == 0x41          # halt + day bit 8


def test_mbc3_rtc_day_carry():
    cart = mbc3_rtc()
    set_halt(cart)
    rtc_select(cart, 0x0B)
    cart.write(0xA000, 0xFF)
    rtc_select(cart, 0x0C)
    cart.write(0xA000, 0x01)                 # day 511, running
    cart.rtc.last = time.time() - 200_000    # > 2 days of wall time
    latch(cart)
    assert cart.rtc.regs[4] & 0x80           # carry set
    assert (cart.rtc.regs[3] | ((cart.rtc.regs[4] & 1) << 8)) < 511


def test_mbc3_no_rtc_pokemon_path():
    cart = Cartridge(make_cart_rom(0x13, banks=64, ram_kb=32))
    assert cart.rtc is None
    cart.write(0x0000, 0x0A)
    cart.write(0x4000, 0x08)                 # RTC select has no effect
    assert cart.read(0xA000) == 0xFF
    cart.write(0xA000, 0x55)                 # ignored
    cart.write(0x4000, 0x00)
    assert cart.read(0xA000) == 0x00
    cart.write(0x4000, 0x03)                 # RAM bank 3 of 4
    cart.write(0xA000, 0x99)
    cart.write(0x4000, 0x03)
    assert cart.read(0xA000) == 0x99


# --- Pokemon Red (MBC3 + battery): boot to title, SRAM save persistence -----

POKEMON_ROM = (Path(__file__).parent.parent / "src" / "rom" /
               "Pokemon - Red Version (USA, Europe) (SGB Enhanced).gb")
POKEMON_PNG = Path(__file__).parent / "roms" / "other" / "pokemon-title-actual.png"


@pytest.mark.slow
def test_pokemon_red_title_and_battery_save():
    if not POKEMON_ROM.exists():
        pytest.skip("commercial ROM not present")
    sav = POKEMON_ROM.with_suffix(".sav")
    if sav.exists():
        sav.unlink()                       # start from clean SRAM
    gb = GameBoy(POKEMON_ROM)
    for _ in range(1500):                  # ~25s of Game Boy time: intro -> title
        gb.run_frame()
    pygame.image.save(framebuffer_surface(gb.ppu.framebuffer, palette=GRAY_PALETTE),
                      str(POKEMON_PNG))
    # the game keeps save bookkeeping in SRAM during boot/intro
    assert sum(1 for b in gb.cart.ram if b) > 0
    assert gb.cart.save_ram(force=True) is True
    assert sav.exists()
    cart2 = Cartridge.from_file(POKEMON_ROM)   # fresh cart loads the .sav
    assert bytes(cart2.ram) == bytes(gb.cart.ram)


# --- MBC5 -------------------------------------------------------------------

def test_mbc5_rom_bank_9bit():
    cart = Cartridge(make_cart_rom(0x1B, banks=512, ram_kb=32))
    assert bank_at(cart, 0x4000) == 1
    cart.write(0x2000, 0x00)                 # bank 0 is a valid target
    cart.write(0x3000, 0x00)
    assert bank_at(cart, 0x4000) == 0
    cart.write(0x2000, 0x34)
    cart.write(0x3000, 0x01)                 # bit 8
    assert bank_at(cart, 0x4000) == 0x134
    cart.write(0x3000, 0x00)
    assert bank_at(cart, 0x4000) == 0x34
    cart.write(0x2000, 0xFF)
    cart.write(0x3000, 0x01)
    assert bank_at(cart, 0x4000) == 0x1FF


def test_mbc5_ram_bank():
    cart = Cartridge(make_cart_rom(0x1B, banks=16, ram_kb=128))
    cart.write(0x0000, 0x0A)
    for bank in (0, 1, 8, 15):
        cart.write(0x4000, bank)
        cart.write(0xA000, 0x50 + bank)
    for bank in (0, 1, 8, 15):
        cart.write(0x4000, bank)
        assert cart.read(0xA000) == 0x50 + bank


# --- MBC2 -------------------------------------------------------------------

def test_mbc2_nibble_ram_and_bank():
    cart = Cartridge(make_cart_rom(0x06, banks=16))
    assert isinstance(cart, MBC2)
    assert bank_at(cart, 0x4000) == 1
    cart.write(0x2100, 0x05)                 # ROM bank register (bit 8 set)
    assert bank_at(cart, 0x4000) == 5
    cart.write(0x0000, 0x0A)                 # RAM enable (bit 8 clear)
    assert cart.read(0xA000) == 0xF0
    cart.write(0xA000, 0xAB)                 # only low nibble kept
    assert cart.read(0xA000) == 0xFB
    cart.write(0x2000, 0x00)                 # not a bank write (bit 8 clear)
    assert bank_at(cart, 0x4000) == 5
    cart.write(0x0000, 0x00)
    assert cart.read(0xA000) == 0xFF


# --- battery saves ----------------------------------------------------------

def test_save_roundtrip(tmp_path):
    rom_path = tmp_path / "game.gb"
    rom_path.write_bytes(make_cart_rom(0x03, banks=4, ram_kb=8))
    assert Cartridge.from_file(rom_path).sav_path == tmp_path / "game.sav"
    cart = Cartridge.from_file(rom_path)
    cart.write(0x0000, 0x0A)
    for i in range(0x2000):
        cart.write(0xA000 + i, (i * 7) & 0xFF)
    assert cart.ram_dirty
    assert cart.save_ram() is True
    assert cart.ram_dirty is False
    assert cart.save_ram() is False           # unchanged: no rewrite
    cart2 = Cartridge.from_file(rom_path)
    assert bytes(cart2.ram) == bytes(cart.ram)
    cart2.write(0x0000, 0x0A)
    assert cart2.read(0xA000 + 123) == cart.read(0xA000 + 123)


def test_disabled_ram_writes_not_saved(tmp_path):
    rom_path = tmp_path / "game.gb"
    rom_path.write_bytes(make_cart_rom(0x03, banks=4, ram_kb=8))
    cart = Cartridge.from_file(rom_path)
    cart.write(0xA000, 0x55)                  # RAM disabled: ignored
    assert not cart.ram_dirty
    assert cart.save_ram() is False
    assert not (tmp_path / "game.sav").exists()
    cart2 = Cartridge.from_file(rom_path)
    assert cart2.ram[0] == 0


def test_no_battery_no_save(tmp_path):
    rom_path = tmp_path / "plain.gb"
    rom_path.write_bytes(make_cart_rom(0x02, banks=4, ram_kb=8))
    cart = Cartridge.from_file(rom_path)
    cart.write(0x0000, 0x0A)
    cart.write(0xA000, 0x42)
    assert cart.save_ram(force=True) is False
    assert not (tmp_path / "plain.sav").exists()


def test_rtc_persist_roundtrip(tmp_path):
    rom_path = tmp_path / "rtc.gb"
    rom_path.write_bytes(make_cart_rom(0x10, banks=16, ram_kb=32))
    cart = Cartridge.from_file(rom_path)
    cart.write(0x0000, 0x0A)
    set_halt(cart)
    for reg, val in ((0x08, 11), (0x09, 22), (0x0A, 3), (0x0B, 0x34), (0x0C, 0x41)):
        rtc_select(cart, reg)
        cart.write(0xA000, val)
    latch(cart)
    cart.write(0x4000, 1)
    cart.write(0xA000, 0x77)
    assert cart.save_ram() is True            # dirty from RTC + RAM writes
    cart2 = Cartridge.from_file(rom_path)
    assert cart2.rtc.regs == [11, 22, 3, 0x34, 0x41]
    assert cart2.rtc.latch == [11, 22, 3, 0x34, 0x41]
    cart2.write(0x0000, 0x0A)
    cart2.write(0x4000, 1)
    assert cart2.read(0xA000) == 0x77


def test_sav_without_rtc_footer(tmp_path):
    """A plain RAM-only .sav (no 48-byte footer) still loads."""
    rom_path = tmp_path / "rtc.gb"
    rom_path.write_bytes(make_cart_rom(0x10, banks=16, ram_kb=32))
    (tmp_path / "rtc.sav").write_bytes(bytes(range(256)) * 128)   # 32KiB, no footer
    cart = Cartridge.from_file(rom_path)
    assert cart.ram[0] == 0 and cart.ram[1] == 1
    assert cart.rtc.regs == [0] * 5
