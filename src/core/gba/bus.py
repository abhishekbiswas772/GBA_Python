"""GBA memory map + IO register file.

Region tables R8/R16/R32/W8/W16/W32 are indexed by address >> 24; reads return
the aligned value (the CPU rotates unaligned LDR/LDRH). wait16/wait32 hold the
extra wait cycles of a data access per region (from WAITCNT for the cartridge).

IO writes go through write16(offset, value, mask) handlers so that byte writes
to write-1-to-clear registers (IF) only touch the written byte.
"""
from array import array

from src.core.adaptors.snapshot import Snapshottable
from src.core.gba.common import (
    BIOS_SIZE, EWRAM_SIZE, IWRAM_SIZE, IO_SIZE, PAL_SIZE, VRAM_SIZE, OAM_SIZE, ROM_MAX,
    BIOS_WORDS, BIOS_LATCH_STARTUP,
    REG_IE, REG_IF, REG_IME, REG_WAITCNT, REG_POSTFLG, REG_KEYINPUT,
    WS_N, WS0_S, WS1_S, WS2_S, SRAM_WAIT, SAVE_EEPROM, GPIO_BASE,
)


def _view(buf):
    return memoryview(buf).cast("H"), memoryview(buf).cast("I")


def hle_bios_image() -> bytearray:
    bios = bytearray(BIOS_SIZE)
    for addr, word in BIOS_WORDS.items():
        bios[addr:addr + 4] = word.to_bytes(4, "little")
    return bios


def pad_rom(rom: bytes) -> bytearray:
    """ROM padded to the full 32 MB window with open-bus data ((addr >> 1) & 0xFFFF)."""
    size = (len(rom) + 3) & ~3
    out = bytearray(rom) + bytes(size - len(rom))
    if size < ROM_MAX:
        pattern = array("H", range(65536)).tobytes()        # 128 KB, repeats exactly
        start = size // 2 % 65536 * 2
        need = ROM_MAX - size
        tail = (pattern[start:] + pattern * (need // len(pattern) + 1))[:need]
        out += tail
    return out


_VIEWS = (("bios", "bios16", "bios32"), ("ewram", "ew16", "ew32"), ("iwram", "iw16", "iw32"),
          ("io", "io16", None), ("pal", "pal16", "pal32"), ("vram", "vram16", "vram32"),
          ("oam", "oam16", "oam32"), ("rom", "rom16", "rom32"))


class Bus(Snapshottable):
    # memoryviews can't be pickled (see src/core/adaptors/snapshot)
    _NOT_STATE = tuple(v for _, v16, v32 in _VIEWS for v in (v16, v32) if v)

    def _rebuild_transient(self) -> None:
        for buf, v16, v32 in _VIEWS:
            h, w = _view(getattr(self, buf))
            setattr(self, v16, h)
            if v32:
                setattr(self, v32, w)

    def __init__(self, rom: bytes, bios: bytes | None = None, save=None, rtc=None):
        self.bios = bytearray(bios) if bios is not None else hle_bios_image()
        self.bios16, self.bios32 = _view(self.bios)
        self.ewram = bytearray(EWRAM_SIZE)
        self.ew16, self.ew32 = _view(self.ewram)
        self.iwram = bytearray(IWRAM_SIZE)
        self.iw16, self.iw32 = _view(self.iwram)
        self.io = bytearray(IO_SIZE)
        self.io16, _ = _view(self.io)
        self.pal = bytearray(PAL_SIZE)
        self.pal16, self.pal32 = _view(self.pal)
        self.vram = bytearray(VRAM_SIZE)
        self.vram16, self.vram32 = _view(self.vram)
        self.oam = bytearray(OAM_SIZE)
        self.oam16, self.oam32 = _view(self.oam)
        self.rom_size = len(rom)
        self.rom = pad_rom(rom)
        self.rom16, self.rom32 = _view(self.rom)
        self.save = save
        self.rtc = rtc                  # cartridge GPIO real-time clock, or None
        self.eeprom = save if save is not None and getattr(save, "kind", None) == SAVE_EEPROM else None

        self.ie = 0
        self.if_ = 0
        self.ime = 0
        self.cpu = None
        self.ppu = None
        self.timers = None
        self.dma = None
        self.apu = None
        self.keys = 0x3FF               # KEYINPUT, active low
        self.io_read = {}
        self.io_write = {}

        self.R8 = [self._open8] * 256
        self.R16 = [self._open16] * 256
        self.R32 = [self._open32] * 256
        self.W8 = [self._nowrite] * 256
        self.W16 = [self._nowrite] * 256
        self.W32 = [self._nowrite] * 256
        self.wait16 = [0] * 256
        self.wait32 = [0] * 256
        self._code_cost16 = [1] * 256
        self._code_cost32 = [1] * 256
        self._install_regions()
        self._install_io()
        self.set_waitcnt(0)

    # ------------------------------------------------------------ open bus / BIOS
    def _open32(self, a):
        return 0

    def _open16(self, a):
        return 0

    def _open8(self, a):
        return 0

    def _nowrite(self, a, v):
        pass

    def _bios_word(self, a):
        cpu = self.cpu
        if a < BIOS_SIZE and cpu is not None and cpu.pc < BIOS_SIZE:
            return self.bios32[(a & 0x3FFC) >> 2]
        if a >= BIOS_SIZE:
            return 0
        return cpu.bios_latch if cpu is not None else BIOS_LATCH_STARTUP

    # ------------------------------------------------------------ regions
    def _install_regions(self):
        R8, R16, R32, W8, W16, W32 = self.R8, self.R16, self.R32, self.W8, self.W16, self.W32

        def b32(a):
            return self._bios_word(a)

        def b16(a):
            return (self._bios_word(a) >> ((a & 2) << 3)) & 0xFFFF

        def b8(a):
            return (self._bios_word(a) >> ((a & 3) << 3)) & 0xFF

        R8[0], R16[0], R32[0] = b8, b16, b32

        def ram(buf, v16, v32, mask, region):
            def r8(a):
                return buf[a & mask]

            def r16(a):
                return v16[(a & mask) >> 1]

            def r32(a):
                return v32[(a & mask) >> 2]

            def w8(a, v):
                buf[a & mask] = v

            def w16(a, v):
                v16[(a & mask) >> 1] = v

            def w32(a, v):
                v32[(a & mask) >> 2] = v
            R8[region], R16[region], R32[region] = r8, r16, r32
            W8[region], W16[region], W32[region] = w8, w16, w32

        ram(self.ewram, self.ew16, self.ew32, EWRAM_SIZE - 1, 2)
        ram(self.iwram, self.iw16, self.iw32, IWRAM_SIZE - 1, 3)

        # IO
        def io8(a):
            if a & 0xFFFFFF >= IO_SIZE:
                return 0
            return (self.io_read16(a & 0x3FE) >> ((a & 1) << 3)) & 0xFF

        def io16(a):
            if a & 0xFFFFFF >= IO_SIZE:
                return 0
            return self.io_read16(a & 0x3FE)

        def io32(a):
            if a & 0xFFFFFF >= IO_SIZE:
                return 0
            o = a & 0x3FC
            return self.io_read16(o) | (self.io_read16(o + 2) << 16)

        def iow8(a, v):
            if a & 0xFFFFFF < IO_SIZE:
                sh = (a & 1) << 3
                self.io_write16(a & 0x3FE, v << sh, 0xFF << sh)

        def iow16(a, v):
            if a & 0xFFFFFF < IO_SIZE:
                self.io_write16(a & 0x3FE, v, 0xFFFF)

        def iow32(a, v):
            if a & 0xFFFFFF < IO_SIZE:
                o = a & 0x3FC
                self.io_write16(o, v & 0xFFFF, 0xFFFF)
                self.io_write16(o + 2, v >> 16, 0xFFFF)
        R8[4], R16[4], R32[4], W8[4], W16[4], W32[4] = io8, io16, io32, iow8, iow16, iow32

        # palette (8-bit writes store the byte to both halves)
        pal, p16, p32 = self.pal, self.pal16, self.pal32

        def pr8(a):
            return pal[a & 0x3FF]

        def pr16(a):
            return p16[(a & 0x3FF) >> 1]

        def pr32(a):
            return p32[(a & 0x3FF) >> 2]

        def pw8(a, v):
            p16[(a & 0x3FF) >> 1] = v * 0x101

        def pw16(a, v):
            p16[(a & 0x3FF) >> 1] = v

        def pw32(a, v):
            p32[(a & 0x3FF) >> 2] = v
        R8[5], R16[5], R32[5], W8[5], W16[5], W32[5] = pr8, pr16, pr32, pw8, pw16, pw32

        # VRAM: 96 KB, 128 KB window (0x18000-0x1FFFF mirrors 0x10000-0x17FFF)
        vram, v16, v32 = self.vram, self.vram16, self.vram32

        def voff(a):
            o = a & 0x1FFFF
            return o - 0x8000 if o >= 0x18000 else o

        def vr8(a):
            return vram[voff(a)]

        def vr16(a):
            return v16[voff(a) >> 1]

        def vr32(a):
            return v32[voff(a) >> 2]

        def vw8(a, v):
            o = voff(a)
            limit = 0x14000 if (self.io16[0] & 7) >= 3 else 0x10000
            if o < limit:                       # OBJ VRAM ignores byte writes
                v16[o >> 1] = v * 0x101

        def vw16(a, v):
            v16[voff(a) >> 1] = v

        def vw32(a, v):
            v32[voff(a) >> 2] = v
        R8[6], R16[6], R32[6], W8[6], W16[6], W32[6] = vr8, vr16, vr32, vw8, vw16, vw32

        # OAM (byte writes ignored)
        oam, o16, o32 = self.oam, self.oam16, self.oam32

        def or8(a):
            return oam[a & 0x3FF]

        def or16(a):
            return o16[(a & 0x3FF) >> 1]

        def or32(a):
            return o32[(a & 0x3FF) >> 2]

        def ow16(a, v):
            o16[(a & 0x3FF) >> 1] = v

        def ow32(a, v):
            o32[(a & 0x3FF) >> 2] = v
        R8[7], R16[7], R32[7], W16[7], W32[7] = or8, or16, or32, ow16, ow32

        # cartridge ROM (3 waitstate mirrors), EEPROM in the top region on EEPROM carts
        rom, r16_, r32_ = self.rom, self.rom16, self.rom32
        eeprom = self.eeprom
        eeprom_base = 0x0D000000 if self.rom_size <= 0x1000000 else 0x0DFFFF00

        def rr8(a):
            return rom[a & 0x1FFFFFF]

        def rr16(a):
            return r16_[(a & 0x1FFFFFF) >> 1]

        def rr32(a):
            return r32_[(a & 0x1FFFFFF) >> 2]
        for reg in range(8, 14):
            R8[reg], R16[reg], R32[reg] = rr8, rr16, rr32
        if self.rtc is not None:                 # GPIO port at 0x080000C4-0x080000C9
            rtc = self.rtc

            def gr16(a):
                if GPIO_BASE <= a < GPIO_BASE + 6:
                    v = rtc.read16((a & ~1) - 0x08000000)
                    if v is not None:
                        return v
                return r16_[(a & 0x1FFFFFF) >> 1]

            def gr8(a):
                return (gr16(a) >> ((a & 1) << 3)) & 0xFF

            def gr32(a):
                a &= ~3
                return gr16(a) | (gr16(a + 2) << 16)

            def gw16(a, v):
                if GPIO_BASE <= a < GPIO_BASE + 6:
                    rtc.write16((a & ~1) - 0x08000000, v)

            def gw8(a, v):
                gw16(a, v)

            def gw32(a, v):
                a &= ~3
                gw16(a, v & 0xFFFF)
                gw16(a + 2, v >> 16)
            R8[8], R16[8], R32[8] = gr8, gr16, gr32
            W8[8], W16[8], W32[8] = gw8, gw16, gw32
        if eeprom is not None:
            def er16(a):
                if a >= eeprom_base:
                    return eeprom.read_bit()
                return r16_[(a & 0x1FFFFFF) >> 1]

            def ew16(a, v):
                if a >= eeprom_base:
                    eeprom.write_bit(v)
            R16[0xD], W16[0xD] = er16, ew16

        # SRAM / Flash: 8-bit bus; wider reads return the byte repeated
        save = self.save

        def sr8(a):
            return save.read8(a & 0xFFFF) if save is not None and eeprom is None else 0xFF

        def sr16(a):
            return sr8(a) * 0x0101

        def sr32(a):
            return sr8(a) * 0x01010101

        def sw8(a, v):
            if save is not None and eeprom is None:
                save.write8(a & 0xFFFF, v & 0xFF)

        def sw16(a, v):
            sw8(a, (v >> ((a & 1) << 3)) & 0xFF)

        def sw32(a, v):
            sw8(a, (v >> ((a & 3) << 3)) & 0xFF)
        for reg in (0xE, 0xF):
            R8[reg], R16[reg], R32[reg], W8[reg], W16[reg], W32[reg] = sr8, sr16, sr32, sw8, sw16, sw32

    def code32(self, reg):
        """Instruction-fetch view for region reg: (uint32 view or None, mask, cycles)."""
        cost = self._code_cost32[reg & 0xFF]
        if reg == 3:
            return self.iw32, IWRAM_SIZE - 1, cost
        if 8 <= reg <= 13:
            return self.rom32, ROM_MAX - 1, cost
        if reg == 2:
            return self.ew32, EWRAM_SIZE - 1, cost
        return None, 0, cost

    def code16(self, reg):
        cost = self._code_cost16[reg & 0xFF]
        if 8 <= reg <= 13:
            return self.rom16, ROM_MAX - 1, cost
        if reg == 3:
            return self.iw16, IWRAM_SIZE - 1, cost
        if reg == 2:
            return self.ew16, EWRAM_SIZE - 1, cost
        return None, 0, cost

    def set_waitcnt(self, v: int) -> None:
        w16, w32 = self.wait16, self.wait32
        c16, c32 = self._code_cost16, self._code_cost32
        w16[2], w32[2] = 2, 5
        c16[2], c32[2] = 3, 6
        for reg in (5, 6):
            w16[reg], w32[reg] = 0, 1
            c16[reg], c32[reg] = 1, 2
        prefetch = bool(v & 0x4000)
        for base, n_bits, s_tab, s_bit in ((8, (v >> 2) & 3, WS0_S, (v >> 4) & 1),
                                           (10, (v >> 5) & 3, WS1_S, (v >> 7) & 1),
                                           (12, (v >> 8) & 3, WS2_S, (v >> 10) & 1)):
            n = WS_N[n_bits]
            s = s_tab[s_bit]
            for reg in (base, base + 1):
                w16[reg] = n
                w32[reg] = n + s + 1
                seq = max(1, (1 + s) >> 1) if prefetch else 1 + s
                c16[reg] = seq
                c32[reg] = 2 * seq
        sw = SRAM_WAIT[v & 3]
        for reg in (0xE, 0xF):
            w16[reg] = w32[reg] = sw
        if self.cpu is not None:
            self.cpu.limit = 0           # refetch code costs

    # ------------------------------------------------------------ IO registers
    def _install_io(self):
        rd, wr = self.io_read, self.io_write
        rd[REG_IE] = lambda: self.ie
        rd[REG_IF] = lambda: self.if_
        rd[REG_IME] = lambda: self.ime
        rd[REG_KEYINPUT] = lambda: self.keys
        rd[REG_IME + 2] = lambda: 0

        def w_ie(v, m):
            self.ie = ((self.ie & ~m) | (v & m)) & 0x3FFF
            self._irq_changed()

        def w_if(v, m):
            self.if_ &= ~(v & m)

        def w_ime(v, m):
            self.ime = ((self.ime & ~m) | (v & m)) & 1
            self._irq_changed()

        def w_waitcnt(v, m):
            val = ((self.io16[REG_WAITCNT >> 1] & ~m) | (v & m)) & 0x5FFF
            self.io16[REG_WAITCNT >> 1] = val
            self.set_waitcnt(val)

        def w_postflg(v, m):
            if m & 0xFF:
                self.io[REG_POSTFLG] = v & 1
            if m & 0xFF00:                        # HALTCNT: bit 7 = stop, else halt
                if self.cpu is not None:
                    self.cpu.halted = True
                    self.cpu.limit = 0

        wr[REG_IE] = w_ie
        wr[REG_IF] = w_if
        wr[REG_IME] = w_ime
        wr[REG_WAITCNT] = w_waitcnt
        wr[REG_POSTFLG] = w_postflg
        wr[REG_KEYINPUT] = lambda v, m: None
        rd[REG_POSTFLG] = lambda: self.io[REG_POSTFLG]

    def _irq_changed(self):
        if self.cpu is not None:
            self.cpu.limit = 0

    def request_irq(self, bits: int) -> None:
        self.if_ |= bits
        if self.ie & bits and self.cpu is not None:
            self.cpu.limit = 0

    def io_read16(self, off: int) -> int:
        h = self.io_read.get(off)
        if h is not None:
            return h()
        return self.io16[off >> 1]

    def io_write16(self, off: int, v: int, m: int) -> None:
        h = self.io_write.get(off)
        if h is not None:
            h(v & m, m)
        else:
            i = off >> 1
            self.io16[i] = (self.io16[i] & ~m) | (v & m)

    # ------------------------------------------------------------ convenience (tests, HLE, DMA)
    def read8(self, a):
        a &= 0xFFFFFFFF
        return self.R8[a >> 24](a)

    def read16(self, a):
        a &= 0xFFFFFFFF
        return self.R16[a >> 24](a)

    def read32(self, a):
        a &= 0xFFFFFFFF
        return self.R32[a >> 24](a)

    def write8(self, a, v):
        a &= 0xFFFFFFFF
        self.W8[a >> 24](a, v & 0xFF)

    def write16(self, a, v):
        a &= 0xFFFFFFFF
        self.W16[a >> 24](a, v & 0xFFFF)

    def write32(self, a, v):
        a &= 0xFFFFFFFF
        self.W32[a >> 24](a, v & 0xFFFFFFFF)
