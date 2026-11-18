"""Game Boy Color: mode select, VRAM/WRAM banks, KEY1 double speed, HDMA, palettes, rendering."""
from src.core.gameboy.cartridge import Cartridge
from src.core.gameboy.cpu import CPU
from src.core.gameboy.gameboy import GameBoy
from src.core.gameboy.mmu import MMU
from src.core.gameboy.ppu import PPU
from src.core.gameboy.timer import Timer
from tests.helpers import make_rom


def cgb_rom(program: bytes = b"", flag: int = 0xC0) -> bytes:
    rom = bytearray(make_rom(program))
    rom[0x0143] = flag
    return bytes(rom)


def cgb_system(program: bytes = b""):
    mmu = MMU(Cartridge(cgb_rom(program)), cgb=True)
    timer = Timer(mmu)
    mmu.timer = timer
    ppu = PPU(mmu, cgb=True)
    mmu.ppu = ppu
    cpu = CPU(mmu)
    cpu.pc = 0x150
    return cpu, mmu, ppu


def write_rom(tmp_path, data: bytes, name="game.gbc"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- mode selection ---------------------------------------------------------------

def test_header_selects_cgb_mode(tmp_path):
    gb = GameBoy(write_rom(tmp_path, cgb_rom(flag=0x80)))
    assert gb.cgb and gb.cart.header.supports_cgb
    assert gb.cpu.a == 0x11                         # games check A=0x11 to detect CGB
    assert gb.mmu.read(0xFF4D) == 0x7E


def test_force_dmg_mode(tmp_path):
    gb = GameBoy(write_rom(tmp_path, cgb_rom(flag=0x80)), cgb=False)
    assert not gb.cgb
    assert gb.cpu.a == 0x01
    for addr in (0xFF4D, 0xFF4F, 0xFF55, 0xFF68, 0xFF69, 0xFF70):
        assert gb.mmu.read(addr) == 0xFF            # CGB registers absent on DMG


def test_dmg_rom_stays_dmg(tmp_path):
    gb = GameBoy(write_rom(tmp_path, make_rom(), "old.gb"))
    assert not gb.cgb


def test_cgb_title_excludes_flag_byte():
    rom = bytearray(cgb_rom())
    rom[0x134:0x143] = b"ABCDEFGHIJKLMNO"
    assert Cartridge(bytes(rom)).header.title == "ABCDEFGHIJKLMNO"


# --- memory banks -------------------------------------------------------------------

def test_vram_bank_switch():
    _, mmu, _ = cgb_system()
    mmu.write(0xFF40, 0x00)                         # LCD off: VRAM always accessible
    mmu.write(0x8000, 0x11)
    mmu.write(0xFF4F, 0x01)
    assert mmu.read(0xFF4F) == 0xFF
    mmu.write(0x8000, 0x22)
    assert mmu.read(0x8000) == 0x22
    assert mmu.vram[0x0000] == 0x11 and mmu.vram[0x2000] == 0x22
    mmu.write(0xFF4F, 0x00)
    assert mmu.read(0x8000) == 0x11 and mmu.read(0xFF4F) == 0xFE


def test_wram_bank_switch_and_echo():
    _, mmu, _ = cgb_system()
    mmu.write(0xC000, 0xAA)                         # bank 0 fixed
    for bank in range(8):
        mmu.write(0xFF70, bank)
        mmu.write(0xD000, 0x40 + bank)
    for bank in range(1, 8):
        mmu.write(0xFF70, bank)
        assert mmu.read(0xD000) == 0x40 + bank
        assert mmu.read(0xF000) == 0x40 + bank      # echo follows the bank
        assert mmu.read(0xFF70) == 0xF8 | bank
    mmu.write(0xFF70, 0)                            # bank 0 selects bank 1
    assert mmu.read(0xD000) == 0x40 + 1
    assert mmu.read(0xC000) == 0xAA


def test_oam_dma_reads_banked_wram():
    _, mmu, _ = cgb_system()
    mmu.write(0xFF70, 3)
    for i in range(160):
        mmu.write(0xD000 + i, i ^ 0x5A)
    mmu.write(0xFF46, 0xD0)
    assert bytes(mmu.oam) == bytes(i ^ 0x5A for i in range(160))


# --- double speed ---------------------------------------------------------------------

def test_key1_stop_switches_speed():
    # LD A,1 ; LDH (0x4D),A ; STOP
    cpu, mmu, _ = cgb_system(bytes([0x3E, 0x01, 0xE0, 0x4D, 0x10, 0x00]))
    for _ in range(3):
        cpu.step()
    assert mmu.double_speed == 1
    assert mmu.read(0xFF4D) == 0xFE                 # bit 7 = double, prepare cleared


def test_stop_without_prepare_keeps_speed():
    cpu, mmu, _ = cgb_system(bytes([0x10, 0x00]))
    cpu.step()
    assert mmu.double_speed == 0


def test_double_speed_ppu_runs_at_half_rate(tmp_path):
    gb = GameBoy(write_rom(tmp_path, cgb_rom(bytes([0x3E, 0x01, 0xE0, 0x4D, 0x10, 0x00, 0x18, 0xFE]))))
    while gb.cpu.pc != 0x156:                       # boot stub at 0x100 jumps to 0x150
        gb.steps()
    assert gb.mmu.double_speed
    lc0, ly0, t0 = gb.ppu.line_cycles, gb.ppu.ly, gb.timer.div
    cpu_cycles = sum(gb.steps() for _ in range(100))          # JR loop
    ppu_dots = (gb.ppu.ly - ly0) * 456 + gb.ppu.line_cycles - lc0
    assert ppu_dots == cpu_cycles // 2
    assert (gb.timer.div - t0) & 0xFFFF == cpu_cycles         # DIV follows the CPU clock


# --- HDMA -------------------------------------------------------------------------------

def setup_hdma(mmu, src, dst, data):
    for i, b in enumerate(data):
        mmu.write(src + i, b)
    mmu.write(0xFF51, src >> 8)
    mmu.write(0xFF52, src & 0xFF)
    mmu.write(0xFF53, (dst >> 8) & 0xFF)
    mmu.write(0xFF54, dst & 0xFF)


def test_general_purpose_hdma():
    _, mmu, _ = cgb_system()
    data = bytes(range(1, 33))
    setup_hdma(mmu, 0xC000, 0x8100, data)
    mmu.write(0xFF4F, 1)                            # into bank 1
    mmu.write(0xFF55, 0x01)                         # 2 blocks
    assert mmu.vram[0x2100:0x2120] == data
    assert mmu.read(0xFF55) == 0xFF
    assert mmu.stall == 64


def test_hdma_registers_mask_addresses():
    _, mmu, _ = cgb_system()
    mmu.write(0xFF51, 0xC1); mmu.write(0xFF52, 0x2F)
    mmu.write(0xFF53, 0xFF); mmu.write(0xFF54, 0xFF)
    assert mmu.hdma_src == 0xC120                   # low 4 bits ignored
    assert mmu.hdma_dst == 0x1FF0                   # 0x8000 | (x & 0x1FF0)


def test_hblank_hdma_one_block_per_line_and_cancel():
    _, mmu, ppu = cgb_system()
    data = bytes(range(64))
    setup_hdma(mmu, 0xC000, 0x8000, data)
    mmu.write(0xFF55, 0x83)                         # HBlank mode, 4 blocks
    assert mmu.read(0xFF55) == 0x03
    ppu.step(456)                                   # line 0 HBlank -> block 1
    assert mmu.vram[0:16] == data[:16] and mmu.vram[16] == 0
    assert mmu.read(0xFF55) == 0x02
    ppu.step(456)
    assert mmu.vram[16:32] == data[16:32]
    mmu.write(0xFF55, 0x00)                         # bit 7 = 0 while active: cancel
    assert mmu.read(0xFF55) == 0x81                 # bit 7 set, 2 blocks were left
    ppu.step(456 * 3)
    assert mmu.vram[32] == 0


def test_hblank_hdma_completes_and_skips_vblank():
    _, mmu, ppu = cgb_system()
    setup_hdma(mmu, 0xC000, 0x8000, bytes([7]) * 32)
    ppu.step(456 * 143)                             # go to the last visible line
    mmu.write(0xFF55, 0x81)                         # 2 blocks
    ppu.step(456)                                   # line 143 -> one block
    assert mmu.read(0xFF55) == 0x00
    ppu.step(456 * 10)                              # VBlank lines: no transfers
    assert mmu.vram[16] == 0
    ppu.step(456)                                   # line 0 of next frame
    assert mmu.vram[16:32] == bytes([7]) * 16
    assert mmu.read(0xFF55) == 0xFF


# --- palettes -------------------------------------------------------------------------------

def test_palette_auto_increment_and_readback():
    _, mmu, ppu = cgb_system()
    mmu.write(0xFF68, 0x80)                         # index 0, auto-increment
    for b in range(8):
        mmu.write(0xFF69, 0x10 + b)
    assert ppu.bg_pal[0:8] == bytes(range(0x10, 0x18))
    assert mmu.read(0xFF68) == 0xC8                 # bit 6 reads 1
    mmu.write(0xFF68, 0x03)
    assert mmu.read(0xFF69) == 0x13
    mmu.write(0xFF69, 0x99)                         # no auto-increment
    assert mmu.read(0xFF68) == 0x43
    mmu.write(0xFF6A, 0xBF)                         # OBJ index 63, wraps to 0
    mmu.write(0xFF6B, 0x01)
    mmu.write(0xFF6B, 0x02)
    assert ppu.obj_pal[63] == 0x01 and ppu.obj_pal[0] == 0x02
    assert mmu.read(0xFF6A) == 0xC1                 # index wrapped 63 -> 0 -> 1


def set_color(ppu, obj: bool, pal: int, idx: int, rgb555: int):
    ram = ppu.obj_pal if obj else ppu.bg_pal
    j = (pal * 4 + idx) * 2
    ram[j], ram[j + 1] = rgb555 & 0xFF, rgb555 >> 8
    ppu._rgb_t = None


# --- rendering ---------------------------------------------------------------------------------

def test_bg_tile_attributes_palette_bank_flip():
    _, mmu, ppu = cgb_system()
    v = mmu.vram
    v[0x1800] = 1                                   # map (0,0) -> tile 1
    v[0x3800] = 0x02 | 0x08 | 0x20                  # palette 2, VRAM bank 1, X flip
    v[0x2010] = 0x80                                # bank-1 tile 1 row 0: pixel 0 = colour 1
    v[0x0010] = 0xFF                                # bank-0 tile 1 (must not be used)
    set_color(ppu, False, 2, 1, 0x001F)             # pure red
    ppu.step(456)
    fb = ppu.framebuffer
    assert fb[7] == 2 * 4 + 1                       # flipped: pixel lands at x=7
    assert fb[0] == 2 * 4 + 0
    assert ppu.rgb[7 * 3:7 * 3 + 3] == bytes([0xFF, 0, 0])


def test_bg_tile_y_flip():
    _, mmu, ppu = cgb_system()
    v = mmu.vram
    v[0x1800] = 1
    v[0x3800] = 0x40                                # Y flip
    v[0x0010 + 7 * 2] = 0xFF                        # row 7 = colour 1 -> shows on line 0
    ppu.step(456)
    assert ppu.framebuffer[0] == 1


def place_sprite(mmu, i, x, y, tile, attr):
    mmu.oam[i * 4:i * 4 + 4] = bytes([y + 16, x + 8, tile, attr])


def test_sprites_prioritised_by_oam_index_not_x():
    _, mmu, ppu = cgb_system()
    mmu.vram[0x20:0x30] = b"\xFF\x00" * 8           # tile 2: solid colour 1
    place_sprite(mmu, 0, 4, 0, 2, 0x01)             # OAM 0, palette 1, further right
    place_sprite(mmu, 1, 0, 0, 2, 0x02)             # OAM 1, palette 2, further left
    ppu.write(0xFF40, 0x93)
    ppu.step(456)
    assert ppu.framebuffer[5] == 32 + 1 * 4 + 1     # overlap: OAM 0 wins on CGB
    assert ppu.framebuffer[1] == 32 + 2 * 4 + 1


def test_bg_priority_attribute_and_master_priority():
    _, mmu, ppu = cgb_system()
    v = mmu.vram
    v[0x1800] = v[0x1801] = 1
    v[0x3800] = 0x80                                # tile (0,0): BG-over-OBJ attribute
    v[0x10:0x20] = b"\xFF\x00" * 8                  # tile 1 = colour 1 everywhere
    v[0x20:0x30] = b"\xFF\xFF" * 8                  # tile 2 = colour 3
    place_sprite(mmu, 0, 4, 0, 2, 0x00)             # spans x 4-11, both tiles
    ppu.write(0xFF40, 0x93)                         # LCDC.0 = 1: priorities apply
    ppu.step(456)
    assert ppu.framebuffer[4] == 1                  # BG wins (attribute)
    assert ppu.framebuffer[8] == 32 + 3             # tile (1,0) has no priority bit
    ppu.write(0xFF40, 0x92)                         # LCDC.0 = 0: OBJ always on top
    ppu.step(456)
    assert ppu.framebuffer[160 + 4] == 32 + 3


def test_lcdc0_clear_still_draws_bg_in_cgb():
    _, mmu, ppu = cgb_system()
    mmu.vram[0x1800] = 1
    mmu.vram[0x10:0x20] = b"\xFF\x00" * 8
    ppu.write(0xFF40, 0x90)                         # BG "off" bit clear
    ppu.step(456)
    assert ppu.framebuffer[0] == 1                  # still rendered on CGB


def test_lcd_off_blanks_rgb_to_white():
    _, mmu, ppu = cgb_system()
    set_color(ppu, False, 0, 0, 0x0000)             # palette 0 colour 0 = black
    ppu.step(456)
    assert ppu.rgb[0:3] == b"\x00\x00\x00"
    ppu.write(0xFF40, 0x11)
    assert ppu.rgb[0:3] == b"\xff\xff\xff"
