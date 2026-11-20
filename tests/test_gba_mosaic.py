"""GBA mosaic tests (MOSAIC register 0x0400004C, GBATEK semantics).

Register layout: bits 0-3 BG X size-1, 4-7 BG Y size-1, 8-11 OBJ X size-1,
12-15 OBJ Y size-1. BG mosaic (BGnCNT bit 6) repeats each X-group's leftmost
pixel across the group and gives every line of a Y-group the data of the
group's topmost line. OBJ mosaic (attribute 0 bit 12) is horizontal only.

Scenes are driven headlessly: IO registers go through the bus, VRAM/palette
are filled with known patterns and the PPU renders scanlines directly.
"""
import os

import pytest

from src.core import gba as gba_core

if os.environ.get("GBA_CORE_REQUIRED") and not gba_core.AVAILABLE:
    raise RuntimeError(f"GBA core required but not built: {gba_core.IMPORT_ERROR}")
pytestmark = pytest.mark.skipif(not gba_core.AVAILABLE, reason="GBA core not built")

from src.core.gba import WIDTH, GbaCore as GBA

RED = 0x001F
GREEN = 0x03E0
BLUE = 0x7C00
YELLOW = 0x03FF
BLACK = 0x0000
COLOURS = (0x001F, 0x03E0, 0x7C00, 0x03FF, 0x7C1F, 0x7FE0, 0x0010, 0x4210)


def make_gba() -> GBA:
    return GBA(b"\x00" * 0x200)


def px_line(gba: GBA, y: int) -> list[int]:
    """The 240 BGR555 colours of scanline y in the framebuffer."""
    row = gba.ppu.fb[y * WIDTH * 2:(y + 1) * WIDTH * 2]
    return [row[i] | (row[i + 1] << 8) for i in range(0, len(row), 2)]


def text_scene(gba: GBA, rows) -> None:
    """Mode 0, BG0 (char base block 1, screen block 0): the map is all tile 0,
    so the given 8x8 tile repeats across and down the whole screen."""
    bus = gba.bus
    bus.write16(0x04000000, 0x0100)          # DISPCNT: mode 0, BG0 on
    bus.write16(0x04000008, 0x0044)          # BG0CNT: char base block 1, mosaic enabled
    for r, row in enumerate(rows):
        word = sum(p << (4 * i) for i, p in enumerate(row))
        bus.write32(0x06004000 + 4 * r, word)


def text_palette(gba: GBA) -> None:
    """Palette entries 1..8 = COLOURS."""
    for i, col in enumerate(COLOURS, start=1):
        gba.bus.write16(0x05000000 + 2 * i, col)


# ------------------------------------------------------------------ register
def test_mosaic_register_read_write():
    g = make_gba()
    bus = g.bus
    assert bus.read16(0x0400004C) == 0
    bus.write16(0x0400004C, 0x3210)          # all four size fields
    assert bus.read16(0x0400004C) == 0x3210
    bus.write16(0x0400004C, 0x0000)
    assert bus.read16(0x0400004C) == 0x0000
    bus.write8(0x0400004D, 0x5B)             # byte write -> OBJ size fields
    assert bus.read16(0x0400004C) == 0x5B00
    bus.write8(0x0400004C, 0x07)             # byte write -> BG size fields
    assert bus.read16(0x0400004C) == 0x5B07


# ------------------------------------------------------------------ BG mosaic
def test_bg_mosaic_x_groups_repeat_leftmost_pixel():
    g = make_gba()
    bus = g.bus
    text_scene(g, [list(range(1, 9))] * 8)    # columns 1..8 repeat every tile
    text_palette(g)
    c = COLOURS
    bus.write16(0x0400004C, 0)               # mosaic off
    g.ppu.render_line(3)
    assert px_line(g, 3) == list(c) * 30                         # unmodified
    bus.write16(0x0400004C, 0x0001)          # BG X size-1 = 1 -> groups of 2
    g.ppu.render_line(4)
    assert px_line(g, 4) == [c[0], c[0], c[2], c[2], c[4], c[4], c[6], c[6]] * 30
    bus.write16(0x0400004C, 0x0003)          # groups of 4
    g.ppu.render_line(5)
    assert px_line(g, 5) == [c[0]] * 4 + [c[4]] * 4 + [c[0]] * 4 + [c[4]] * 4 +         ([c[0]] * 4 + [c[4]] * 4) * 28


def test_bg_mosaic_y_repeats_topmost_line():
    g = make_gba()
    bus = g.bus
    text_scene(g, [[c] * 8 for c in range(1, 9)])     # tile row r = colour r+1
    text_palette(g)
    bus.write16(0x0400004C, 0)               # mosaic off
    for y in range(8):
        g.ppu.render_line(y)
    raw = [px_line(g, y) for y in range(8)]
    for y in range(8):
        assert raw[y] == [COLOURS[y]] * WIDTH         # one tile row per line
    bus.write16(0x0400004C, 0x0010)          # BG Y size-1 = 1 -> line pairs
    for y in range(8):
        g.ppu.render_line(y)
    got = [px_line(g, y) for y in range(8)]
    assert got == [raw[0], raw[0], raw[2], raw[2], raw[4], raw[4], raw[6], raw[6]]


def test_bg_mosaic_y_affine():
    g = make_gba()
    bus = g.bus
    bus.write16(0x04000000, 0x0402)          # DISPCNT: mode 2, BG2 on
    bus.write16(0x0400000C, 0x2044)          # BG2CNT: char block 1, mosaic, wraparound
    bus.write16(0x04000020, 0x0100)          # PA = 256 (identity)
    bus.write16(0x04000026, 0x0100)          # PD = 256 (one map row per line)
    bus.write16(0x04000028, 0)               # BG2X = 0 (latches ref x)
    bus.write16(0x0400002C, 0)               # BG2Y = 0 (latches ref y)
    for r in range(8):                       # affine tile 0 (8bpp): row r = colour r+1
        bus.vram[0x4000 + 8 * r:0x4000 + 8 * r + 8] = bytes([r + 1] * 8)
    text_palette(g)
    bus.write16(0x0400004C, 0)               # mosaic off
    for y in range(8):
        g.ppu.render_line(y)
    raw = [px_line(g, y) for y in range(8)]
    for y in range(8):
        assert raw[y] == [COLOURS[y]] * WIDTH
    bus.write16(0x0400004C, 0x0010)          # BG Y size-1 = 1 -> line pairs
    bus.write16(0x0400002C, 0)               # re-latch reference points to line 0
    for y in range(8):
        g.ppu.render_line(y)
    got = [px_line(g, y) for y in range(8)]
    assert got == [raw[0], raw[0], raw[2], raw[2], raw[4], raw[4], raw[6], raw[6]]


def test_bg_mosaic_x_bitmap_mode4():
    g = make_gba()
    bus = g.bus
    bus.write16(0x04000000, 0x0404)          # DISPCNT: mode 4, BG2 on
    bus.write16(0x0400000C, 0x0040)          # BG2CNT: mosaic on
    bus.write16(0x04000020, 0x0100)          # PA = 256
    bus.write16(0x04000026, 0x0100)          # PD = 256
    bus.write16(0x04000028, 0)               # BG2X = 0
    bus.write16(0x0400002C, 0)               # BG2Y = 0
    for r in range(8):                       # bitmap rows: columns 1, 2, 3, 4 repeating
        bus.vram[r * WIDTH:r * WIDTH + WIDTH] = bytes([1, 2, 3, 4] * 60)
    text_palette(g)
    c = COLOURS
    bus.write16(0x0400004C, 0)               # mosaic off
    g.ppu.render_line(0)
    assert px_line(g, 0) == [c[0], c[1], c[2], c[3]] * 60
    bus.write16(0x0400004C, 0x0001)          # BG X size-1 = 1 -> groups of 2
    bus.write16(0x0400002C, 0)               # re-latch reference points to line 0
    g.ppu.render_line(0)
    assert px_line(g, 0) == [c[0], c[0], c[2], c[2]] * 60


# ------------------------------------------------------------------ OBJ mosaic
def _obj_scene(gba: GBA, row_word: int, attr0: int) -> None:
    """Mode 0 with a single 8x8 sprite at (0, 0), tile 0 at OBJ VRAM base."""
    bus = gba.bus
    bus.write16(0x04000000, 0x1040)          # DISPCNT: mode 0, OBJ on, 1D mapping
    bus.write16(0x05000202, BLUE)            # OBJ palette 1, 2
    bus.write16(0x05000204, YELLOW)
    for r in range(8):
        bus.write32(0x06010000 + 4 * r, row_word)
    for n in range(1, 128):                  # zeroed OAM = 127 more visible sprites at 0,0
        bus.write16(0x07000000 + 8 * n, 0x0200)   # attr0 bit 9: disable
    bus.write16(0x07000000, attr0)           # attr0: y=0
    bus.write16(0x07000002, 0x0000)          # attr1: x=0
    bus.write16(0x07000004, 0x0000)          # attr2: tile 0, prio 0, bank 0


def test_obj_mosaic_x_sprite_line():
    g = make_gba()
    bus = g.bus
    word = sum(p << (4 * i) for i, p in enumerate((1, 2, 2, 1, 1, 2, 2, 1)))
    _obj_scene(g, word, 0x1000)              # attr0 mosaic on
    bus.write16(0x0400004C, 0x0100)          # OBJ X size-1 = 1 -> groups of 2
    g.ppu.render_line(0)
    got = px_line(g, 0)
    assert got[:8] == [BLUE, BLUE, YELLOW, YELLOW] * 2
    assert got[8:] == [BLACK] * (WIDTH - 8)  # backdrop outside the sprite
    # same sprite without the attribute-0 mosaic bit renders unmodified
    bus.write16(0x07000000, 0x0000)
    g.ppu.render_line(1)
    assert px_line(g, 1)[:8] == [BLUE, YELLOW, YELLOW, BLUE] * 2
    # mosaic bit set but register size 0 == mosaic disabled
    bus.write16(0x0400004C, 0x0000)
    bus.write16(0x07000000, 0x1000)
    g.ppu.render_line(2)
    assert px_line(g, 2)[:8] == [BLUE, YELLOW, YELLOW, BLUE] * 2


def test_obj_mosaic_transparent_leader_wipes_group():
    g = make_gba()
    bus = g.bus
    # pixels 0,1,2,0,1,2,0,1: colour 0 is transparent
    word = sum(p << (4 * i) for i, p in enumerate((0, 1, 2, 0, 1, 2, 0, 1)))
    _obj_scene(g, word, 0x1000)              # attr0 mosaic on
    bus.write16(0x0400004C, 0x0100)          # OBJ X size-1 = 1 -> groups of 2
    g.ppu.render_line(0)
    got = px_line(g, 0)
    # each dot takes the group's leftmost pixel (transparency included)
    assert got[:8] == [BLACK, BLACK, YELLOW, YELLOW, BLUE, BLUE, BLACK, BLACK]


# ------------------------------------------------------------------ regression
def test_mosaic_zero_matches_disabled():
    g = make_gba()
    bus = g.bus
    rows = [[((r + c) & 1) + 1 for c in range(8)] for r in range(8)]   # checkerboard
    text_scene(g, rows)
    text_palette(g)
    bus.write16(0x04000008, 0x0044)          # BG0CNT: mosaic bit set
    bus.write16(0x0400004C, 0x0000)          # but every size field 0
    for y in range(8):
        g.ppu.render_line(y)
    on = [bytes(g.ppu.fb[y * WIDTH * 2:(y + 1) * WIDTH * 2]) for y in range(8)]
    bus.write16(0x04000008, 0x0004)          # mosaic bit clear
    for y in range(8):
        g.ppu.render_line(y + 8)             # same tile rows repeat every 8 lines
    off = [bytes(g.ppu.fb[y * WIDTH * 2:(y + 1) * WIDTH * 2]) for y in range(8, 16)]
    assert on == off
