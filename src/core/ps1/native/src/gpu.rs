//! GPU: GP0 drawing (software rasterizer into 1024×512 15-bit VRAM), GP1
//! display control, GPUSTAT/GPUREAD, and scanline timing.

pub const VRAM_W: usize = 1024;
pub const VRAM_H: usize = 512;

/// NTSC timing, in GPU video clocks (CPU clock × 11/7).
pub const CLOCKS_PER_LINE: u32 = 3413;
pub const LINES_NTSC: u32 = 263;
pub const LINES_PAL: u32 = 314;
pub const CLOCKS_PER_LINE_PAL: u32 = 3406;

const DITHER: [[i32; 4]; 4] = [[-4, 0, -3, 1], [2, -2, 3, -1], [-3, 1, -4, 0], [3, -1, 2, -2]];

#[derive(Clone, Copy, Default)]
struct Vtx {
    x: i32,
    y: i32,
    r: i32,
    g: i32,
    b: i32,
    u: i32,
    v: i32,
}

#[derive(Clone, Copy, Default)]
struct Tex {
    page_x: u32,
    page_y: u32,
    depth: u32,
    clut_x: u32,
    clut_y: u32,
}

#[derive(Clone, Copy)]
struct Transfer {
    x: u32,
    y: u32,
    w: u32,
    h: u32,
    cx: u32,
    cy: u32,
}

impl Transfer {
    fn next(&mut self) -> (usize, bool) {
        let a = (((self.y + self.cy) & 511) as usize) * VRAM_W + ((self.x + self.cx) & 1023) as usize;
        self.cx += 1;
        if self.cx == self.w {
            self.cx = 0;
            self.cy += 1;
        }
        (a, self.cy >= self.h)
    }
}

#[derive(Clone)]
pub struct Gpu {
    pub vram: Vec<u16>,
    // drawing environment
    page: u32,          // E1 bits 0..8 (+ bit 11 texture disable)
    dither: bool,
    draw_to_display: bool,
    tex_disable: bool,
    flip_x: bool,
    flip_y: bool,
    tw: (u32, u32, u32, u32), // mask x, mask y, offset x, offset y (8-pixel units)
    area: (i32, i32, i32, i32),
    offset: (i32, i32),
    mask_set: bool,
    mask_check: bool,
    allow_tex_disable: bool,
    // display
    pub disp_x: u32,
    pub disp_y: u32,
    pub hrange: (u32, u32),
    pub vrange: (u32, u32),
    pub mode: u32,
    pub display_off: bool,
    dma_dir: u32,
    pub irq_flag: bool,
    pub irq_pending: bool,
    // command processing
    fifo: Vec<u32>,
    need: usize,
    poly_line: bool,
    upload: Option<Transfer>,
    download: Option<Transfer>,
    read_latch: u32,
    // timing
    pub line: u32,
    pub clock: u32,
    pub field: bool,
    pub frame_done: bool,
    pub frames: u64,
}

#[inline(always)]
fn sext11(v: u32) -> i32 {
    ((v as i32) << 21) >> 21
}

fn cmd_len(op: u32) -> usize {
    match op {
        0x02 => 3,
        0x20..=0x3F => {
            let quad = op & 0x08 != 0;
            let tex = op & 0x04 != 0;
            let gour = op & 0x10 != 0;
            let n = if quad { 4 } else { 3 };
            1 + n * (1 + tex as usize) + if gour { n - 1 } else { 0 }
        }
        0x40..=0x5F => {
            let gour = op & 0x10 != 0;
            if gour { 4 } else { 3 } // polylines continue past this (see gp0)
        }
        0x60..=0x7F => {
            let tex = op & 0x04 != 0;
            let var = (op >> 3) & 3 == 0;
            2 + tex as usize + var as usize
        }
        0x80..=0x9F => 4,
        0xA0..=0xDF => 3,
        _ => 1,
    }
}

impl Gpu {
    pub fn new() -> Self {
        let mut g = Gpu {
            vram: vec![0; VRAM_W * VRAM_H],
            page: 0,
            dither: false,
            draw_to_display: false,
            tex_disable: false,
            flip_x: false,
            flip_y: false,
            tw: (0, 0, 0, 0),
            area: (0, 0, 0, 0),
            offset: (0, 0),
            mask_set: false,
            mask_check: false,
            allow_tex_disable: false,
            disp_x: 0,
            disp_y: 0,
            hrange: (0x200, 0xC00),
            vrange: (0x10, 0x100),
            mode: 0,
            display_off: true,
            dma_dir: 0,
            irq_flag: false,
            irq_pending: false,
            fifo: Vec::with_capacity(16),
            need: 0,
            poly_line: false,
            upload: None,
            download: None,
            read_latch: 0,
            line: 0,
            clock: 0,
            field: false,
            frame_done: false,
            frames: 0,
        };
        g.reset();
        g
    }

    fn reset(&mut self) {
        self.page = 0;
        self.dither = false;
        self.draw_to_display = false;
        self.tex_disable = false;
        self.flip_x = false;
        self.flip_y = false;
        self.tw = (0, 0, 0, 0);
        self.area = (0, 0, 0, 0);
        self.offset = (0, 0);
        self.mask_set = false;
        self.mask_check = false;
        self.disp_x = 0;
        self.disp_y = 0;
        self.hrange = (0x200, 0xC00);
        self.vrange = (0x10, 0x100);
        self.mode = 0;
        self.display_off = true;
        self.dma_dir = 0;
        self.irq_flag = false;
        self.fifo.clear();
        self.need = 0;
        self.upload = None;
        self.download = None;
    }

    pub fn pal(&self) -> bool {
        self.mode & 0x08 != 0
    }

    pub fn interlaced(&self) -> bool {
        self.mode & 0x20 != 0
    }

    /// Display width in pixels for the current mode.
    pub fn display_width(&self) -> u32 {
        if self.mode & 0x40 != 0 {
            368
        } else {
            [256, 320, 512, 640][(self.mode & 3) as usize]
        }
    }

    pub fn display_height(&self) -> u32 {
        let lines = self.vrange.1.saturating_sub(self.vrange.0).clamp(1, if self.pal() { 288 } else { 240 });
        if self.mode & 0x04 != 0 && self.interlaced() { lines * 2 } else { lines }
    }

    pub fn dot_divider(&self) -> u32 {
        if self.mode & 0x40 != 0 {
            7
        } else {
            [10, 8, 5, 4][(self.mode & 3) as usize]
        }
    }

    pub fn status(&self) -> u32 {
        let mut s = self.page & 0x1FF;
        s |= (self.dither as u32) << 9;
        s |= (self.draw_to_display as u32) << 10;
        s |= (self.mask_set as u32) << 11;
        s |= (self.mask_check as u32) << 12;
        // interlace field: always 1 when not interlaced
        s |= ((!self.interlaced() || self.field) as u32) << 13;
        s |= ((self.mode >> 7) & 1) << 14;
        s |= (self.tex_disable as u32) << 15;
        s |= ((self.mode >> 6) & 1) << 16;
        s |= (self.mode & 3) << 17;
        s |= ((self.mode >> 2) & 1) << 19;
        s |= ((self.mode >> 3) & 1) << 20;
        s |= ((self.mode >> 4) & 1) << 21;
        s |= ((self.mode >> 5) & 1) << 22;
        s |= (self.display_off as u32) << 23;
        s |= (self.irq_flag as u32) << 24;
        let ready_cmd = self.upload.is_none();
        let ready_read = self.download.is_some();
        let ready_dma = true;
        s |= (ready_cmd as u32) << 26;
        s |= (ready_read as u32) << 27;
        s |= (ready_dma as u32) << 28;
        s |= self.dma_dir << 29;
        let req = match self.dma_dir {
            0 => false,
            1 => true,
            2 => ready_dma,
            _ => ready_read,
        };
        s |= (req as u32) << 25;
        // even/odd line (non-interlaced) or field (interlaced); 0 during vblank
        let vb = self.in_vblank();
        let odd = if self.interlaced() { self.field } else { self.line & 1 == 1 };
        s |= ((odd && !vb) as u32) << 31;
        s
    }

    pub fn in_vblank(&self) -> bool {
        self.line < self.vrange.0 || self.line >= self.vrange.1
    }

    pub fn read(&mut self) -> u32 {
        if let Some(mut t) = self.download {
            let (a0, _) = t.next();
            let (a1, done) = t.next();
            let v = self.vram[a0] as u32 | ((self.vram[a1] as u32) << 16);
            self.download = if done { None } else { Some(t) };
            self.read_latch = v;
            return v;
        }
        self.read_latch
    }

    // ------------------------------------------------------------ GP1
    pub fn gp1(&mut self, v: u32) {
        match v >> 24 {
            0x00 => self.reset(),
            0x01 => {
                self.fifo.clear();
                self.need = 0;
                self.upload = None;
            }
            0x02 => self.irq_flag = false,
            0x03 => self.display_off = v & 1 != 0,
            0x04 => self.dma_dir = v & 3,
            0x05 => {
                self.disp_x = v & 0x3FE;
                self.disp_y = (v >> 10) & 0x1FF;
            }
            0x06 => self.hrange = (v & 0xFFF, (v >> 12) & 0xFFF),
            0x07 => self.vrange = (v & 0x3FF, (v >> 10) & 0x3FF),
            0x08 => self.mode = v & 0xFF,
            0x09 => self.allow_tex_disable = v & 1 != 0,
            0x10..=0x1F => {
                self.read_latch = match v & 7 {
                    2 => self.tw.0 | (self.tw.1 << 5) | (self.tw.2 << 10) | (self.tw.3 << 15),
                    3 => (self.area.0 as u32 & 0x3FF) | ((self.area.1 as u32 & 0x3FF) << 10),
                    4 => (self.area.2 as u32 & 0x3FF) | ((self.area.3 as u32 & 0x3FF) << 10),
                    5 => (self.offset.0 as u32 & 0x7FF) | ((self.offset.1 as u32 & 0x7FF) << 11),
                    7 => 2,
                    _ => self.read_latch,
                };
            }
            _ => {}
        }
    }

    // ------------------------------------------------------------ GP0
    pub fn gp0(&mut self, w: u32) {
        if let Some(mut t) = self.upload {
            for h in [w as u16, (w >> 16) as u16] {
                let (a, done) = t.next();
                if !(self.mask_check && self.vram[a] & 0x8000 != 0) {
                    self.vram[a] = h | if self.mask_set { 0x8000 } else { 0 };
                }
                if done {
                    self.upload = None;
                    return;
                }
            }
            self.upload = Some(t);
            return;
        }
        if self.poly_line {
            // terminator ends the polyline; otherwise each vertex draws a segment
            if w & 0xF000_F000 == 0x5000_5000 {
                self.poly_line = false;
                self.fifo.clear();
                self.need = 0;
                return;
            }
            self.fifo.push(w);
            let gour = self.fifo[0] & 0x1000_0000 != 0;
            let per = if gour { 2 } else { 1 };
            if self.fifo.len() >= self.need + per {
                self.polyline_segment();
            }
            return;
        }
        if self.fifo.is_empty() {
            self.need = cmd_len(w >> 24);
        }
        self.fifo.push(w);
        if self.fifo.len() < self.need {
            return;
        }
        let op = self.fifo[0] >> 24;
        match op {
            0x00 | 0x03..=0x1E => {}
            0x01 => {}
            0x02 => self.fill(),
            0x1F => {
                self.irq_flag = true;
                self.irq_pending = true;
            }
            0x20..=0x3F => self.polygon(op),
            0x40..=0x5F => {
                if op & 0x08 != 0 {
                    // polyline: draw the first segment, then keep streaming
                    self.line_from_fifo(op);
                    self.poly_line = true;
                    return;
                }
                self.line_from_fifo(op);
            }
            0x60..=0x7F => self.rect(op),
            0x80..=0x9F => self.copy(),
            0xA0..=0xBF => {
                let (x, y, w, h) = self.xfer_args();
                self.upload = Some(Transfer { x, y, w, h, cx: 0, cy: 0 });
            }
            0xC0..=0xDF => {
                let (x, y, w, h) = self.xfer_args();
                self.download = Some(Transfer { x, y, w, h, cx: 0, cy: 0 });
            }
            0xE1 => {
                let v = self.fifo[0];
                self.page = v & 0x1FF;
                self.dither = v & (1 << 9) != 0;
                self.draw_to_display = v & (1 << 10) != 0;
                self.tex_disable = self.allow_tex_disable && v & (1 << 11) != 0;
                self.flip_x = v & (1 << 12) != 0;
                self.flip_y = v & (1 << 13) != 0;
            }
            0xE2 => {
                let v = self.fifo[0];
                self.tw = (v & 0x1F, (v >> 5) & 0x1F, (v >> 10) & 0x1F, (v >> 15) & 0x1F);
            }
            0xE3 => {
                let v = self.fifo[0];
                self.area.0 = (v & 0x3FF) as i32;
                self.area.1 = ((v >> 10) & 0x1FF) as i32;
            }
            0xE4 => {
                let v = self.fifo[0];
                self.area.2 = (v & 0x3FF) as i32;
                self.area.3 = ((v >> 10) & 0x1FF) as i32;
            }
            0xE5 => {
                let v = self.fifo[0];
                self.offset = (sext11(v & 0x7FF), sext11((v >> 11) & 0x7FF));
            }
            0xE6 => {
                let v = self.fifo[0];
                self.mask_set = v & 1 != 0;
                self.mask_check = v & 2 != 0;
            }
            _ => {}
        }
        self.fifo.clear();
        self.need = 0;
    }

    fn xfer_args(&self) -> (u32, u32, u32, u32) {
        let p = self.fifo[1];
        let s = self.fifo[2];
        let x = p & 0x3FF;
        let y = (p >> 16) & 0x1FF;
        let w = ((s & 0xFFFF).wrapping_sub(1) & 0x3FF) + 1;
        let h = (((s >> 16) & 0xFFFF).wrapping_sub(1) & 0x1FF) + 1;
        (x, y, w, h)
    }

    // ------------------------------------------------------------ primitives
    fn fill(&mut self) {
        let c = self.fifo[0];
        let p = self.fifo[1];
        let s = self.fifo[2];
        let col = rgb24_to_15(c);
        let x0 = p & 0x3F0;
        let y0 = (p >> 16) & 0x1FF;
        let w = ((s & 0x3FF) + 0xF) & !0xF;
        let h = (s >> 16) & 0x1FF;
        for y in 0..h {
            let row = (((y0 + y) & 511) as usize) * VRAM_W;
            for x in 0..w {
                self.vram[row + ((x0 + x) & 1023) as usize] = col;
            }
        }
    }

    fn copy(&mut self) {
        let s = self.fifo[1];
        let d = self.fifo[2];
        let z = self.fifo[3];
        let (sx, sy) = (s & 0x3FF, (s >> 16) & 0x1FF);
        let (dx, dy) = (d & 0x3FF, (d >> 16) & 0x1FF);
        let w = ((z & 0xFFFF).wrapping_sub(1) & 0x3FF) + 1;
        let h = (((z >> 16) & 0xFFFF).wrapping_sub(1) & 0x1FF) + 1;
        let mut buf = vec![0u16; w as usize];
        for y in 0..h {
            let srow = (((sy + y) & 511) as usize) * VRAM_W;
            let drow = (((dy + y) & 511) as usize) * VRAM_W;
            for x in 0..w {
                buf[x as usize] = self.vram[srow + ((sx + x) & 1023) as usize];
            }
            for x in 0..w {
                let a = drow + ((dx + x) & 1023) as usize;
                if self.mask_check && self.vram[a] & 0x8000 != 0 {
                    continue;
                }
                self.vram[a] = buf[x as usize] | if self.mask_set { 0x8000 } else { 0 };
            }
        }
    }

    fn vertex_xy(&self, w: u32) -> (i32, i32) {
        (sext11(w & 0x7FF) + self.offset.0, sext11((w >> 16) & 0x7FF) + self.offset.1)
    }

    fn polygon(&mut self, op: u32) {
        let quad = op & 0x08 != 0;
        let tex = op & 0x04 != 0;
        let gour = op & 0x10 != 0;
        let semi = op & 0x02 != 0;
        let raw = op & 0x01 != 0;
        let n = if quad { 4 } else { 3 };
        let mut v = [Vtx::default(); 4];
        let mut idx = 0;
        let mut col = self.fifo[0];
        let mut clut = 0u32;
        let mut page = self.page;
        for (k, vk) in v.iter_mut().enumerate().take(n) {
            if k > 0 && gour {
                col = self.fifo[idx];
                idx += 1;
            }
            if k == 0 {
                idx = 1;
            }
            let (x, y) = self.vertex_xy(self.fifo[idx]);
            idx += 1;
            vk.x = x;
            vk.y = y;
            vk.r = (col & 0xFF) as i32;
            vk.g = ((col >> 8) & 0xFF) as i32;
            vk.b = ((col >> 16) & 0xFF) as i32;
            if tex {
                let uvw = self.fifo[idx];
                idx += 1;
                vk.u = (uvw & 0xFF) as i32;
                vk.v = ((uvw >> 8) & 0xFF) as i32;
                if k == 0 {
                    clut = uvw >> 16;
                }
                if k == 1 {
                    page = (uvw >> 16) & 0x1FF;
                    // polygons update the drawing mode's texture page
                    self.page = (self.page & !0x1FF) | page;
                    if self.allow_tex_disable {
                        self.tex_disable = (uvw >> 16) & 0x800 != 0;
                    }
                }
            }
        }
        let t = Tex {
            page_x: (page & 0xF) * 64,
            page_y: ((page >> 4) & 1) * 256,
            depth: (page >> 7) & 3,
            clut_x: (clut & 0x3F) * 16,
            clut_y: (clut >> 6) & 0x1FF,
        };
        let blend = if semi { Some((page >> 5) & 3) } else { None };
        let tex = tex && !self.tex_disable;
        let dither = self.dither && (gour || (tex && !raw));
        self.triangle(v[0], v[1], v[2], tex.then_some(t), raw, gour, blend, dither);
        if quad {
            self.triangle(v[1], v[2], v[3], tex.then_some(t), raw, gour, blend, dither);
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn triangle(&mut self, a: Vtx, b: Vtx, c: Vtx, tex: Option<Tex>, raw: bool, gour: bool,
                blend: Option<u32>, dither: bool) {
        let (v0, mut v1, mut v2) = (a, b, c);
        let area = (v1.x - v0.x) as i64 * (v2.y - v0.y) as i64 - (v2.x - v0.x) as i64 * (v1.y - v0.y) as i64;
        if area == 0 {
            return;
        }
        if area < 0 {
            std::mem::swap(&mut v1, &mut v2);
        }
        let area = area.abs();
        let xs = [v0.x, v1.x, v2.x];
        let ys = [v0.y, v1.y, v2.y];
        let (minx, maxx) = (*xs.iter().min().unwrap(), *xs.iter().max().unwrap());
        let (miny, maxy) = (*ys.iter().min().unwrap(), *ys.iter().max().unwrap());
        if maxx - minx >= 1024 || maxy - miny >= 512 {
            return;
        }
        let x0 = minx.max(self.area.0);
        let x1 = (maxx - 1).min(self.area.2);
        let y0 = miny.max(self.area.1);
        let y1 = (maxy - 1).min(self.area.3);
        if x0 > x1 || y0 > y1 {
            return;
        }

        // edge functions (counter-clockwise after the swap), top-left fill rule
        let edge = |p: &Vtx, q: &Vtx| -> (i64, i64, i64, i64) {
            let a = (p.y - q.y) as i64;
            let b = (q.x - p.x) as i64;
            let c0 = p.x as i64 * q.y as i64 - p.y as i64 * q.x as i64;
            // inside lies to +x of a left edge (a > 0) and to +y of a top edge (a == 0, b > 0)
            let tl = (a > 0 || (a == 0 && b > 0)) as i64;
            (a, b, c0, tl - 1)
        };
        let e0 = edge(&v1, &v2);
        let e1 = edge(&v2, &v0);
        let e2 = edge(&v0, &v1);

        // attribute planes: value(x,y) = base + dx*(x-v0.x) + dy*(y-v0.y), 16.16 fixed
        let plane = |f: fn(&Vtx) -> i32| -> (i64, i64, i64) {
            let (a0, a1, a2) = (f(&v0) as i64, f(&v1) as i64, f(&v2) as i64);
            let dx = ((a1 - a0) * (v2.y - v0.y) as i64 - (a2 - a0) * (v1.y - v0.y) as i64) * 65536 / area;
            let dy = ((a2 - a0) * (v1.x - v0.x) as i64 - (a1 - a0) * (v2.x - v0.x) as i64) * 65536 / area;
            (a0 * 65536 + 32768, dx, dy)
        };
        let pr = plane(|v| v.r);
        let pg = plane(|v| v.g);
        let pb = plane(|v| v.b);
        let pu = plane(|v| v.u);
        let pv = plane(|v| v.v);
        let flat = (v0.r, v0.g, v0.b);

        for y in y0..=y1 {
            for x in x0..=x1 {
                let w0 = e0.0 * x as i64 + e0.1 * y as i64 + e0.2;
                let w1 = e1.0 * x as i64 + e1.1 * y as i64 + e1.2;
                let w2 = e2.0 * x as i64 + e2.1 * y as i64 + e2.2;
                if w0 + e0.3 < 0 || w1 + e1.3 < 0 || w2 + e2.3 < 0 {
                    continue;
                }
                let dxp = (x - v0.x) as i64;
                let dyp = (y - v0.y) as i64;
                let at = |p: (i64, i64, i64)| ((p.0 + p.1 * dxp + p.2 * dyp) >> 16) as i32;
                let (r, g, b) = if gour { (at(pr), at(pg), at(pb)) } else { flat };
                match tex {
                    Some(t) => {
                        let u = at(pu).clamp(0, 255) as u32;
                        let v = at(pv).clamp(0, 255) as u32;
                        let texel = self.texel(&t, u, v);
                        if texel == 0 {
                            continue;
                        }
                        self.plot_textured(x, y, texel, r, g, b, raw, blend, dither);
                    }
                    None => self.plot_shaded(x, y, r, g, b, blend, dither),
                }
            }
        }
    }

    fn texel(&self, t: &Tex, u: u32, v: u32) -> u16 {
        let (mx, my, ox, oy) = self.tw;
        let u = (u & !(mx * 8)) | ((ox & mx) * 8);
        let v = (v & !(my * 8)) | ((oy & my) * 8);
        let row = (((t.page_y + v) & 511) as usize) * VRAM_W;
        match t.depth {
            0 => {
                let w = self.vram[row + ((t.page_x + u / 4) & 1023) as usize];
                let i = (w >> ((u & 3) * 4)) & 0xF;
                self.vram[(t.clut_y as usize) * VRAM_W + ((t.clut_x + i as u32) & 1023) as usize]
            }
            1 => {
                let w = self.vram[row + ((t.page_x + u / 2) & 1023) as usize];
                let i = (w >> ((u & 1) * 8)) & 0xFF;
                self.vram[(t.clut_y as usize) * VRAM_W + ((t.clut_x + i as u32) & 1023) as usize]
            }
            _ => self.vram[row + ((t.page_x + u) & 1023) as usize],
        }
    }

    #[inline]
    fn put(&mut self, x: i32, y: i32, col: u16, bit15: u16, blend: Option<u32>) {
        let a = (y as usize & 511) * VRAM_W + (x as usize & 1023);
        let dst = self.vram[a];
        if self.mask_check && dst & 0x8000 != 0 {
            return;
        }
        let out = match blend {
            Some(m) => blend_px(dst, col, m),
            None => col,
        };
        self.vram[a] = (out & 0x7FFF) | bit15 | if self.mask_set { 0x8000 } else { 0 };
    }

    #[allow(clippy::too_many_arguments)]
    fn plot_shaded(&mut self, x: i32, y: i32, r: i32, g: i32, b: i32, blend: Option<u32>, dither: bool) {
        let d = if dither { DITHER[(y & 3) as usize][(x & 3) as usize] } else { 0 };
        let q = |c: i32| ((c + d).clamp(0, 255) >> 3) as u16;
        let col = q(r) | (q(g) << 5) | (q(b) << 10);
        self.put(x, y, col, 0, blend);
    }

    #[allow(clippy::too_many_arguments)]
    fn plot_textured(&mut self, x: i32, y: i32, texel: u16, r: i32, g: i32, b: i32, raw: bool,
                     blend: Option<u32>, dither: bool) {
        let bit15 = texel & 0x8000;
        let col = if raw {
            texel & 0x7FFF
        } else {
            let d = if dither { DITHER[(y & 3) as usize][(x & 3) as usize] } else { 0 };
            let m = |t5: u16, c: i32| ((((t5 as i32) * c) >> 4) + d).clamp(0, 255) as u16 >> 3;
            m(texel & 0x1F, r) | (m((texel >> 5) & 0x1F, g) << 5) | (m((texel >> 10) & 0x1F, b) << 10)
        };
        // textured pixels are only blended when the texel's bit 15 is set
        let blend = if bit15 != 0 { blend } else { None };
        self.put(x, y, col, bit15, blend);
    }

    fn rect(&mut self, op: u32) {
        let tex = op & 0x04 != 0;
        let semi = op & 0x02 != 0;
        let raw = op & 0x01 != 0;
        let col = self.fifo[0];
        let (x, y) = self.vertex_xy(self.fifo[1]);
        let mut i = 2;
        let (mut u0, mut v0, mut clut) = (0u32, 0u32, 0u32);
        if tex {
            let w = self.fifo[2];
            u0 = w & 0xFF;
            v0 = (w >> 8) & 0xFF;
            clut = w >> 16;
            i = 3;
        }
        let (w, h) = match (op >> 3) & 3 {
            0 => {
                let s = self.fifo[i];
                ((s & 0x3FF) as i32, ((s >> 16) & 0x1FF) as i32)
            }
            1 => (1, 1),
            2 => (8, 8),
            _ => (16, 16),
        };
        let page = self.page;
        let t = Tex {
            page_x: (page & 0xF) * 64,
            page_y: ((page >> 4) & 1) * 256,
            depth: (page >> 7) & 3,
            clut_x: (clut & 0x3F) * 16,
            clut_y: (clut >> 6) & 0x1FF,
        };
        let blend = if semi { Some((page >> 5) & 3) } else { None };
        let tex = tex && !self.tex_disable;
        let (r, g, b) = ((col & 0xFF) as i32, ((col >> 8) & 0xFF) as i32, ((col >> 16) & 0xFF) as i32);
        for dy in 0..h {
            let py = y + dy;
            if py < self.area.1 || py > self.area.3 {
                continue;
            }
            for dx in 0..w {
                let px = x + dx;
                if px < self.area.0 || px > self.area.2 {
                    continue;
                }
                if tex {
                    let u = if self.flip_x { u0.wrapping_sub(dx as u32) } else { u0 + dx as u32 } & 0xFF;
                    let v = if self.flip_y { v0.wrapping_sub(dy as u32) } else { v0 + dy as u32 } & 0xFF;
                    let texel = self.texel(&t, u, v);
                    if texel == 0 {
                        continue;
                    }
                    self.plot_textured(px, py, texel, r, g, b, raw, blend, false);
                } else {
                    self.plot_shaded(px, py, r, g, b, blend, false);
                }
            }
        }
    }

    fn line_from_fifo(&mut self, op: u32) {
        let gour = op & 0x10 != 0;
        let c0 = self.fifo[0];
        let p0 = self.fifo[1];
        let (c1, p1) = if gour { (self.fifo[2], self.fifo[3]) } else { (c0, self.fifo[2]) };
        self.draw_line(op, c0, p0, c1, p1);
        // keep the last vertex for polylines: fifo = [cmd, p1] (+ c1 for gouraud)
        let first = (self.fifo[0] & 0xFF00_0000) | (c1 & 0x00FF_FFFF);
        self.fifo.clear();
        self.fifo.push(first);
        self.fifo.push(p1);
        self.need = 2;
    }

    fn polyline_segment(&mut self) {
        let op = self.fifo[0] >> 24;
        let gour = op & 0x10 != 0;
        let c0 = self.fifo[0];
        let p0 = self.fifo[1];
        let (c1, p1) = if gour { (self.fifo[2], self.fifo[3]) } else { (c0, self.fifo[2]) };
        self.draw_line(op, c0, p0, c1, p1);
        let first = (self.fifo[0] & 0xFF00_0000) | (c1 & 0x00FF_FFFF);
        self.fifo.clear();
        self.fifo.push(first);
        self.fifo.push(p1);
        self.need = 2;
    }

    fn draw_line(&mut self, op: u32, c0: u32, p0: u32, c1: u32, p1: u32) {
        let gour = op & 0x10 != 0;
        let semi = op & 0x02 != 0;
        let (x0, y0) = self.vertex_xy(p0);
        let (x1, y1) = self.vertex_xy(p1);
        let (dx, dy) = (x1 - x0, y1 - y0);
        if dx.abs() >= 1024 || dy.abs() >= 512 {
            return;
        }
        let blend = if semi { Some((self.page >> 5) & 3) } else { None };
        let dither = self.dither && gour;
        let steps = dx.abs().max(dy.abs());
        let ch = |c: u32, s: u32| ((c >> s) & 0xFF) as i32;
        for k in 0..=steps {
            let (x, y, r, g, b) = if steps == 0 {
                (x0, y0, ch(c0, 0), ch(c0, 8), ch(c0, 16))
            } else {
                // round-to-nearest interpolation along the major axis
                let lerp = |a: i32, b: i32| a + ((b - a) as i64 * k as i64 * 2 + steps as i64).div_euclid(2 * steps as i64) as i32;
                let x = lerp(x0, x1);
                let y = lerp(y0, y1);
                if gour {
                    (x, y, lerp(ch(c0, 0), ch(c1, 0)), lerp(ch(c0, 8), ch(c1, 8)), lerp(ch(c0, 16), ch(c1, 16)))
                } else {
                    (x, y, ch(c0, 0), ch(c0, 8), ch(c0, 16))
                }
            };
            if x < self.area.0 || x > self.area.2 || y < self.area.1 || y > self.area.3 {
                continue;
            }
            self.plot_shaded(x, y, r, g, b, blend, dither);
        }
    }

    // ------------------------------------------------------------ timing
    /// Advance by `video` GPU clocks. Returns (hblank lines started, vblank entered, vblank left).
    pub fn tick(&mut self, video: u32) -> (u32, bool, bool) {
        let per_line = if self.pal() { CLOCKS_PER_LINE_PAL } else { CLOCKS_PER_LINE };
        let lines = if self.pal() { LINES_PAL } else { LINES_NTSC };
        self.clock += video;
        let mut hbl = 0;
        let mut vb_on = false;
        let mut vb_off = false;
        while self.clock >= per_line {
            self.clock -= per_line;
            let was = self.in_vblank();
            self.line += 1;
            hbl += 1;
            if self.line >= lines {
                self.line = 0;
                if self.interlaced() {
                    self.field = !self.field;
                }
            }
            let now = self.in_vblank();
            if now && !was {
                vb_on = true;
                self.frame_done = true;
                self.frames += 1;
            }
            if was && !now {
                vb_off = true;
            }
        }
        (hbl, vb_on, vb_off)
    }

    // ------------------------------------------------------------ output
    /// The displayed area as RGB888 (`width × height × 3`).
    pub fn frame_rgb(&self) -> (u32, u32, Vec<u8>) {
        let w = self.display_width();
        let h = self.display_height();
        let mut out = vec![0u8; (w * h * 3) as usize];
        if self.display_off {
            return (w, h, out);
        }
        let deep = self.mode & 0x10 != 0;
        for y in 0..h {
            let row = (((self.disp_y + y) & 511) as usize) * VRAM_W;
            let o = (y * w * 3) as usize;
            if deep {
                // 24-bit: three bytes per pixel packed across halfwords
                let base = self.disp_x as usize * 2;
                for x in 0..w as usize {
                    let byte = |k: usize| {
                        let bi = base + x * 3 + k;
                        let hw = self.vram[row + ((bi / 2) & 1023)];
                        if bi & 1 == 0 { hw as u8 } else { (hw >> 8) as u8 }
                    };
                    out[o + x * 3] = byte(0);
                    out[o + x * 3 + 1] = byte(1);
                    out[o + x * 3 + 2] = byte(2);
                }
            } else {
                for x in 0..w as usize {
                    let p = self.vram[row + ((self.disp_x as usize + x) & 1023)];
                    let e = |c: u16| ((c << 3) | (c >> 2)) as u8;
                    out[o + x * 3] = e(p & 0x1F);
                    out[o + x * 3 + 1] = e((p >> 5) & 0x1F);
                    out[o + x * 3 + 2] = e((p >> 10) & 0x1F);
                }
            }
        }
        (w, h, out)
    }
}

impl Default for Gpu {
    fn default() -> Self {
        Self::new()
    }
}

#[inline]
fn rgb24_to_15(c: u32) -> u16 {
    let r = (c & 0xFF) >> 3;
    let g = ((c >> 8) & 0xFF) >> 3;
    let b = ((c >> 16) & 0xFF) >> 3;
    (r | (g << 5) | (b << 10)) as u16
}

#[inline]
fn blend_px(back: u16, front: u16, mode: u32) -> u16 {
    let ch = |v: u16, s: u16| ((v >> s) & 0x1F) as i32;
    let mut out = 0u16;
    for s in [0u16, 5, 10] {
        let b = ch(back, s);
        let f = ch(front, s);
        let v = match mode {
            0 => (b + f) >> 1,
            1 => b + f,
            2 => b - f,
            _ => b + (f >> 2),
        }
        .clamp(0, 31);
        out |= (v as u16) << s;
    }
    out
}
