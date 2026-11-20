//! MDEC: the motion decoder (JPEG-like macroblocks to pixels), used for FMV and
//! still images. Run-length + quantized DCT input, IDCT with the program's scale
//! table, YUV to RGB, output as 4/8-bit mono or 15/24-bit colour.
//!
//! Commands are decoded as soon as their parameters are complete; the output waits
//! in a FIFO for the CPU (0x1F801820 reads) or DMA1.

use std::collections::VecDeque;

/// Stream position k -> row-major index in the block (zigzag order, row-major layout).
const ZIGZAG_ROWS: [usize; 64] = [
    0, 8, 1, 2, 9, 16, 24, 17, 10, 3, 4, 11, 18, 25, 32, 40, 33, 26, 19, 12, 5, 6, 13, 20, 27, 34, 41, 48, 56, 49,
    42, 35, 28, 21, 14, 7, 15, 22, 29, 36, 43, 50, 57, 58, 51, 44, 37, 30, 23, 31, 38, 45, 52, 59, 60, 53, 46, 39,
    47, 54, 61, 62, 55, 63,
];

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct Mdec {
    lum_q: Vec<u8>,   // 64
    col_q: Vec<u8>,   // 64
    scale: Vec<i16>,  // 64
    cmd: u32,
    remaining: u32,   // parameter words still expected
    params: Vec<u32>,
    pub out: VecDeque<u32>,
    /// CPU reads of colour output come per 8x8 quadrant (Y1, Y2, Y3, Y4 of each
    /// macroblock); DMA1 gets rows of the whole 16x16 macroblock.
    #[serde(default)]
    cpu_buf: VecDeque<u32>,
    pub dma_in: bool,
    pub dma_out: bool,
    block: u32,       // status "current block"
}

impl Default for Mdec {
    fn default() -> Self {
        Mdec {
            lum_q: vec![0; 64],
            col_q: vec![0; 64],
            scale: vec![0; 64],
            cmd: 0,
            remaining: 0,
            params: Vec::new(),
            out: VecDeque::new(),
            cpu_buf: VecDeque::new(),
            dma_in: false,
            dma_out: false,
            block: 4,
        }
    }
}

fn s10(v: u16) -> i32 {
    ((v as i32) << 22) >> 22
}

fn sext9(v: i64) -> i32 {
    (((v as i32) << 23) >> 23) as i32
}

impl Mdec {
    pub fn reset(&mut self) {
        let (lq, cq, sc) = (std::mem::take(&mut self.lum_q), std::mem::take(&mut self.col_q), std::mem::take(&mut self.scale));
        *self = Mdec { lum_q: lq, col_q: cq, scale: sc, ..Mdec::default() };
    }

    pub fn control(&mut self, v: u32) {
        if v & (1 << 31) != 0 {
            self.reset();
        }
        self.dma_in = v & (1 << 30) != 0;
        self.dma_out = v & (1 << 29) != 0;
    }

    pub fn status(&self) -> u32 {
        let depth = (self.cmd >> 27) & 3;
        let mut s = 0;
        let empty = self.out.is_empty() && self.cpu_buf.is_empty();
        if empty {
            s |= 1 << 31;
        }
        if self.remaining > 0 || !empty {
            s |= 1 << 29; // command busy
        }
        if self.dma_in && self.remaining > 0 {
            s |= 1 << 28;
        }
        if self.dma_out && !self.out.is_empty() {
            s |= 1 << 27;
        }
        s |= depth << 25;
        s |= ((self.cmd >> 26) & 1) << 24; // signed
        s |= ((self.cmd >> 25) & 1) << 23; // bit 15
        s |= (self.block & 7) << 16;
        s | (self.remaining.wrapping_sub(1) & 0xFFFF)
    }

    /// No command waiting for parameters.
    pub fn idle(&self) -> bool {
        self.remaining == 0
    }

    /// DMA1 read: words in macroblock row order.
    pub fn read_dma(&mut self) -> u32 {
        self.out.pop_front().unwrap_or(0)
    }

    /// CPU read of 0x1F801820.
    pub fn read(&mut self) -> u32 {
        if self.cpu_buf.is_empty() {
            let depth = (self.cmd >> 27) & 3;
            // words per 8-pixel block row and per macroblock
            let (row, mb) = match depth {
                2 => (6usize, 192usize),
                3 => (4, 128),
                _ => (0, 0),
            };
            if mb != 0 && self.out.len() >= mb {
                let block: Vec<u32> = self.out.drain(..mb).collect();
                for q in 0..4 {
                    for r in 0..8 {
                        for c in 0..row {
                            let y = r + (q / 2) * 8;
                            self.cpu_buf.push_back(block[y * row * 2 + (q % 2) * row + c]);
                        }
                    }
                }
            }
        }
        self.cpu_buf.pop_front().or_else(|| self.out.pop_front()).unwrap_or(0)
    }

    pub fn write(&mut self, w: u32) {
        if self.remaining == 0 {
            self.cmd = w;
            self.params.clear();
            self.remaining = match w >> 29 {
                1 => w & 0xFFFF,
                2 => if w & 1 != 0 { 32 } else { 16 },
                3 => 32,
                _ => 0,
            };
            return;
        }
        self.params.push(w);
        self.remaining -= 1;
        if self.remaining == 0 {
            self.execute();
        }
    }

    fn execute(&mut self) {
        let p = std::mem::take(&mut self.params);
        let bytes: Vec<u8> = p.iter().flat_map(|w| w.to_le_bytes()).collect();
        match self.cmd >> 29 {
            1 => self.decode(&p),
            2 => {
                self.lum_q.copy_from_slice(&bytes[..64]);
                if self.cmd & 1 != 0 {
                    self.col_q.copy_from_slice(&bytes[64..128]);
                }
            }
            3 => {
                for i in 0..64 {
                    self.scale[i] = i16::from_le_bytes([bytes[i * 2], bytes[i * 2 + 1]]);
                }
            }
            _ => {}
        }
    }

    /// Run-length decode one 8x8 block from `src` at `*pos` into row-major `blk`
    /// (coefficients carry 4 extra fraction bits), then IDCT it; false when data ran out.
    fn rl_block(&self, src: &[u16], pos: &mut usize, qt: &[u8], blk: &mut [i32; 64]) -> bool {
        blk.fill(0);
        let mut n;
        loop {
            if *pos >= src.len() {
                return false;
            }
            n = src[*pos];
            *pos += 1;
            if n != 0xFE00 {
                break;
            }
        }
        let q_scale = (n >> 10) as i32;
        let bias = |v: i32| if v == 0 { 0 } else if v < 0 { 8 } else { -8 };
        let val = s10(n & 0x3FF);
        let dc = if q_scale == 0 { val << 5 } else { ((val * qt[0] as i32) << 4) + bias(val) };
        blk[ZIGZAG_ROWS[0]] = dc.clamp(-0x4000, 0x3FFF);
        let mut k = 0usize;
        while *pos < src.len() {
            n = src[*pos];
            *pos += 1;
            k += (n >> 10) as usize + 1;
            if k < 64 {
                let val = s10(n & 0x3FF);
                let scq = q_scale * qt[k] as i32;
                let c = if scq == 0 { val << 5 } else { (((val * scq) >> 3) << 4) + bias(val) };
                blk[ZIGZAG_ROWS[k]] = c.clamp(-0x4000, 0x3FFF);
            }
            if k >= 63 {
                break;
            }
        }
        self.idct(blk);
        true
    }

    /// One IDCT pass over a row: out[j] = sat16((sum_i row[i] * S[i][j] + 0x20000) >> 18).
    fn idct_row(&self, row: &[i32], out: &mut [i32]) {
        let sc = &self.scale;
        for j in 0..8 {
            let mut sum = 0i64;
            for i in 0..8 {
                sum += row[i] as i64 * sc[i * 8 + j] as i64;
            }
            out[j] = ((sum + 0x20000) >> 18).clamp(-0x8000, 0x7FFF) as i32;
        }
    }

    fn idct(&self, blk: &mut [i32; 64]) {
        let mut p1 = [0i32; 64];
        for x in 0..8 {
            let (src, dst) = (&blk[x * 8..x * 8 + 8], &mut p1[x * 8..x * 8 + 8]);
            self.idct_row(src, dst);
        }
        let mut t = [0i32; 64];
        for x in 0..8 {
            for i in 0..8 {
                t[x * 8 + i] = p1[i * 8 + x];
            }
        }
        let mut row = [0i32; 8];
        for x in 0..8 {
            self.idct_row(&t[x * 8..x * 8 + 8], &mut row);
            for j in 0..8 {
                blk[x * 8 + j] = sext9(row[j] as i64).clamp(-128, 127);
            }
        }
    }

    fn decode(&mut self, p: &[u32]) {
        let src: Vec<u16> = p.iter().flat_map(|w| [*w as u16, (*w >> 16) as u16]).collect();
        let depth = (self.cmd >> 27) & 3;
        let signed = self.cmd & (1 << 26) != 0;
        let add = if signed { 0 } else { 0x80 };
        let bit15 = if self.cmd & (1 << 25) != 0 { 0x8000u32 } else { 0 };
        let mut pos = 0usize;
        let (lq, cq) = (self.lum_q.clone(), self.col_q.clone());
        let mut y = [0i32; 64];
        if depth <= 1 {
            // monochrome: one Y block at a time
            while self.rl_block(&src, &mut pos, &lq, &mut y) {
                let px: Vec<u32> = y.iter().map(|&v| ((v + add) & 0xFF) as u32).collect();
                if depth == 0 {
                    for c in px.chunks(8) {
                        let mut w = 0u32;
                        for (i, b) in c.iter().enumerate() {
                            w |= ((b + 8) >> 4).min(15) << (i * 4);
                        }
                        self.out.push_back(w);
                    }
                } else {
                    for c in px.chunks(4) {
                        self.out.push_back(c[0] | (c[1] << 8) | (c[2] << 16) | (c[3] << 24));
                    }
                }
            }
            return;
        }
        let mut cr = [0i32; 64];
        let mut cb = [0i32; 64];
        'mb: loop {
            if !self.rl_block(&src, &mut pos, &cq, &mut cr) || !self.rl_block(&src, &mut pos, &cq, &mut cb) {
                break;
            }
            let mut rgb = [0u32; 256];
            for (xx, yy) in [(0, 0), (8, 0), (0, 8), (8, 8)] {
                if !self.rl_block(&src, &mut pos, &lq, &mut y) {
                    break 'mb;
                }
                for py in 0..8 {
                    for px in 0..8 {
                        let ci = (px + xx) / 2 + ((py + yy) / 2) * 8;
                        let (r_, b_) = (cr[ci], cb[ci]);
                        // BT.601 with the hardware's rounding (formulas as in Mednafen / DuckStation)
                        let r = (359 * r_ + 0x80) >> 8;
                        let g = (((-88 * b_) & !0x1F) + ((-183 * r_) & !0x07) + 0x80) >> 8;
                        let b = (454 * b_ + 0x80) >> 8;
                        let l = y[px + py * 8];
                        let ch = |v: i32| ((sext9((l + v) as i64).clamp(-128, 127) + add) & 0xFF) as u32;
                        rgb[(px + xx) + (py + yy) * 16] = ch(r) | (ch(g) << 8) | (ch(b) << 16);
                    }
                }
            }
            if depth == 2 {
                let bytes: Vec<u8> = rgb.iter().flat_map(|c| [*c as u8, (*c >> 8) as u8, (*c >> 16) as u8]).collect();
                for c in bytes.chunks(4) {
                    self.out.push_back(u32::from_le_bytes([c[0], c[1], c[2], c[3]]));
                }
            } else {
                let e = |c: u32| ((c + 4) >> 3).min(0x1F);
                let p15 = |c: u32| e(c & 0xFF) | (e((c >> 8) & 0xFF) << 5) | (e((c >> 16) & 0xFF) << 10) | bit15;
                for c in rgb.chunks(2) {
                    self.out.push_back(p15(c[0]) | (p15(c[1]) << 16));
                }
            }
        }
    }
}
