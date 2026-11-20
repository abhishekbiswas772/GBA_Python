//! High-level emulation of the GBA BIOS software interrupts (`src/core/gba/bios.py`).
//!
//! IntrWait / VBlankIntrWait halt and rewind the PC onto the SWI, so after the
//! interrupt handler returns the SWI runs again and re-checks the BIOS IF flags.

use crate::cpu::{MODE_IRQ, MODE_SVC, MODE_SYS};
use crate::gba::*;

const BIOS_IF: usize = 0x7FF8;

fn s16(v: u32) -> i64 {
    v as u16 as i16 as i64
}

fn s32(v: u32) -> i64 {
    v as i32 as i64
}

/// C-style truncating division.
fn int_div(a: i64, b: i64) -> i64 {
    let q = a.abs() / b.abs();
    if (a < 0) == (b < 0) {
        q
    } else {
        -q
    }
}

fn arctan(i: i64) -> i64 {
    let i = i as i128;
    let a = -((i * i) >> 14);
    let mut b = ((0xA9 * a) >> 14) + 0x390;
    b = ((b * a) >> 14) + 0x91C;
    b = ((b * a) >> 14) + 0xFB6;
    b = ((b * a) >> 14) + 0x16AA;
    b = ((b * a) >> 14) + 0x2081;
    b = ((b * a) >> 14) + 0x3651;
    b = ((b * a) >> 14) + 0xA2F9;
    ((i * b) >> 16) as i64
}

fn angle(raw: u32) -> f64 {
    (raw >> 8) as f64 / 128.0 * std::f64::consts::PI
}

/// Python's int(float) & mask, for finite values.
fn trunc(x: f64) -> u32 {
    (x as i64) as u32
}

impl Gba {
    fn write_block(&mut self, addr: u32, data: &[u8], unit: u32) {
        if let Some((b, off)) = self.plain_slice(addr, data.len(), true) {
            self.buf_mut(b)[off..off + data.len()].copy_from_slice(data);
            return;
        }
        if unit == 1 {
            for (i, &b) in data.iter().enumerate() {
                self.write8(addr.wrapping_add(i as u32), b as u32);
            }
        } else {
            let mut i = 0;
            while i + 1 < data.len() {
                self.write16(addr.wrapping_add(i as u32), data[i] as u32 | ((data[i + 1] as u32) << 8));
                i += 2;
            }
        }
    }

    fn read_block(&mut self, addr: u32, n: usize) -> Vec<u8> {
        if let Some((b, off)) = self.plain_slice(addr, n, false) {
            return self.buf(b)[off..off + n].to_vec();
        }
        (0..n).map(|i| self.read8(addr.wrapping_add(i as u32)) as u8).collect()
    }

    pub fn hle_swi(&mut self, n: u32) -> u64 {
        self.cpu.bios_latch = BIOS_LATCH_SWI;
        match n {
            0x00 => self.soft_reset(),
            0x01 => self.register_ram_reset(),
            0x02 | 0x03 => {
                self.cpu.halted = true;
                self.cpu.limit = 0;
                3
            }
            0x04 => {
                let (d, f) = (self.cpu.r[0], self.cpu.r[1] & 0xFFFF);
                self.intr_wait(d, f)
            }
            0x05 => self.intr_wait(1, 1),
            0x06 => {
                let (a, b) = (self.cpu.r[0], self.cpu.r[1]);
                self.div(a, b)
            }
            0x07 => {
                let (a, b) = (self.cpu.r[1], self.cpu.r[0]);
                self.div(a, b)
            }
            0x08 => {
                self.cpu.r[0] = isqrt(self.cpu.r[0] as u64) as u32;
                30
            }
            0x09 => {
                self.cpu.r[0] = arctan(s32(self.cpu.r[0])) as u32;
                30
            }
            0x0A => self.arctan2(),
            0x0B => self.cpu_set(),
            0x0C => self.cpu_fast_set(),
            0x0D => {
                self.cpu.r[0] = 0xBAAE187F;
                3
            }
            0x0E => self.bg_affine_set(),
            0x0F => self.obj_affine_set(),
            0x10 => self.bit_unpack(),
            0x11 => self.lz77(false),
            0x12 => self.lz77(true),
            0x13 => self.huff_uncomp(),
            0x14 => self.rl_uncomp(false),
            0x15 => self.rl_uncomp(true),
            0x16 => self.diff_unfilter(1, false),
            0x17 => self.diff_unfilter(1, true),
            0x18 => self.diff_unfilter(2, false),
            0x19 => {
                let level = if self.cpu.r[0] != 0 { 0x200 } else { 0 };
                let v = self.read16(0x04000088);
                self.write16(0x04000088, (v & !0x3FF) | level);
                10
            }
            0x1F => {
                let (wa, mk, fp) = (self.cpu.r[0], self.cpu.r[1], self.cpu.r[2]);
                let freq = self.read32(wa.wrapping_add(4)) as f64;
                let e = (180.0 - mk as f64 - fp as f64 / 256.0) / 12.0;
                self.cpu.r[0] = trunc(freq / 2f64.powf(e));
                20
            }
            _ => 3,
        }
    }

    fn soft_reset(&mut self) -> u64 {
        let flag = self.read8(0x03007FFA);
        self.iwram[0x7E00..0x8000].fill(0);
        let c = &mut self.cpu;
        c.switch_mode(MODE_IRQ);
        c.r[13] = SP_IRQ;
        c.r[14] = 0;
        c.spsr[c.bank] = 0;
        c.switch_mode(MODE_SVC);
        c.r[13] = SP_SVC;
        c.r[14] = 0;
        c.spsr[c.bank] = 0;
        c.switch_mode(MODE_SYS);
        c.r[13] = SP_USR;
        for i in 0..13 {
            c.r[i] = 0;
        }
        c.r[14] = 0;
        c.thumb = false;
        c.irq_disable = 0;
        c.fiq_disable = 0;
        c.pc = if flag != 0 { 0x02000000 } else { ROM_START };
        c.limit = 0;
        3
    }

    fn register_ram_reset(&mut self) -> u64 {
        let f = self.cpu.r[0];
        if f & 0x01 != 0 {
            self.ewram.fill(0);
        }
        if f & 0x02 != 0 {
            self.iwram[..0x7E00].fill(0);
        }
        if f & 0x04 != 0 {
            self.pal.fill(0);
        }
        if f & 0x08 != 0 {
            self.vram.fill(0);
        }
        if f & 0x10 != 0 {
            self.oam.fill(0);
        }
        if f & 0x80 != 0 {
            for off in (0..0x60).step_by(2) {
                self.write16(0x04000000 + off, 0);
            }
            self.write16(0x04000000, 0x0080);
            // the real BIOS leaves the BG2/BG3 affine matrices at identity
            for pa in [REG_BG2PA as u32, REG_BG3PA as u32] {
                self.write16(0x04000000 + pa, 0x100);
                self.write16(0x04000000 + pa + 6, 0x100);
            }
        }
        100
    }

    fn intr_wait(&mut self, discard: u32, flags: u32) -> u64 {
        self.ime = 1;
        if discard != 0 && !self.cpu.intrwait {
            let v = rd16(&self.iwram, BIOS_IF) & !flags & 0xFFFF;
            wr16(&mut self.iwram, BIOS_IF, v);
        }
        let cur = rd16(&self.iwram, BIOS_IF);
        if cur & flags != 0 {
            wr16(&mut self.iwram, BIOS_IF, cur & !flags & 0xFFFF);
            self.cpu.intrwait = false;
            return 3;
        }
        self.cpu.intrwait = true;
        self.cpu.halted = true;
        let back = if self.cpu.thumb { 2 } else { 4 };
        self.cpu.pc = self.cpu.pc.wrapping_sub(back);
        self.cpu.limit = 0;
        3
    }

    fn div(&mut self, num: u32, den: u32) -> u64 {
        let n = s32(num);
        let d = s32(den);
        let r = &mut self.cpu.r;
        if d == 0 {
            r[0] = if n < 0 { u32::MAX } else { 1 };
            r[1] = n as u32;
            r[3] = 1;
            return 20;
        }
        let q = int_div(n, d);
        let rem = n - q * d;
        r[0] = q as u32;
        r[1] = rem as u32;
        r[3] = q.unsigned_abs() as u32;
        40
    }

    fn arctan2(&mut self) -> u64 {
        let x = s32(self.cpu.r[0]);
        let y = s32(self.cpu.r[1]);
        let res = if y == 0 {
            if x < 0 {
                0x8000
            } else {
                0
            }
        } else if x == 0 {
            if y < 0 {
                0xC000
            } else {
                0x4000
            }
        } else if y >= 0 {
            if x >= 0 {
                if x >= y {
                    arctan(int_div(y << 14, x))
                } else {
                    0x4000 - arctan(int_div(x << 14, y))
                }
            } else if -x >= y {
                arctan(int_div(y << 14, x)) + 0x8000
            } else {
                0x4000 - arctan(int_div(x << 14, y))
            }
        } else if x <= 0 {
            if -x > -y {
                arctan(int_div(y << 14, x)) + 0x8000
            } else {
                0xC000 - arctan(int_div(x << 14, y))
            }
        } else if x >= -y {
            arctan(int_div(y << 14, x)) + 0x10000
        } else {
            0xC000 - arctan(int_div(x << 14, y))
        };
        self.cpu.r[0] = (res as u32) & 0xFFFF;
        40
    }

    fn cpu_set(&mut self) -> u64 {
        let (mut src, mut dst, cnt) = (self.cpu.r[0], self.cpu.r[1], self.cpu.r[2]);
        let n = cnt & 0x1FFFFF;
        let fixed = cnt & 0x0100_0000 != 0;
        let unit = if cnt & 0x0400_0000 != 0 {
            src &= !3;
            dst &= !3;
            4
        } else {
            src &= !1;
            dst &= !1;
            2
        };
        self.bios_copy(src, dst, n, unit, fixed);
        10 + n as u64 * 2
    }

    fn cpu_fast_set(&mut self) -> u64 {
        let (src, dst, cnt) = (self.cpu.r[0] & !3, self.cpu.r[1] & !3, self.cpu.r[2]);
        let n = ((cnt & 0x1FFFFF) + 7) & !7;
        self.bios_copy(src, dst, n, 4, cnt & 0x0100_0000 != 0);
        10 + n as u64
    }

    fn rd_unit(&mut self, a: u32, unit: u32) -> u32 {
        if unit == 4 {
            self.read32(a)
        } else {
            self.read16(a)
        }
    }

    fn wr_unit(&mut self, a: u32, v: u32, unit: u32) {
        if unit == 4 {
            self.write32(a, v)
        } else {
            self.write16(a, v)
        }
    }

    fn bios_copy(&mut self, src: u32, dst: u32, n: u32, unit: u32, fixed: bool) {
        if n == 0 || src < 0x02000000 {
            return;
        }
        let nbytes = (n * unit) as usize;
        if let Some((db, d_o)) = self.plain_slice(dst, nbytes, true) {
            if fixed {
                let v = self.rd_unit(src, unit).to_le_bytes();
                let out = self.buf_mut(db);
                for k in 0..n as usize {
                    out[d_o + k * unit as usize..d_o + (k + 1) * unit as usize].copy_from_slice(&v[..unit as usize]);
                }
                return;
            }
            if let Some((sb, so)) = self.plain_slice(src, nbytes, false) {
                let data = self.buf(sb)[so..so + nbytes].to_vec();
                self.buf_mut(db)[d_o..d_o + nbytes].copy_from_slice(&data);
                return;
            }
        }
        if fixed {
            let v = self.rd_unit(src, unit);
            for k in 0..n {
                self.wr_unit(dst.wrapping_add(k * unit), v, unit);
            }
        } else {
            for k in 0..n {
                let v = self.rd_unit(src.wrapping_add(k * unit), unit);
                self.wr_unit(dst.wrapping_add(k * unit), v, unit);
            }
        }
    }

    fn bg_affine_set(&mut self) -> u64 {
        let (mut src, mut dst, n) = (self.cpu.r[0], self.cpu.r[1], self.cpu.r[2]);
        for _ in 0..n {
            let ox = s32(self.read32(src)) as f64 / 256.0;
            let oy = s32(self.read32(src.wrapping_add(4))) as f64 / 256.0;
            let cx = s16(self.read16(src.wrapping_add(8))) as f64;
            let cy = s16(self.read16(src.wrapping_add(10))) as f64;
            let sx = s16(self.read16(src.wrapping_add(12))) as f64 / 256.0;
            let sy = s16(self.read16(src.wrapping_add(14))) as f64 / 256.0;
            let th = angle(self.read16(src.wrapping_add(16)));
            let (mut a, mut d) = (th.cos(), th.cos());
            let (mut b, mut c) = (th.sin(), th.sin());
            a *= sx;
            b *= -sx;
            c *= sy;
            d *= sy;
            let rx = ox - (a * cx + b * cy);
            let ry = oy - (c * cx + d * cy);
            self.write16(dst, trunc(a * 256.0) & 0xFFFF);
            self.write16(dst.wrapping_add(2), trunc(b * 256.0) & 0xFFFF);
            self.write16(dst.wrapping_add(4), trunc(c * 256.0) & 0xFFFF);
            self.write16(dst.wrapping_add(6), trunc(d * 256.0) & 0xFFFF);
            self.write32(dst.wrapping_add(8), trunc(rx * 256.0));
            self.write32(dst.wrapping_add(12), trunc(ry * 256.0));
            src = src.wrapping_add(20);
            dst = dst.wrapping_add(16);
        }
        20 * n.max(1) as u64
    }

    fn obj_affine_set(&mut self) -> u64 {
        let (mut src, mut dst, n, stride) = (self.cpu.r[0], self.cpu.r[1], self.cpu.r[2], self.cpu.r[3]);
        for _ in 0..n {
            let sx = s16(self.read16(src)) as f64 / 256.0;
            let sy = s16(self.read16(src.wrapping_add(2))) as f64 / 256.0;
            let th = angle(self.read16(src.wrapping_add(4)));
            let (mut a, mut d) = (th.cos(), th.cos());
            let (mut b, mut c) = (th.sin(), th.sin());
            a *= sx;
            b *= -sx;
            c *= sy;
            d *= sy;
            self.write16(dst, trunc(a * 256.0) & 0xFFFF);
            self.write16(dst.wrapping_add(stride), trunc(b * 256.0) & 0xFFFF);
            self.write16(dst.wrapping_add(stride.wrapping_mul(2)), trunc(c * 256.0) & 0xFFFF);
            self.write16(dst.wrapping_add(stride.wrapping_mul(3)), trunc(d * 256.0) & 0xFFFF);
            src = src.wrapping_add(8);
            dst = dst.wrapping_add(stride.wrapping_mul(4));
        }
        20 * n.max(1) as u64
    }

    fn bit_unpack(&mut self) -> u64 {
        let (src, mut dst, info) = (self.cpu.r[0], self.cpu.r[1], self.cpu.r[2]);
        let length = self.read16(info);
        let sw = self.read8(info.wrapping_add(2));
        let dw = self.read8(info.wrapping_add(3));
        let mut off = self.read32(info.wrapping_add(4)) as u64;
        let zero_flag = off & 0x8000_0000 != 0;
        off &= 0x7FFF_FFFF;
        if ![1, 2, 4, 8].contains(&sw) || ![1, 2, 4, 8, 16, 32].contains(&dw) {
            return 10;
        }
        let mut out: u64 = 0;
        let mut bits = 0u32;
        let smask = (1u64 << sw) - 1;
        let dmask = (1u64 << dw) - 1;
        for i in 0..length {
            let byte = self.read8(src.wrapping_add(i)) as u64;
            let mut sh = 0;
            while sh < 8 {
                let mut v = (byte >> sh) & smask;
                if v != 0 || zero_flag {
                    v += off;
                }
                out |= (v & dmask) << bits;
                bits += dw;
                if bits >= 32 {
                    self.write32(dst, out as u32);
                    dst = dst.wrapping_add(4);
                    out = 0;
                    bits = 0;
                }
                sh += sw;
            }
        }
        10 + length as u64 * 8
    }

    fn lz77(&mut self, vram: bool) -> u64 {
        let (src, dst) = (self.cpu.r[0], self.cpu.r[1]);
        let size = (self.read32(src) >> 8) as usize;
        let data = self.read_block(src.wrapping_add(4), (size * 2 + 16).min(0x1000000));
        let mut out: Vec<u8> = Vec::with_capacity(size + 1);
        let mut i = 0usize;
        'outer: while out.len() < size {
            let Some(&flags0) = data.get(i) else { break };
            let mut flags = flags0;
            i += 1;
            for _ in 0..8 {
                if out.len() >= size {
                    break;
                }
                if flags & 0x80 != 0 {
                    let (Some(&b1), Some(&b2)) = (data.get(i), data.get(i + 1)) else { break 'outer };
                    i += 2;
                    let n = (b1 >> 4) as usize + 3;
                    let disp = ((((b1 & 15) as usize) << 8) | b2 as usize) + 1;
                    let start = out.len() as isize - disp as isize;
                    for k in 0..n {
                        let j = start + k as isize;
                        let v = if j >= 0 { out[j as usize] } else { 0 };
                        out.push(v);
                    }
                } else {
                    let Some(&b) = data.get(i) else { break 'outer };
                    out.push(b);
                    i += 1;
                }
                flags <<= 1;
            }
        }
        out.truncate(size);
        if vram && out.len() & 1 != 0 {
            out.push(0);
        }
        self.write_block(dst, &out, if vram { 2 } else { 1 });
        20 + size as u64 * 2
    }

    fn rl_uncomp(&mut self, vram: bool) -> u64 {
        let (src, dst) = (self.cpu.r[0], self.cpu.r[1]);
        let size = (self.read32(src) >> 8) as usize;
        let data = self.read_block(src.wrapping_add(4), (size + size / 2 + 16).min(0x1000000));
        let mut out: Vec<u8> = Vec::with_capacity(size + 1);
        let mut i = 0usize;
        while out.len() < size {
            let Some(&f) = data.get(i) else { break };
            i += 1;
            if f & 0x80 != 0 {
                let Some(&b) = data.get(i) else { break };
                out.extend(std::iter::repeat(b).take((f & 0x7F) as usize + 3));
                i += 1;
            } else {
                let n = (f & 0x7F) as usize + 1;
                let end = (i + n).min(data.len());
                out.extend_from_slice(&data[i.min(end)..end]);
                i += n;
            }
        }
        out.truncate(size);
        if vram && out.len() & 1 != 0 {
            out.push(0);
        }
        self.write_block(dst, &out, if vram { 2 } else { 1 });
        20 + size as u64 * 2
    }

    fn huff_uncomp(&mut self) -> u64 {
        let (src, mut dst) = (self.cpu.r[0], self.cpu.r[1]);
        let h = self.read32(src);
        let bits = h & 15;
        let size = (h >> 8) as u64;
        if bits == 0 {
            return 20 + size * 8; // would never finish
        }
        let tree_size = (self.read8(src.wrapping_add(4)) + 1) * 2;
        let tree = src.wrapping_add(5);
        let mut stream = src.wrapping_add(4).wrapping_add(tree_size);
        let mut out: u64 = 0;
        let mut nbits = 0u32;
        let mut written = 0u64;
        let mut node = tree;
        while written < size {
            let word = self.read32(stream);
            stream = stream.wrapping_add(4);
            for b in (0..32).rev() {
                let n = self.read8(node);
                let nxt = (node & !1).wrapping_add(((n & 0x3F) + 1) * 2);
                let leaf;
                if word >> b & 1 != 0 {
                    leaf = n & 0x40;
                    node = nxt.wrapping_add(1);
                } else {
                    leaf = n & 0x80;
                    node = nxt;
                }
                if leaf != 0 {
                    out |= (self.read8(node) as u64) << nbits;
                    nbits += bits;
                    node = tree;
                    if nbits >= 32 {
                        self.write32(dst, out as u32);
                        dst = dst.wrapping_add(4);
                        written += 4;
                        out = 0;
                        nbits = 0;
                        if written >= size {
                            break;
                        }
                    }
                }
            }
        }
        20 + size * 8
    }

    fn diff_unfilter(&mut self, width: u32, vram: bool) -> u64 {
        let (src, dst) = (self.cpu.r[0], self.cpu.r[1]);
        let size = (self.read32(src) >> 8) as usize;
        let data = self.read_block(src.wrapping_add(4), size);
        let mut out = vec![0u8; data.len()];
        if width == 1 {
            let mut acc = 0u8;
            for (i, &b) in data.iter().enumerate() {
                acc = acc.wrapping_add(b);
                out[i] = acc;
            }
        } else {
            let mut acc = 0u16;
            let mut i = 0;
            while i + 1 < data.len() {
                acc = acc.wrapping_add(data[i] as u16 | ((data[i + 1] as u16) << 8));
                out[i] = acc as u8;
                out[i + 1] = (acc >> 8) as u8;
                i += 2;
            }
        }
        self.write_block(dst, &out, if vram || width == 2 { 2 } else { 1 });
        20 + size as u64 * 2
    }
}

fn isqrt(n: u64) -> u64 {
    if n < 2 {
        return n;
    }
    let mut x = (n as f64).sqrt() as u64;
    while x * x > n {
        x -= 1;
    }
    while (x + 1) * (x + 1) <= n {
        x += 1;
    }
    x
}
