"""2C02 PPU: scanline renderer with dot-exact register timing.

Time is counted in PPU dots (3 per CPU cycle). The PPU only catches up
(``sync(cpu_cycle)``) when the CPU touches a PPU register or the system loop
reaches a scanline boundary, and then processes the per-line events in order:

    visible line 0-239: dot 0 draw the line, 256 coarse/fine Y++, 257 copy X from t,
                        260 mapper scanline clock (MMC3 IRQ)
    line 241 dot 1:     vblank flag + NMI
    pre-render 261:     dot 1 clear flags, 256/257/260 as visible, 280 copy Y from t,
                        odd frames are one dot shorter while rendering

Each line is drawn whole at its dot 0 from the registers at that moment.
Writes in hblank or mid-line therefore take effect on the next line, which is
what raster-split games (status bars, parallax) need. Sprite-0 hit is computed
while drawing, and its flag rises at the exact dot of the overlapping pixel.

Speed: every nametable row (32 tiles x 8 pixels for one fine-Y) is decoded once
into palette indices and cached by its tile bytes, attributes, fine Y and the
CHR banks in use. A scrolled line is then two slices of cached rows, sprites
are bucketed per line once per OAM change, and colours go through byte
translate tables. The output is ``rgb``, 256x240x3 bytes.
"""
from __future__ import annotations

from src.core.nes.common import (
    DOTS_PER_LINE, HEIGHT, PALETTE, PRERENDER_LINE, VBLANK_LINE, VISIBLE_LINES, WIDTH,
)

# 16-bit (hi << 8 | lo) bitplane pair -> 8 pixel values 0-3, left to right
_DECODE: list[bytes] = []
for _w in range(65536):
    _lo, _hi = _w & 0xFF, _w >> 8
    _DECODE.append(bytes((((_lo >> (7 - b)) & 1) | (((_hi >> (7 - b)) & 1) << 1)) for b in range(8)))
_DECODE_REV = [d[::-1] for d in _DECODE]
# attribute palette p: pixel 0 stays 0 (backdrop), 1-3 -> p*4 + pixel
_PAL_T = [bytes((0 if v == 0 else (p * 4 + v)) if v < 4 else 0 for v in range(256)) for p in range(4)]
_BLANK_LINE = bytes(WIDTH)
_ROW_CACHE_MAX = 8192
NMI_OFF_WINDOW = 1                    # dots after vblank start where disabling NMI still cancels it
NMI_ON_MARGIN = 1                     # dots before the vblank clear where enabling NMI is too late


def _inc_y(v: int) -> int:
    if (v & 0x7000) != 0x7000:
        return v + 0x1000
    v &= ~0x7000
    y = (v >> 5) & 0x1F
    if y == 29:
        y = 0
        v ^= 0x0800
    elif y == 31:
        y = 0
    else:
        y += 1
    return (v & ~0x03E0) | (y << 5)


class PPU:
    def __init__(self, mapper, chr_mem: bytearray, chr_ram: bool):
        self.mapper = mapper
        self.chr = chr_mem
        self.chr_ram = chr_ram
        self.chr_banks = mapper.chr_banks
        self.nt = mapper.nt
        self.ciram = bytearray(0x1000)        # 2 KB on the console; 4 KB for four-screen carts
        self.palette = bytearray(32)
        self.oam = bytearray(256)
        self.rgb = bytearray(WIDTH * HEIGHT * 3)
        self.ctrl = self.mask = self.status = 0
        self.oam_addr = 0
        self.v = self.t = 0
        self.fine_x = 0
        self.w = 0
        self.buffer = 0                       # $2007 read buffer
        self.bus_latch = 0                    # open bus
        self.frame = 0
        self.odd = False
        self.line = 0
        self.line_start = 0                   # absolute dot of the current line's dot 0
        self.ev = 0                           # index of the next event in this line
        self.s0_at = 0                        # absolute dot where sprite-0 hit rises (0 = none)
        self.nmi = None                       # callable() raising the CPU's NMI
        self.skip_render = False              # frameskip: events and sprite-0 only
        self._rows: dict = {}
        self._chr_gen = 0                     # bumps on CHR-RAM writes
        self._colour = None                   # (r, g, b) translate tables for palette RAM
        self._oam_key = None
        self._spr_lines = None
        self.lines_drawn = 0
        self._a12_level = 0
        self.now = 0                          # dot the PPU was last synced to
        self.nmi_cancel = None                # callable() withdrawing a just-raised NMI
        self._suppress_vbl = False
        self.nmi_dot = 0                      # absolute dot of the last vblank NMI

    # ------------------------------------------------------------ timing
    def _events(self, line: int) -> tuple:
        if line < VISIBLE_LINES:
            return (0, 256, 257, 260, 324)
        if line == VBLANK_LINE:
            return (1,)
        if line == PRERENDER_LINE:
            return (1, 256, 257, 260, 280, 324)
        return ()

    def a12_dot(self) -> int:
        """Dot where PPU address line A12 rises each rendered line (MMC3 IRQ clock):
        260 when sprites fetch from $1000, 324 when only the background does, 0 = never."""
        c = self.ctrl
        spr_hi = c & 0x20 or c & 0x08
        bg_hi = c & 0x10
        if spr_hi and not bg_hi:
            return 260
        if bg_hi and not spr_hi:
            return 324
        return 0

    def _a12(self, addr: int) -> None:
        """A12 seen on the address bus outside rendering ($2006 / $2007)."""
        hi = addr & 0x1000
        if hi and not self._a12_level and self.mapper.wants_scanlines:
            self.mapper.scanline()
        self._a12_level = hi

    def _line_len(self, line: int) -> int:
        if line == PRERENDER_LINE and self.odd and self.mask & 0x18:
            return DOTS_PER_LINE - 1
        return DOTS_PER_LINE

    def sync(self, cpu_cycle: int) -> None:
        """Process every PPU event up to and including dot cpu_cycle * 3."""
        target = cpu_cycle * 3
        self.now = target
        while True:
            line = self.line
            evs = self._events(line)
            while self.ev < len(evs):
                at = self.line_start + evs[self.ev]
                if at > target:
                    self._s0_check(target)
                    return
                self._event(line, evs[self.ev])
                self.ev += 1
            end = self.line_start + self._line_len(line)
            if end > target:
                self._s0_check(target)
                return
            self.line_start = end
            self.ev = 0
            line += 1
            if line == 262:
                line = 0
                self.odd = not self.odd
            self.line = line

    def next_line_cycle(self) -> int:
        """CPU cycle at which the next line starts (for the system loop)."""
        return (self.line_start + self._line_len(self.line)) // 3 + 1

    def _s0_check(self, target: int) -> None:
        if self.s0_at and target >= self.s0_at:
            self.status |= 0x40
            self.s0_at = 0

    def _event(self, line: int, dot: int) -> None:
        rendering = self.mask & 0x18
        if dot == 0:
            if self.s0_at:                    # hit on the previous line not yet seen
                self.status |= 0x40
                self.s0_at = 0
            self._draw(line, rendering)
        elif dot == 256:
            if rendering:
                self.v = _inc_y(self.v)
        elif dot == 257:
            if rendering:
                self.v = (self.v & ~0x041F) | (self.t & 0x041F)
        elif dot == 260 or dot == 324:
            if rendering and self.mapper.wants_scanlines and self.a12_dot() == dot:
                self.mapper.scanline()
        elif dot == 280:
            if rendering:
                self.v = (self.v & ~0x7BE0) | (self.t & 0x7BE0)
        elif dot == 1:
            if line == VBLANK_LINE:
                self.frame += 1
                if self._suppress_vbl:            # $2002 read one dot early: no flag, no NMI
                    self._suppress_vbl = False
                else:
                    self.status |= 0x80
                    if self.ctrl & 0x80 and self.nmi is not None:
                        self.nmi_dot = self.line_start + 1
                        self.nmi()
            else:                             # pre-render: clear vblank, sprite 0, overflow
                self.status &= 0x1F
                self.s0_at = 0

    # ------------------------------------------------------------ registers (CPU side)
    def read_reg(self, reg: int) -> int:
        if reg == 2:
            if self.line == VBLANK_LINE:          # race with the vblank flag (dot exact)
                d = self.now - self.line_start
                if d == 0:
                    self._suppress_vbl = True
                elif d <= 2 and self.nmi_cancel is not None:
                    self.nmi_cancel()
            st = self.status
            self.status &= 0x7F
            self.w = 0
            val = (st & 0xE0) | (self.bus_latch & 0x1F)
        elif reg == 4:
            val = self.oam[self.oam_addr]
        elif reg == 7:
            addr = self.v & 0x3FFF
            if addr >= 0x3F00:
                val = self.palette[self._pal_index(addr)]
                if self.mask & 0x01:
                    val &= 0x30
                self.buffer = self._vram_read(addr - 0x1000)
            else:
                val = self.buffer
                self.buffer = self._vram_read(addr)
            self.v = (self.v + (32 if self.ctrl & 0x04 else 1)) & 0x7FFF
            self._a12(self.v)
        else:
            val = self.bus_latch
        self.bus_latch = val
        return val

    def write_reg(self, reg: int, val: int) -> bool:
        """Returns True when the write raised an NMI (CPU must stop and take it)."""
        self.bus_latch = val
        if reg == 0:
            was = self.ctrl & 0x80
            self.ctrl = val
            if was and not val & 0x80 and 0 <= self.now - self.nmi_dot <= NMI_OFF_WINDOW                     and self.nmi_cancel is not None:
                self.nmi_cancel()             # NMI disabled before the CPU sampled it
            self.t = (self.t & ~0x0C00) | ((val & 3) << 10)
            if not was and val & 0x80 and self.status & 0x80 and self.nmi is not None:
                # the flag is cleared at pre-render dot 1: a write landing within a
                # dot of that no longer sees vblank
                if self.line == PRERENDER_LINE - 1:
                    clear_at = self.line_start + DOTS_PER_LINE + 1
                elif self.line == PRERENDER_LINE:
                    clear_at = self.line_start + 1
                else:
                    clear_at = 1 << 62
                if clear_at - self.now > NMI_ON_MARGIN:
                    self.nmi()
                    return True
        elif reg == 1:
            if (val ^ self.mask) & 0x01:
                self._colour = None
            self.mask = val
        elif reg == 3:
            self.oam_addr = val
        elif reg == 4:
            self.oam[self.oam_addr] = val
            self.oam_addr = (self.oam_addr + 1) & 0xFF
        elif reg == 5:
            if self.w == 0:
                self.t = (self.t & ~0x001F) | (val >> 3)
                self.fine_x = val & 7
                self.w = 1
            else:
                self.t = (self.t & ~0x73E0) | ((val & 0x07) << 12) | ((val & 0xF8) << 2)
                self.w = 0
        elif reg == 6:
            if self.w == 0:
                self.t = (self.t & 0x00FF) | ((val & 0x3F) << 8)
                self.w = 1
            else:
                self.t = (self.t & 0x7F00) | val
                self.v = self.t
                self.w = 0
                if not self.mask & 0x18 or self.line >= VISIBLE_LINES and self.line != PRERENDER_LINE:
                    self._a12(self.v)
        elif reg == 7:
            self._vram_write(self.v & 0x3FFF, val)
            self.v = (self.v + (32 if self.ctrl & 0x04 else 1)) & 0x7FFF
            self._a12(self.v)
        return False

    def oam_dma(self, data: bytes) -> None:
        a = self.oam_addr
        if a == 0:
            self.oam[:] = data
        else:
            for k in range(256):
                self.oam[(a + k) & 0xFF] = data[k]

    # ------------------------------------------------------------ VRAM
    @staticmethod
    def _pal_index(addr: int) -> int:
        i = addr & 0x1F
        if i & 0x13 == 0x10:                  # $3F10/14/18/1C mirror $3F00/04/08/0C
            i &= 0x0F
        return i

    def _vram_read(self, addr: int) -> int:
        addr &= 0x3FFF
        if addr < 0x2000:
            return self.chr[self.chr_banks[addr >> 10] + (addr & 0x3FF)]
        if addr < 0x3F00:
            return self.ciram[self.nt[(addr >> 10) & 3] + (addr & 0x3FF)]
        return self.palette[self._pal_index(addr)]

    def _vram_write(self, addr: int, val: int) -> None:
        if addr < 0x2000:
            if self.chr_ram:
                self.chr[self.chr_banks[addr >> 10] + (addr & 0x3FF)] = val
                self._chr_gen += 1
        elif addr < 0x3F00:
            self.ciram[self.nt[(addr >> 10) & 3] + (addr & 0x3FF)] = val
        else:
            self.palette[self._pal_index(addr)] = val & 0x3F
            self._colour = None

    # ------------------------------------------------------------ drawing
    def _colour_tables(self):
        t = self._colour
        if t is None:
            pal = self.palette
            grey = 0x30 if self.mask & 0x01 else 0x3F
            cols = []
            for idx in range(256):
                i = idx & 0x1F
                if idx >= 32 or (i & 3) == 0:          # transparent -> universal backdrop
                    i = 0
                cols.append(PALETTE[pal[i] & grey])
            t = self._colour = tuple(bytes(c[ch] for c in cols) for ch in range(3))
        return t

    def _row(self, nt_slot: int, coarse_y: int, fine_y: int, base: int) -> bytes:
        """256 palette indices of one nametable row at one fine Y (cached)."""
        ciram = self.ciram
        off = self.nt[nt_slot]
        row_off = off + coarse_y * 32
        tiles = bytes(ciram[row_off:row_off + 32])
        attrs = bytes(ciram[off + 0x3C0 + (coarse_y >> 2) * 8: off + 0x3C0 + (coarse_y >> 2) * 8 + 8])
        bank0 = base >> 10
        banks = self.chr_banks
        key = (tiles, attrs, fine_y | ((coarse_y & 2) << 2) | (base >> 8),
               banks[bank0], banks[bank0 + 1], banks[bank0 + 2], banks[bank0 + 3], self._chr_gen)
        rows = self._rows
        r = rows.get(key)
        if r is not None:
            return r
        chr_mem = self.chr
        shift_y = (coarse_y & 2) << 1
        parts = []
        for cx in range(32):
            a = base + tiles[cx] * 16 + fine_y
            p = chr_mem[banks[a >> 10] + (a & 0x3FF)]
            q = chr_mem[banks[(a + 8) >> 10] + ((a + 8) & 0x3FF)]
            pal = (attrs[cx >> 2] >> (shift_y | (cx & 2))) & 3
            parts.append(_DECODE[(q << 8) | p].translate(_PAL_T[pal]))
        r = b"".join(parts)
        if len(rows) >= _ROW_CACHE_MAX:
            rows.clear()
        rows[key] = r
        return r

    def _bg_line(self) -> bytes:
        v = self.v
        coarse_x = v & 0x1F
        coarse_y = (v >> 5) & 0x1F
        fine_y = (v >> 12) & 7
        nt_slot = (v >> 10) & 3
        base = 0x1000 if self.ctrl & 0x10 else 0
        if coarse_y >= 30:                    # rows 30/31 are attribute bytes read as tiles
            return self._attr_row_line(nt_slot, coarse_y, fine_y, base, coarse_x)
        left = self._row(nt_slot, coarse_y, fine_y, base)
        right = self._row(nt_slot ^ 1, coarse_y, fine_y, base)
        start = coarse_x * 8 + self.fine_x
        both = left + right
        return both[start:start + WIDTH]

    def _attr_row_line(self, nt_slot, coarse_y, fine_y, base, coarse_x) -> bytes:
        # rare: scrolled into the attribute table (some games briefly do this)
        out = []
        chr_mem, banks, ciram = self.chr, self.chr_banks, self.ciram
        for k in range(33):
            cx = (coarse_x + k) & 31
            slot = nt_slot ^ (1 if coarse_x + k >= 32 else 0)
            off = self.nt[slot]
            tile = ciram[off + coarse_y * 32 + cx]
            a = base + tile * 16 + fine_y
            p = chr_mem[banks[a >> 10] + (a & 0x3FF)]
            q = chr_mem[banks[(a + 8) >> 10] + ((a + 8) & 0x3FF)]
            out.append(_DECODE[(q << 8) | p])
        line = b"".join(out)
        return line[self.fine_x:self.fine_x + WIDTH]

    def _sprite_lines(self, tall: bool):
        lines = [[] for _ in range(VISIBLE_LINES)]
        h = 16 if tall else 8
        oam = self.oam
        for n in range(64):
            y = oam[n * 4] + 1
            if y >= VISIBLE_LINES:
                continue
            for yy in range(y, min(y + h, VISIBLE_LINES)):
                bucket = lines[yy]
                if len(bucket) < 8:
                    bucket.append(n)
                elif len(bucket) == 8:
                    bucket.append(-1)          # overflow marker
        return lines

    def _draw(self, y: int, rendering: int) -> None:
        if y >= VISIBLE_LINES:
            return
        mask = self.mask
        show_bg = mask & 0x08
        show_spr = mask & 0x10
        bucket = None
        if show_spr:
            tall = bool(self.ctrl & 0x20)
            key = (bytes(self.oam), tall)
            if key != self._oam_key:
                self._oam_key = key
                self._spr_lines = self._sprite_lines(tall)
            bucket = self._spr_lines[y]
        if self.skip_render:
            # frameskip: the only observable effect of drawing is sprite-0 hit
            if not (show_bg and bucket and bucket[0] == 0):
                return
            bucket = bucket[:1]
        bg = self._bg_line() if show_bg else _BLANK_LINE
        if show_bg and not mask & 0x02:
            bg = _BLANK_LINE[:8] + bg[8:]
        line = bg
        if bucket:
            line = self._sprites(y, bucket, bg, tall, mask)
        if self.skip_render:
            return
        self.lines_drawn += 1
        r, g, b = self._colour_tables()
        off = y * WIDTH * 3
        seg = bytearray(WIDTH * 3)
        seg[0::3] = line.translate(r)
        seg[1::3] = line.translate(g)
        seg[2::3] = line.translate(b)
        self.rgb[off:off + WIDTH * 3] = seg

    def _sprites(self, y: int, bucket, bg: bytes, tall: bool, mask: int) -> bytes:
        oam = self.oam
        chr_mem, banks = self.chr, self.chr_banks
        out = bytearray(bg)
        taken = bytearray(WIDTH)               # sprite pixel already decided (front-most wins)
        left_clip = 0 if mask & 0x04 else 8
        bg_left = 0 if mask & 0x02 else 8
        s0_x = -1
        for n in bucket:
            if n < 0:
                self.status |= 0x20           # sprite overflow (simplified: >8 on a line)
                break
            o = n * 4
            row = y - (oam[o] + 1)
            tile = oam[o + 1]
            attr = oam[o + 2]
            sx = oam[o + 3]
            if attr & 0x80:                    # vertical flip
                row = (15 if tall else 7) - row
            if tall:
                table = (tile & 1) * 0x1000
                tile &= 0xFE
                if row >= 8:
                    tile += 1
                    row -= 8
            else:
                table = 0x1000 if self.ctrl & 0x08 else 0
            a = table + tile * 16 + row
            p = chr_mem[banks[a >> 10] + (a & 0x3FF)]
            q = chr_mem[banks[(a + 8) >> 10] + ((a + 8) & 0x3FF)]
            if not (p | q):
                continue
            px = (_DECODE_REV if attr & 0x40 else _DECODE)[(q << 8) | p]
            pal = 16 + (attr & 3) * 4
            behind = attr & 0x20
            for k in range(8):
                v = px[k]
                if not v:
                    continue
                x = sx + k
                if x >= WIDTH or x < left_clip:
                    continue
                if n == 0 and s0_x < 0 and x < 255 and x >= bg_left and bg[x] & 3:
                    s0_x = x
                if taken[x]:
                    continue
                taken[x] = 1
                if behind and bg[x] & 3:
                    continue
                out[x] = pal + v
        if s0_x >= 0 and not self.status & 0x40:
            self.s0_at = self.line_start + s0_x + 2
        return bytes(out)
