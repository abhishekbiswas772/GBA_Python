"""GBA core tests: jsmolka gba-tests ROMs (MIT, see tests/roms/gba/gba-tests-LICENSE.txt)
plus unit checks of the HLE BIOS, saves and PPU. The ROM tests report the number of
the first failed test in r12 once the CPU reaches its idle loop (0 = all passed)."""
import os
from pathlib import Path

import pytest

from src.core import gba as gba_core

if os.environ.get("GBA_CORE_REQUIRED") and not gba_core.AVAILABLE:
    raise RuntimeError(f"GBA core required but not built: {gba_core.IMPORT_ERROR}")
pytestmark = pytest.mark.skipif(not gba_core.AVAILABLE, reason="GBA core not built")

from src.core.gba import HEIGHT, WIDTH, GbaCore as GBA

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
# Save-type detection, EEPROM and Flash are unit-tested in Rust: `cargo test` in
# src/core/gba/native (src/cart.rs).
def test_save_kind_detected():
    assert GBA((ROMS / "flash128.gba").read_bytes()).cart.save_kind == "flash128"
    assert GBA((ROMS / "sram.gba").read_bytes()).cart.save_kind == "sram"
    assert GBA((ROMS / "none.gba").read_bytes()).cart.save_kind == "none"


def test_sav_file_written_and_reloaded(tmp_path):
    rom = tmp_path / "sram.gba"
    rom.write_bytes((ROMS / "sram.gba").read_bytes())
    g = GBA(rom)
    g.run_frames(30)                              # the test ROM writes SRAM
    assert g.save() and g.cart.sav_path.exists()
    data = g.cart.sav_path.read_bytes()
    assert len(data) == 0x8000 and data != b"\xff" * 0x8000
    assert GBA(rom).gba.save_data() == data       # loaded back into a new session


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
    assert gba.frame == 400
    assert _text_rows(gba) > 100                  # a full picture, not a blank screen
