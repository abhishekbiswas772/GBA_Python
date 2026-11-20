"""High-level emulation of the GBA BIOS software interrupts.

The BIOS image itself only holds the real IRQ dispatcher (0x128) and the words
that open-bus BIOS reads return; SWIs are executed here in Python.

IntrWait / VBlankIntrWait halt and rewind the PC onto the SWI, so after the
interrupt handler returns the SWI runs again and re-checks the BIOS IF flags,
just like the real BIOS loop.
"""
import math

from src.core.gba.common import (
    M32, BIOS_LATCH_SWI, BIOS_IF_ADDR, MODE_IRQ, MODE_SVC, MODE_SYS,
    SP_USR, SP_IRQ, SP_SVC, ROM_START, REG_DISPCNT, REG_BG2PA, REG_BG3PA,
)
from src.core.gba.dma import plain_slice


def _s16(v: int) -> int:
    return (v ^ 0x8000) - 0x8000


def _s32(v: int) -> int:
    return (v ^ 0x80000000) - 0x80000000


def write_block(bus, addr: int, data: bytes, unit: int = 1) -> None:
    """Write bytes to memory; slice copy for plain memory, else `unit`-wide writes."""
    d = plain_slice(bus, addr, len(data), write=True)
    if d is not None:
        buf, off = d
        buf[off:off + len(data)] = data
        return
    if unit == 1:
        for i, b in enumerate(data):
            bus.write8(addr + i, b)
    else:
        for i in range(0, len(data) - 1, 2):
            bus.write16(addr + i, data[i] | (data[i + 1] << 8))


def read_block(bus, addr: int, n: int) -> bytes:
    s = plain_slice(bus, addr, n)
    if s is not None:
        buf, off = s
        return bytes(buf[off:off + n])
    return bytes(bus.read8(addr + i) for i in range(n))


# ------------------------------------------------------------------ SWIs
def soft_reset(cpu, bus):
    flag = bus.read8(0x03007FFA)
    for a in range(0x7E00, 0x8000):
        bus.iwram[a] = 0
    cpu.switch_mode(MODE_IRQ)
    cpu.r[13], cpu.r[14] = SP_IRQ, 0
    cpu.spsr[cpu.bank] = 0
    cpu.switch_mode(MODE_SVC)
    cpu.r[13], cpu.r[14] = SP_SVC, 0
    cpu.spsr[cpu.bank] = 0
    cpu.switch_mode(MODE_SYS)
    cpu.r[13] = SP_USR
    for i in range(13):
        cpu.r[i] = 0
    cpu.r[14] = 0
    cpu.thumb = False
    cpu.irq_disable = cpu.fiq_disable = 0
    cpu.pc = 0x02000000 if flag else ROM_START
    cpu.limit = 0
    return 3


def register_ram_reset(cpu, bus):
    f = cpu.r[0]
    if f & 0x01:
        bus.ewram[:] = bytes(len(bus.ewram))
    if f & 0x02:
        bus.iwram[:0x7E00] = bytes(0x7E00)
    if f & 0x04:
        bus.pal[:] = bytes(len(bus.pal))
    if f & 0x08:
        bus.vram[:] = bytes(len(bus.vram))
    if f & 0x10:
        bus.oam[:] = bytes(len(bus.oam))
    if f & 0x80:
        for off in range(0x000, 0x060, 2):
            bus.write16(0x04000000 + off, 0)
        bus.write16(0x04000000 + REG_DISPCNT, 0x0080)
        # The real BIOS leaves the BG2/BG3 affine matrices at identity, not zero
        # (PA = PD = 1.0). Games that never write them (FireRed's Oak intro draws
        # him on affine BG2) would otherwise sample one texel for the whole line.
        for pa in (REG_BG2PA, REG_BG3PA):
            bus.write16(0x04000000 + pa, 0x100)
            bus.write16(0x04000000 + pa + 6, 0x100)
    return 100


def halt(cpu, bus):
    cpu.halted = True
    cpu.limit = 0
    return 3


def intr_wait(cpu, bus, discard: int, flags: int):
    bus.ime = 1
    i = (BIOS_IF_ADDR & 0x7FFF) >> 1
    if discard and not cpu.intrwait:
        bus.iw16[i] &= ~flags & 0xFFFF
    cur = bus.iw16[i]
    if cur & flags:
        bus.iw16[i] = cur & ~flags & 0xFFFF
        cpu.intrwait = False
        return 3
    cpu.intrwait = True
    cpu.halted = True
    cpu.pc -= 2 if cpu.thumb else 4        # re-run this SWI after the IRQ returns
    cpu.limit = 0
    return 3


def div(cpu, bus, num=None, den=None):
    r = cpu.r
    n = _s32(r[0] if num is None else num)
    d = _s32(r[1] if den is None else den)
    if d == 0:
        r[0] = (-1 if n < 0 else 1) & M32
        r[1] = n & M32
        r[3] = 1
        return 20
    q = abs(n) // abs(d)
    if (n < 0) != (d < 0):
        q = -q
    rem = n - q * d
    r[0] = q & M32
    r[1] = rem & M32
    r[3] = abs(q) & M32
    return 40


def div_arm(cpu, bus):
    return div(cpu, bus, cpu.r[1], cpu.r[0])


def sqrt(cpu, bus):
    cpu.r[0] = math.isqrt(cpu.r[0])
    return 30


def _arctan(i: int) -> int:
    a = -((i * i) >> 14)
    b = ((0xA9 * a) >> 14) + 0x390
    b = ((b * a) >> 14) + 0x91C
    b = ((b * a) >> 14) + 0xFB6
    b = ((b * a) >> 14) + 0x16AA
    b = ((b * a) >> 14) + 0x2081
    b = ((b * a) >> 14) + 0x3651
    b = ((b * a) >> 14) + 0xA2F9
    return (i * b) >> 16


def arctan(cpu, bus):
    cpu.r[0] = _arctan(_s32(cpu.r[0])) & M32
    return 30


def arctan2(cpu, bus):
    x = _s32(cpu.r[0])
    y = _s32(cpu.r[1])
    if y == 0:
        res = 0x8000 if x < 0 else 0
    elif x == 0:
        res = 0xC000 if y < 0 else 0x4000
    elif y >= 0:
        if x >= 0:
            res = _arctan(_int_div(y << 14, x)) if x >= y else 0x4000 - _arctan(_int_div(x << 14, y))
        elif -x >= y:
            res = _arctan(_int_div(y << 14, x)) + 0x8000
        else:
            res = 0x4000 - _arctan(_int_div(x << 14, y))
    else:
        if x <= 0:
            res = _arctan(_int_div(y << 14, x)) + 0x8000 if -x > -y else 0xC000 - _arctan(_int_div(x << 14, y))
        elif x >= -y:
            res = _arctan(_int_div(y << 14, x)) + 0x10000
        else:
            res = 0xC000 - _arctan(_int_div(x << 14, y))
    cpu.r[0] = res & 0xFFFF
    return 40


def _int_div(a: int, b: int) -> int:
    """C-style truncating division (used by ArcTan2 sub-terms)."""
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def cpu_set(cpu, bus):
    src, dst, cnt = cpu.r[0], cpu.r[1], cpu.r[2]
    n = cnt & 0x1FFFFF
    fixed = bool(cnt & 0x01000000)
    if cnt & 0x04000000:
        src &= ~3
        dst &= ~3
        unit, rd, wr = 4, bus.read32, bus.write32
    else:
        src &= ~1
        dst &= ~1
        unit, rd, wr = 2, bus.read16, bus.write16
    _copy(bus, src, dst, n, unit, fixed, rd, wr)
    return 10 + n * 2


def cpu_fast_set(cpu, bus):
    src, dst, cnt = cpu.r[0] & ~3, cpu.r[1] & ~3, cpu.r[2]
    n = ((cnt & 0x1FFFFF) + 7) & ~7
    _copy(bus, src, dst, n, 4, bool(cnt & 0x01000000), bus.read32, bus.write32)
    return 10 + n


def _copy(bus, src, dst, n, unit, fixed, rd, wr):
    if n == 0 or src < 0x02000000:                # BIOS-protected source: ignored
        return
    nbytes = n * unit
    d = plain_slice(bus, dst, nbytes, write=True)
    if d is not None:
        buf, off = d
        if fixed:
            buf[off:off + nbytes] = rd(src).to_bytes(unit, "little") * n
            return
        s = plain_slice(bus, src, nbytes)
        if s is not None:
            sb, so = s
            buf[off:off + nbytes] = sb[so:so + nbytes]
            return
    if fixed:
        v = rd(src)
        for k in range(n):
            wr(dst + k * unit, v)
    else:
        for k in range(n):
            wr(dst + k * unit, rd(src + k * unit))


def bios_checksum(cpu, bus):
    cpu.r[0] = 0xBAAE187F
    return 3


def _angle(raw: int) -> float:
    return (raw >> 8) / 128.0 * math.pi


def bg_affine_set(cpu, bus):
    src, dst, n = cpu.r[0], cpu.r[1], cpu.r[2]
    for _ in range(n):
        ox = _s32(bus.read32(src)) / 256.0
        oy = _s32(bus.read32(src + 4)) / 256.0
        cx = _s16(bus.read16(src + 8))
        cy = _s16(bus.read16(src + 10))
        sx = _s16(bus.read16(src + 12)) / 256.0
        sy = _s16(bus.read16(src + 14)) / 256.0
        th = _angle(bus.read16(src + 16))
        a = d = math.cos(th)
        b = c = math.sin(th)
        a *= sx
        b *= -sx
        c *= sy
        d *= sy
        rx = ox - (a * cx + b * cy)
        ry = oy - (c * cx + d * cy)
        bus.write16(dst, int(a * 256) & 0xFFFF)
        bus.write16(dst + 2, int(b * 256) & 0xFFFF)
        bus.write16(dst + 4, int(c * 256) & 0xFFFF)
        bus.write16(dst + 6, int(d * 256) & 0xFFFF)
        bus.write32(dst + 8, int(rx * 256) & M32)
        bus.write32(dst + 12, int(ry * 256) & M32)
        src += 20
        dst += 16
    return 20 * max(n, 1)


def obj_affine_set(cpu, bus):
    src, dst, n, stride = cpu.r[0], cpu.r[1], cpu.r[2], cpu.r[3]
    for _ in range(n):
        sx = _s16(bus.read16(src)) / 256.0
        sy = _s16(bus.read16(src + 2)) / 256.0
        th = _angle(bus.read16(src + 4))
        a = d = math.cos(th)
        b = c = math.sin(th)
        a *= sx
        b *= -sx
        c *= sy
        d *= sy
        bus.write16(dst, int(a * 256) & 0xFFFF)
        bus.write16(dst + stride, int(b * 256) & 0xFFFF)
        bus.write16(dst + stride * 2, int(c * 256) & 0xFFFF)
        bus.write16(dst + stride * 3, int(d * 256) & 0xFFFF)
        src += 8
        dst += stride * 4
    return 20 * max(n, 1)


def bit_unpack(cpu, bus):
    src, dst, info = cpu.r[0], cpu.r[1], cpu.r[2]
    length = bus.read16(info)
    sw = bus.read8(info + 2)
    dw = bus.read8(info + 3)
    off = bus.read32(info + 4)
    zero_flag = bool(off & 0x80000000)
    off &= 0x7FFFFFFF
    if sw not in (1, 2, 4, 8) or dw not in (1, 2, 4, 8, 16, 32):
        return 10
    out = 0
    bits = 0
    smask = (1 << sw) - 1
    for i in range(length):
        byte = bus.read8(src + i)
        for sh in range(0, 8, sw):
            v = (byte >> sh) & smask
            if v or zero_flag:
                v += off
            out |= (v & ((1 << dw) - 1)) << bits
            bits += dw
            if bits >= 32:
                bus.write32(dst, out & M32)
                dst += 4
                out = 0
                bits = 0
    return 10 + length * 8


def _header(bus, src):
    h = bus.read32(src)
    return h >> 8


def lz77(cpu, bus, vram: bool):
    src, dst = cpu.r[0], cpu.r[1]
    size = _header(bus, src)
    data = read_block(bus, src + 4, min(size * 2 + 16, 0x1000000))
    out = bytearray()
    i = 0
    try:
        while len(out) < size:
            flags = data[i]
            i += 1
            for _ in range(8):
                if len(out) >= size:
                    break
                if flags & 0x80:
                    b1, b2 = data[i], data[i + 1]
                    i += 2
                    n = (b1 >> 4) + 3
                    disp = (((b1 & 15) << 8) | b2) + 1
                    start = len(out) - disp
                    for k in range(n):
                        out.append(out[start + k] if start + k >= 0 else 0)
                else:
                    out.append(data[i])
                    i += 1
                flags <<= 1
    except IndexError:
        pass
    out = out[:size]
    if vram and len(out) & 1:
        out.append(0)
    write_block(bus, dst, bytes(out), 2 if vram else 1)
    return 20 + size * 2


def rl_uncomp(cpu, bus, vram: bool):
    src, dst = cpu.r[0], cpu.r[1]
    size = _header(bus, src)
    data = read_block(bus, src + 4, min(size + size // 2 + 16, 0x1000000))
    out = bytearray()
    i = 0
    try:
        while len(out) < size:
            f = data[i]
            i += 1
            if f & 0x80:
                out += bytes([data[i]]) * ((f & 0x7F) + 3)
                i += 1
            else:
                n = (f & 0x7F) + 1
                out += data[i:i + n]
                i += n
    except IndexError:
        pass
    out = out[:size]
    if vram and len(out) & 1:
        out.append(0)
    write_block(bus, dst, bytes(out), 2 if vram else 1)
    return 20 + size * 2


def huff_uncomp(cpu, bus):
    src, dst = cpu.r[0], cpu.r[1]
    h = bus.read32(src)
    bits = h & 15
    size = h >> 8
    tree_size = (bus.read8(src + 4) + 1) * 2
    tree = src + 5
    stream = src + 4 + tree_size
    out = 0
    nbits = 0
    written = 0
    node = tree
    while written < size:
        word = bus.read32(stream)
        stream += 4
        for b in range(31, -1, -1):
            n = bus.read8(node)
            nxt = (node & ~1) + ((n & 0x3F) + 1) * 2
            if word >> b & 1:
                leaf = n & 0x40
                node = nxt + 1
            else:
                leaf = n & 0x80
                node = nxt
            if leaf:
                out |= bus.read8(node) << nbits
                nbits += bits
                node = tree
                if nbits >= 32:
                    bus.write32(dst, out & M32)
                    dst += 4
                    written += 4
                    out = 0
                    nbits = 0
                    if written >= size:
                        break
    return 20 + size * 8


def diff_unfilter(cpu, bus, width: int, vram: bool):
    src, dst = cpu.r[0], cpu.r[1]
    size = _header(bus, src)
    data = read_block(bus, src + 4, size)
    out = bytearray(len(data))
    if width == 1:
        acc = 0
        for i, b in enumerate(data):
            acc = (acc + b) & 0xFF
            out[i] = acc
    else:
        acc = 0
        for i in range(0, len(data) - 1, 2):
            acc = (acc + (data[i] | (data[i + 1] << 8))) & 0xFFFF
            out[i] = acc & 0xFF
            out[i + 1] = acc >> 8
    write_block(bus, dst, bytes(out), 2 if (vram or width == 2) else 1)
    return 20 + size * 2


def sound_bias(cpu, bus):
    level = 0x200 if cpu.r[0] else 0
    bus.write16(0x04000088, (bus.read16(0x04000088) & ~0x3FF) | level)
    return 10


def midi_key2freq(cpu, bus):
    wa, mk, fp = cpu.r[0], cpu.r[1], cpu.r[2]
    freq = bus.read32(wa + 4)
    cpu.r[0] = int(freq / 2 ** ((180 - mk - fp / 256) / 12)) & M32
    return 20


def _nop(cpu, bus):
    return 3


SWI_TABLE = {
    0x00: soft_reset,
    0x01: register_ram_reset,
    0x02: halt,
    0x03: halt,
    0x04: lambda c, b: intr_wait(c, b, c.r[0], c.r[1] & 0xFFFF),
    0x05: lambda c, b: intr_wait(c, b, 1, 1),
    0x06: div,
    0x07: div_arm,
    0x08: sqrt,
    0x09: arctan,
    0x0A: arctan2,
    0x0B: cpu_set,
    0x0C: cpu_fast_set,
    0x0D: bios_checksum,
    0x0E: bg_affine_set,
    0x0F: obj_affine_set,
    0x10: bit_unpack,
    0x11: lambda c, b: lz77(c, b, False),
    0x12: lambda c, b: lz77(c, b, True),
    0x13: huff_uncomp,
    0x14: lambda c, b: rl_uncomp(c, b, False),
    0x15: lambda c, b: rl_uncomp(c, b, True),
    0x16: lambda c, b: diff_unfilter(c, b, 1, False),
    0x17: lambda c, b: diff_unfilter(c, b, 1, True),
    0x18: lambda c, b: diff_unfilter(c, b, 2, False),
    0x19: sound_bias,
    0x1F: midi_key2freq,
}


def hle_swi(cpu, n: int) -> int:
    cpu.bios_latch = BIOS_LATCH_SWI
    fn = SWI_TABLE.get(n, _nop)
    return fn(cpu, cpu.bus)
