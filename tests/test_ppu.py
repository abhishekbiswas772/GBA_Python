from tests.helpers import build_ppu

W = 160


def row(fb, y):
    return fb[y * W:(y + 1) * W]


# --- mode timing / LY ------------------------------------------------------

def test_mode_timing_per_scanline():
    mmu, ppu = build_ppu()
    assert (ppu.ly, ppu.mode) == (0, 2)
    ppu.step(79)
    assert ppu.mode == 2
    ppu.step(1)
    assert ppu.mode == 3
    ppu.step(171)
    assert ppu.mode == 3
    ppu.step(1)
    assert ppu.mode == 0
    ppu.step(203)
    assert ppu.mode == 0
    ppu.step(1)
    assert (ppu.ly, ppu.mode) == (1, 2)


def test_ly_progression_and_wrap():
    mmu, ppu = build_ppu()
    ppu.step(456 * 144)
    assert (ppu.ly, ppu.mode) == (144, 1)
    ppu.step(456 * 9)
    assert ppu.ly == 153
    ppu.step(456)
    assert (ppu.ly, ppu.mode) == (0, 2)
    assert ppu.frame_count == 1
    assert ppu.frame_ready


def test_ly_is_read_only():
    mmu, ppu = build_ppu()
    ppu.step(456 * 3)
    mmu.write(0xFF44, 0x55)
    assert mmu.read(0xFF44) == ppu.ly == 3


# --- interrupts ------------------------------------------------------------

def test_vblank_if_at_line_144():
    mmu, ppu = build_ppu()
    ppu.step(456 * 144 - 1)
    assert not mmu.io[0x0F] & 0x01
    ppu.step(1)
    assert mmu.io[0x0F] & 0x01


def test_lyc_coincidence_stat_irq_edge():
    mmu, ppu = build_ppu()
    mmu.write(0xFF45, 2)       # LYC=2
    mmu.write(0xFF41, 0x40)    # enable LYC==LY interrupt
    assert not mmu.io[0x0F] & 0x02
    ppu.step(456 * 2)
    assert ppu.ly == 2
    assert mmu.read(0xFF41) & 0x04          # coincidence flag
    assert mmu.io[0x0F] & 0x02              # STAT IRQ fired
    mmu.io[0x0F] = 0
    ppu.step(100)
    assert not mmu.io[0x0F] & 0x02          # no storm while condition holds
    ppu.step(356)                           # ly=3, coincidence cleared
    assert not mmu.read(0xFF41) & 0x04
    mmu.io[0x0F] = 0
    while ppu.ly != 2:                      # next frame, coincidence again
        ppu.step(456)
    assert mmu.io[0x0F] & 0x02


def test_stat_mode2_irq_rises_once_per_line():
    mmu, ppu = build_ppu()
    mmu.write(0xFF41, 0x20)    # mode 2 interrupt
    assert mmu.io[0x0F] & 0x02              # already in mode 2 -> rising edge
    mmu.io[0x0F] = 0
    ppu.step(40)
    assert not mmu.io[0x0F] & 0x02
    ppu.step(40)                            # mode 3
    mmu.io[0x0F] = 0
    ppu.step(456 - 80)                      # next line starts in mode 2
    assert ppu.ly == 1
    assert mmu.io[0x0F] & 0x02


def test_stat_mode1_irq_once_per_vblank():
    mmu, ppu = build_ppu()
    mmu.write(0xFF41, 0x10)    # mode 1 interrupt
    mmu.io[0x0F] = 0
    ppu.step(456 * 144)
    assert mmu.io[0x0F] & 0x02
    mmu.io[0x0F] = 0
    ppu.step(456)                           # still mode 1 -> no new edge
    assert not mmu.io[0x0F] & 0x02


def test_stat_register_bits():
    mmu, ppu = build_ppu()
    assert mmu.read(0xFF41) == 0x86         # 0x80 | coinc(ly=lyc=0) | mode 2
    mmu.write(0xFF41, 0x68)
    assert mmu.read(0xFF41) == 0xEE         # writable bits 3-6 only


# --- background ------------------------------------------------------------

def test_bg_tile_fetch_and_scroll_wrap():
    mmu, ppu = build_ppu()
    vram = mmu.vram
    mmu.write(0xFF47, 0xE4)    # identity BGP
    mmu.write(0xFF40, 0x91)    # BG on, unsigned tiles, map 0x9800
    vram[0x10] = 0xFF          # tile 1 = all color 1
    vram[0x1800:0x1820] = bytes(31) + bytes([1])   # row 0: col 31 = tile 1
    mmu.write(0xFF43, 252)     # SCX=252 -> wraps across the tile row
    ppu.step(456)
    fb = ppu.framebuffer
    assert fb[0:4] == bytes([1, 1, 1, 1])
    assert fb[4:8] == bytes(4)


def test_bg_scroll_y_wrap():
    mmu, ppu = build_ppu()
    vram = mmu.vram
    mmu.write(0xFF47, 0xE4)
    mmu.write(0xFF40, 0x91)
    vram[0x10:0x20] = bytes([0xFF, 0x00] * 8)  # tile 1 = all color 1
    vram[0x1800 + 31 * 32] = 1     # map row 31, col 0 = tile 1
    mmu.write(0xFF42, 255)         # SCY=255 -> line 0 uses map row 31
    ppu.step(456)
    fb = ppu.framebuffer
    assert fb[0:8] == bytes([1] * 8)
    assert fb[8:16] == bytes(8)


def test_bg_signed_tile_addressing():
    mmu, ppu = build_ppu()
    vram = mmu.vram
    mmu.write(0xFF47, 0xE4)
    mmu.write(0xFF40, 0x81)    # BG on, signed tiles ($8800 region), map 0x9800
    vram[0x1800] = 0xA1        # tile index $a1 = -95
    off = 0x800 + (((0xA1 + 128) & 0xFF) << 4)   # = 0x8A10
    assert off == 0xA10
    vram[off] = 0xFF           # all color 1
    ppu.step(456)
    fb = ppu.framebuffer
    assert fb[0:8] == bytes([1] * 8)


def test_bg_disable_shows_bgp_color0():
    mmu, ppu = build_ppu()
    vram = mmu.vram
    mmu.write(0xFF47, 0xE4)    # BGP color 0 -> 0
    mmu.write(0xFF40, 0x91)
    vram[0x10] = 0xFF
    vram[0x1800:0x1820] = bytes([1] * 32)
    ppu.step(456)
    assert ppu.framebuffer[0:W] == bytes([1] * W)
    mmu.write(0xFF40, 0x90)    # BG off
    ppu.step(456)
    assert ppu.framebuffer[W:2 * W] == bytes(W)


# --- window ----------------------------------------------------------------

def test_window_placement_wx7():
    mmu, ppu = build_ppu()
    vram = mmu.vram
    mmu.write(0xFF47, 0xE4)
    vram[0x10:0x20] = bytes([0xFF, 0x00] * 8)   # tile 1 = color 1
    vram[0x1C00:0x1C20] = bytes([1] * 32)   # window map row 0
    mmu.write(0xFF40, 0xF1)    # window on, map 0x9C00, unsigned tiles
    mmu.write(0xFF4A, 0)       # WY=0
    mmu.write(0xFF4B, 7)       # WX=7 -> window starts at x=0
    ppu.step(456)
    fb = ppu.framebuffer
    assert fb[0:W] == bytes([1] * W)

    mmu.write(0xFF4B, 87)      # WX=87 -> window starts at x=80
    ppu.step(456)
    fb = ppu.framebuffer
    assert row(fb, 1)[0:80] == bytes(80)
    assert row(fb, 1)[80:160] == bytes([1] * 80)


def test_window_line_counter_advances_per_line():
    mmu, ppu = build_ppu()
    vram = mmu.vram
    mmu.write(0xFF47, 0xE4)
    vram[0x10:0x20] = bytes([0xFF, 0x00] * 8)   # tile 1 = color 1
    vram[0x20:0x30] = bytes([0x00, 0xFF] * 8)   # tile 2 = color 2
    vram[0x1C00:0x1C20] = bytes([1] * 32)       # window map tile row 0
    vram[0x1C20:0x1C40] = bytes([2] * 32)       # window map tile row 1
    mmu.write(0xFF40, 0xF1)
    mmu.write(0xFF4A, 2)       # WY=2
    mmu.write(0xFF4B, 7)
    for _ in range(2):
        ppu.step(456)          # lines 0-1: window not started
    fb = ppu.framebuffer
    assert row(fb, 0) == bytes(W)
    assert row(fb, 1) == bytes(W)
    for y in range(2, 10):     # window lines 0-7 -> tile row 0
        ppu.step(456)
        assert row(fb, y) == bytes([1] * W)
    for y in range(10, 18):    # window lines 8-15 -> tile row 1
        ppu.step(456)
        assert row(fb, y) == bytes([2] * W)


def test_window_line_counter_persists_when_hidden():
    mmu, ppu = build_ppu()
    vram = mmu.vram
    mmu.write(0xFF47, 0xE4)
    vram[0x10:0x20] = bytes([0xFF, 0x00] * 8)   # tile 1 = color 1
    vram[0x20:0x30] = bytes([0x00, 0xFF] * 8)   # tile 2 = color 2
    vram[0x30:0x40] = bytes([0xFF, 0xFF] * 8)   # tile 3 = color 3
    vram[0x1C00:0x1C20] = bytes([1] * 32)
    vram[0x1C20:0x1C40] = bytes([2] * 32)
    vram[0x1C40:0x1C60] = bytes([3] * 32)
    mmu.write(0xFF40, 0xF1)
    mmu.write(0xFF4A, 2)       # WY=2
    mmu.write(0xFF4B, 7)
    for _ in range(2):
        ppu.step(456)          # lines 0-1
    for _ in range(4):
        ppu.step(456)          # lines 2-5: window lines 0-3
    assert ppu.win_y == 4
    mmu.write(0xFF4B, 167)     # window hidden
    for _ in range(15):
        ppu.step(456)          # lines 6-20: blank
    mmu.write(0xFF4B, 7)       # window shown again
    ppu.step(456)              # line 21: window line 4 (counter persisted)
    assert ppu.win_y == 5
    assert row(ppu.framebuffer, 21) == bytes([1] * W)


def test_window_hidden_by_wx_offscreen():
    mmu, ppu = build_ppu()
    vram = mmu.vram
    mmu.write(0xFF47, 0xE4)
    vram[0x10:0x20] = bytes([0xFF, 0x00] * 8)   # tile 1 = color 1
    vram[0x1C00:0x1C20] = bytes([1] * 32)
    mmu.write(0xFF40, 0xF1)
    mmu.write(0xFF4A, 0)
    mmu.write(0xFF4B, 167)     # offscreen
    ppu.step(456)
    assert ppu.framebuffer[0:W] == bytes(W)
    mmu.write(0xFF4B, 166)     # one pixel at x=159
    ppu.step(456)
    assert row(ppu.framebuffer, 1)[0:159] == bytes(159)
    assert row(ppu.framebuffer, 1)[159] == 1


# --- sprites ---------------------------------------------------------------

def _sprite_setup(lcdc=0x93):
    mmu, ppu = build_ppu()
    mmu.write(0xFF47, 0xE4)
    mmu.write(0xFF48, 0xE4)    # OBP0 identity
    mmu.write(0xFF49, 0xE4)
    mmu.write(0xFF40, lcdc)    # LCD + BG + OBJ on, unsigned BG tiles
    return mmu, ppu


def test_sprite_draw():
    mmu, ppu = _sprite_setup()
    mmu.vram[0x10] = 0xFF      # tile 1 = color 1
    mmu.oam[0:4] = bytes([16, 8, 1, 0])    # screen (0, 0)
    ppu.step(456)
    fb = ppu.framebuffer
    assert fb[0:8] == bytes([1] * 8)
    assert fb[8:16] == bytes(8)


def test_sprite_x_flip():
    mmu, ppu = _sprite_setup()
    mmu.vram[0x10] = 0x80      # tile 1 = color 1 at left pixel only
    mmu.oam[0:4] = bytes([16, 8, 1, 0x20])
    ppu.step(456)
    fb = ppu.framebuffer
    assert fb[0:7] == bytes(7)
    assert fb[7] == 1


def test_sprite_y_flip():
    mmu, ppu = _sprite_setup()
    mmu.vram[0x10] = 0xFF      # tile 1 row 0 = color 1
    mmu.oam[0:4] = bytes([16, 8, 1, 0x40])
    ppu.step(456)
    assert ppu.framebuffer[0:8] == bytes(8)
    for _ in range(7):
        ppu.step(456)
    assert row(ppu.framebuffer, 7)[0:8] == bytes([1] * 8)


def test_sprite_palette_selection():
    mmu, ppu = _sprite_setup()
    mmu.write(0xFF49, 0x08)    # OBP1: color 1 -> 2
    mmu.vram[0x10] = 0xFF
    mmu.oam[0:4] = bytes([16, 8, 1, 0x10])     # OBP1
    ppu.step(456)
    assert ppu.framebuffer[0:8] == bytes([2] * 8)


def test_sprite_priority_behind_bg():
    mmu, ppu = _sprite_setup()
    vram = mmu.vram
    vram[0x10] = 0xFF          # tile 1 = color 1 (bg)
    vram[0x20:0x22] = b"\x00\xFF"   # tile 2 = color 2 (sprite)
    vram[0x1800] = 1           # bg row 0: col 0 = color 1, col 1 = color 0
    mmu.oam[0:4] = bytes([16, 8, 2, 0x80])     # screen x 0-7, behind BG
    mmu.oam[4:8] = bytes([16, 16, 2, 0x80])    # screen x 8-15, behind BG
    ppu.step(456)
    fb = ppu.framebuffer
    assert fb[0:8] == bytes([1] * 8)       # hidden behind BG color 1
    assert fb[8:16] == bytes([2] * 8)      # visible over BG color 0


def test_sprite_x_priority_wins():
    mmu, ppu = _sprite_setup()
    vram = mmu.vram
    vram[0x10:0x12] = b"\xFF\x00"    # tile 1 = color 1
    vram[0x20:0x22] = b"\x00\xFF"    # tile 2 = color 2
    mmu.oam[0:4] = bytes([16, 12, 1, 0])   # idx 0: screen x 4-11
    mmu.oam[4:8] = bytes([16, 8, 2, 0])    # idx 1: screen x 0-7
    ppu.step(456)
    fb = ppu.framebuffer
    assert fb[0:8] == bytes([2] * 8)       # lower X wins the overlap
    assert fb[8:12] == bytes([1] * 4)


def test_sprite_same_x_oam_order_wins():
    mmu, ppu = _sprite_setup()
    vram = mmu.vram
    vram[0x10:0x12] = b"\xFF\x00"
    vram[0x20:0x22] = b"\x00\xFF"
    mmu.oam[0:4] = bytes([16, 8, 1, 0])
    mmu.oam[4:8] = bytes([16, 8, 2, 0])
    ppu.step(456)
    assert ppu.framebuffer[0:8] == bytes([1] * 8)


def test_sprite_10_per_line_limit():
    mmu, ppu = _sprite_setup()
    vram = mmu.vram
    vram[0x10:0x12] = b"\xFF\x00"
    vram[0x20:0x22] = b"\x00\xFF"
    for i in range(10):
        mmu.oam[i * 4:i * 4 + 4] = bytes([16, 8 + i * 8, 1, 0])
    mmu.oam[40:44] = bytes([16, 8, 2, 0])  # 11th sprite must be dropped
    ppu.step(456)
    fb = ppu.framebuffer
    assert fb[0:8] == bytes([1] * 8)
    assert 2 not in fb[0:W]


def test_sprite_8x16_ignores_tile_bit0():
    mmu, ppu = _sprite_setup(lcdc=0x97)     # 8x16 sprites
    vram = mmu.vram
    vram[0x20:0x30] = bytes([0xFF, 0x00] * 8)   # tile 2 = color 1
    vram[0x30:0x40] = bytes([0x00, 0xFF] * 8)   # tile 3 = color 2
    mmu.oam[0:4] = bytes([16, 8, 3, 0])     # tile 3: bit 0 ignored -> tiles 2+3
    ppu.step(456)
    assert ppu.framebuffer[0:8] == bytes([1] * 8)      # top half = tile 2
    for _ in range(7):
        ppu.step(456)
    assert row(ppu.framebuffer, 7)[0:8] == bytes([1] * 8)
    ppu.step(456)
    assert row(ppu.framebuffer, 8)[0:8] == bytes([2] * 8)   # bottom half = tile 3


def test_sprite_8x16_y_flip():
    mmu, ppu = _sprite_setup(lcdc=0x97)
    vram = mmu.vram
    vram[0x20:0x30] = bytes([0xFF, 0x00] * 8)
    vram[0x30:0x40] = bytes([0x00, 0xFF] * 8)
    mmu.oam[0:4] = bytes([16, 8, 2, 0x40])     # Y flip
    ppu.step(456)
    assert ppu.framebuffer[0:8] == bytes([2] * 8)      # bottom tile shows first
    for _ in range(7):
        ppu.step(456)
    ppu.step(456)
    assert row(ppu.framebuffer, 8)[0:8] == bytes([1] * 8)


# --- locking / DMA / LCD off ----------------------------------------------

def test_vram_oam_locking():
    mmu, ppu = build_ppu()
    mmu.write(0xFF40, 0x91)    # LCD on
    mmu.write(0x8000, 0x42)    # mode 2: VRAM writable
    assert mmu.read(0x8000) == 0x42
    ppu.step(80)               # mode 3: VRAM locked
    assert mmu.read(0x8000) == 0xFF
    mmu.write(0x8000, 0x99)
    assert mmu.vram[0] == 0x42
    ppu.step(172)              # mode 0
    assert mmu.read(0x8000) == 0x42
    mmu.write(0xFE00, 0x42)    # mode 0: OAM writable
    assert mmu.read(0xFE00) == 0x42
    ppu.step(204)              # next line, mode 2: OAM locked
    assert (ppu.ly, ppu.mode) == (1, 2)
    assert mmu.read(0xFE00) == 0xFF
    mmu.write(0xFE00, 0x99)
    assert mmu.oam[0] == 0x42
    ppu.step(80)               # mode 3: OAM locked
    assert mmu.read(0xFE00) == 0xFF
    ppu.step(172)              # mode 0: OAM readable again
    assert mmu.read(0xFE00) == 0x42
    ppu.step(204 + 456 * 142)  # vblank
    assert ppu.mode == 1
    assert mmu.read(0x8000) == 0x42
    assert mmu.read(0xFE00) == 0x42


def test_lcd_off_unlocks_memory():
    mmu, ppu = build_ppu()
    mmu.write(0xFF40, 0x11)    # LCD off
    mmu.write(0x8000, 0x77)
    mmu.write(0xFE00, 0x88)
    assert mmu.read(0x8000) == 0x77
    assert mmu.read(0xFE00) == 0x88


def test_oam_dma():
    mmu, ppu = build_ppu()
    mmu.wram[0:160] = bytes(range(160))
    mmu.write(0xFF46, 0xC0)    # copy 0xC000-0xC09F -> OAM
    assert mmu.read(0xFF46) == 0xC0
    assert bytes(mmu.oam) == bytes(range(160))


def test_lcd_off_blank_keeps_counting():
    mmu, ppu = build_ppu()
    mmu.write(0xFF47, 0xE4)
    mmu.write(0xFF40, 0x91)
    mmu.vram[0x10] = 0xFF
    mmu.vram[0x1800] = 1
    ppu.step(456)
    assert ppu.framebuffer[0] == 1
    mmu.write(0xFF40, 0x11)    # LCD off -> blank screen
    fc = ppu.frame_count
    ppu.step(456 * 154)
    assert ppu.frame_count == fc + 1        # still counts frames
    assert ppu.framebuffer == bytes(W * 144)


def test_lcd_off_freezes_ly_and_suppresses_irqs():
    mmu, ppu = build_ppu()
    mmu.write(0xFF40, 0x91)
    ppu.step(456 * 50)
    mmu.write(0xFF41, 0x78)                  # all STAT sources enabled
    mmu.write(0xFF40, 0x11)                  # LCD off
    mmu.io[0x0F] = 0
    ppu.step(456 * 154 * 2)
    assert ppu.ly == 0
    assert mmu.read(0xFF41) & 0x03 == 0      # mode 0
    assert mmu.io[0x0F] & 0x03 == 0          # no VBlank / STAT
    mmu.write(0xFF40, 0x91)                  # back on: restarts at LY 0
    assert ppu.ly == 0 and ppu.line_cycles == 0
