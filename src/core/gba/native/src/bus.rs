//! Memory map, IO register dispatch and waitstates (`src/core/gba/bus.py`).
//!
//! Reads return the aligned value (the CPU rotates unaligned LDR/LDRH). IO
//! writes go through `io_write16(offset, value, mask)` so that byte writes to
//! write-1-to-clear registers (IF) only touch the written byte.

use crate::cart::Save;
use crate::gba::*;

const REG_IE: u32 = 0x200;
const REG_IF: u32 = 0x202;
const REG_WAITCNT: u32 = 0x204;
const REG_IME: u32 = 0x208;
const REG_KEYINPUT: u32 = 0x130;
const GPIO_BASE: u32 = 0x080000C4;

const WS_N: [u32; 4] = [4, 3, 2, 8];
const WS0_S: [u32; 2] = [2, 1];
const WS1_S: [u32; 2] = [4, 1];
const WS2_S: [u32; 2] = [8, 1];
const SRAM_WAIT: [u32; 4] = [4, 3, 2, 8];

#[inline(always)]
fn voff(a: u32) -> usize {
    let o = (a & 0x1FFFF) as usize;
    if o >= 0x18000 {
        o - 0x8000
    } else {
        o
    }
}

impl Gba {
    #[inline]
    pub fn io16(&self, off: usize) -> u32 {
        rd16(&self.io, off)
    }

    #[inline]
    pub fn set_io16(&mut self, off: usize, v: u32) {
        wr16(&mut self.io, off, v);
    }

    #[inline]
    fn bios_word(&self, a: u32) -> u32 {
        if (a as usize) < BIOS_SIZE && (self.cpu.pc as usize) < BIOS_SIZE {
            return rd32(&self.bios, (a & 0x3FFC) as usize);
        }
        if a as usize >= BIOS_SIZE {
            return 0;
        }
        self.cpu.bios_latch
    }

    // ------------------------------------------------------------ save chip (8-bit bus)
    #[inline]
    fn sr8(&mut self, a: u32) -> u32 {
        match &mut self.save {
            Save::Sram(s) => s.read8(a & 0xFFFF),
            Save::Flash(f) => f.read8(a & 0xFFFF),
            _ => 0xFF,
        }
    }

    #[inline]
    fn sw8(&mut self, a: u32, v: u32) {
        match &mut self.save {
            Save::Sram(s) => s.write8(a & 0xFFFF, v & 0xFF),
            Save::Flash(f) => f.write8(a & 0xFFFF, v & 0xFF),
            _ => {}
        }
    }

    // ------------------------------------------------------------ GPIO (RTC) in ROM space
    fn gr16(&mut self, a: u32) -> u32 {
        if (GPIO_BASE..GPIO_BASE + 6).contains(&a) {
            if let Some(rtc) = &self.rtc {
                if let Some(v) = rtc.read16((a & !1) - 0x08000000) {
                    return v;
                }
            }
        }
        rd16(&self.rom, ((a & 0x1FFFFFF) & !1) as usize)
    }

    fn gw16(&mut self, a: u32, v: u32) {
        if (GPIO_BASE..GPIO_BASE + 6).contains(&a) {
            if let Some(rtc) = &mut self.rtc {
                rtc.write16((a & !1) - 0x08000000, v);
            }
        }
    }

    // ------------------------------------------------------------ reads
    pub fn r8(&mut self, a: u32) -> u32 {
        match a >> 24 {
            0 => (self.bios_word(a) >> ((a & 3) << 3)) & 0xFF,
            2 => self.ewram[(a as usize) & (EWRAM_SIZE - 1)] as u32,
            3 => self.iwram[(a as usize) & (IWRAM_SIZE - 1)] as u32,
            4 => {
                if a & 0xFFFFFF >= IO_SIZE as u32 {
                    return 0;
                }
                (self.io_read16(a & 0x3FE) >> ((a & 1) << 3)) & 0xFF
            }
            5 => self.pal[(a & 0x3FF) as usize] as u32,
            6 => self.vram[voff(a)] as u32,
            7 => self.oam[(a & 0x3FF) as usize] as u32,
            8 if self.rtc.is_some() => (self.gr16(a) >> ((a & 1) << 3)) & 0xFF,
            8..=13 => self.rom[(a & 0x1FFFFFF) as usize] as u32,
            0xE | 0xF => self.sr8(a),
            _ => 0,
        }
    }

    pub fn r16(&mut self, a: u32) -> u32 {
        match a >> 24 {
            0 => (self.bios_word(a) >> ((a & 2) << 3)) & 0xFFFF,
            2 => rd16(&self.ewram, (a as usize) & (EWRAM_SIZE - 1) & !1),
            3 => rd16(&self.iwram, (a as usize) & (IWRAM_SIZE - 1) & !1),
            4 => {
                if a & 0xFFFFFF >= IO_SIZE as u32 {
                    return 0;
                }
                self.io_read16(a & 0x3FE)
            }
            5 => rd16(&self.pal, (a & 0x3FE) as usize),
            6 => rd16(&self.vram, voff(a) & !1),
            7 => rd16(&self.oam, (a & 0x3FE) as usize),
            8 if self.rtc.is_some() => self.gr16(a),
            0xD if matches!(self.save, Save::Eeprom(_)) => {
                if a >= self.eeprom_base {
                    if let Save::Eeprom(e) = &mut self.save {
                        return e.read_bit();
                    }
                }
                rd16(&self.rom, ((a & 0x1FFFFFF) & !1) as usize)
            }
            8..=13 => rd16(&self.rom, ((a & 0x1FFFFFF) & !1) as usize),
            0xE | 0xF => self.sr8(a) * 0x0101,
            _ => 0,
        }
    }

    pub fn r32(&mut self, a: u32) -> u32 {
        match a >> 24 {
            0 => self.bios_word(a),
            2 => rd32(&self.ewram, (a as usize) & (EWRAM_SIZE - 1) & !3),
            3 => rd32(&self.iwram, (a as usize) & (IWRAM_SIZE - 1) & !3),
            4 => {
                if a & 0xFFFFFF >= IO_SIZE as u32 {
                    return 0;
                }
                let o = a & 0x3FC;
                self.io_read16(o) | (self.io_read16(o + 2) << 16)
            }
            5 => rd32(&self.pal, (a & 0x3FC) as usize),
            6 => rd32(&self.vram, voff(a) & !3),
            7 => rd32(&self.oam, (a & 0x3FC) as usize),
            8 if self.rtc.is_some() => {
                let a = a & !3;
                self.gr16(a) | (self.gr16(a + 2) << 16)
            }
            8..=13 => rd32(&self.rom, ((a & 0x1FFFFFF) & !3) as usize),
            0xE | 0xF => self.sr8(a) * 0x01010101,
            _ => 0,
        }
    }

    // ------------------------------------------------------------ writes
    pub fn w8(&mut self, a: u32, v: u32) {
        match a >> 24 {
            2 => self.ewram[(a as usize) & (EWRAM_SIZE - 1)] = v as u8,
            3 => self.iwram[(a as usize) & (IWRAM_SIZE - 1)] = v as u8,
            4 => {
                if a & 0xFFFFFF < IO_SIZE as u32 {
                    let sh = (a & 1) << 3;
                    self.io_write16(a & 0x3FE, v << sh, 0xFF << sh);
                }
            }
            5 => wr16(&mut self.pal, (a & 0x3FE) as usize, v * 0x101),
            6 => {
                let o = voff(a);
                let limit = if (self.io16(0) & 7) >= 3 { 0x14000 } else { 0x10000 };
                if o < limit {
                    wr16(&mut self.vram, o & !1, v * 0x101);
                }
            }
            8 if self.rtc.is_some() => self.gw16(a, v),
            0xE | 0xF => self.sw8(a, v),
            _ => {}
        }
    }

    pub fn w16(&mut self, a: u32, v: u32) {
        match a >> 24 {
            2 => wr16(&mut self.ewram, (a as usize) & (EWRAM_SIZE - 1) & !1, v),
            3 => wr16(&mut self.iwram, (a as usize) & (IWRAM_SIZE - 1) & !1, v),
            4 => {
                if a & 0xFFFFFF < IO_SIZE as u32 {
                    self.io_write16(a & 0x3FE, v, 0xFFFF);
                }
            }
            5 => wr16(&mut self.pal, (a & 0x3FE) as usize, v),
            6 => wr16(&mut self.vram, voff(a) & !1, v),
            7 => wr16(&mut self.oam, (a & 0x3FE) as usize, v),
            8 if self.rtc.is_some() => self.gw16(a, v),
            0xD => {
                if a >= self.eeprom_base {
                    if let Save::Eeprom(e) = &mut self.save {
                        e.write_bit(v);
                    }
                }
            }
            0xE | 0xF => self.sw8(a, (v >> ((a & 1) << 3)) & 0xFF),
            _ => {}
        }
    }

    pub fn w32(&mut self, a: u32, v: u32) {
        match a >> 24 {
            2 => wr32(&mut self.ewram, (a as usize) & (EWRAM_SIZE - 1) & !3, v),
            3 => wr32(&mut self.iwram, (a as usize) & (IWRAM_SIZE - 1) & !3, v),
            4 => {
                if a & 0xFFFFFF < IO_SIZE as u32 {
                    let o = a & 0x3FC;
                    self.io_write16(o, v & 0xFFFF, 0xFFFF);
                    self.io_write16(o + 2, v >> 16, 0xFFFF);
                }
            }
            5 => wr32(&mut self.pal, (a & 0x3FC) as usize, v),
            6 => wr32(&mut self.vram, voff(a) & !3, v),
            7 => wr32(&mut self.oam, (a & 0x3FC) as usize, v),
            8 if self.rtc.is_some() => {
                let a = a & !3;
                self.gw16(a, v & 0xFFFF);
                self.gw16(a + 2, v >> 16);
            }
            0xE | 0xF => self.sw8(a, (v >> ((a & 3) << 3)) & 0xFF),
            _ => {}
        }
    }

    // ------------------------------------------------------------ waitstates
    pub fn set_waitcnt(&mut self, v: u32) {
        self.wait16[2] = 2;
        self.wait32[2] = 5;
        self.code16[2] = 3;
        self.code32[2] = 6;
        for reg in [5, 6] {
            self.wait16[reg] = 0;
            self.wait32[reg] = 1;
            self.code16[reg] = 1;
            self.code32[reg] = 2;
        }
        let prefetch = v & 0x4000 != 0;
        let groups = [
            (8, (v >> 2) & 3, WS0_S[((v >> 4) & 1) as usize]),
            (10, (v >> 5) & 3, WS1_S[((v >> 7) & 1) as usize]),
            (12, (v >> 8) & 3, WS2_S[((v >> 10) & 1) as usize]),
        ];
        for (base, n_bits, s) in groups {
            let n = WS_N[n_bits as usize];
            for reg in [base, base + 1] {
                self.wait16[reg] = n;
                self.wait32[reg] = n + s + 1;
                let seq = if prefetch { ((1 + s) >> 1).max(1) } else { 1 + s };
                self.code16[reg] = seq;
                self.code32[reg] = 2 * seq;
            }
        }
        let sw = SRAM_WAIT[(v & 3) as usize];
        for reg in [0xE, 0xF] {
            self.wait16[reg] = sw;
            self.wait32[reg] = sw;
        }
        self.cpu.limit = 0;
    }

    // ------------------------------------------------------------ interrupts
    pub fn request_irq(&mut self, bits: u32) {
        self.if_ |= bits;
        if self.ie & bits != 0 {
            self.cpu.limit = 0;
        }
    }

    // ------------------------------------------------------------ IO registers
    pub fn io_read16(&mut self, off: u32) -> u32 {
        match off {
            0x004 => self.ppu.stat,
            0x006 => self.ppu.vcount,
            0x082 => self.apu.cnt_h,
            0x084 => self.apu_read_cnt_x(),
            0x0A0 | 0x0A2 | 0x0A4 | 0x0A6 => 0,
            0x0B0..=0x0DF => {
                let rel = off - 0xB0;
                if rel % 12 == 10 {
                    self.dma.ctrl[(rel / 12) as usize]
                } else {
                    0
                }
            }
            0x100..=0x10F => {
                let i = ((off - 0x100) >> 2) as usize;
                if off & 2 == 0 {
                    self.timer_read(i)
                } else {
                    self.timers.ctrl[i]
                }
            }
            REG_KEYINPUT => self.keys,
            REG_IE => self.ie,
            REG_IF => self.if_,
            REG_IME => self.ime,
            0x20A => 0,
            0x300 => self.io[REG_POSTFLG] as u32,
            _ => self.io16(off as usize),
        }
    }

    pub fn io_write16(&mut self, off: u32, v: u32, m: u32) {
        let v = v & m;
        match off {
            0x004 => self.ppu_write_stat(v, m),
            0x006 => {}
            0x028 | 0x02A | 0x02C | 0x02E | 0x038 | 0x03A | 0x03C | 0x03E => {
                self.store_io(off, v, m);
                self.ppu_latch_ref(if off < 0x38 { 0 } else { 1 });
            }
            0x060..=0x080 => self.apu_write_psg(off, v, m),
            0x082 => self.apu_write_cnt_h(v, m),
            0x084 => self.apu_write_cnt_x(v, m),
            0x090..=0x09F => self.apu_write_wave(off, v, m),
            0x0A0 | 0x0A2 => self.apu_write_fifo(0, v, m),
            0x0A4 | 0x0A6 => self.apu_write_fifo(1, v, m),
            0x0B0..=0x0DF if (off - 0xB0) % 12 == 10 => self.dma_write_ctrl(((off - 0xB0) / 12) as usize, v, m),
            0x100..=0x10F => {
                let i = ((off - 0x100) >> 2) as usize;
                if off & 2 == 0 {
                    self.timers.reload[i] = (self.timers.reload[i] & !m) | v;
                } else {
                    self.timer_write_ctrl(i, v, m);
                }
            }
            REG_KEYINPUT => {}
            REG_IE => {
                self.ie = ((self.ie & !m) | v) & 0x3FFF;
                self.cpu.limit = 0;
            }
            REG_IF => self.if_ &= !v,
            REG_IME => {
                self.ime = ((self.ime & !m) | v) & 1;
                self.cpu.limit = 0;
            }
            REG_WAITCNT => {
                let val = ((self.io16(REG_WAITCNT as usize) & !m) | v) & 0x5FFF;
                self.set_io16(REG_WAITCNT as usize, val);
                self.set_waitcnt(val);
            }
            0x300 => {
                if m & 0xFF != 0 {
                    self.io[REG_POSTFLG] = (v & 1) as u8;
                }
                if m & 0xFF00 != 0 {
                    self.cpu.halted = true;
                    self.cpu.limit = 0;
                }
            }
            _ => self.store_io(off, v, m),
        }
    }

    #[inline]
    pub fn store_io(&mut self, off: u32, v: u32, m: u32) {
        let o = off as usize;
        let old = self.io16(o);
        self.set_io16(o, (old & !m) | (v & m));
    }

    // ------------------------------------------------------------ convenience
    pub fn read8(&mut self, a: u32) -> u32 {
        self.r8(a)
    }
    pub fn read16(&mut self, a: u32) -> u32 {
        self.r16(a)
    }
    pub fn read32(&mut self, a: u32) -> u32 {
        self.r32(a)
    }
    pub fn write8(&mut self, a: u32, v: u32) {
        self.w8(a, v & 0xFF)
    }
    pub fn write16(&mut self, a: u32, v: u32) {
        self.w16(a, v & 0xFFFF)
    }
    pub fn write32(&mut self, a: u32, v: u32) {
        self.w32(a, v)
    }
}
