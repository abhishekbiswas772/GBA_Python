"""SWI 01h RegisterRamReset (bit 7, "reset other registers") must leave the
BG2/BG3 affine matrices at identity (PA = PD = 0x100, PB = PC = 0), as the real
BIOS does. Games such as Pokemon FireRed call it once at boot and then draw an
affine BG without ever writing the matrix; with a zero matrix every pixel of a
line samples the same texel (usually transparent), so the layer vanishes.

The scene is synthetic: mode 1, affine BG2 with a 256x256 8-bit map, one
256-colour tile placed at map cell (11, 2) like a small picture.
"""
from src.core.gba.common import WIDTH
from src.core.gba.gba import GBA

IO = 0x04000000
RED = 0x001F


def make_gba() -> GBA:
    return GBA(b"\x00" * 0x200)


def px_line(gba: GBA, y: int) -> list[int]:
    row = gba.ppu.fb[y * WIDTH * 2:(y + 1) * WIDTH * 2]
    return [row[i] | (row[i + 1] << 8) for i in range(0, len(row), 2)]


def test_register_ram_reset_leaves_identity_affine_matrix():
    g = make_gba()
    bus = g.bus
    for off in (0x20, 0x22, 0x24, 0x26, 0x30, 0x32, 0x34, 0x36):
        bus.write16(IO + off, 0x1234)            # garbage before the reset
    g.cpu.r[0] = 0xFF
    g.cpu.swi(0x01)
    got = [bus.read16(IO + off) for off in (0x20, 0x22, 0x24, 0x26, 0x30, 0x32, 0x34, 0x36)]
    assert got == [0x100, 0, 0, 0x100, 0x100, 0, 0, 0x100]
    assert bus.read16(IO + 0x00) == 0x0080       # forced blank
    assert bus.read16(IO + 0x08) == 0            # other video regs still cleared


def test_affine_bg_visible_after_register_ram_reset():
    g = make_gba()
    bus = g.bus
    g.cpu.r[0] = 0xFF                            # what a game's crt0 does at boot
    g.cpu.swi(0x01)
    # Mode 1, BG2 on; BG2CNT: priority 1, char base 0, 256-colour bit, map block 28, 256x256
    bus.write16(IO + 0x00, 0x0401)
    bus.write16(IO + 0x0C, 0x5C81)
    bus.write32(IO + 0x28, 0)                    # BG2X = BG2Y = 0 (game writes these)
    bus.write32(IO + 0x2C, 0)
    bus.write16(0x05000002, RED)                 # BG palette entry 1
    for i in range(64):                          # tile 0x18: solid colour 1
        bus.vram[0x18 * 64 + i] = 1
    bus.vram[0xE000 + 2 * 32 + 11] = 0x18        # map cell (11, 2) of a 32-tile-wide map
    g.ppu._latch_ref(0)                          # as at the start of a frame
    for y in range(24):
        g.ppu.render_line(y)
    line = px_line(g, 18)                        # tile row 2
    assert line[88:96] == [RED] * 8              # x = 11 * 8 .. 11 * 8 + 7
    assert line[80:88] == [0] * 8 and line[96:104] == [0] * 8   # backdrop either side
    assert set(px_line(g, 4)) == {0}             # tile row 0 is empty
