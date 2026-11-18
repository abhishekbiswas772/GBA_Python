# ---------------------------------------------------------------- system
CYCLES_PER_SECOND = 4_194_304


# ---------------------------------------------------------------- APU
SAMPLE_RATE = 44_100
FS_PERIOD = 8_192
DUTY = (0b00000001, 0b10000001, 0b10000111, 0b01111110)
SCALE = 32_767 // (2 * 15 * 4 * 8)      # full -60..+60 swing * master vol 8 fits after DC block
HP = 0.001                              # DC blocker coefficient (~7 Hz)

# register file 0xFF10-0xFF26: write masks (stored bits) / read-as-one masks
# NRx4: only bit 6 (length enable) reads back; trigger + freq-high read as 1
APU_WRITE_MASK = bytes((0x7F, 0xFF, 0xFF, 0xFF, 0x40, 0x00, 0xFF, 0xFF, 0xFF, 0x40,
                        0x80, 0xFF, 0x60, 0xFF, 0x40, 0x00, 0xFF, 0xFF, 0xFF, 0x40,
                        0xFF, 0xFF, 0x80))
APU_READ_OR = bytes((0x80, 0x3F, 0x00, 0xFF, 0xBF, 0xFF, 0x3F, 0x00, 0xFF, 0xBF,
                     0x7F, 0xFF, 0x9F, 0xFF, 0xBF, 0xFF, 0xFF, 0x00, 0x00, 0xBF,
                     0x00, 0x00, 0x70))


# ---------------------------------------------------------------- cartridge
NINTENDO_LOGO = bytes.fromhex(
    "CEED6666CC0D000B03730083000C000D"
    "0008111F8889000EDCCC6EE6DDDDD999"
    "BBBB67636E0EECCCDDDC999FBBB9333E"
)


CARTRIDGE_TYPES = {
    0x00: "ROM ONLY",
    0x01: "MBC1",
    0x02: "MBC1+RAM",
    0x03: "MBC1+RAM+BATTERY",
    0x05: "MBC2",
    0x06: "MBC2+BATTERY",
    0x08: "ROM+RAM",
    0x09: "ROM+RAM+BATTERY",
    0x0B: "MMM01",
    0x0C: "MMM01+RAM",
    0x0D: "MMM01+RAM+BATTERY",
    0x0F: "MBC3+TIMER+BATTERY",
    0x10: "MBC3+TIMER+RAM+BATTERY",
    0x11: "MBC3",
    0x12: "MBC3+RAM",
    0x13: "MBC3+RAM+BATTERY",
    0x19: "MBC5",
    0x1A: "MBC5+RAM",
    0x1B: "MBC5+RAM+BATTERY",
    0x1C: "MBC5+RUMBLE",
    0x1D: "MBC5+RUMBLE+RAM",
    0x1E: "MBC5+RUMBLE+RAM+BATTERY",
    0x20: "MBC6",
    0x22: "MBC7+SENSOR+RUMBLE+RAM+BATTERY",
    0xFC: "POCKET CAMERA",
    0xFD: "BANDAI TAMA5",
    0xFE: "HuC3",
    0xFF: "HuC1+RAM+BATTERY",
}


RAM_SIZES = {
    0x00: 0,
    0x01: 0,
    0x02: 8,
    0x03: 32,
    0x04: 128,
    0x05: 64
}

BATTERY_TYPES = {0x03, 0x06, 0x09, 0x0D, 0x0F, 0x10, 0x13, 0x1B, 0x1E, 0xFF}
RTC_TYPES = {0x0F, 0x10}


# ---------------------------------------------------------------- CPU / disassembler
FLAG_Z = 0x80
FLAG_N = 0x40
FLAG_H = 0x20
FLAG_C = 0x10

R8 = ["b", "c", "d", "e", "h", "l", "(hl)", "a"]
R16 = ["bc", "de", "hl", "sp"]
R16_STACK = ["bc", "de", "hl", "af"]
CONDITIONS = ["nz", "z", "nc", "c"]
ALU = ["add a,", "adc a,", "sub ", "sbc a,", "and ", "xor ", "or ", "cp "]
ROT = ["rlc", "rrc", "rl", "rr", "sla", "sra", "swap", "srl"]
ACC_OPS = ["rlca", "rrca", "rla", "rra", "daa", "cpl", "scf", "ccf"]
INDIRECT = ["(bc)", "(de)", "(hl+)", "(hl-)"]


# ---------------------------------------------------------------- MMU
# DMG IO read-back: bits OR'd in on read. 0xFF = unmapped (always reads 0xFF).
# Registers owned by joypad/timer/APU/PPU apply their own masks.
IO_READ_OR = bytearray([0xFF] * 0x80)
IO_READ_OR[0x01] = 0x00      # SB
IO_READ_OR[0x02] = 0x7E      # SC: only bits 7 and 0 exist
IO_READ_OR[0x0F] = 0xE0      # IF: upper 3 bits read as 1


# ---------------------------------------------------------------- timer
TIMER_FREQ_BITS = (9, 3, 5, 7)          # DIV bit watched by TIMA for each TAC clock select


# ---------------------------------------------------------------- joypad
RIGHT, LEFT, UP, DOWN = 0, 1, 2, 3
A, B, SELECT, START = 4, 5, 6, 7

BUTTON_NAMES = {"right": RIGHT, "left": LEFT, "up": UP, "down": DOWN,
                "a": A, "b": B, "select": SELECT, "start": START}


# ---------------------------------------------------------------- PPU
WIDTH = 160
HEIGHT = 144
LINE_DOTS = 456
FRAME_DOTS = LINE_DOTS * 154

PIXEL_SHIFTS = (7, 6, 5, 4, 3, 2, 1, 0)
REV = bytes(int(f"{b:08b}"[::-1], 2) for b in range(256))   # bit-reversed byte (sprite X-flip)
BLANK = bytes(WIDTH)


# ---------------------------------------------------------------- display
DMG_PALETTE = ((0xE0, 0xF8, 0xD0), (0x88, 0xC0, 0x70), (0x34, 0x68, 0x56), (0x08, 0x18, 0x20))
GRAY_PALETTE = ((0xFF, 0xFF, 0xFF), (0xAA, 0xAA, 0xAA), (0x55, 0x55, 0x55), (0x00, 0x00, 0x00))


def signed8(value: int) -> int:
    return (value ^ 0x80) - 0x80
