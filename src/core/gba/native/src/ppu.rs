//! GBA PPU (`src/core/gba/ppu.py`): scanline timing, IRQ/DMA triggers and line
//! rendering into a 240x160 little-endian BGR555 framebuffer.
//!
//! The Python core renders with byte-lane big-integer masks; this does the same
//! thing per pixel and produces identical output (including that lines with
//! alpha / brightness effects drop bit 15 of every pixel).

use serde::{Deserialize, Serialize};

use crate::dma::{TIMING_HBLANK, TIMING_VBLANK};
use crate::gba::*;

pub const W: usize = 240;
pub const H: usize = 160;
const LINE_CYCLES: u64 = 1232;
const HBLANK_START: u64 = 1006;
const TOTAL_LINES: u32 = 228;

const STAT_VBLANK: u32 = 0x01;
const STAT_HBLANK: u32 = 0x02;
const STAT_VCOUNT: u32 = 0x04;
const STAT_VBLANK_IRQ: u32 = 0x08;
const STAT_HBLANK_IRQ: u32 = 0x10;
const STAT_VCOUNT_IRQ: u32 = 0x20;
const IRQ_VBLANK: u32 = 1;
const IRQ_HBLANK: u32 = 2;
const IRQ_VCOUNT: u32 = 4;

const REG_DISPCNT: usize = 0x000;
const REG_BG0CNT: usize = 0x008;
const REG_BG0HOFS: usize = 0x010;
const REG_BG2X: usize = 0x028;
const REG_BG2Y: usize = 0x02C;
const REG_BG3X: usize = 0x038;
const REG_BG3Y: usize = 0x03C;
const REG_WIN0H: usize = 0x040;
const REG_WIN1H: usize = 0x042;
const REG_WIN0V: usize = 0x044;
const REG_WIN1V: usize = 0x046;
const REG_WININ: usize = 0x048;
const REG_WINOUT: usize = 0x04A;
const REG_MOSAIC: usize = 0x04C;
const REG_BLDCNT: usize = 0x050;
const REG_BLDALPHA: usize = 0x052;
const REG_BLDY: usize = 0x054;

const TEXT_BG_SIZE: [(usize, usize); 4] = [(32, 32), (64, 32), (32, 64), (64, 64)];
const OBJ_SIZE: [[(i32, i32); 4]; 4] = [
    [(8, 8), (16, 16), (32, 32), (64, 64)],
    [(16, 8), (32, 8), (32, 16), (64, 32)],
    [(8, 16), (8, 32), (16, 32), (32, 64)],
    [(8, 8), (8, 8), (8, 8), (8, 8)],
];
const ID_OBJ: u8 = 4;
const ID_BACKDROP: u8 = 5;
const ID_NONE: u8 = 6;

fn s28(v: u32) -> i64 {
    let v = (v & 0x0FFF_FFFF) as i64;
    if v & 0x0800_0000 != 0 {
        v - (1 << 28)
    } else {
        v
    }
}

fn s16(v: u32) -> i64 {
    v as u16 as i16 as i64
}

fn in_range(v: u32, a: u32, b: u32) -> bool {
    if a <= b {
        a <= v && v < b
    } else {
        v >= a || v < b
    }
}

/// Repeat each X-group's leftmost pixel across the group; `x0` is the screen x of line[0].
fn mosaic_x(line: &[u8], size: usize, x0: usize) -> Vec<u8> {
    if size <= 1 {
        return line.to_vec();
    }
    (0..line.len())
        .map(|i| {
            let x = x0 + i;
            let j = (x - x % size) as isize - x0 as isize;
            if j >= 0 {
                line[j as usize]
            } else {
                0
            }
        })
        .collect()
}

#[derive(Serialize, Deserialize)]
pub struct Ppu {
    pub vcount: u32,
    pub stat: u32,
    pub in_hblank: bool,
    pub next_event: u64,
    pub frame: u64,
    pub skip_render: bool,
    pub fb: Vec<u8>,
    pub ref_x: [i64; 2],
    pub ref_y: [i64; 2],
}

impl Ppu {
    pub fn new() -> Ppu {
        Ppu {
            vcount: 0,
            stat: 0,
            in_hblank: false,
            next_event: HBLANK_START,
            frame: 0,
            skip_render: false,
            fb: vec![0; W * H * 2],
            ref_x: [0; 2],
            ref_y: [0; 2],
        }
    }
}

impl Default for Ppu {
    fn default() -> Self {
        Ppu::new()
    }
}

struct Obj {
    idx: [u8; W],
    prio: [u8; W],
    semi: [bool; W],
    win: [bool; W],
}

impl Gba {
    // ------------------------------------------------------------ registers
    pub fn ppu_write_stat(&mut self, v: u32, m: u32) {
        let m = m & 0xFF38;
        self.ppu.stat = (self.ppu.stat & !m) | (v & m);
        self.ppu_check_vcount();
    }

    fn reg32(&self, off: usize) -> u32 {
        self.io16(off) | (self.io16(off + 2) << 16)
    }

    pub fn ppu_latch_ref(&mut self, bg: usize) {
        let (xo, yo) = if bg == 0 { (REG_BG2X, REG_BG2Y) } else { (REG_BG3X, REG_BG3Y) };
        self.ppu.ref_x[bg] = s28(self.reg32(xo));
        self.ppu.ref_y[bg] = s28(self.reg32(yo));
    }

    fn ppu_check_vcount(&mut self) {
        if self.ppu.vcount == self.ppu.stat >> 8 {
            if self.ppu.stat & STAT_VCOUNT == 0 {
                self.ppu.stat |= STAT_VCOUNT;
                if self.ppu.stat & STAT_VCOUNT_IRQ != 0 {
                    self.request_irq(IRQ_VCOUNT);
                }
            }
        } else {
            self.ppu.stat &= !STAT_VCOUNT;
        }
    }

    // ------------------------------------------------------------ timing
    pub fn ppu_event(&mut self) {
        let t = self.ppu.next_event;
        if !self.ppu.in_hblank {
            let y = self.ppu.vcount;
            if (y as usize) < H {
                if self.ppu.skip_render {
                    self.advance_ref();
                } else {
                    self.render_line(y as usize);
                }
            }
            self.ppu.in_hblank = true;
            self.ppu.stat |= STAT_HBLANK;
            if self.ppu.stat & STAT_HBLANK_IRQ != 0 {
                self.request_irq(IRQ_HBLANK);
            }
            if (y as usize) < H {
                self.dma_trigger(TIMING_HBLANK);
            }
            self.ppu.next_event = t + (LINE_CYCLES - HBLANK_START);
            return;
        }
        self.ppu.in_hblank = false;
        self.ppu.stat &= !STAT_HBLANK;
        let mut y = self.ppu.vcount + 1;
        if y == TOTAL_LINES {
            y = 0;
        }
        self.ppu.vcount = y;
        if y as usize == H {
            self.ppu.stat |= STAT_VBLANK;
            if self.ppu.stat & STAT_VBLANK_IRQ != 0 {
                self.request_irq(IRQ_VBLANK);
            }
            self.dma_trigger(TIMING_VBLANK);
            self.ppu_latch_ref(0);
            self.ppu_latch_ref(1);
            self.ppu.frame += 1;
        } else if y == TOTAL_LINES - 1 {
            self.ppu.stat &= !STAT_VBLANK;
        }
        self.ppu_check_vcount();
        self.ppu.next_event = t + HBLANK_START;
    }

    fn advance_ref(&mut self) {
        for bg in 0..2 {
            let base = REG_BG2PA + 16 * bg;
            self.ppu.ref_x[bg] += s16(self.io16(base + 2));
            self.ppu.ref_y[bg] += s16(self.io16(base + 6));
        }
    }

    // ------------------------------------------------------------ mosaic
    fn mosaic_bg_y(&self, y: usize, cnt: u32) -> usize {
        if cnt & 0x40 != 0 {
            let v = ((self.io16(REG_MOSAIC) >> 4) & 0xF) as usize + 1;
            return y - y % v;
        }
        y
    }

    fn mosaic_refs(&self, bg: usize, y: usize) -> (i64, i64) {
        let k = bg - 2;
        let (mut x, mut yv) = (self.ppu.ref_x[k], self.ppu.ref_y[k]);
        if self.io16(REG_BG0CNT + 2 * bg) & 0x40 != 0 {
            let d = (y % (((self.io16(REG_MOSAIC) >> 4) & 0xF) as usize + 1)) as i64;
            if d != 0 {
                let base = REG_BG2PA + 16 * k;
                x -= d * s16(self.io16(base + 2));
                yv -= d * s16(self.io16(base + 6));
            }
        }
        (x, yv)
    }

    fn affine_params(&self, bg: usize) -> (i64, i64) {
        let base = REG_BG2PA + 16 * (bg - 2);
        (s16(self.io16(base)), s16(self.io16(base + 4)))
    }

    // ------------------------------------------------------------ backgrounds
    fn text_bg(&self, bg: usize, y: usize, out: &mut [u8; W]) {
        let cnt = self.io16(REG_BG0CNT + 2 * bg);
        let y = self.mosaic_bg_y(y, cnt);
        let hofs = (self.io16(REG_BG0HOFS + 4 * bg) & 0x1FF) as usize;
        let vofs = (self.io16(REG_BG0HOFS + 4 * bg + 2) & 0x1FF) as usize;
        let char_base = ((cnt >> 2) & 3) as usize * 0x4000;
        let screen_base = ((cnt >> 8) & 31) as usize * 0x800;
        let c256 = cnt & 0x80 != 0;
        let (wt, ht) = TEXT_BG_SIZE[(cnt >> 14) as usize];
        let yy = (y + vofs) & (ht * 8 - 1);
        let ty = yy >> 3;
        let r = yy & 7;
        let row_base = screen_base + (ty >> 5) * if wt == 64 { 0x1000 } else { 0x800 } + (ty & 31) * 64;
        let xx = hofs & (wt * 8 - 1);
        let tx0 = xx >> 3;
        let fine = xx & 7;
        let vram = &self.vram;
        let mut line = [0u8; 31 * 8];
        let tiles = if fine != 0 { 31 } else { 30 };
        for k in 0..tiles {
            let tx = (tx0 + k) & (wt - 1);
            let ea = (row_base + if tx >= 32 { 0x800 } else { 0 } + (tx & 31) * 2) & 0xFFFF;
            let e = rd16(vram, ea & !1);
            let row = if e & 0x800 != 0 { 7 - r } else { r };
            let dst = &mut line[k * 8..k * 8 + 8];
            if c256 {
                let a = char_base + (e & 0x3FF) as usize * 64 + row * 8;
                if a >= 0x10000 {
                    continue;
                }
                for i in 0..8 {
                    dst[i] = vram[a + if e & 0x400 != 0 { 7 - i } else { i }];
                }
            } else {
                let a = char_base + (e & 0x3FF) as usize * 32 + row * 4;
                if a >= 0x10000 {
                    continue;
                }
                let word = rd32(vram, a);
                let base = ((e >> 12) << 4) as u8;
                for i in 0..8 {
                    let p = ((word >> (4 * i)) & 15) as u8;
                    let px = if p != 0 { base | p } else { 0 };
                    dst[if e & 0x400 != 0 { 7 - i } else { i }] = px;
                }
            }
        }
        out.copy_from_slice(&line[fine..fine + W]);
    }

    fn affine_bg(&self, bg: usize, y: usize, out: &mut [u8; W]) {
        let cnt = self.io16(REG_BG0CNT + 2 * bg);
        let char_base = ((cnt >> 2) & 3) as i64 * 0x4000;
        let screen_base = ((cnt >> 8) & 31) as i64 * 0x800;
        let size: i64 = 128 << (cnt >> 14);
        let tiles = size >> 3;
        let wrap = cnt & 0x2000 != 0;
        let (pa, pc) = self.affine_params(bg);
        let (mut x, mut yv) = self.mosaic_refs(bg, y);
        let smask = size - 1;
        let vram = &self.vram;
        for o in out.iter_mut() {
            let mut tx = x >> 8;
            let mut ty = yv >> 8;
            x += pa;
            yv += pc;
            *o = 0;
            if wrap {
                tx &= smask;
                ty &= smask;
            } else if tx < 0 || ty < 0 || tx >= size || ty >= size {
                continue;
            }
            let t = vram[((screen_base + (ty >> 3) * tiles + (tx >> 3)) & 0xFFFF) as usize] as i64;
            let a = char_base + t * 64 + (ty & 7) * 8 + (tx & 7);
            if a < 0x10000 {
                *o = vram[a as usize];
            }
        }
    }

    fn bitmap4(&self, dispcnt: u32, y: usize, out: &mut [u8; W]) {
        let base: i64 = if dispcnt & 0x10 != 0 { 0xA000 } else { 0 };
        let (pa, pc) = self.affine_params(2);
        let (mut x, mut yv) = self.mosaic_refs(2, y);
        for o in out.iter_mut() {
            let (tx, ty) = (x >> 8, yv >> 8);
            x += pa;
            yv += pc;
            *o = if (0..W as i64).contains(&tx) && (0..H as i64).contains(&ty) {
                self.vram[(base + ty * W as i64 + tx) as usize]
            } else {
                0
            };
        }
    }

    /// Mode 3/5 BG2 line: (lo, hi, opaque).
    fn bitmap_direct(&self, mode: u32, dispcnt: u32, y: usize) -> ([u8; W], [u8; W], [bool; W]) {
        let (base, bw, bh): (i64, i64, i64) = if mode == 3 {
            (0, W as i64, H as i64)
        } else {
            (if dispcnt & 0x10 != 0 { 0xA000 } else { 0 }, 160, 128)
        };
        let (pa, pc) = self.affine_params(2);
        let (mut x, mut yv) = self.mosaic_refs(2, y);
        let mut lo = [0u8; W];
        let mut hi = [0u8; W];
        let mut opq = [false; W];
        for i in 0..W {
            let (tx, ty) = (x >> 8, yv >> 8);
            x += pa;
            yv += pc;
            if (0..bw).contains(&tx) && (0..bh).contains(&ty) {
                let a = (base + (ty * bw + tx) * 2) as usize;
                lo[i] = self.vram[a];
                hi[i] = self.vram[a + 1] & 0x7F;
                opq[i] = true;
            }
        }
        (lo, hi, opq)
    }

    // ------------------------------------------------------------ sprites
    fn render_objs(&self, y: usize, mode: u32, dispcnt: u32) -> Obj {
        let mut o = Obj { idx: [0; W], prio: [4; W], semi: [false; W], win: [false; W] };
        let oam = &self.oam;
        let vram = &self.vram;
        let one_d = dispcnt & 0x40 != 0;
        let bitmap = mode >= 3;
        let obj_mx = ((self.io16(REG_MOSAIC) >> 8) & 0xF) as usize + 1;
        for n in 0..128usize {
            let a0 = rd16(oam, n * 8);
            let affine = a0 & 0x100 != 0;
            let objmode = (a0 >> 10) & 3;
            if (!affine && a0 & 0x200 != 0) || objmode == 3 {
                continue;
            }
            let a1 = rd16(oam, n * 8 + 2);
            let (w, h) = OBJ_SIZE[(a0 >> 14) as usize][(a1 >> 14) as usize];
            let double = affine && a0 & 0x200 != 0;
            let (bw, bh) = if double { (2 * w, 2 * h) } else { (w, h) };
            let rel = ((y as u32).wrapping_sub(a0 & 0xFF) & 0xFF) as i32;
            if rel >= bh {
                continue;
            }
            let mut sx = (a1 & 0x1FF) as i32;
            if sx >= 256 {
                sx -= 512;
            }
            if sx >= W as i32 || sx + bw <= 0 {
                continue;
            }
            let a2 = rd16(oam, n * 8 + 4);
            let tile = (a2 & 0x3FF) as i32;
            if bitmap && tile < 512 {
                continue;
            }
            let p = ((a2 >> 10) & 3) as u8;
            let bank = (a2 >> 12) as u8;
            let c256 = a0 & 0x2000 != 0;
            let step = if c256 { 2 } else { 1 };
            let tw = w >> 3;
            let line: Vec<u8> = if affine {
                self.obj_affine(a1, tile, w, h, bw, bh, rel, c256, bank, one_d)
            } else {
                let row = if a1 & 0x2000 != 0 { h - 1 - rel } else { rel };
                let (ty, r) = (row >> 3, (row & 7) as usize);
                let hf = a1 & 0x1000 != 0;
                let mut line = Vec::with_capacity(w as usize);
                for c in 0..tw {
                    let tx = if hf { tw - 1 - c } else { c };
                    let t = if one_d {
                        tile + (ty * tw + tx) * step
                    } else {
                        (tile & !(step - 1)) + ty * 32 + tx * step
                    };
                    let a = 0x10000 + (t & 0x3FF) as usize * 32;
                    if c256 {
                        let s = (a + r * 8).min(vram.len());
                        let e = (a + r * 8 + 8).min(vram.len());
                        let d = &vram[s..e];
                        if hf {
                            line.extend(d.iter().rev());
                        } else {
                            line.extend_from_slice(d);
                        }
                    } else {
                        let word = rd32(vram, (a + r * 4) & !3);
                        let base = bank << 4;
                        let mut px = [0u8; 8];
                        for i in 0..8 {
                            let v = ((word >> (4 * i)) & 15) as u8;
                            px[if hf { 7 - i } else { i }] = if v != 0 { base | v } else { 0 };
                        }
                        line.extend_from_slice(&px);
                    }
                }
                line
            };
            let x0 = sx.max(0) as usize;
            let x1 = (sx + bw).min(W as i32) as usize;
            let s0 = (x0 as i32 - sx) as usize;
            let s1 = (x1 as i32 - sx) as usize;
            let mut seg: Vec<u8> = line[s0.min(line.len())..s1.min(line.len())].to_vec();
            if a0 & 0x1000 != 0 && obj_mx > 1 {
                seg = mosaic_x(&seg, obj_mx, x0);
            }
            if !seg.iter().any(|&v| v != 0) {
                continue;
            }
            if objmode == 2 {
                for (i, &v) in seg.iter().enumerate() {
                    if v != 0 {
                        o.win[x0 + i] = true;
                    }
                }
                continue;
            }
            for (i, &v) in seg.iter().enumerate() {
                let x = x0 + i;
                if v != 0 && o.prio[x] > p {
                    o.idx[x] = v;
                    o.prio[x] = p;
                    o.semi[x] = objmode == 1;
                }
            }
        }
        o
    }

    #[allow(clippy::too_many_arguments)]
    fn obj_affine(&self, a1: u32, tile: i32, w: i32, h: i32, bw: i32, bh: i32, rel: i32, c256: bool, bank: u8, one_d: bool) -> Vec<u8> {
        let oam = &self.oam;
        let vram = &self.vram;
        let g = (((a1 >> 9) & 31) * 16) as usize;
        let pa = s16(rd16(oam, (g + 3) * 2));
        let pb = s16(rd16(oam, (g + 7) * 2));
        let pc = s16(rd16(oam, (g + 11) * 2));
        let pd = s16(rd16(oam, (g + 15) * 2));
        let (hw, hh) = ((bw >> 1) as i64, (bh >> 1) as i64);
        let dy = rel as i64 - hh;
        let tw = w >> 3;
        let step = if c256 { 2 } else { 1 };
        let (cx, cy) = ((w >> 1) as i64, (h >> 1) as i64);
        let mut out = vec![0u8; bw as usize];
        let mut x = pa * -hw + pb * dy;
        let mut yv = pc * -hw + pd * dy;
        let base_bank = bank << 4;
        for o in out.iter_mut() {
            let tx = (x >> 8) + cx;
            let ty = (yv >> 8) + cy;
            x += pa;
            yv += pc;
            if tx < 0 || ty < 0 || tx >= w as i64 || ty >= h as i64 {
                continue;
            }
            let (tx, ty) = (tx as i32, ty as i32);
            let t = if one_d {
                tile + ((ty >> 3) * tw + (tx >> 3)) * step
            } else {
                (tile & !(step - 1)) + (ty >> 3) * 32 + (tx >> 3) * step
            };
            let a = 0x10000 + (t & 0x3FF) as usize * 32;
            if c256 {
                *o = *vram.get(a + (ty & 7) as usize * 8 + (tx & 7) as usize).unwrap_or(&0);
            } else {
                let b = vram[a + (ty & 7) as usize * 4 + ((tx & 7) >> 1) as usize];
                let px = if tx & 1 != 0 { b >> 4 } else { b & 15 };
                if px != 0 {
                    *o = base_bank | px;
                }
            }
        }
        out
    }

    // ------------------------------------------------------------ windows
    /// Per-pixel enable bits (0-3 BG, 4 OBJ, 5 effects).
    fn window_masks(&self, y: usize, dispcnt: u32, obj: Option<&Obj>) -> [u8; W] {
        let winin = self.io16(REG_WININ);
        let winout = self.io16(REG_WINOUT);
        let mut en = [0u8; W];
        let mut covered = [false; W];
        for (k, (hreg, vreg, bits)) in [(REG_WIN0H, REG_WIN0V, winin & 0x3F), (REG_WIN1H, REG_WIN1V, (winin >> 8) & 0x3F)]
            .into_iter()
            .enumerate()
        {
            if dispcnt & (0x2000 << k) == 0 {
                continue;
            }
            let h = self.io16(hreg);
            let v = self.io16(vreg);
            if !in_range(y as u32, v >> 8, v & 0xFF) {
                continue;
            }
            let x1 = ((h >> 8) as usize).min(W);
            let x2 = ((h & 0xFF) as usize).min(W);
            for x in 0..W {
                let inside = if x1 <= x2 { x1 <= x && x < x2 } else { x < x2 || x >= x1 };
                if inside && !covered[x] {
                    covered[x] = true;
                    en[x] = bits as u8;
                }
            }
        }
        if dispcnt & 0x8000 != 0 {
            if let Some(o) = obj {
                for x in 0..W {
                    if o.win[x] && !covered[x] {
                        covered[x] = true;
                        en[x] = ((winout >> 8) & 0x3F) as u8;
                    }
                }
            }
        }
        for x in 0..W {
            if !covered[x] {
                en[x] = (winout & 0x3F) as u8;
            }
        }
        en
    }

    // ------------------------------------------------------------ line
    fn put(&mut self, x: usize, off: usize, lo: u8, hi: u8) {
        self.ppu.fb[off + 2 * x] = lo;
        self.ppu.fb[off + 2 * x + 1] = hi;
    }

    pub fn render_line(&mut self, y: usize) {
        let dispcnt = self.io16(REG_DISPCNT);
        let off = y * W * 2;
        if dispcnt & 0x80 != 0 {
            for x in 0..W {
                self.put(x, off, 0xFF, 0x7F);
            }
            self.advance_ref();
            return;
        }
        let mode = dispcnt & 7;
        let bg_mx = (self.io16(REG_MOSAIC) & 0xF) as usize + 1;
        let mut layers: Vec<(u32, usize, [u8; W])> = Vec::with_capacity(4);
        let mut direct = None;
        let mut buf = [0u8; W];
        let cnt_of = |g: &Gba, bg: usize| g.io16(REG_BG0CNT + 2 * bg);
        let mos = |line: [u8; W], cnt: u32| -> [u8; W] {
            if cnt & 0x40 != 0 && bg_mx > 1 {
                let v = mosaic_x(&line, bg_mx, 0);
                let mut out = [0u8; W];
                out.copy_from_slice(&v);
                out
            } else {
                line
            }
        };
        let text_bgs: &[usize] = match mode {
            0 => &[0, 1, 2, 3],
            1 => &[0, 1],
            _ => &[],
        };
        for &bg in text_bgs {
            if dispcnt & (0x100 << bg) != 0 {
                let cnt = cnt_of(self, bg);
                self.text_bg(bg, y, &mut buf);
                layers.push((cnt & 3, bg, mos(buf, cnt)));
            }
        }
        let affine_bgs: &[usize] = match mode {
            1 => &[2],
            2 => &[2, 3],
            _ => &[],
        };
        for &bg in affine_bgs {
            if dispcnt & (0x100 << bg) != 0 {
                let cnt = cnt_of(self, bg);
                self.affine_bg(bg, y, &mut buf);
                layers.push((cnt & 3, bg, mos(buf, cnt)));
            }
        }
        if mode >= 3 && dispcnt & 0x400 != 0 {
            let cnt = cnt_of(self, 2);
            if mode == 4 {
                self.bitmap4(dispcnt, y, &mut buf);
                layers.push((cnt & 3, 2, mos(buf, cnt)));
            } else if mode == 3 || mode == 5 {
                let (mut lo, mut hi, mut opq) = self.bitmap_direct(mode, dispcnt, y);
                if cnt & 0x40 != 0 && bg_mx > 1 {
                    lo = mos(lo, cnt);
                    hi = mos(hi, cnt);
                    let o: Vec<u8> = opq.iter().map(|&b| b as u8).collect();
                    let o = mosaic_x(&o, bg_mx, 0);
                    for i in 0..W {
                        opq[i] = o[i] != 0;
                    }
                }
                direct = Some((lo, hi, opq, cnt & 3));
            }
        }

        let obj = if dispcnt & 0x1000 != 0 { Some(self.render_objs(y, mode, dispcnt)) } else { None };
        let enable = if dispcnt & 0xE000 != 0 { Some(self.window_masks(y, dispcnt, obj.as_ref())) } else { None };
        let en = |x: usize, lid: u8| enable.map_or(true, |e| e[x] >> lid & 1 != 0);

        if let Some((lo_b, hi_b, opq, prio)) = direct {
            let (bd_lo, bd_hi) = (self.pal[0], self.pal[1]);
            for x in 0..W {
                let m = opq[x] && en(x, 2);
                let (mut lo, mut hi) = if m { (lo_b[x], hi_b[x]) } else { (bd_lo, bd_hi) };
                if let Some(o) = &obj {
                    let v = o.idx[x];
                    if v != 0 && ((o.prio[x] as u32) <= prio || !m) && en(x, 4) {
                        lo = self.pal[512 + 2 * v as usize];
                        hi = self.pal[513 + 2 * v as usize];
                    }
                }
                self.put(x, off, lo, hi);
            }
            self.advance_ref();
            return;
        }

        // painter order: priority 3 -> 0; at equal priority BG3..BG0, then OBJ on top
        layers.sort_by_key(|l| (std::cmp::Reverse(l.0), std::cmp::Reverse(l.1)));
        let bldcnt = self.io16(REG_BLDCNT);
        let bmode = (bldcnt >> 6) & 3;
        let bld = self.io16(REG_BLDALPHA);
        let eva = (bld & 31).min(16);
        let evb = ((bld >> 8) & 31).min(16);
        let evy = (self.io16(REG_BLDY) & 31).min(16);

        let mut top = [0u8; W];
        let mut top_id = [ID_BACKDROP; W];
        let mut sec = [0u8; W];
        let mut sec_id = [ID_NONE; W];
        let mut top_semi = [false; W];
        let mut li = 0;
        for p in (0..4u32).rev() {
            while li < layers.len() && layers[li].0 == p {
                let (_, bg, ref idx) = layers[li];
                for x in 0..W {
                    if idx[x] != 0 && en(x, bg as u8) {
                        sec[x] = top[x];
                        sec_id[x] = top_id[x];
                        top[x] = idx[x];
                        top_id[x] = bg as u8;
                        top_semi[x] = false;
                    }
                }
                li += 1;
            }
            if let Some(o) = &obj {
                for x in 0..W {
                    if o.idx[x] != 0 && o.prio[x] as u32 == p && en(x, ID_OBJ) {
                        sec[x] = top[x];
                        sec_id[x] = top_id[x];
                        top[x] = o.idx[x];
                        top_id[x] = ID_OBJ;
                        top_semi[x] = o.semi[x];
                    }
                }
            }
        }

        let pal = &self.pal;
        let colour = |idx: u8, id: u8| -> (u8, u8) {
            let o = if id == ID_OBJ { 512 } else { 0 } + 2 * idx as usize;
            (pal[o], pal[o + 1])
        };
        let mut out_lo = [0u8; W];
        let mut out_hi = [0u8; W];
        let mut effects = [0u8; W]; // 0 none, 1 alpha, 2 brightness
        let mut any = false;
        for x in 0..W {
            let (lo, hi) = colour(top[x], top_id[x]);
            out_lo[x] = lo;
            out_hi[x] = hi;
            let eff = en(x, 5);
            let first = top_id[x] <= 5 && bldcnt >> top_id[x] & 1 != 0;
            let second = sec_id[x] <= 5 && bldcnt >> (8 + sec_id[x]) & 1 != 0;
            let alpha = (top_semi[x] || (first && bmode == 1)) && second && eff;
            let bright = !alpha && first && eff && (bmode == 2 || bmode == 3);
            if alpha {
                effects[x] = 1;
                any = true;
            } else if bright {
                effects[x] = 2;
                any = true;
            }
        }
        if !any {
            for x in 0..W {
                self.put(x, off, out_lo[x], out_hi[x]);
            }
            self.advance_ref();
            return;
        }
        for x in 0..W {
            let (lo, hi) = (out_lo[x] as u32, out_hi[x] as u32);
            let mut ch = [lo & 31, (lo >> 5) | ((hi & 3) << 3), (hi >> 2) & 31];
            if effects[x] == 1 {
                let (slo, shi) = colour(sec[x], sec_id[x]);
                let (slo, shi) = (slo as u32, shi as u32);
                let sch = [slo & 31, (slo >> 5) | ((shi & 3) << 3), (shi >> 2) & 31];
                for k in 0..3 {
                    ch[k] = ((ch[k] * eva + sch[k] * evb) >> 4).min(31);
                }
            } else if effects[x] == 2 {
                for c in ch.iter_mut() {
                    *c = if bmode == 2 { (*c + ((31 - *c) * evy >> 4)).min(31) } else { *c - (*c * evy >> 4) };
                }
            }
            let lo = (ch[0] | ((ch[1] & 7) << 5)) as u8;
            let hi = (((ch[1] >> 3) & 3) | (ch[2] << 2)) as u8;
            out_lo[x] = lo;
            out_hi[x] = hi;
        }
        for x in 0..W {
            self.put(x, off, out_lo[x], out_hi[x]);
        }
        self.advance_ref();
    }
}
