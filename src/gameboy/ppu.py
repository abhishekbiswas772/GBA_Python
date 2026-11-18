from src.gameboy.common import (
    WIDTH, HEIGHT, LINE_DOTS, FRAME_DOTS,
    PIXEL_SHIFTS as _SHIFTS, REV, BLANK,
    STRIP_PRIO, WHITE_RGB,
)

_PAIRS: list = [None] * 65536      # lazily-filled tile-row decode cache


def _pair(lo: int, hi: int) -> bytes:
    key = (lo << 8) | hi
    p = _PAIRS[key]
    if p is None:
        p = bytes((((hi >> s) & 1) << 1) | ((lo >> s) & 1) for s in _SHIFTS)
        _PAIRS[key] = p
    return p


def _palette_table(pal: int) -> bytes:
    return bytes((pal >> ((i & 3) * 2)) & 3 for i in range(256))


def _palette4(pal: int) -> tuple:
    return tuple((pal >> (i * 2)) & 3 for i in range(4))


# --- CGB helpers ---------------------------------------------------------------
# A CGB BG/window pixel is encoded in one byte: bit 5 = BG-to-OBJ priority (tile
# attribute bit 7), bits 2-4 = palette, bits 0-1 = colour. Final framebuffer
# indices are 0-31 for BG (pal*4+colour) and 32-63 for OBJ (32+pal*4+colour).

_CGB_ATTR_T = {}            # attr & 0x87 -> translate table for raw colour 0-3
_CPAIRS = {}                # (lo, hi, attr) -> encoded 8-pixel tile row


def _attr_table(a: int) -> bytes:
    t = _CGB_ATTR_T.get(a)
    if t is None:
        add = (0x20 if a & 0x80 else 0) | ((a & 7) << 2)
        t = bytes(((i & 3) | add) for i in range(256))
        _CGB_ATTR_T[a] = t
    return t


def _expand5(c: int) -> int:
    return (c << 3) | (c >> 2)


def _cgb_rgb_tables(bg_pal, obj_pal) -> tuple:
    """64-entry (BG 0-31, OBJ 32-63) R/G/B translate tables from palette RAM."""
    r = bytearray(256)
    g = bytearray(256)
    b = bytearray(256)
    for i in range(64):
        ram = bg_pal if i < 32 else obj_pal
        j = (i & 31) * 2
        c = ram[j] | (ram[j + 1] << 8)
        r[i] = _expand5(c & 0x1F)
        g[i] = _expand5((c >> 5) & 0x1F)
        b[i] = _expand5((c >> 10) & 0x1F)
    return bytes(r), bytes(g), bytes(b)


class PPU:
    def __init__(self, mmu, cgb: bool = False):
        self.mmu = mmu
        self.cgb = cgb
        self.lcdc = 0x91
        self.stat_en = 0x00
        self.scy = 0
        self.scx = 0
        self.ly = 0
        self.lyc = 0
        self.dma = 0xFF
        self.bgp = 0xFC
        self.obp0 = 0xFF
        self.obp1 = 0xFF
        self.wy = 0
        self.wx = 0

        self.mode = 2
        self.line_cycles = 0
        self.win_y = 0
        self.win_active = False
        self.framebuffer = bytearray(WIDTH * HEIGHT)
        self.frame_count = 0
        self.frame_ready = False
        self.vram_locked = False
        self.oam_locked = False
        self._stat_line = False
        self._off_cycles = 0

        self._bgp_tr = _palette_table(self.bgp)
        self._obp0_t = _palette4(self.obp0)
        self._obp1_t = _palette4(self.obp1)

        # CGB colour palettes: 8 x 4 colours x 2 bytes (RGB555, little endian)
        self.bg_pal = bytearray(b"\xff" * 64)      # boot ROM leaves BG palettes white
        self.obj_pal = bytearray(b"\xff" * 64)
        self.bcps = 0
        self.ocps = 0
        self._rgb_t = None                          # cached translate tables (None = dirty)
        self.rgb = bytearray(WHITE_RGB)             # CGB output, 160x144 RGB888

    # --- CGB palette registers (FF68-FF6B) --------------------------------------

    def read_cgb(self, addr: int) -> int:
        if addr == 0xFF68:
            return self.bcps | 0x40
        if addr == 0xFF69:
            return self.bg_pal[self.bcps & 0x3F]
        if addr == 0xFF6A:
            return self.ocps | 0x40
        return self.obj_pal[self.ocps & 0x3F]

    def write_cgb(self, addr: int, value: int) -> None:
        if addr == 0xFF68:
            self.bcps = value & 0xBF
        elif addr == 0xFF69:
            self.bg_pal[self.bcps & 0x3F] = value
            self._rgb_t = None
            if self.bcps & 0x80:
                self.bcps = 0x80 | ((self.bcps + 1) & 0x3F)
        elif addr == 0xFF6A:
            self.ocps = value & 0xBF
        else:
            self.obj_pal[self.ocps & 0x3F] = value
            self._rgb_t = None
            if self.ocps & 0x80:
                self.ocps = 0x80 | ((self.ocps + 1) & 0x3F)

    def read(self, addr: int) -> int:
        if addr == 0xFF40: return self.lcdc
        if addr == 0xFF41:
            coinc = 0x04 if self.ly == self.lyc else 0
            return 0x80 | self.stat_en | coinc | self.mode
        if addr == 0xFF42: return self.scy
        if addr == 0xFF43: return self.scx
        if addr == 0xFF44: return self.ly
        if addr == 0xFF45: return self.lyc
        if addr == 0xFF46: return self.dma
        if addr == 0xFF47: return self.bgp
        if addr == 0xFF48: return self.obp0
        if addr == 0xFF49: return self.obp1
        if addr == 0xFF4A: return self.wy
        return self.wx

    def write(self, addr: int, value: int) -> None:
        if addr == 0xFF40:
            was_on = self.lcdc & 0x80
            self.lcdc = value
            if was_on and not value & 0x80:
                # LCD off: LY=0, mode 0, no PPU interrupts, screen blank (white)
                self.ly = 0
                self.line_cycles = 0
                self.win_y = 0
                self.win_active = False
                self._off_cycles = 0
                self.framebuffer[:] = bytes(WIDTH * HEIGHT)
                if self.cgb:
                    self.rgb[:] = WHITE_RGB
                self.mode = 0
                self.vram_locked = self.oam_locked = False
                self._stat_line = False
            elif not was_on and value & 0x80:
                # LCD on: frame restarts at LY=0, dot 0
                self.ly = 0
                self.line_cycles = 0
                self._set_mode(2)
            else:
                self._set_mode(self.mode)
        elif addr == 0xFF41:
            self.stat_en = value & 0x78
            self._update_stat()
        elif addr == 0xFF42:
            self.scy = value
        elif addr == 0xFF43:
            self.scx = value
        elif addr == 0xFF44:
            pass
        elif addr == 0xFF45:
            self.lyc = value
            self._update_stat()
        elif addr == 0xFF46:
            self.dma = value
            self.mmu.dma_transfer(value << 8)
        elif addr == 0xFF47:
            self.bgp = value
            self._bgp_tr = _palette_table(value)
        elif addr == 0xFF48:
            self.obp0 = value
            self._obp0_t = _palette4(value)
        elif addr == 0xFF49:
            self.obp1 = value
            self._obp1_t = _palette4(value)
        elif addr == 0xFF4A:
            self.wy = value
        else:
            self.wx = value

    def step(self, cycles: int) -> None:
        if not self.lcdc & 0x80:
            # Off: LY frozen, no IRQs; keep a frame tick so run_frame() still returns
            self._off_cycles += cycles
            while self._off_cycles >= FRAME_DOTS:
                self._off_cycles -= FRAME_DOTS
                self.frame_count += 1
                self.frame_ready = True
            return
        while cycles > 0:
            if self.ly >= 144:
                nxt = LINE_DOTS
            elif self.line_cycles < 80:
                nxt = 80
            elif self.line_cycles < 252:
                nxt = 252
            else:
                nxt = LINE_DOTS
            step = nxt - self.line_cycles
            if step > cycles:
                self.line_cycles += cycles
                return
            self.line_cycles = nxt
            cycles -= step
            if self.ly < 144 and nxt == 80:
                self._set_mode(3)
            elif self.ly < 144 and nxt == 252:
                self._set_mode(0)
                self._render_line()
                if self.mmu.hdma_active:
                    self.mmu.hdma_hblank()
            else:
                self._end_line()

    def _end_line(self) -> None:
        self.line_cycles = 0
        self.ly += 1
        if self.ly == 144:
            self.mmu.request_interrupt(0x01)
            self.frame_count += 1
            self.frame_ready = True
            self._set_mode(1)
        elif self.ly == 154:
            self.ly = 0
            self.win_y = 0
            self.win_active = False
            self._set_mode(2)
        elif self.ly < 144:
            self._set_mode(2)
        else:
            self._update_stat()

    def _set_mode(self, mode: int) -> None:
        self.mode = mode
        on = self.lcdc & 0x80
        self.vram_locked = on and mode == 3
        self.oam_locked = on and mode in (2, 3)
        self._update_stat()

    def _update_stat(self) -> None:
        coinc = self.ly == self.lyc
        line = bool(
            (self.mode == 0 and self.stat_en & 0x08)
            or (self.mode == 1 and self.stat_en & 0x10)
            or (self.mode == 2 and self.stat_en & 0x20)
            or (coinc and self.stat_en & 0x40)
        )
        if line and not self._stat_line:
            self.mmu.request_interrupt(0x02)
        self._stat_line = line

    def _map_row(self, map_off: int, y8: int, trow: int, signed: bool) -> bytearray:
        vram = self.mmu.vram
        row = bytearray(256)
        i = map_off + y8 * 32
        if signed:
            for tx in range(32):
                off = 0x800 + (((vram[i + tx] + 128) & 0xFF) << 4) + trow
                row[tx * 8:tx * 8 + 8] = _pair(vram[off], vram[off + 1])
        else:
            for tx in range(32):
                off = (vram[i + tx] << 4) + trow
                row[tx * 8:tx * 8 + 8] = _pair(vram[off], vram[off + 1])
        return row

    def _render_line(self) -> None:
        if self.cgb:
            self._render_line_cgb()
            return
        y = self.ly
        base = y * WIDTH
        fb = self.framebuffer
        if not (self.lcdc & 0x80):
            fb[base:base + WIDTH] = BLANK
            return

        bg_on = self.lcdc & 0x01
        signed = not (self.lcdc & 0x10)
        raw = BLANK

        if bg_on:
            sy = (self.scy + y) & 0xFF
            map_off = 0x1C00 if (self.lcdc & 0x08) else 0x1800
            row = self._map_row(map_off, sy >> 3, (sy & 7) * 2, signed)
            scx = self.scx
            raw = (bytes(row[scx:]) + bytes(row[:scx]))[:WIDTH]

            if self.lcdc & 0x20 and self.wx <= 166 and (self.win_active or y >= self.wy):
                self.win_active = True
                x0 = self.wx - 7
                cut = 0
                if x0 < 0:
                    cut = -x0
                    x0 = 0
                w = WIDTH - x0
                if w > 0:
                    wmap = 0x1C00 if (self.lcdc & 0x40) else 0x1800
                    wrow = self._map_row(wmap, self.win_y >> 3, (self.win_y & 7) * 2, signed)
                    raw = raw[:x0] + bytes(wrow[cut:cut + w])
                self.win_y += 1

        fb[base:base + WIDTH] = raw.translate(self._bgp_tr)

        if self.lcdc & 0x02:
            self._render_sprites(y, base, raw)

    def _render_sprites(self, y: int, base: int, raw: bytes) -> None:
        oam = self.mmu.oam
        vram = self.mmu.vram
        h = 16 if self.lcdc & 0x04 else 8
        sel = []
        for i in range(40):
            sy = oam[i << 2] - 16
            if sy <= y < sy + h:
                sel.append(i)
                if len(sel) == 10:
                    break
        if not sel:
            return
        sel.sort(key=lambda i: (oam[(i << 2) + 1], i))
        fb = self.framebuffer
        claimed = bytearray(WIDTH)
        for i in sel:
            o = i << 2
            sx = oam[o + 1] - 8
            if sx <= -8 or sx >= WIDTH:
                continue
            attr = oam[o + 3]
            row = y - (oam[o] - 16)
            if attr & 0x40:
                row = h - 1 - row
            t = oam[o + 2]
            if h == 16:
                if row >= 8:
                    t |= 1
                    row -= 8
                else:
                    t &= 0xFE
            off = (t << 4) + (row << 1)
            lo, hi = vram[off], vram[off + 1]
            if attr & 0x20:
                lo, hi = REV[lo], REV[hi]
            p = _pair(lo, hi)
            pal = self._obp1_t if attr & 0x10 else self._obp0_t
            behind = attr & 0x80
            for dx in range(8):
                x = sx + dx
                if x < 0 or x >= WIDTH or claimed[x]:
                    continue
                c = p[dx]
                if not c:
                    continue
                claimed[x] = 1
                if behind and raw[x]:
                    continue
                fb[base + x] = pal[c]

    # --- CGB rendering ------------------------------------------------------------

    def _cgb_map_row(self, map_off: int, line: int, signed: bool) -> bytearray:
        """256 encoded BG pixels for one map row, honouring per-tile attributes."""
        vram = self.mmu.vram
        row = bytearray(256)
        i = map_off + (line >> 3) * 32
        fine = line & 7
        cache = _CPAIRS
        for tx in range(32):
            t = vram[i + tx]
            a = vram[0x2000 + i + tx]
            off = (0x2000 if a & 0x08 else 0) + ((0x800 + (((t + 128) & 0xFF) << 4)) if signed else (t << 4))
            off += (7 - fine if a & 0x40 else fine) << 1
            lo, hi = vram[off], vram[off + 1]
            if a & 0x20:
                lo, hi = REV[lo], REV[hi]
            key = (lo << 16) | (hi << 8) | (a & 0x87)
            px = cache.get(key)
            if px is None:
                px = _pair(lo, hi).translate(_attr_table(a & 0x87))
                cache[key] = px
            row[tx * 8:tx * 8 + 8] = px
        return row

    def _render_line_cgb(self) -> None:
        y = self.ly
        base = y * WIDTH
        lcdc = self.lcdc
        signed = not (lcdc & 0x10)

        sy = (self.scy + y) & 0xFF
        row = self._cgb_map_row(0x1C00 if lcdc & 0x08 else 0x1800, sy, signed)
        scx = self.scx
        raw = (bytes(row[scx:]) + bytes(row[:scx]))[:WIDTH]

        if lcdc & 0x20 and self.wx <= 166 and (self.win_active or y >= self.wy):
            self.win_active = True
            x0 = self.wx - 7
            cut = 0
            if x0 < 0:
                cut = -x0
                x0 = 0
            w = WIDTH - x0
            if w > 0:
                wrow = self._cgb_map_row(0x1C00 if lcdc & 0x40 else 0x1800, self.win_y, signed)
                raw = raw[:x0] + bytes(wrow[cut:cut + w])
            self.win_y += 1

        line = bytearray(raw.translate(STRIP_PRIO))
        if lcdc & 0x02:
            self._render_sprites_cgb(y, line, raw)
        self.framebuffer[base:base + WIDTH] = line

        t = self._rgb_t
        if t is None:
            t = self._rgb_t = _cgb_rgb_tables(self.bg_pal, self.obj_pal)
        seg = bytearray(WIDTH * 3)
        seg[0::3] = line.translate(t[0])
        seg[1::3] = line.translate(t[1])
        seg[2::3] = line.translate(t[2])
        self.rgb[base * 3:base * 3 + WIDTH * 3] = seg

    def _render_sprites_cgb(self, y: int, line: bytearray, raw: bytes) -> None:
        oam = self.mmu.oam
        vram = self.mmu.vram
        h = 16 if self.lcdc & 0x04 else 8
        sel = []
        for i in range(40):
            sy = oam[i << 2] - 16
            if sy <= y < sy + h:
                sel.append(i)
                if len(sel) == 10:
                    break
        if not sel:
            return
        if self.mmu.opri & 1:                      # OPRI=1: DMG-style X priority
            sel.sort(key=lambda i: (oam[(i << 2) + 1], i))
        master = self.lcdc & 0x01                  # 0: sprites always above BG
        claimed = bytearray(WIDTH)
        for i in sel:                              # lowest OAM index wins
            o = i << 2
            sx = oam[o + 1] - 8
            if sx <= -8 or sx >= WIDTH:
                continue
            attr = oam[o + 3]
            r = y - (oam[o] - 16)
            if attr & 0x40:
                r = h - 1 - r
            t = oam[o + 2]
            if h == 16:
                t = (t | 1) if r >= 8 else (t & 0xFE)
                r &= 7
            off = (0x2000 if attr & 0x08 else 0) + (t << 4) + (r << 1)
            lo, hi = vram[off], vram[off + 1]
            if attr & 0x20:
                lo, hi = REV[lo], REV[hi]
            p = _pair(lo, hi)
            add = 32 + ((attr & 7) << 2)
            behind = attr & 0x80
            for dx in range(8):
                x = sx + dx
                if x < 0 or x >= WIDTH or claimed[x]:
                    continue
                c = p[dx]
                if not c:
                    continue
                claimed[x] = 1
                if master:
                    bg = raw[x]
                    if bg & 3 and (behind or bg & 0x20):
                        continue
                line[x] = add + c

