"""GBA PPU: scanline timing, IRQ/DMA triggers and line rendering.

The framebuffer `fb` is 240x160 little-endian BGR555 (bytearray, 2 bytes per
pixel) so the front end can hand it to SDL with 15-bit masks.

Rendering is vectorised per scanline: every layer becomes a 240-byte string of
palette indices (0 = transparent). Layers are combined back-to-front by
priority with big-integer masks (one byte lane per pixel), so priority,
windows and the "top two layers" needed for blending are all C-speed
operations. Alpha / brightness effects run lane-wise on packed integers.
Only affine layers and sprites are touched per pixel.
Mosaic (REG 0400004C) is applied to each layer's pixel data as it is produced:
each X-group repeats its leftmost pixel, BG Y-groups repeat the group's
topmost line (OBJ mosaic is horizontal only).
"""
from src.core.gba.common import (
    WIDTH, HEIGHT, LINE_CYCLES, HBLANK_START, TOTAL_LINES,
    REG_DISPCNT, REG_DISPSTAT, REG_VCOUNT, REG_BG0CNT, REG_BG0HOFS, REG_BG2PA,
    REG_BG2X, REG_BG2Y, REG_BG3X, REG_BG3Y, REG_WIN0H, REG_WIN1H, REG_WIN0V, REG_WIN1V,
    REG_WININ, REG_WINOUT, REG_MOSAIC, REG_BLDCNT, REG_BLDALPHA, REG_BLDY,
    STAT_VBLANK, STAT_HBLANK, STAT_VCOUNT, STAT_VBLANK_IRQ, STAT_HBLANK_IRQ, STAT_VCOUNT_IRQ,
    IRQ_VBLANK, IRQ_HBLANK, IRQ_VCOUNT, DMA_TIMING_VBLANK, DMA_TIMING_HBLANK,
    TEXT_BG_SIZE, OBJ_SIZE,
)

WHITE_LINE = b"\xff\x7f" * WIDTH
W = WIDTH
FULL = (1 << (8 * W)) - 1
ONES = int.from_bytes(b"\x01" * W, "little")
NZ = bytes([0] + [0xFF] * 255)                       # byte != 0 -> 0xFF
GT = [bytes(0xFF if v > p else 0 for v in range(256)) for p in range(4)]   # prio lane > p
EQ = [bytes(0xFF if v == p else 0 for v in range(256)) for p in range(4)]
ID_BACKDROP = 5
ID_NONE = 6
ID_OBJ = 4
IS_OBJ = bytes(0xFF if v == ID_OBJ else 0 for v in range(256))
# 16-bit lane helpers for blending
L16 = int.from_bytes(b"\xff\x0f" * W, "little")       # 0x0FFF per lane
C31 = int.from_bytes((0x8000 - 32).to_bytes(2, "little") * W, "little")
H15 = int.from_bytes(b"\x00\x80" * W, "little")
LANE31 = int.from_bytes(b"\x1f\x00" * W, "little")
R_T = bytes(v & 31 for v in range(256))               # lo byte -> red
GL_T = bytes(v >> 5 for v in range(256))              # lo byte -> green bits 0-2
GH_T = bytes((v & 3) << 3 for v in range(256))        # hi byte -> green bits 3-4
B_T = bytes((v >> 2) & 31 for v in range(256))        # hi byte -> blue
PACK_GLO = bytes(((v & 7) << 5) & 0xFF for v in range(256))
PACK_GHI = bytes((v >> 3) & 3 for v in range(256))
PACK_BHI = bytes((v & 31) << 2 for v in range(256))


def _s28(v: int) -> int:
    v &= 0x0FFFFFFF
    return v - (1 << 28) if v & 0x08000000 else v


def _s16(v: int) -> int:
    return (v ^ 0x8000) - 0x8000


def _i(b) -> int:
    return int.from_bytes(b, "little")


def _b(n: int, size: int = W) -> bytes:
    return n.to_bytes(size, "little")


def _expand(b: bytes) -> int:
    ba = bytearray(2 * len(b))
    ba[0::2] = b
    return int.from_bytes(ba, "little")


def _compress(n: int) -> bytes:
    return n.to_bytes(2 * W, "little")[0::2]


def _range_mask(x1: int, x2: int) -> int:
    """Window horizontal range [x1, x2) as a lane mask (x1 > x2 wraps)."""
    x1 = min(x1, W)
    x2 = min(x2, W)
    if x1 <= x2:
        b = b"\x00" * x1 + b"\xff" * (x2 - x1) + b"\x00" * (W - x2)
    else:
        b = b"\xff" * x2 + b"\x00" * (x1 - x2) + b"\xff" * (W - x1)
    return _i(b)


_TARGETS = {}
_BRIGHT = {}


def _target_tables(bldcnt: int):
    t = _TARGETS.get(bldcnt)
    if t is None:
        t = _TARGETS[bldcnt] = (
            bytes(0xFF if (k <= 5 and bldcnt >> k & 1) else 0 for k in range(256)),
            bytes(0xFF if (k <= 5 and bldcnt >> (8 + k) & 1) else 0 for k in range(256)))
    return t


def _bright_table(bmode: int, evy: int) -> bytes:
    t = _BRIGHT.get((bmode, evy))
    if t is None:
        if bmode == 2:
            t = bytes(min(31, c + ((31 - c) * evy >> 4)) if c < 32 else c for c in range(256))
        else:
            t = bytes(max(0, c - (c * evy >> 4)) if c < 32 else c for c in range(256))
        _BRIGHT[(bmode, evy)] = t
    return t


def _in_range(v: int, a: int, b: int) -> bool:
    return a <= v < b if a <= b else (v >= a or v < b)


def _mosaic_x(line: bytes, size: int, x0: int = 0) -> bytes:
    """Repeat each X-group's leftmost pixel across the whole group.

    Groups are aligned to the left screen edge; `x0` is the screen x of
    line[0]. Group leaders left of the segment give transparent pixels
    (hardware snaps the sample coordinate to the group start)."""
    if size <= 1:
        return line
    out = bytearray(len(line))
    for i in range(len(line)):
        j = (x0 + i) - (x0 + i) % size - x0
        out[i] = line[j] if j >= 0 else 0
    return bytes(out)


class PPU:
    def __init__(self, bus):
        self.bus = bus
        self.vcount = 0
        self.stat = 0
        self.in_hblank = False
        self.next_event = HBLANK_START
        self.frame = 0
        self.skip_render = False        # frameskip: run the timing, don't draw lines
        self.fb = bytearray(WIDTH * HEIGHT * 2)
        self.ref_x = [0, 0]             # internal affine reference points (BG2, BG3)
        self.ref_y = [0, 0]
        self.dma = None
        self._row4 = {}
        self._chan_cache = {}
        self._oam_snap = None
        self._obj_lines = None
        bus.ppu = self
        bus.io_read[REG_DISPSTAT] = lambda: self.stat
        bus.io_write[REG_DISPSTAT] = self._w_stat
        bus.io_read[REG_VCOUNT] = lambda: self.vcount
        bus.io_write[REG_VCOUNT] = lambda v, m: None
        for k, off in enumerate((REG_BG2X, REG_BG2Y, REG_BG3X, REG_BG3Y)):
            for half in (0, 2):
                bus.io_write[off + half] = (lambda v, m, o=off + half, k=k: self._w_ref(o, v, m, k))

    # ------------------------------------------------------------ registers
    def _w_stat(self, v: int, m: int) -> None:
        m &= 0xFF38
        self.stat = (self.stat & ~m) | (v & m)
        self._check_vcount()

    def _w_ref(self, off: int, v: int, m: int, k: int) -> None:
        io16 = self.bus.io16
        io16[off >> 1] = (io16[off >> 1] & ~m) | v
        self._latch_ref(k >> 1)

    def _reg32(self, off: int) -> int:
        io16 = self.bus.io16
        return io16[off >> 1] | (io16[(off >> 1) + 1] << 16)

    def _latch_ref(self, bg: int) -> None:
        xo, yo = (REG_BG2X, REG_BG2Y) if bg == 0 else (REG_BG3X, REG_BG3Y)
        self.ref_x[bg] = _s28(self._reg32(xo))
        self.ref_y[bg] = _s28(self._reg32(yo))

    def _check_vcount(self) -> None:
        if self.vcount == self.stat >> 8:
            if not self.stat & STAT_VCOUNT:
                self.stat |= STAT_VCOUNT
                if self.stat & STAT_VCOUNT_IRQ:
                    self.bus.request_irq(IRQ_VCOUNT)
        else:
            self.stat &= ~STAT_VCOUNT

    # ------------------------------------------------------------ timing
    def event(self) -> None:
        t = self.next_event
        bus = self.bus
        if not self.in_hblank:
            y = self.vcount
            if y < HEIGHT:
                if self.skip_render:
                    self._advance_ref()          # keep affine BG state as if drawn
                else:
                    self.render_line(y)
            self.in_hblank = True
            self.stat |= STAT_HBLANK
            if self.stat & STAT_HBLANK_IRQ:
                bus.request_irq(IRQ_HBLANK)
            if y < HEIGHT and self.dma is not None:
                self.dma.trigger(DMA_TIMING_HBLANK)
            self.next_event = t + (LINE_CYCLES - HBLANK_START)
            return
        self.in_hblank = False
        self.stat &= ~STAT_HBLANK
        y = self.vcount + 1
        if y == TOTAL_LINES:
            y = 0
        self.vcount = y
        if y == HEIGHT:
            self.stat |= STAT_VBLANK
            if self.stat & STAT_VBLANK_IRQ:
                bus.request_irq(IRQ_VBLANK)
            if self.dma is not None:
                self.dma.trigger(DMA_TIMING_VBLANK)
            self._latch_ref(0)
            self._latch_ref(1)
            self.frame += 1
        elif y == TOTAL_LINES - 1:
            self.stat &= ~STAT_VBLANK
        self._check_vcount()
        self.next_event = t + HBLANK_START

    def _advance_ref(self) -> None:
        io16 = self.bus.io16
        for bg in (0, 1):
            base = (REG_BG2PA >> 1) + 8 * bg
            self.ref_x[bg] += _s16(io16[base + 1])     # PB (dmx)
            self.ref_y[bg] += _s16(io16[base + 3])     # PD (dmy)

    # ------------------------------------------------------------ mosaic
    def _mosaic_bg_y(self, y: int, cnt: int) -> int:
        """Screen y snapped to the first line of its BG-mosaic Y group."""
        if cnt & 0x40:
            y -= y % ((self.bus.io16[REG_MOSAIC >> 1] >> 4 & 0xF) + 1)
        return y

    def _mosaic_refs(self, bg: int, y: int):
        """BGn affine reference points stepped back to the first line of the
        BG-mosaic Y group (identity when the layer has mosaic disabled)."""
        io16 = self.bus.io16
        k = bg - 2
        x, yv = self.ref_x[k], self.ref_y[k]
        if io16[(REG_BG0CNT >> 1) + bg] & 0x40:
            d = y % ((io16[REG_MOSAIC >> 1] >> 4 & 0xF) + 1)
            if d:
                base = (REG_BG2PA >> 1) + 8 * k
                x -= d * _s16(io16[base + 1])         # back up PB per line
                yv -= d * _s16(io16[base + 3])        # back up PD per line
        return x, yv

    # ------------------------------------------------------------ palette tables
    def _channels(self):
        """Per-palette-content tables: (bg_lo, bg_hi, obj_lo, obj_hi)."""
        pal = self.bus.pal
        key = bytes(pal)
        t = self._chan_cache.get(key)
        if t is None:
            if len(self._chan_cache) > 256:
                self._chan_cache.clear()
            t = (bytes(pal[0:512:2]), bytes(pal[1:512:2]),
                 bytes(pal[512:1024:2]), bytes(pal[513:1024:2]))
            self._chan_cache[key] = t
        return t

    # ------------------------------------------------------------ line
    def render_line(self, y: int) -> None:
        bus = self.bus
        io16 = bus.io16
        dispcnt = io16[REG_DISPCNT >> 1]
        off = y * W * 2
        fb = self.fb
        if dispcnt & 0x80:
            fb[off:off + W * 2] = WHITE_LINE
            self._advance_ref()
            return
        mode = dispcnt & 7
        layers = []                       # (prio, bg, idx bytes)
        direct = None                     # mode 3/5 BG2 colour line (lo, hi bytes, prio)
        bg_mx = (io16[REG_MOSAIC >> 1] & 0xF) + 1      # BG mosaic X group size

        def mos(line, cnt):
            return _mosaic_x(line, bg_mx) if cnt & 0x40 else line

        if mode == 0:
            for bg in range(4):
                if dispcnt & (0x100 << bg):
                    cnt = io16[(REG_BG0CNT >> 1) + bg]
                    layers.append((cnt & 3, bg, mos(self._text_bg(bg, y), cnt)))
        elif mode == 1:
            for bg in (0, 1):
                if dispcnt & (0x100 << bg):
                    cnt = io16[(REG_BG0CNT >> 1) + bg]
                    layers.append((cnt & 3, bg, mos(self._text_bg(bg, y), cnt)))
            if dispcnt & 0x400:
                cnt = io16[(REG_BG0CNT >> 1) + 2]
                layers.append((cnt & 3, 2, mos(self._affine_bg(2, y), cnt)))
        elif mode == 2:
            for bg in (2, 3):
                if dispcnt & (0x100 << bg):
                    cnt = io16[(REG_BG0CNT >> 1) + bg]
                    layers.append((cnt & 3, bg, mos(self._affine_bg(bg, y), cnt)))
        elif dispcnt & 0x400:
            cnt = io16[(REG_BG0CNT >> 1) + 2]
            if mode == 4:
                layers.append((cnt & 3, 2, mos(self._bitmap4(dispcnt, y), cnt)))
            elif mode in (3, 5):
                lo, hi, opq = self._bitmap_direct(mode, dispcnt, y)
                if cnt & 0x40:
                    lo, hi, opq = mos(lo, cnt), mos(hi, cnt), mos(opq, cnt)
                direct = (lo, hi, opq, cnt & 3)

        obj = self._render_objs(y, mode, dispcnt) if dispcnt & 0x1000 else None
        bg_lo, bg_hi, obj_lo, obj_hi = self._channels()

        # windows -> per-layer enable masks (bits 0-3 BG, 4 OBJ, 5 effects)
        enable = None
        if dispcnt & 0xE000:
            enable = self._window_masks(y, dispcnt, obj)

        bldcnt = io16[REG_BLDCNT >> 1]
        bmode = (bldcnt >> 6) & 3
        semi_any = obj is not None and obj[3]
        effects = bmode != 0 or semi_any

        if direct is not None:
            self._compose_direct(off, direct, obj, obj_lo, obj_hi, enable)
            self._advance_ref()
            return

        # painter order: priority 3 -> 0; at equal priority BG3..BG0, then OBJ on top
        layers.sort(key=lambda t: (-t[0], -t[1]))       # back to front
        order = []
        li = 0
        for p in (3, 2, 1, 0):
            while li < len(layers) and layers[li][0] == p:
                _, bg, idx = layers[li]
                order.append((bg, int.from_bytes(idx, "little"), idx))
                li += 1
            if obj is not None:
                order.append((ID_OBJ, p, None))

        if not effects:
            top = 0
            top_obj = 0
            for lid, val, idx in order:
                if lid == ID_OBJ:
                    plane = obj[0]
                    pm = _i(obj[1].translate(EQ[val]))
                    L = _i(plane) & pm
                    m = _i(plane.translate(NZ)) & pm
                else:
                    L = val
                    m = _i(idx.translate(NZ))
                if enable is not None:
                    m &= enable[lid]
                if not m:
                    continue
                nm = FULL ^ m
                top = (L & m) | (top & nm)
                if lid == ID_OBJ:
                    top_obj |= m
                else:
                    top_obj &= nm
            tb = _b(top)
            lo = _i(tb.translate(bg_lo))
            hi = _i(tb.translate(bg_hi))
            if top_obj:
                nm = FULL ^ top_obj
                lo = (_i(tb.translate(obj_lo)) & top_obj) | (lo & nm)
                hi = (_i(tb.translate(obj_hi)) & top_obj) | (hi & nm)
            fb[off:off + 2 * W:2] = _b(lo)
            fb[off + 1:off + 2 * W:2] = _b(hi)
            self._advance_ref()
            return

        # --- with blending: track the top two layers per pixel
        top = 0
        top_id = ID_BACKDROP * ONES
        sec = 0
        sec_id = ID_NONE * ONES
        top_semi = 0
        for lid, val, idx in order:
            if lid == ID_OBJ:
                plane = obj[0]
                pm = _i(obj[1].translate(EQ[val]))
                L = _i(plane) & pm
                m = _i(plane.translate(NZ)) & pm
            else:
                L = val
                m = _i(idx.translate(NZ))
            if enable is not None:
                m &= enable[lid]
            if not m:
                continue
            nm = FULL ^ m
            sec = (top & m) | (sec & nm)
            sec_id = (top_id & m) | (sec_id & nm)
            top = (L & m) | (top & nm)
            top_id = ((lid * ONES) & m) | (top_id & nm)
            if lid == ID_OBJ:
                top_semi = (_i(obj[2]) & m) | (top_semi & nm)
            else:
                top_semi &= nm

        tb, tib = _b(top), _b(top_id)
        sb, sib = _b(sec), _b(sec_id)
        tobj = _i(tib.translate(IS_OBJ))
        sobj = _i(sib.translate(IS_OBJ))

        def colour(idx_b, objm):
            lo = _i(idx_b.translate(bg_lo))
            hi = _i(idx_b.translate(bg_hi))
            if objm:
                nm = FULL ^ objm
                lo = (_i(idx_b.translate(obj_lo)) & objm) | (lo & nm)
                hi = (_i(idx_b.translate(obj_hi)) & objm) | (hi & nm)
            return _b(lo), _b(hi)

        tlo, thi = colour(tb, tobj)
        first_t, second_t = _target_tables(bldcnt)
        first = _i(tib.translate(first_t))
        second = _i(sib.translate(second_t))
        eff = enable[5] if enable is not None else FULL
        alpha = (top_semi | (first if bmode == 1 else 0)) & second & eff
        bright = (first & eff & (FULL ^ alpha)) if bmode in (2, 3) else 0
        if not alpha and not bright:
            fb[off:off + 2 * W:2] = tlo
            fb[off + 1:off + 2 * W:2] = thi
            self._advance_ref()
            return

        tr = tlo.translate(R_T)
        tg = _b(_i(tlo.translate(GL_T)) | _i(thi.translate(GH_T)))
        tbl = thi.translate(B_T)
        out = [_i(tr), _i(tg), _i(tbl)]
        if alpha:
            slo, shi = colour(sb, sobj)
            sr = slo.translate(R_T)
            sg = _b(_i(slo.translate(GL_T)) | _i(shi.translate(GH_T)))
            sbl = shi.translate(B_T)
            bld = io16[REG_BLDALPHA >> 1]
            eva = min(bld & 31, 16)
            evb = min((bld >> 8) & 31, 16)
            nm = FULL ^ alpha
            for k, (a, b) in enumerate(((tr, sr), (tg, sg), (tbl, sbl))):
                s = ((_expand(a) * eva + _expand(b) * evb) >> 4) & L16
                ov = ((s + C31) & H15) >> 15
                ovm = ov * 0xFFFF
                s = (s & ~ovm) | (LANE31 & ovm)
                out[k] = (_i(_compress(s)) & alpha) | (out[k] & nm)
        if bright:
            tab = _bright_table(bmode, min(io16[REG_BLDY >> 1] & 31, 16))
            nm = FULL ^ bright
            for k in range(3):
                ch = _b(out[k])
                out[k] = (_i(ch.translate(tab)) & bright) | (out[k] & nm)
        rb, gb, bb = _b(out[0]), _b(out[1]), _b(out[2])
        lo = _b(_i(rb) | _i(gb.translate(PACK_GLO)))
        hi = _b(_i(gb.translate(PACK_GHI)) | _i(bb.translate(PACK_BHI)))
        fb[off:off + 2 * W:2] = lo
        fb[off + 1:off + 2 * W:2] = hi
        self._advance_ref()

    # ------------------------------------------------------------ windows
    def _window_masks(self, y: int, dispcnt: int, obj):
        io16 = self.bus.io16
        winin = io16[REG_WININ >> 1]
        winout = io16[REG_WINOUT >> 1]
        regions = []                                 # (mask, enable bits), highest priority first
        covered = 0
        for k, (hreg, vreg, bits) in enumerate(((REG_WIN0H, REG_WIN0V, winin & 0x3F),
                                               (REG_WIN1H, REG_WIN1V, (winin >> 8) & 0x3F))):
            if not dispcnt & (0x2000 << k):
                continue
            h = io16[hreg >> 1]
            v = io16[vreg >> 1]
            if not _in_range(y, v >> 8, v & 0xFF):
                continue
            m = _range_mask(h >> 8, h & 0xFF) & (FULL ^ covered)
            regions.append((m, bits))
            covered |= m
        if dispcnt & 0x8000 and obj is not None:
            m = _i(obj[4]) & (FULL ^ covered)
            regions.append((m, (winout >> 8) & 0x3F))
            covered |= m
        regions.append((FULL ^ covered, winout & 0x3F))
        enable = []
        for k in range(6):
            e = 0
            for m, bits in regions:
                if bits >> k & 1:
                    e |= m
            enable.append(e)
        return enable

    # ------------------------------------------------------------ backgrounds
    def _row4bpp(self, word: int, hf: bool, bank: int) -> bytes:
        key = (word << 16) | (bank << 12) | (0x400 if hf else 0)   # same layout as map entry bits
        r = self._row4.get(key)
        if r is None:
            base = bank << 4
            px = [((word >> (4 * i)) & 15) for i in range(8)]
            r = bytes((base | p) if p else 0 for p in px)
            if hf:
                r = r[::-1]
            if len(self._row4) > 200_000:
                self._row4.clear()
            self._row4[key] = r
        return r

    def _text_bg(self, bg: int, y: int) -> bytes:
        bus = self.bus
        io16 = bus.io16
        cnt = io16[(REG_BG0CNT >> 1) + bg]
        y = self._mosaic_bg_y(y, cnt)                # BG mosaic: group's first line
        hofs = io16[(REG_BG0HOFS >> 1) + 2 * bg] & 0x1FF
        vofs = io16[(REG_BG0HOFS >> 1) + 2 * bg + 1] & 0x1FF
        char_base = ((cnt >> 2) & 3) * 0x4000
        screen_base = ((cnt >> 8) & 31) * 0x800
        c256 = cnt & 0x80
        wt, ht = TEXT_BG_SIZE[cnt >> 14]
        yy = (y + vofs) & (ht * 8 - 1)
        ty = yy >> 3
        r = yy & 7
        row_base = screen_base + (ty >> 5) * (0x1000 if wt == 64 else 0x800) + (ty & 31) * 64
        xx = hofs & (wt * 8 - 1)
        tx0 = xx >> 3
        fine = xx & 7
        vram = bus.vram
        v16 = bus.vram16
        v32 = bus.vram32
        cache = self._row4
        get = cache.get
        parts = []
        append = parts.append
        wmask = wt - 1
        zero8 = bytes(8)
        rv = 7 - r
        for k in range(31 if fine else 30):
            tx = (tx0 + k) & wmask
            e = v16[((row_base + (0x800 if tx >= 32 else 0) + (tx & 31) * 2) & 0xFFFF) >> 1]
            if c256:
                a = char_base + (e & 0x3FF) * 64 + (rv if e & 0x800 else r) * 8
                if a >= 0x10000:
                    append(zero8)
                    continue
                d = bytes(vram[a:a + 8])
                append(d[::-1] if e & 0x400 else d)
            else:
                a = char_base + (e & 0x3FF) * 32 + (rv if e & 0x800 else r) * 4
                if a >= 0x10000:
                    append(zero8)
                    continue
                word = v32[a >> 2]
                d = get((word << 16) | (e & 0xF400))        # row data + palette bank + hflip
                if d is None:
                    d = self._row4bpp(word, bool(e & 0x400), e >> 12)
                append(d)
        line = b"".join(parts)
        return line[fine:fine + W]

    def _affine_params(self, bg: int):
        io16 = self.bus.io16
        base = (REG_BG2PA >> 1) + 8 * (bg - 2)
        return _s16(io16[base]), _s16(io16[base + 2])     # PA (dx), PC (dy)

    def _affine_bg(self, bg: int, y: int) -> bytes:
        bus = self.bus
        io16 = bus.io16
        cnt = io16[(REG_BG0CNT >> 1) + bg]
        char_base = ((cnt >> 2) & 3) * 0x4000
        screen_base = ((cnt >> 8) & 31) * 0x800
        size = 128 << (cnt >> 14)
        tiles = size >> 3
        wrap = cnt & 0x2000
        pa, pc = self._affine_params(bg)
        x, yv = self._mosaic_refs(bg, y)
        vram = bus.vram
        smask = size - 1
        if pa == 256 and pc == 0:
            ty = yv >> 8
            if wrap:
                ty &= smask
            elif not 0 <= ty < size:
                return bytes(W)
            sx = x >> 8
            map_row = screen_base + (ty >> 3) * tiles
            r8 = (ty & 7) * 8
            parts = []
            first = sx >> 3
            fine = sx & 7
            for k in range(31 if fine else 30):
                tx = first + k
                if wrap:
                    tx &= tiles - 1
                elif not 0 <= tx < tiles:
                    parts.append(b"\x00" * 8)
                    continue
                t = vram[(map_row + tx) & 0xFFFF]
                a = char_base + t * 64 + r8
                parts.append(bytes(vram[a:a + 8]) if a < 0x10000 else b"\x00" * 8)
            return b"".join(parts)[fine:fine + W]
        out = bytearray(W)
        for i in range(W):
            tx = x >> 8
            ty = yv >> 8
            x += pa
            yv += pc
            if wrap:
                tx &= smask
                ty &= smask
            elif tx < 0 or ty < 0 or tx >= size or ty >= size:
                continue
            t = vram[(screen_base + (ty >> 3) * tiles + (tx >> 3)) & 0xFFFF]
            a = char_base + t * 64 + (ty & 7) * 8 + (tx & 7)
            if a < 0x10000:
                out[i] = vram[a]
        return bytes(out)

    def _bitmap4(self, dispcnt: int, y: int) -> bytes:
        vram = self.bus.vram
        base = 0xA000 if dispcnt & 0x10 else 0
        pa, pc = self._affine_params(2)
        x, yv = self._mosaic_refs(2, y)
        if pa == 256 and pc == 0 and not x & 0xFF:
            row = yv >> 8
            col = x >> 8
            if not 0 <= row < HEIGHT:
                return bytes(W)
            start = base + row * W
            if col == 0:
                return bytes(vram[start:start + W])
            out = bytearray(W)
            lo, hi = max(0, -col), min(W, W - col)
            if lo < hi:
                out[lo:hi] = vram[start + col + lo:start + col + hi]
            return bytes(out)
        out = bytearray(W)
        for i in range(W):
            tx, ty = x >> 8, yv >> 8
            x += pa
            yv += pc
            if 0 <= tx < W and 0 <= ty < HEIGHT:
                out[i] = vram[base + ty * W + tx]
        return bytes(out)

    def _bitmap_direct(self, mode: int, dispcnt: int, y: int):
        """Mode 3/5 BG2 line: (lo bytes, hi bytes, opaque mask)."""
        vram = self.bus.vram
        if mode == 3:
            base, bw, bh = 0, W, HEIGHT
        else:
            base, bw, bh = (0xA000 if dispcnt & 0x10 else 0), 160, 128
        pa, pc = self._affine_params(2)
        x, yv = self._mosaic_refs(2, y)
        lo = bytearray(W)
        hi = bytearray(W)
        opaque = bytearray(W)
        for i in range(W):
            tx, ty = x >> 8, yv >> 8
            x += pa
            yv += pc
            if 0 <= tx < bw and 0 <= ty < bh:
                a = base + (ty * bw + tx) * 2
                lo[i] = vram[a]
                hi[i] = vram[a + 1] & 0x7F
                opaque[i] = 0xFF
        return bytes(lo), bytes(hi), bytes(opaque)

    def _compose_direct(self, off, direct, obj, obj_lo, obj_hi, enable):
        lo_b, hi_b, opq, prio = direct
        pal = self.bus.pal
        bd = _i(bytes(pal[0:1]) * W)
        bdh = _i(bytes(pal[1:2]) * W)
        m = _i(opq)
        if enable is not None:
            m &= enable[2]
        nm = FULL ^ m
        lo = (_i(lo_b) & m) | (bd & nm)
        hi = (_i(hi_b) & m) | (bdh & nm)
        if obj is not None:
            plane, pr = obj[0], obj[1]
            om = _i(plane.translate(NZ))
            # OBJ over BG2 where its priority <= BG2 priority, or BG2 transparent
            le = 0
            for p in range(prio + 1):
                le |= _i(pr.translate(EQ[p]))
            om &= le | nm
            if enable is not None:
                om &= enable[4]
            if om:
                onm = FULL ^ om
                lo = (_i(plane.translate(obj_lo)) & om) | (lo & onm)
                hi = (_i(plane.translate(obj_hi)) & om) | (hi & onm)
        self.fb[off:off + 2 * W:2] = _b(lo)
        self.fb[off + 1:off + 2 * W:2] = _b(hi)

    # ------------------------------------------------------------ sprites
    def _render_objs(self, y: int, mode: int, dispcnt: int):
        """-> (idx bytes, prio bytes (4 = none), semi mask bytes, any_semi, objwin mask bytes)."""
        bus = self.bus
        oam16 = bus.oam16
        vram = bus.vram
        v32 = bus.vram32
        idx = bytearray(W)
        prio = bytearray(b"\x04" * W)
        semi = bytearray(W)
        win = bytearray(W)
        any_semi = False
        any_px = False
        one_d = bool(dispcnt & 0x40)
        bitmap = mode >= 3
        obj_mx = (bus.io16[REG_MOSAIC >> 1] >> 8 & 0xF) + 1   # OBJ mosaic X group size
        row4 = self._row4bpp
        snap = bytes(bus.oam)
        if snap != self._oam_snap:
            self._oam_snap = snap
            self._obj_lines = self._sprite_lines()
        for n in self._obj_lines[y]:
            a0 = oam16[n * 4]
            affine = a0 & 0x100
            objmode = (a0 >> 10) & 3
            a1 = oam16[n * 4 + 1]
            w, h = OBJ_SIZE[a0 >> 14][a1 >> 14]
            double = affine and a0 & 0x200
            bw, bh = (2 * w, 2 * h) if double else (w, h)
            rel = (y - (a0 & 0xFF)) & 0xFF
            sx = a1 & 0x1FF
            if sx >= 256:
                sx -= 512
            if sx >= W or sx + bw <= 0:
                continue
            a2 = oam16[n * 4 + 2]
            tile = a2 & 0x3FF
            if bitmap and tile < 512:
                continue
            p = (a2 >> 10) & 3
            bank = a2 >> 12
            c256 = bool(a0 & 0x2000)
            step = 2 if c256 else 1
            tw = w >> 3
            if affine:
                line = self._obj_affine(a1, tile, w, h, bw, bh, rel, c256, bank, one_d)
            else:
                row = (h - 1 - rel) if a1 & 0x2000 else rel
                ty, r = row >> 3, row & 7
                hf = bool(a1 & 0x1000)
                parts = []
                for c in range(tw):
                    tx = (tw - 1 - c) if hf else c
                    if one_d:
                        t = tile + (ty * tw + tx) * step
                    else:
                        t = (tile & ~(step - 1)) + ty * 32 + tx * step
                    a = 0x10000 + (t & 0x3FF) * 32
                    if c256:
                        d = bytes(vram[a + r * 8:a + r * 8 + 8])
                        parts.append(d[::-1] if hf else d)
                    else:
                        parts.append(row4(v32[(a + r * 4) >> 2], hf, bank))
                line = b"".join(parts)
            x0 = max(sx, 0)
            x1 = min(sx + bw, W)
            seg = line[x0 - sx:x1 - sx]
            if a0 & 0x1000 and obj_mx > 1:
                seg = _mosaic_x(seg, obj_mx, x0)      # OBJ mosaic: horizontal only
            n_ = x1 - x0
            nzm = _i(seg.translate(NZ))
            if not nzm:
                continue
            if objmode == 2:
                win[x0:x1] = _b(_i(win[x0:x1]) | nzm, n_)
                continue
            can = nzm & _i(bytes(prio[x0:x1]).translate(GT[p]))
            if not can:
                continue
            full = (1 << (8 * n_)) - 1
            inv = full ^ can
            idx[x0:x1] = _b((_i(seg) & can) | (_i(idx[x0:x1]) & inv), n_)
            prio[x0:x1] = _b(((p * _i(b"\x01" * n_)) & can) | (_i(prio[x0:x1]) & inv), n_)
            if objmode == 1:
                semi[x0:x1] = _b(can | (_i(semi[x0:x1]) & inv), n_)
                any_semi = True
            elif any_semi:
                semi[x0:x1] = _b(_i(semi[x0:x1]) & inv, n_)
            any_px = True
        if not any_px and not any(win):
            return (bytes(W), b"\x04" * W, bytes(W), False, bytes(W))
        return bytes(idx), bytes(prio), bytes(semi), any_semi, bytes(win)

    def _sprite_lines(self):
        """OAM index lists per visible scanline (rebuilt when OAM changes)."""
        oam16 = self.bus.oam16
        lines = [[] for _ in range(HEIGHT)]
        for n in range(128):
            a0 = oam16[n * 4]
            affine = a0 & 0x100
            if (not affine and a0 & 0x200) or (a0 >> 10) & 3 == 3:
                continue
            w, h = OBJ_SIZE[a0 >> 14][oam16[n * 4 + 1] >> 14]
            bh = 2 * h if affine and a0 & 0x200 else h
            y0 = a0 & 0xFF
            for rel in range(bh):
                line = (y0 + rel) & 0xFF
                if line < HEIGHT:
                    lines[line].append(n)
        return lines

    def _obj_affine(self, a1, tile, w, h, bw, bh, rel, c256, bank, one_d) -> bytes:
        oam16 = self.bus.oam16
        vram = self.bus.vram
        g = ((a1 >> 9) & 31) * 16
        pa = _s16(oam16[g + 3])
        pb = _s16(oam16[g + 7])
        pc = _s16(oam16[g + 11])
        pd = _s16(oam16[g + 15])
        hw, hh = bw >> 1, bh >> 1
        dy = rel - hh
        tw = w >> 3
        step = 2 if c256 else 1
        cx, cy = w >> 1, h >> 1
        out = bytearray(bw)
        x = pa * (-hw) + pb * dy
        yv = pc * (-hw) + pd * dy
        base_bank = bank << 4
        for i in range(bw):
            tx = (x >> 8) + cx
            ty = (yv >> 8) + cy
            x += pa
            yv += pc
            if tx < 0 or ty < 0 or tx >= w or ty >= h:
                continue
            if one_d:
                t = tile + ((ty >> 3) * tw + (tx >> 3)) * step
            else:
                t = (tile & ~(step - 1)) + (ty >> 3) * 32 + (tx >> 3) * step
            a = 0x10000 + (t & 0x3FF) * 32
            if c256:
                out[i] = vram[a + (ty & 7) * 8 + (tx & 7)]
            else:
                b = vram[a + (ty & 7) * 4 + ((tx & 7) >> 1)]
                px = (b >> 4) if tx & 1 else (b & 15)
                if px:
                    out[i] = base_bank | px
        return bytes(out)
