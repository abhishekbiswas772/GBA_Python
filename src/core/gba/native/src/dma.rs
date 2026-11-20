//! DMA channels 0-3 (`src/core/gba/dma.py`). Transfers run synchronously when
//! triggered and charge their cycles to the CPU clock.

use serde::{Deserialize, Serialize};

use crate::cart::Save;
use crate::gba::Gba;

const REG_DMA0SAD: usize = 0x0B0;
const DMA_STRIDE: usize = 12;
const COUNT_MASK: [u32; 4] = [0x3FFF, 0x3FFF, 0x3FFF, 0xFFFF];
const SRC_MASK: [u32; 4] = [0x07FF_FFFF, 0x0FFF_FFFF, 0x0FFF_FFFF, 0x0FFF_FFFF];
const DST_MASK: [u32; 4] = [0x07FF_FFFF, 0x07FF_FFFF, 0x07FF_FFFF, 0x0FFF_FFFF];
pub const TIMING_NOW: u32 = 0;
pub const TIMING_VBLANK: u32 = 1;
pub const TIMING_HBLANK: u32 = 2;
pub const TIMING_SPECIAL: u32 = 3;
const IRQ_DMA0: u32 = 0x0100;

#[derive(Serialize, Deserialize, Default)]
pub struct Dma {
    pub src: [u32; 4],
    pub dst: [u32; 4],
    pub count: [u32; 4],
    pub ctrl: [u32; 4],
    pub active: [bool; 4],
}

impl Dma {
    pub fn new() -> Dma {
        Dma::default()
    }
}

/// A contiguous plain-memory buffer (see `plain_slice`).
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Buf {
    Ewram,
    Iwram,
    Pal,
    Vram,
    Oam,
    Rom,
}

impl Gba {
    /// (buffer, offset) if [addr, addr+n) is contiguous plain memory. ROM only for reads.
    pub fn plain_slice(&self, addr: u32, n: usize, write: bool) -> Option<(Buf, usize)> {
        let reg = addr >> 24;
        let fit = |o: usize, lim: usize, b: Buf| if o + n <= lim { Some((b, o)) } else { None };
        match reg {
            2 => fit((addr & 0x3FFFF) as usize, 0x40000, Buf::Ewram),
            3 => fit((addr & 0x7FFF) as usize, 0x8000, Buf::Iwram),
            5 => fit((addr & 0x3FF) as usize, 0x400, Buf::Pal),
            6 => {
                let mut o = (addr & 0x1FFFF) as usize;
                if o >= 0x18000 {
                    o -= 0x8000;
                }
                fit(o, 0x18000, Buf::Vram)
            }
            7 => fit((addr & 0x3FF) as usize, 0x400, Buf::Oam),
            8..=13 if !write && !(reg == 13 && matches!(self.save, Save::Eeprom(_))) => {
                fit((addr & 0x1FF_FFFF) as usize, self.rom.len(), Buf::Rom)
            }
            _ => None,
        }
    }

    pub fn buf(&self, b: Buf) -> &[u8] {
        match b {
            Buf::Ewram => &self.ewram,
            Buf::Iwram => &self.iwram,
            Buf::Pal => &self.pal,
            Buf::Vram => &self.vram,
            Buf::Oam => &self.oam,
            Buf::Rom => &self.rom,
        }
    }

    pub fn buf_mut(&mut self, b: Buf) -> &mut [u8] {
        match b {
            Buf::Ewram => &mut self.ewram,
            Buf::Iwram => &mut self.iwram,
            Buf::Pal => &mut self.pal,
            Buf::Vram => &mut self.vram,
            Buf::Oam => &mut self.oam,
            Buf::Rom => &mut self.rom,
        }
    }

    fn dma_reg32(&self, off: usize) -> u32 {
        self.io16(off) | (self.io16(off + 2) << 16)
    }

    pub fn dma_write_ctrl(&mut self, i: usize, v: u32, m: u32) {
        let old = self.dma.ctrl[i];
        let mut new = (old & !m) | v;
        new &= if i != 3 { 0xF7E0 } else { 0xFFE0 };
        self.dma.ctrl[i] = new;
        let base = REG_DMA0SAD + DMA_STRIDE * i;
        if new & 0x8000 != 0 && old & 0x8000 == 0 {
            self.dma.src[i] = self.dma_reg32(base) & SRC_MASK[i];
            self.dma.dst[i] = self.dma_reg32(base + 4) & DST_MASK[i];
            self.dma_load_count(i);
            self.dma.active[i] = true;
            if (new >> 12) & 3 == TIMING_NOW {
                self.dma_run(i, false);
            }
        } else if new & 0x8000 == 0 {
            self.dma.active[i] = false;
        }
    }

    fn dma_load_count(&mut self, i: usize) {
        let base = REG_DMA0SAD + DMA_STRIDE * i;
        let n = self.io16(base + 8) & COUNT_MASK[i];
        self.dma.count[i] = if n != 0 { n } else { COUNT_MASK[i] + 1 };
    }

    pub fn dma_trigger(&mut self, timing: u32) {
        for i in 0..4 {
            if self.dma.active[i] && (self.dma.ctrl[i] >> 12) & 3 == timing {
                self.dma_run(i, false);
            }
        }
    }

    /// Sound FIFO A/B wants data: run DMA1/2 in special (FIFO) mode aimed at it.
    pub fn dma_fifo_request(&mut self, fifo_addr: u32) {
        for i in [1, 2] {
            if self.dma.active[i]
                && (self.dma.ctrl[i] >> 12) & 3 == TIMING_SPECIAL
                && (self.dma.dst[i] & 0x0FFF_FFFF) == fifo_addr
            {
                self.dma_run(i, true);
            }
        }
    }

    pub fn dma_run(&mut self, i: usize, sound: bool) {
        let ctrl = self.dma.ctrl[i];
        let word = ctrl & 0x0400 != 0 || sound;
        let unit: u32 = if word { 4 } else { 2 };
        let mut dctl = (ctrl >> 5) & 3;
        let sctl = (ctrl >> 7) & 3;
        let count = if sound { 4 } else { self.dma.count[i] };
        if sound {
            dctl = 2;
        }
        let src = self.dma.src[i] & !(unit - 1);
        let dst = self.dma.dst[i] & !(unit - 1);
        let step = |c: u32| -> i64 {
            match c {
                0 | 3 => unit as i64,
                1 => -(unit as i64),
                _ => 0,
            }
        };
        let sstep = step(sctl);
        let dstep = step(dctl);
        let nbytes = (count * unit) as usize;
        if dst >> 24 == 0xD {
            if let Save::Eeprom(e) = &mut self.save {
                e.begin(count);
            }
        }

        let mut done = false;
        if sstep == unit as i64 && dstep == unit as i64 {
            if let (Some((sb, so)), Some((db, d_o))) =
                (self.plain_slice(src, nbytes, false), self.plain_slice(dst, nbytes, true))
            {
                let data = self.buf(sb)[so..so + nbytes].to_vec();
                self.buf_mut(db)[d_o..d_o + nbytes].copy_from_slice(&data);
                done = true;
            }
        } else if sstep == 0 && dstep == unit as i64 {
            if let Some((db, d_o)) = self.plain_slice(dst, nbytes, true) {
                if src >> 24 != 0xD {
                    let val = if word { self.r32(src) } else { self.r16(src) };
                    let bytes = val.to_le_bytes();
                    let out = self.buf_mut(db);
                    for k in 0..count as usize {
                        out[d_o + k * unit as usize..d_o + (k + 1) * unit as usize]
                            .copy_from_slice(&bytes[..unit as usize]);
                    }
                    done = true;
                }
            }
        }
        if !done {
            let (mut s, mut d) = (src as i64, dst as i64);
            for _ in 0..count {
                let su = s as u32;
                let du = d as u32;
                if word {
                    let v = self.r32(su);
                    self.w32(du, v);
                } else {
                    let v = self.r16(su);
                    self.w16(du, v);
                }
                s = su as i64 + sstep;
                d = du as i64 + dstep;
            }
        }
        self.dma.src[i] = (src as i64 + sstep * count as i64) as u32;
        if dctl != 3 {
            self.dma.dst[i] = (dst as i64 + dstep * count as i64) as u32;
        }

        let waits = if word { &self.wait32 } else { &self.wait16 };
        let cost = 2 + count as u64 * (2 + waits[(src >> 24) as usize] as u64 + waits[(dst >> 24) as usize] as u64);
        self.cpu.now += cost;

        let timing = (ctrl >> 12) & 3;
        if ctrl & 0x0200 != 0 && timing != TIMING_NOW {
            if !sound {
                self.dma_load_count(i);
            }
            if dctl == 3 {
                let base = REG_DMA0SAD + DMA_STRIDE * i;
                self.dma.dst[i] = self.dma_reg32(base + 4) & DST_MASK[i];
            }
        } else {
            self.dma.ctrl[i] = ctrl & 0x7FFF;
            self.dma.active[i] = false;
        }
        if ctrl & 0x4000 != 0 {
            self.request_irq(IRQ_DMA0 << i);
        }
    }
}
