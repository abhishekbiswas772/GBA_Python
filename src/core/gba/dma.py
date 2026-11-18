"""DMA channels 0-3.

Transfers run synchronously when triggered (immediate / V-Blank / H-Blank /
sound FIFO) and charge their cycles to the CPU clock. Incrementing copies
between plain memory regions use slice copies; anything else (IO, EEPROM,
decrementing or fixed addresses) goes unit by unit through the bus tables.
"""
from src.core.gba.common import (
    REG_DMA0SAD, DMA_STRIDE, DMA_COUNT_MASK, DMA_SRC_MASK, DMA_DST_MASK,
    DMA_TIMING_NOW, DMA_TIMING_SPECIAL, IRQ_DMA0,
)


def plain_slice(bus, addr: int, nbytes: int, write: bool = False):
    """(buffer, offset) if [addr, addr+nbytes) is contiguous plain memory, else None.
    ROM is only returned for reads."""
    reg = addr >> 24
    if reg == 2:
        o = addr & 0x3FFFF
        return (bus.ewram, o) if o + nbytes <= 0x40000 else None
    if reg == 3:
        o = addr & 0x7FFF
        return (bus.iwram, o) if o + nbytes <= 0x8000 else None
    if reg == 5:
        o = addr & 0x3FF
        return (bus.pal, o) if o + nbytes <= 0x400 else None
    if reg == 6:
        o = addr & 0x1FFFF
        if o >= 0x18000:
            o -= 0x8000
        return (bus.vram, o) if o + nbytes <= 0x18000 else None
    if reg == 7:
        o = addr & 0x3FF
        return (bus.oam, o) if o + nbytes <= 0x400 else None
    if not write and 8 <= reg <= 13 and not (reg == 13 and bus.eeprom is not None):
        o = addr & 0x1FFFFFF
        return (bus.rom, o) if o + nbytes <= len(bus.rom) else None
    return None


class DMA:
    def __init__(self, bus):
        self.bus = bus
        self.src = [0] * 4
        self.dst = [0] * 4
        self.count = [0] * 4
        self.ctrl = [0] * 4
        self.active = [False] * 4
        for i in range(4):
            base = REG_DMA0SAD + DMA_STRIDE * i
            bus.io_write[base + 10] = (lambda v, m, i=i: self._w_ctrl(i, v, m))
            bus.io_read[base + 10] = (lambda i=i: self.ctrl[i])
            for off in (0, 2, 4, 6, 8):               # SAD / DAD / CNT_L are write-only
                bus.io_read[base + off] = (lambda: 0)

    def _reg32(self, off: int) -> int:
        io16 = self.bus.io16
        return io16[off >> 1] | (io16[(off >> 1) + 1] << 16)

    def _w_ctrl(self, i: int, v: int, m: int) -> None:
        old = self.ctrl[i]
        new = (old & ~m) | v
        if i != 3:
            new &= 0xF7E0                              # no game-pak DRQ
        else:
            new &= 0xFFE0
        self.ctrl[i] = new
        base = REG_DMA0SAD + DMA_STRIDE * i
        if new & 0x8000 and not old & 0x8000:
            self.src[i] = self._reg32(base) & DMA_SRC_MASK[i]
            self.dst[i] = self._reg32(base + 4) & DMA_DST_MASK[i]
            self._load_count(i)
            self.active[i] = True
            if (new >> 12) & 3 == DMA_TIMING_NOW:
                self.run(i)
        elif not new & 0x8000:
            self.active[i] = False

    def _load_count(self, i: int) -> None:
        base = REG_DMA0SAD + DMA_STRIDE * i
        n = self.bus.io16[(base + 8) >> 1] & DMA_COUNT_MASK[i]
        self.count[i] = n if n else DMA_COUNT_MASK[i] + 1

    def trigger(self, timing: int) -> None:
        for i in range(4):
            if self.active[i] and (self.ctrl[i] >> 12) & 3 == timing:
                self.run(i)

    def fifo_request(self, fifo_addr: int) -> None:
        """Sound FIFO A/B wants data: run DMA1/2 in special (FIFO) mode aimed at it."""
        for i in (1, 2):
            if (self.active[i] and (self.ctrl[i] >> 12) & 3 == DMA_TIMING_SPECIAL
                    and (self.dst[i] & 0x0FFFFFFF) == fifo_addr):
                self.run(i, sound=True)

    def run(self, i: int, sound: bool = False) -> None:
        bus = self.bus
        ctrl = self.ctrl[i]
        word = bool(ctrl & 0x0400) or sound
        unit = 4 if word else 2
        dctl = (ctrl >> 5) & 3
        sctl = (ctrl >> 7) & 3
        count = 4 if sound else self.count[i]
        if sound:
            dctl = 2
        src = self.src[i] & ~(unit - 1)
        dst = self.dst[i] & ~(unit - 1)
        sstep = (unit, -unit, 0, unit)[sctl]
        dstep = (unit, -unit, 0, unit)[dctl]
        nbytes = count * unit
        if bus.eeprom is not None and dst >> 24 == 0xD:
            bus.eeprom.begin(count)

        done = False
        if sstep == unit and dstep == unit:
            s = plain_slice(bus, src, nbytes)
            d = plain_slice(bus, dst, nbytes, write=True)
            if s is not None and d is not None:
                sb, so = s
                db, do = d
                db[do:do + nbytes] = sb[so:so + nbytes]
                done = True
        elif sstep == 0 and dstep == unit:
            d = plain_slice(bus, dst, nbytes, write=True)
            if d is not None and src >> 24 != 0xD:
                val = bus.read32(src) if word else bus.read16(src)
                db, do = d
                db[do:do + nbytes] = val.to_bytes(unit, "little") * count
                done = True
        if not done:
            R = bus.R32 if word else bus.R16
            W = bus.W32 if word else bus.W16
            s, d = src, dst
            for _ in range(count):
                s &= 0xFFFFFFFF
                d &= 0xFFFFFFFF
                W[d >> 24](d, R[s >> 24](s))
                s += sstep
                d += dstep
        self.src[i] = (src + sstep * count) & 0xFFFFFFFF
        if dctl != 3:
            self.dst[i] = (dst + dstep * count) & 0xFFFFFFFF

        waits = bus.wait32 if word else bus.wait16
        cost = 2 + count * (2 + waits[(src >> 24) & 0xFF] + waits[(dst >> 24) & 0xFF])
        cpu = bus.cpu
        if cpu is not None:
            cpu.now += cost

        timing = (ctrl >> 12) & 3
        if ctrl & 0x0200 and timing != DMA_TIMING_NOW:     # repeat
            if not sound:
                self._load_count(i)
            if dctl == 3:
                self.dst[i] = self._reg32(REG_DMA0SAD + DMA_STRIDE * i + 4) & DMA_DST_MASK[i]
        else:
            self.ctrl[i] = ctrl & 0x7FFF
            self.active[i] = False
        if ctrl & 0x4000:
            bus.request_irq(IRQ_DMA0 << i)
