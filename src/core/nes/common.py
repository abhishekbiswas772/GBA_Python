"""NES (NTSC, Ricoh 2A03 CPU + 2C02 PPU) constants shared by the core modules."""

CPU_HZ = 1_789_773                 # NTSC CPU clock (master 21.477272 MHz / 12)
DOTS_PER_LINE = 341
LINES_PER_FRAME = 262              # 0-239 visible, 240 post-render, 241-260 vblank, 261 pre-render
VISIBLE_LINES = 240
VBLANK_LINE = 241
PRERENDER_LINE = 261
# one frame = 262 * 341 dots = 89342 dots (one less on odd frames while rendering)
FRAME_DOTS = LINES_PER_FRAME * DOTS_PER_LINE
FPS = CPU_HZ * 3 / (FRAME_DOTS - 0.5)   # ~60.0988 Hz

WIDTH = 256
HEIGHT = 240
SAMPLE_RATE = 44_100

# Standard joypad: bit order of the $4016 / $4017 shift register.
BUTTON_ORDER = ("a", "b", "select", "start", "up", "down", "left", "right")
BUTTON_BITS = {name: i for i, name in enumerate(BUTTON_ORDER)}

# 2C02 colour palette (64 entries, RGB) - the widely used "2C02G" style palette.
_PALETTE_HEX = (
    "626262 001fb2 2404c8 5200b2 730076 800024 730b00 522800 244400 005700 005c00 005324 003c76 000000 000000 000000 "
    "ababab 0d57ff 4b30ff 8a13ff bc08d6 d21269 c72e00 9d5400 607b00 209800 00a300 009942 007db4 000000 000000 000000 "
    "ffffff 53aeff 9085ff d365ff ff57ff ff5dcf ff7757 fa9e00 bdc700 7ae700 43f611 26ef7e 2cd5f6 4e4e4e 000000 000000 "
    "ffffff b6e1ff ced1ff e9c3ff ffbcff ffbdf4 ffc6c3 ffd59a e9e681 cef481 b6fb9a a9fac3 a9f0f4 b8b8b8 000000 000000"
)
PALETTE = tuple(tuple(int(h[i:i + 2], 16) for i in (0, 2, 4)) for h in _PALETTE_HEX.split())
assert len(PALETTE) == 64

MIRROR_HORIZONTAL = "h"            # $2000=$2400, $2800=$2C00 (vertical scrolling games)
MIRROR_VERTICAL = "v"              # $2000=$2800, $2400=$2C00 (horizontal scrolling games)
MIRROR_SINGLE0 = "0"
MIRROR_SINGLE1 = "1"
MIRROR_FOUR = "4"
