# ---------------------------------------------------------------- system
CPU_HZ = 16_777_216
M32 = 0xFFFFFFFF

# ---------------------------------------------------------------- CPU
MODE_USR = 0x10
MODE_FIQ = 0x11
MODE_IRQ = 0x12
MODE_SVC = 0x13
MODE_ABT = 0x17
MODE_UND = 0x1B
MODE_SYS = 0x1F

# register bank per mode: 0 usr/sys, 1 fiq, 2 irq, 3 svc, 4 abt, 5 und
MODE_BANK = {MODE_USR: 0, MODE_SYS: 0, MODE_FIQ: 1, MODE_IRQ: 2,
             MODE_SVC: 3, MODE_ABT: 4, MODE_UND: 5}

# packed flags nibble (cpu.f): N=8 Z=4 C=2 V=1
FLAG_N = 8
FLAG_Z = 4
FLAG_C = 2
FLAG_V = 1

VEC_RESET = 0x00
VEC_UNDEF = 0x04
VEC_SWI = 0x08
VEC_IRQ = 0x18

# ---------------------------------------------------------------- BIOS (HLE)
BIOS_SIZE = 0x4000
# open-bus BIOS reads return the last opcode the BIOS fetched (pc + 8)
BIOS_LATCH_STARTUP = 0xE129F000      # bios[0x0DC + 8]
BIOS_LATCH_SWI = 0xE3A02004          # bios[0x188 + 8]
BIOS_LATCH_IRQ = 0xE25EF004          # bios[0x134 + 8]
BIOS_LATCH_IRQ_RET = 0xE55EC002      # bios[0x13C + 8]
# words placed in the HLE BIOS image: real-BIOS IRQ dispatcher at 0x128 + latch values
BIOS_WORDS = {
    0x018: 0xEA000042,   # b 0x128
    0x0E4: BIOS_LATCH_STARTUP,
    0x128: 0xE92D500F,   # stmfd sp!, {r0-r3, r12, lr}
    0x12C: 0xE3A00301,   # mov   r0, #0x04000000
    0x130: 0xE28FE000,   # add   lr, pc, #0
    0x134: 0xE510F004,   # ldr   pc, [r0, #-4]      -> user handler at [0x03FFFFFC]
    0x138: 0xE8BD500F,   # ldmfd sp!, {r0-r3, r12, lr}
    0x13C: 0xE25EF004,   # subs  pc, lr, #4
    0x144: BIOS_LATCH_IRQ_RET,
    0x190: BIOS_LATCH_SWI,
}
BIOS_IF_ADDR = 0x03007FF8           # BIOS interrupt-check flags (IntrWait)
IRQ_HANDLER_ADDR = 0x03007FFC

# post-BIOS register state (BIOS skipped)
SP_USR = 0x03007F00
SP_IRQ = 0x03007FA0
SP_SVC = 0x03007FE0
ROM_START = 0x08000000

# ---------------------------------------------------------------- memory map
EWRAM_SIZE = 0x40000
IWRAM_SIZE = 0x8000
IO_SIZE = 0x400
PAL_SIZE = 0x400
VRAM_SIZE = 0x18000
OAM_SIZE = 0x400
ROM_MAX = 0x2000000                  # 32 MB address window

# ---------------------------------------------------------------- IO register offsets
REG_DISPCNT = 0x000
REG_DISPSTAT = 0x004
REG_VCOUNT = 0x006
REG_BG0CNT = 0x008
REG_BG0HOFS = 0x010
REG_BG2PA = 0x020
REG_BG2X = 0x028
REG_BG2Y = 0x02C
REG_BG3PA = 0x030
REG_BG3X = 0x038
REG_BG3Y = 0x03C
REG_WIN0H = 0x040
REG_WIN1H = 0x042
REG_WIN0V = 0x044
REG_WIN1V = 0x046
REG_WININ = 0x048
REG_WINOUT = 0x04A
REG_MOSAIC = 0x04C
REG_BLDCNT = 0x050
REG_BLDALPHA = 0x052
REG_BLDY = 0x054
REG_SOUND_FIRST = 0x060
REG_SOUNDCNT_H = 0x082
REG_SOUNDCNT_X = 0x084
REG_SOUNDBIAS = 0x088
REG_WAVE_RAM = 0x090
REG_FIFO_A = 0x0A0
REG_FIFO_B = 0x0A4
REG_DMA0SAD = 0x0B0
DMA_STRIDE = 12
REG_TM0CNT = 0x100
REG_KEYINPUT = 0x130
REG_KEYCNT = 0x132
REG_IE = 0x200
REG_IF = 0x202
REG_WAITCNT = 0x204
REG_IME = 0x208
REG_POSTFLG = 0x300                  # low byte POSTFLG, high byte HALTCNT

# ---------------------------------------------------------------- interrupts
IRQ_VBLANK = 0x0001
IRQ_HBLANK = 0x0002
IRQ_VCOUNT = 0x0004
IRQ_TIMER0 = 0x0008                  # timer n = IRQ_TIMER0 << n
IRQ_SERIAL = 0x0080
IRQ_DMA0 = 0x0100                    # dma n = IRQ_DMA0 << n
IRQ_KEYPAD = 0x1000
IRQ_GAMEPAK = 0x2000

# ---------------------------------------------------------------- PPU
WIDTH = 240
HEIGHT = 160
LINE_CYCLES = 1232
HBLANK_START = 1006                  # cycle within a line where the H-Blank flag rises
TOTAL_LINES = 228
FRAME_CYCLES = LINE_CYCLES * TOTAL_LINES

STAT_VBLANK = 0x01
STAT_HBLANK = 0x02
STAT_VCOUNT = 0x04
STAT_VBLANK_IRQ = 0x08
STAT_HBLANK_IRQ = 0x10
STAT_VCOUNT_IRQ = 0x20

# text BG sizes (tiles) by BGxCNT bits 14-15
TEXT_BG_SIZE = ((32, 32), (64, 32), (32, 64), (64, 64))
# affine BG size (tiles) by BGxCNT bits 14-15
AFFINE_BG_SIZE = (16, 32, 64, 128)
# OBJ size in pixels [shape][size] -> (w, h)
OBJ_SIZE = (
    ((8, 8), (16, 16), (32, 32), (64, 64)),      # square
    ((16, 8), (32, 8), (32, 16), (64, 32)),      # horizontal
    ((8, 16), (8, 32), (16, 32), (32, 64)),      # vertical
    ((8, 8), (8, 8), (8, 8), (8, 8)),            # prohibited
)

# ---------------------------------------------------------------- DMA
DMA_TIMING_NOW = 0
DMA_TIMING_VBLANK = 1
DMA_TIMING_HBLANK = 2
DMA_TIMING_SPECIAL = 3
DMA_COUNT_MASK = (0x3FFF, 0x3FFF, 0x3FFF, 0xFFFF)
DMA_SRC_MASK = (0x07FFFFFF, 0x0FFFFFFF, 0x0FFFFFFF, 0x0FFFFFFF)
DMA_DST_MASK = (0x07FFFFFF, 0x07FFFFFF, 0x07FFFFFF, 0x0FFFFFFF)

# ---------------------------------------------------------------- timers
TIMER_SHIFT = (0, 6, 8, 10)          # prescaler 1 / 64 / 256 / 1024

# ---------------------------------------------------------------- waitstates (WAITCNT)
WS_N = (4, 3, 2, 8)                  # first-access wait for WS0/1/2 bits
WS0_S = (2, 1)
WS1_S = (4, 1)
WS2_S = (8, 1)
SRAM_WAIT = (4, 3, 2, 8)

# ---------------------------------------------------------------- keypad (KEYINPUT bit numbers)
KEY_A = 0
KEY_B = 1
KEY_SELECT = 2
KEY_START = 3
KEY_RIGHT = 4
KEY_LEFT = 5
KEY_UP = 6
KEY_DOWN = 7
KEY_R = 8
KEY_L = 9
BUTTON_NAMES = {"a": KEY_A, "b": KEY_B, "select": KEY_SELECT, "start": KEY_START,
                "right": KEY_RIGHT, "left": KEY_LEFT, "up": KEY_UP, "down": KEY_DOWN,
                "r": KEY_R, "l": KEY_L}

# ---------------------------------------------------------------- cartridge / saves
SAVE_NONE = "none"
SAVE_SRAM = "sram"
SAVE_FLASH64 = "flash64"
SAVE_FLASH128 = "flash128"
SAVE_EEPROM = "eeprom"
# library ID strings embedded in the ROM by Nintendo's SDK
SAVE_SIGNATURES = (
    (b"EEPROM_V", SAVE_EEPROM),
    (b"SRAM_F_V", SAVE_SRAM),
    (b"SRAM_V", SAVE_SRAM),
    (b"FLASH1M_V", SAVE_FLASH128),
    (b"FLASH512_V", SAVE_FLASH64),
    (b"FLASH_V", SAVE_FLASH64),
)
SRAM_SIZE = 0x8000
FLASH_BANK = 0x10000
FLASH_ID_64 = (0x32, 0x1B)           # Panasonic MN63F805MNP (manufacturer, device)
FLASH_ID_128 = (0x62, 0x13)          # Sanyo LE26FV10N1TS
EEPROM_SMALL = 0x200                 # 4 kbit, 6-bit addresses
EEPROM_LARGE = 0x2000                # 64 kbit, 14-bit addresses

# ---------------------------------------------------------------- cartridge GPIO / RTC
RTC_SIGNATURE = b"SIIRTC_V"          # Nintendo's S-3511 RTC library ID string
GPIO_BASE = 0x080000C4
GPIO_DATA = 0x0C4                    # ROM offsets of the GPIO registers
GPIO_DIRECTION = 0x0C6
GPIO_CONTROL = 0x0C8
RTC_MAGIC = 0x06                     # low nibble of every command byte
RTC_CMD_RESET = 0
RTC_CMD_DATETIME = 2
RTC_CMD_CONTROL = 4
RTC_CMD_TIME = 6
RTC_PARAM_BYTES = (0, 0, 7, 0, 1, 0, 3, 0)   # data bytes per command register
RTC_CONTROL_24H = 0x40
