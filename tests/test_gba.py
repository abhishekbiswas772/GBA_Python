"""GBA core tests: jsmolka gba-tests ROMs (MIT, see tests/roms/gba/gba-tests-LICENSE.txt)
plus unit checks of the HLE BIOS, saves and PPU. The ROM tests report the number of
the first failed test in r12 once the CPU reaches its idle loop (0 = all passed)."""
from pathlib import Path

import pytest

from src.core.gba.cartridge import EEPROM, Flash, detect_save
from src.core.gba.common import SAVE_EEPROM, SAVE_FLASH128, SAVE_SRAM, WIDTH, HEIGHT
from src.core.gba.gba import GBA

ROMS = Path(__file__).parent / "roms" / "gba"
COMMERCIAL = Path(__file__).parent.parent / "rom" / "gba"


def run_test_rom(name: str, max_frames: int = 180) -> GBA:
    gba = GBA((ROMS / name).read_bytes())
    for _ in range(max_frames):
        gba.run_frame()
        pc = gba.cpu.pc
        if gba.cpu.thumb:
            if gba.bus.read16(pc) == 0xE7FE:
                return gba
        elif gba.bus.read32(pc) == 0xEAFFFFFE:          # b . (idle loop)
            return gba
    pytest.fail(f"{name}: idle loop not reached in {max_frames} frames (pc={gba.cpu.pc:08X})")


@pytest.mark.parametrize("name", [
    "arm.gba", "thumb.gba", "memory.gba", "bios.gba",
    "sram.gba", "flash64.gba", "flash128.gba", "none.gba", "unsafe.gba",
])
def test_jsmolka(name):
    gba = run_test_rom(name)
    assert gba.cpu.r[12] == 0, f"{name}: failed test {gba.cpu.r[12]}"


def test_jsmolka_nes_pipeline():
    assert run_test_rom("nes.gba").cpu.r[12] == 0


def _text_rows(gba) -> int:
    """Number of framebuffer rows that are not one uniform colour."""
    fb = gba.ppu.fb
    rows = 0
    for y in range(HEIGHT):
        line = fb[y * WIDTH * 2:(y + 1) * WIDTH * 2]
        if line != line[:2] * WIDTH:
            rows += 1
    return rows


def test_mode4_renders_result_text():
    gba = run_test_rom("arm.gba")
    gba.run_frames(2)
    assert 4 <= _text_rows(gba) <= 16           # one line of 8px glyphs


def test_ppu_shades_gradient():
    gba = GBA((ROMS / "shades.gba").read_bytes())
    gba.run_frames(5)
    row = bytes(gba.ppu.fb[80 * WIDTH * 2:81 * WIDTH * 2])
    colours = {row[i:i + 2] for i in range(0, len(row), 2)}
    assert len(colours) >= 12


# ------------------------------------------------------------------ HLE BIOS
def _bios_gba():
    return GBA((ROMS / "none.gba").read_bytes())


@pytest.mark.parametrize("num,den,q,r", [(100, 7, 14, 2), (-100, 7, -14, -2), (100, -7, -14, 2),
                                         (7, 100, 0, 7)])
def test_bios_div(num, den, q, r):
    g = _bios_gba()
    g.cpu.r[0], g.cpu.r[1] = num & 0xFFFFFFFF, den & 0xFFFFFFFF
    g.cpu.swi(0x06)
    assert g.cpu.r[0] == q & 0xFFFFFFFF
    assert g.cpu.r[1] == r & 0xFFFFFFFF
    assert g.cpu.r[3] == abs(q)


def test_bios_sqrt_and_checksum():
    g = _bios_gba()
    g.cpu.r[0] = 1_000_000
    g.cpu.swi(0x08)
    assert g.cpu.r[0] == 1000
    g.cpu.swi(0x0D)
    assert g.cpu.r[0] == 0xBAAE187F


def test_bios_lz77_and_cpuset():
    g = _bios_gba()
    bus = g.bus
    # LZ77: "ABCD" literal then a back-reference copying 8 bytes -> "ABCDABCDABCD"
    data = bytes([0x10, 12, 0, 0, 0b00001000, 0x41, 0x42, 0x43, 0x44, (8 - 3) << 4, 3])
    bus.ewram[0x100:0x100 + len(data)] = data
    g.cpu.r[0], g.cpu.r[1] = 0x02000100, 0x02001000
    g.cpu.swi(0x11)
    assert bytes(bus.ewram[0x1000:0x100C]) == b"ABCDABCDABCD"
    # CpuSet fill (32-bit, fixed source)
    bus.write32(0x02002000, 0xDEADBEEF)
    g.cpu.r[0], g.cpu.r[1], g.cpu.r[2] = 0x02002000, 0x03000000, (1 << 26) | (1 << 24) | 4
    g.cpu.swi(0x0B)
    assert [bus.read32(0x03000000 + 4 * i) for i in range(4)] == [0xDEADBEEF] * 4


def test_vblank_intr_wait_halts_until_irq():
    g = _bios_gba()
    cpu, bus = g.cpu, g.bus
    bus.write16(0x04000004, 0x0008)              # DISPSTAT: V-Blank IRQ
    bus.write16(0x04000200, 0x0001)              # IE: V-Blank
    cpu.swi(0x05)
    assert cpu.halted and cpu.intrwait


# ------------------------------------------------------------------ saves
def test_save_detection():
    assert detect_save(b"\x00" * 64 + b"EEPROM_V124") == SAVE_EEPROM
    assert detect_save(b"FLASH1M_V103") == SAVE_FLASH128
    assert detect_save(b"SRAM_V113") == SAVE_SRAM


def _eeprom_bits(bits):
    return [int(c) for c in bits]


def test_eeprom_write_then_read_large():
    e = EEPROM()
    addr = format(5, "014b")
    payload = "".join(format(b, "08b") for b in b"ZELDA!MC")
    req = _eeprom_bits("10" + addr + payload + "0")
    e.begin(len(req))
    for bit in req:
        e.write_bit(bit)
    assert e.size == 0x2000 and e.dirty
    req = _eeprom_bits("11" + addr + "0")
    e.begin(len(req))
    for bit in req:
        e.write_bit(bit)
    out = [e.read_bit() for _ in range(68)]
    assert out[:4] == [0, 0, 0, 0]
    data = bytes(int("".join(map(str, out[4 + 8 * k: 12 + 8 * k])), 2) for k in range(8))
    assert data == b"ZELDA!MC"


def test_flash_id_erase_and_bank_switch():
    f = Flash(0x20000)
    cmd = lambda v: (f.write8(0x5555, 0xAA), f.write8(0x2AAA, 0x55), f.write8(0x5555, v))
    cmd(0x90)
    assert (f.read8(0), f.read8(1)) == (0x62, 0x13)
    cmd(0xF0)
    cmd(0xA0)
    f.write8(0x1234, 0x5A)
    assert f.read8(0x1234) == 0x5A
    cmd(0xB0)
    f.write8(0x0000, 1)
    assert f.read8(0x1234) == 0xFF               # bank 1 still erased
    cmd(0x80)
    cmd(0x10)                                    # chip erase
    f.bank = 0
    assert f.read8(0x1234) == 0xFF


# ------------------------------------------------------------------ commercial ROM smoke test
@pytest.mark.slow
def test_minish_cap_boots(tmp_path):
    rom = next(COMMERCIAL.glob("*.gba"), None) if COMMERCIAL.exists() else None
    if rom is None:
        pytest.skip("no .gba ROM in rom/gba/")
    copy = tmp_path / rom.name                    # temp copy: never touch the real .sav
    copy.write_bytes(rom.read_bytes())
    gba = GBA(copy)
    gba.run_frames(400)
    assert gba.ppu.frame == 400
    assert _text_rows(gba) > 100                  # a full picture, not a blank screen
