//! ARM7TDMI (ARMv4T): ARM + THUMB interpreter (`src/core/gba/cpu.py`).
//!
//! Pipeline: while an instruction executes r[15] holds its address + 8 (ARM)
//! or + 4 (THUMB); `cpu.pc` holds the address of the next instruction and
//! branches write it. Handlers return the cycles they took beyond the opcode
//! fetch, exactly like the generated Python handlers.
//!
//! Prefetch / self-modifying code: the two slots after the executing one have
//! already been fetched, so a CPU store into one of them updates memory but the
//! slot still executes its previously-fetched opcode once (`stale`).

use serde::{Deserialize, Serialize};

use crate::gba::Gba;

pub const MODE_USR: u32 = 0x10;
pub const MODE_FIQ: u32 = 0x11;
pub const MODE_IRQ: u32 = 0x12;
pub const MODE_SVC: u32 = 0x13;
pub const MODE_UND: u32 = 0x1B;
pub const MODE_SYS: u32 = 0x1F;
pub const VEC_UNDEF: u32 = 0x04;
pub const VEC_SWI: u32 = 0x08;
pub const VEC_IRQ: u32 = 0x18;
const M: u32 = 0xFFFF_FFFF;

#[inline]
pub fn mode_bank(mode: u32) -> usize {
    match mode {
        0x10 | 0x1F => 0,
        0x11 => 1,
        0x12 => 2,
        0x13 => 3,
        0x17 => 4,
        0x1B => 5,
        _ => 0,
    }
}

/// Condition check on the packed NZCV nibble.
#[inline(always)]
pub fn cond(c: u32, f: u32) -> bool {
    let n = f >> 3 & 1 != 0;
    let z = f >> 2 & 1 != 0;
    let cf = f >> 1 & 1 != 0;
    let v = f & 1 != 0;
    match c {
        0 => z,
        1 => !z,
        2 => cf,
        3 => !cf,
        4 => n,
        5 => !n,
        6 => v,
        7 => !v,
        8 => cf && !z,
        9 => !cf || z,
        10 => n == v,
        11 => n != v,
        12 => !z && n == v,
        13 => z || n != v,
        14 => true,
        _ => false,
    }
}

#[inline(always)]
fn nz(res: u32) -> u32 {
    ((res >> 28) & 8) | (((res == 0) as u32) << 2)
}

#[derive(Serialize, Deserialize, Clone)]
pub struct Cpu {
    pub r: [u32; 16],
    pub f: u32,
    pub irq_disable: u32,
    pub fiq_disable: u32,
    pub thumb: bool,
    pub mode: u32,
    pub bank: usize,
    pub banked: [[u32; 2]; 6],
    pub hi_usr: [u32; 5],
    pub hi_fiq: [u32; 5],
    pub spsr: [u32; 6],
    pub pc: u32,
    pub now: u64,
    pub limit: u64,
    pub deadline: u64,
    pub halted: bool,
    pub intrwait: bool,
    pub bios_latch: u32,
    pub hle: bool,
    pub stale: Vec<(u32, u32)>,
    pub pf_next: u32,
}

impl Cpu {
    pub fn new() -> Cpu {
        Cpu {
            r: [0; 16],
            f: 0,
            irq_disable: 1,
            fiq_disable: 1,
            thumb: false,
            mode: MODE_SVC,
            bank: 3,
            banked: [[0; 2]; 6],
            hi_usr: [0; 5],
            hi_fiq: [0; 5],
            spsr: [0; 6],
            pc: 0,
            now: 0,
            limit: 0,
            deadline: 0,
            halted: false,
            intrwait: false,
            bios_latch: 0,
            hle: false,
            stale: Vec::new(),
            pf_next: 0,
        }
    }

    pub fn get_cpsr(&self) -> u32 {
        (self.f << 28) | (self.irq_disable << 7) | (self.fiq_disable << 6) | ((self.thumb as u32) << 5) | self.mode
    }

    pub fn switch_mode(&mut self, mode: u32) {
        let nb = mode_bank(mode);
        let ob = self.bank;
        if nb != ob {
            self.banked[ob] = [self.r[13], self.r[14]];
            self.r[13] = self.banked[nb][0];
            self.r[14] = self.banked[nb][1];
            if ob == 1 {
                self.hi_fiq.copy_from_slice(&self.r[8..13]);
                let u = self.hi_usr;
                self.r[8..13].copy_from_slice(&u);
            } else if nb == 1 {
                self.hi_usr.copy_from_slice(&self.r[8..13]);
                let fq = self.hi_fiq;
                self.r[8..13].copy_from_slice(&fq);
            }
            self.bank = nb;
        }
        self.mode = mode;
    }

    pub fn set_cpsr(&mut self, v: u32) {
        self.f = (v >> 28) & 15;
        self.irq_disable = (v >> 7) & 1;
        self.fiq_disable = (v >> 6) & 1;
        self.thumb = v & 0x20 != 0;
        self.switch_mode((v & 0x1F) | 0x10);
        self.limit = 0;
    }

    pub fn get_spsr(&self) -> u32 {
        if self.bank != 0 {
            self.spsr[self.bank]
        } else {
            self.get_cpsr()
        }
    }

    pub fn ret_spsr(&mut self, target: u32) {
        if self.bank != 0 {
            let s = self.spsr[self.bank];
            self.set_cpsr(s);
        }
        self.pc = target & if self.thumb { 0xFFFF_FFFE } else { 0xFFFF_FFFC };
        self.stale.clear();
        self.limit = 0;
    }

    fn user_reg(&self, i: usize) -> u32 {
        if i == 15 || i < 8 || self.bank == 0 {
            return self.r[i];
        }
        if i >= 13 {
            return self.banked[0][i - 13];
        }
        if self.bank == 1 {
            self.hi_usr[i - 8]
        } else {
            self.r[i]
        }
    }

    fn set_user_reg(&mut self, i: usize, v: u32) {
        if i < 8 || self.bank == 0 {
            self.r[i] = v;
        } else if i >= 13 {
            self.banked[0][i - 13] = v;
        } else if self.bank == 1 {
            self.hi_usr[i - 8] = v;
        } else {
            self.r[i] = v;
        }
    }

    pub fn exception(&mut self, vector: u32, mode: u32, lr: u32) {
        let cpsr = self.get_cpsr();
        self.switch_mode(mode);
        self.spsr[self.bank] = cpsr;
        self.r[14] = lr;
        self.thumb = false;
        self.irq_disable = 1;
        if mode == MODE_FIQ {
            self.fiq_disable = 1;
        }
        self.pc = vector;
        self.stale.clear();
        self.limit = 0;
    }

    #[inline(always)]
    fn carry(&self) -> u32 {
        (self.f >> 1) & 1
    }

    /// Register shifted by an immediate: (value, shifter carry).
    #[inline(always)]
    fn shift_imm(&self, v: u32, typ: u32, amt: u32) -> (u32, u32) {
        match typ {
            0 => {
                if amt == 0 {
                    (v, self.carry())
                } else {
                    (v << amt, (v >> (32 - amt)) & 1)
                }
            }
            1 => {
                if amt == 0 {
                    (0, v >> 31)
                } else {
                    (v >> amt, (v >> (amt - 1)) & 1)
                }
            }
            2 => {
                if amt == 0 {
                    (if v >> 31 != 0 { M } else { 0 }, v >> 31)
                } else {
                    (((v as i32) >> amt) as u32, (v >> (amt - 1)) & 1)
                }
            }
            _ => {
                if amt == 0 {
                    ((v >> 1) | (self.carry() << 31), v & 1)
                } else {
                    (v.rotate_right(amt), (v >> (amt - 1)) & 1)
                }
            }
        }
    }

    /// Value shifted by a register amount (already & 255): (value, carry).
    #[inline(always)]
    fn shift_reg(&self, v: u32, s: u32, typ: u32) -> (u32, u32) {
        if s == 0 {
            return (v, self.carry());
        }
        match typ {
            0 => {
                if s < 32 {
                    (v << s, (v >> (32 - s)) & 1)
                } else if s == 32 {
                    (0, v & 1)
                } else {
                    (0, 0)
                }
            }
            1 => {
                if s < 32 {
                    (v >> s, (v >> (s - 1)) & 1)
                } else if s == 32 {
                    (0, v >> 31)
                } else {
                    (0, 0)
                }
            }
            2 => {
                if s < 32 {
                    (((v as i32) >> s) as u32, (v >> (s - 1)) & 1)
                } else {
                    (if v >> 31 != 0 { M } else { 0 }, v >> 31)
                }
            }
            _ => {
                let s = s & 31;
                if s == 0 {
                    (v, v >> 31)
                } else {
                    (v.rotate_right(s), (v >> (s - 1)) & 1)
                }
            }
        }
    }

    /// Data-processing ALU. `sflag`: update NZCV; `sc`: shifter carry for logical ops.
    #[inline(always)]
    fn alu(&mut self, opc: u32, a: u32, b: u32, sflag: bool, sc: u32) -> u32 {
        let c_in = self.carry();
        let (res, f) = match opc {
            0 | 8 => (a & b, None),
            1 | 9 => (a ^ b, None),
            12 => (a | b, None),
            13 => (b, None),
            14 => (a & !b, None),
            15 => (!b, None),
            4 | 11 => {
                let (r, c) = a.overflowing_add(b);
                (r, Some(nz(r) | ((c as u32) << 1) | (((a ^ r) & (b ^ r)) >> 31)))
            }
            5 => {
                let t = a as u64 + b as u64 + c_in as u64;
                let r = t as u32;
                (r, Some(nz(r) | (((t >> 32) as u32) << 1) | (((a ^ r) & (b ^ r)) >> 31)))
            }
            2 | 10 => {
                let r = a.wrapping_sub(b);
                (r, Some(nz(r) | (((a >= b) as u32) << 1) | (((a ^ b) & (a ^ r)) >> 31)))
            }
            3 => {
                let r = b.wrapping_sub(a);
                (r, Some(nz(r) | (((b >= a) as u32) << 1) | (((a ^ b) & (b ^ r)) >> 31)))
            }
            6 => {
                let t = a as i64 - b as i64 - 1 + c_in as i64;
                let r = t as u32;
                (r, Some(nz(r) | (((t >= 0) as u32) << 1) | (((a ^ b) & (a ^ r)) >> 31)))
            }
            _ => {
                let t = b as i64 - a as i64 - 1 + c_in as i64;
                let r = t as u32;
                (r, Some(nz(r) | (((t >= 0) as u32) << 1) | (((a ^ b) & (b ^ r)) >> 31)))
            }
        };
        if sflag {
            self.f = match f {
                Some(f) => f,
                None => nz(res) | (sc << 1) | (self.f & 1),
            };
        }
        res
    }
}

impl Default for Cpu {
    fn default() -> Self {
        Cpu::new()
    }
}

impl Gba {
    pub fn reset_skip_bios(&mut self, entry: u32, sp_usr: u32, sp_irq: u32, sp_svc: u32) {
        let c = &mut self.cpu;
        c.switch_mode(MODE_IRQ);
        c.r[13] = sp_irq;
        c.switch_mode(MODE_SVC);
        c.r[13] = sp_svc;
        c.switch_mode(MODE_SYS);
        c.r[13] = sp_usr;
        c.irq_disable = 0;
        c.fiq_disable = 0;
        c.thumb = false;
        c.pc = entry;
    }

    // ------------------------------------------------------------ CPU stores (SMC aware)
    #[inline(always)]
    fn smc_check(&mut self, a: u32, size: i64) {
        let d = a as i64 - self.cpu.pc as i64;
        if d > -4 && d < 8 {
            self.smc_store(a, size);
        }
    }

    fn smc_store(&mut self, a: u32, size: i64) {
        let step: i64 = if self.cpu.thumb { 2 } else { 4 };
        let a = a as i64;
        let end = a + size;
        let pc = self.cpu.pc as i64;
        for s in [pc, pc + step] {
            if a < s + step && s < end && !self.cpu.stale.iter().any(|&(k, _)| k as i64 == s) {
                let su = s as u32;
                let op = if step == 2 { self.r16(su) } else { self.r32(su) };
                self.cpu.stale.push((su, op));
            }
        }
        if !self.cpu.stale.is_empty() {
            self.cpu.pf_next = self.cpu.pc;
        }
    }

    #[inline(always)]
    fn cw8(&mut self, a: u32, v: u32) {
        self.smc_check(a, 1);
        self.w8(a, v);
    }

    #[inline(always)]
    fn cw16(&mut self, a: u32, v: u32) {
        self.smc_check(a, 2);
        self.w16(a, v);
    }

    #[inline(always)]
    fn cw32(&mut self, a: u32, v: u32) {
        self.smc_check(a, 4);
        self.w32(a, v);
    }

    #[inline(always)]
    fn wt32(&self, a: u32) -> u64 {
        self.wait32[(a >> 24) as usize] as u64
    }

    #[inline(always)]
    fn wt16(&self, a: u32) -> u64 {
        self.wait16[(a >> 24) as usize] as u64
    }

    #[inline(always)]
    fn ldr_rot(&mut self, a: u32) -> u32 {
        self.r32(a).rotate_right((a & 3) << 3)
    }

    #[inline(always)]
    fn ldrh_rot(&mut self, a: u32) -> u32 {
        let v = self.r16(a);
        if a & 1 != 0 {
            v.rotate_right(8)
        } else {
            v
        }
    }

    #[inline(always)]
    fn ldrsb(&mut self, a: u32) -> u32 {
        self.r8(a) as u8 as i8 as i32 as u32
    }

    #[inline(always)]
    fn ldrsh(&mut self, a: u32) -> u32 {
        if a & 1 != 0 {
            self.ldrsb(a)
        } else {
            self.r16(a) as u16 as i16 as i32 as u32
        }
    }

    fn swi(&mut self, n: u32) -> u64 {
        if self.cpu.hle {
            return self.hle_swi(n);
        }
        let pc = self.cpu.pc;
        self.cpu.exception(VEC_SWI, MODE_SVC, pc);
        3
    }

    fn undefined(&mut self) -> u64 {
        let pc = self.cpu.pc;
        self.cpu.exception(VEC_UNDEF, MODE_UND, pc);
        3
    }

    // ------------------------------------------------------------ run loop
    /// Run until `now` reaches limit, or an earlier `deadline` set meanwhile.
    pub fn run(&mut self, limit: u64) {
        self.cpu.deadline = limit;
        while self.cpu.now < self.cpu.deadline {
            if self.cpu.halted {
                if self.ie & self.if_ != 0 {
                    self.cpu.halted = false;
                } else {
                    self.cpu.now = self.cpu.deadline;
                    return;
                }
            }
            if self.ime != 0 && self.ie & self.if_ != 0 && self.cpu.irq_disable == 0 {
                let lr = self.cpu.pc.wrapping_add(4);
                self.cpu.exception(VEC_IRQ, MODE_IRQ, lr);
            }
            self.cpu.limit = self.cpu.deadline;
            if self.cpu.thumb {
                self.run_thumb();
            } else {
                self.run_arm();
            }
        }
    }

    #[inline(always)]
    fn fetch32(&mut self, pc: u32) -> u32 {
        match pc >> 24 {
            3 => rd32_(&self.iwram, (pc as usize & 0x7FFF) & !3),
            8..=13 => rd32_(&self.rom, (pc as usize & 0x1FF_FFFF) & !3),
            2 => rd32_(&self.ewram, (pc as usize & 0x3FFFF) & !3),
            _ => {
                if pc < 0x4000 {
                    self.cpu.bios_latch = rd32_(&self.bios, ((pc.wrapping_add(8) & 0x3FFF) & !3) as usize);
                    return rd32_(&self.bios, (pc & !3) as usize);
                }
                self.r32(pc)
            }
        }
    }

    #[inline(always)]
    fn fetch16(&mut self, pc: u32) -> u32 {
        match pc >> 24 {
            8..=13 => rd16_(&self.rom, (pc as usize & 0x1FF_FFFF) & !1),
            3 => rd16_(&self.iwram, (pc as usize & 0x7FFF) & !1),
            2 => rd16_(&self.ewram, (pc as usize & 0x3FFFF) & !1),
            _ => {
                if pc < 0x4000 {
                    self.cpu.bios_latch = rd32_(&self.bios, (pc.wrapping_add(4) & 0x3FFC) as usize);
                    return rd16_(&self.bios, (pc & !1) as usize);
                }
                self.r16(pc)
            }
        }
    }

    #[inline(always)]
    fn take_stale(&mut self, pc: u32, op: u32, step: u32) -> u32 {
        let mut op = op;
        if pc != self.cpu.pf_next {
            self.cpu.stale.clear();
        } else if let Some(i) = self.cpu.stale.iter().position(|&(k, _)| k == pc) {
            op = self.cpu.stale.remove(i).1;
        }
        self.cpu.pf_next = pc.wrapping_add(step);
        op
    }

    fn run_arm(&mut self) {
        while self.cpu.now < self.cpu.limit {
            let pc = self.cpu.pc;
            let mut op = self.fetch32(pc);
            let ft = self.code32[(pc >> 24) as usize] as u64;
            if !self.cpu.stale.is_empty() {
                op = self.take_stale(pc, op, 4);
            }
            self.cpu.r[15] = pc.wrapping_add(8);
            self.cpu.pc = pc.wrapping_add(4);
            let c = op >> 28;
            if c == 14 || cond(c, self.cpu.f) {
                // KNOWN INACCURACY, kept for parity with the former Python core (which
                // did `now += fn() + ft`): cycles a handler adds to `now` itself (an
                // immediate DMA started by a store) are dropped. Same in run_thumb.
                // Fixing it changes timing everywhere, so it is a separate change.
                let t0 = self.cpu.now;
                let cyc = self.exec_arm(op);
                self.cpu.now = t0 + cyc + ft;
            } else {
                self.cpu.now += ft;
            }
        }
    }

    fn run_thumb(&mut self) {
        while self.cpu.now < self.cpu.limit {
            let pc = self.cpu.pc;
            let mut op = self.fetch16(pc);
            let ft = self.code16[(pc >> 24) as usize] as u64;
            if !self.cpu.stale.is_empty() {
                op = self.take_stale(pc, op, 2);
            }
            self.cpu.r[15] = pc.wrapping_add(4);
            self.cpu.pc = pc.wrapping_add(2);
            let t0 = self.cpu.now;
            let cyc = self.exec_thumb(op);
            self.cpu.now = t0 + cyc + ft;
        }
    }

    /// Execute exactly one instruction (tests); returns cycles.
    pub fn step(&mut self) -> u64 {
        let t0 = self.cpu.now;
        self.run(t0 + 1);
        self.cpu.now - t0
    }

    // ------------------------------------------------------------ ARM
    pub fn exec_arm(&mut self, op: u32) -> u64 {
        let top = (op >> 25) & 7;
        if (op & 0x0FFF_FFF0) == 0x012F_FF10 {
            return self.arm_bx(op);
        }
        match top {
            0 => {
                if (op & 0x0FC0_00F0) == 0x0000_0090 {
                    self.arm_mul(op)
                } else if (op & 0x0F80_00F0) == 0x0080_0090 {
                    self.arm_mull(op)
                } else if (op & 0x0FB0_0FF0) == 0x0100_0090 {
                    self.arm_swp(op)
                } else if (op & 0x90) == 0x90 && (op & 0x60) != 0 {
                    self.arm_half(op)
                } else if (op & 0x0190_0000) == 0x0100_0000 {
                    self.arm_psr(op)
                } else {
                    self.arm_dp(op)
                }
            }
            1 => {
                if (op & 0x0190_0000) == 0x0100_0000 {
                    if op & 0x0020_0000 != 0 {
                        self.arm_psr(op)
                    } else {
                        1
                    }
                } else {
                    self.arm_dp(op)
                }
            }
            2 => self.arm_sdt(op),
            3 if op & 0x10 == 0 => self.arm_sdt(op),
            4 => self.arm_block(op),
            5 => {
                let off = (((op & 0xFF_FFFF) << 8) as i32 >> 6) as u32;
                let r15 = self.cpu.r[15];
                if op & 0x0100_0000 != 0 {
                    self.cpu.r[14] = r15.wrapping_sub(4);
                }
                self.cpu.pc = r15.wrapping_add(off);
                2
            }
            7 if op & 0x0100_0000 != 0 => self.swi((op >> 16) & 0xFF),
            _ => self.undefined(),
        }
    }

    fn arm_dp(&mut self, op: u32) -> u64 {
        let opc = (op >> 21) & 15;
        let s = op & 0x0010_0000 != 0;
        let rn = ((op >> 16) & 15) as usize;
        let rd = ((op >> 12) & 15) as usize;
        let mut cycles = 1;
        let mut reg_shift = false;
        let (b, sc) = if op & 0x0200_0000 != 0 {
            let rot = ((op >> 8) & 15) * 2;
            let imm = (op & 0xFF).rotate_right(rot);
            (imm, if rot != 0 { imm >> 31 } else { self.cpu.carry() })
        } else {
            let rm = (op & 15) as usize;
            let typ = (op >> 5) & 3;
            if op & 0x10 != 0 {
                reg_shift = true;
                cycles += 1;
                let v = self.cpu.r[rm].wrapping_add(if rm == 15 { 4 } else { 0 });
                let amt = self.cpu.r[((op >> 8) & 15) as usize] & 255;
                self.cpu.shift_reg(v, amt, typ)
            } else {
                self.cpu.shift_imm(self.cpu.r[rm], typ, (op >> 7) & 31)
            }
        };
        let a = if opc != 13 && opc != 15 {
            self.cpu.r[rn].wrapping_add(if rn == 15 && reg_shift { 4 } else { 0 })
        } else {
            0
        };
        let res = self.cpu.alu(opc, a, b, s && rd != 15, sc);
        if !(8..=11).contains(&opc) {
            if rd == 15 {
                if s {
                    self.cpu.ret_spsr(res);
                } else {
                    self.cpu.pc = res & 0xFFFF_FFFC;
                }
                cycles += 2;
            } else {
                self.cpu.r[rd] = res;
            }
        } else if rd == 15 && s && self.cpu.bank != 0 {
            let sp = self.cpu.spsr[self.cpu.bank];
            self.cpu.set_cpsr(sp);
        }
        cycles
    }

    fn arm_psr(&mut self, op: u32) -> u64 {
        let use_spsr = op & 0x0040_0000 != 0;
        if op & 0x0020_0000 == 0 {
            let rd = ((op >> 12) & 15) as usize;
            self.cpu.r[rd] = if use_spsr { self.cpu.get_spsr() } else { self.cpu.get_cpsr() };
            return 1;
        }
        let mut mask = 0u32;
        if op & 0x0008_0000 != 0 {
            mask |= 0xFF00_0000;
        }
        if op & 0x0004_0000 != 0 {
            mask |= 0x00FF_0000;
        }
        if op & 0x0002_0000 != 0 {
            mask |= 0x0000_FF00;
        }
        if op & 0x0001_0000 != 0 {
            mask |= 0x0000_00FF;
        }
        let v = if op & 0x0200_0000 != 0 {
            (op & 0xFF).rotate_right(((op >> 8) & 15) * 2)
        } else {
            self.cpu.r[(op & 15) as usize]
        };
        if use_spsr {
            let b = self.cpu.bank;
            if b != 0 {
                self.cpu.spsr[b] = (self.cpu.spsr[b] & !mask) | (v & mask);
            }
        } else {
            let m = if self.cpu.mode == MODE_USR { mask & 0xFF00_0000 } else { mask };
            let c = self.cpu.get_cpsr();
            self.cpu.set_cpsr((c & !m) | (v & m));
        }
        1
    }

    fn arm_mul(&mut self, op: u32) -> u64 {
        let rd = ((op >> 16) & 15) as usize;
        let rn = ((op >> 12) & 15) as usize;
        let rs = ((op >> 8) & 15) as usize;
        let rm = (op & 15) as usize;
        let acc = op & 0x0020_0000 != 0;
        let mut res = self.cpu.r[rm].wrapping_mul(self.cpu.r[rs]);
        if acc {
            res = res.wrapping_add(self.cpu.r[rn]);
        }
        self.cpu.r[rd] = res;
        if op & 0x0010_0000 != 0 {
            self.cpu.f = nz(res) | (self.cpu.f & 3);
        }
        if acc {
            3
        } else {
            2
        }
    }

    fn arm_mull(&mut self, op: u32) -> u64 {
        let hi = ((op >> 16) & 15) as usize;
        let lo = ((op >> 12) & 15) as usize;
        let rs = ((op >> 8) & 15) as usize;
        let rm = (op & 15) as usize;
        let signed = op & 0x0040_0000 != 0;
        let acc = op & 0x0020_0000 != 0;
        let mut t = if signed {
            ((self.cpu.r[rm] as i32 as i64) * (self.cpu.r[rs] as i32 as i64)) as u64
        } else {
            (self.cpu.r[rm] as u64) * (self.cpu.r[rs] as u64)
        };
        if acc {
            t = t.wrapping_add(((self.cpu.r[hi] as u64) << 32) | self.cpu.r[lo] as u64);
        }
        self.cpu.r[lo] = t as u32;
        self.cpu.r[hi] = (t >> 32) as u32;
        if op & 0x0010_0000 != 0 {
            self.cpu.f = (((t >> 60) & 8) as u32) | (((t == 0) as u32) << 2) | (self.cpu.f & 3);
        }
        if acc {
            4
        } else {
            3
        }
    }

    fn arm_swp(&mut self, op: u32) -> u64 {
        let rn = ((op >> 16) & 15) as usize;
        let rd = ((op >> 12) & 15) as usize;
        let rm = (op & 15) as usize;
        let a = self.cpu.r[rn];
        if op & 0x0040_0000 != 0 {
            let v = self.r8(a);
            let w = self.cpu.r[rm] & 255;
            self.cw8(a, w);
            self.cpu.r[rd] = v;
        } else {
            let v = self.ldr_rot(a);
            let w = self.cpu.r[rm];
            self.cw32(a, w);
            self.cpu.r[rd] = v;
        }
        4 + 2 * self.wt32(a)
    }

    /// Addressing for single/halfword transfers: (access address, writeback value).
    #[inline(always)]
    fn arm_addr(&self, op: u32, rn: usize, off: u32) -> (u32, Option<u32>) {
        let up = op & 0x0080_0000 != 0;
        let pre = op & 0x0100_0000 != 0;
        let base = self.cpu.r[rn];
        let moved = if up { base.wrapping_add(off) } else { base.wrapping_sub(off) };
        if pre {
            (moved, if op & 0x0020_0000 != 0 { Some(moved) } else { None })
        } else {
            (base, Some(moved))
        }
    }

    fn arm_sdt(&mut self, op: u32) -> u64 {
        let rn = ((op >> 16) & 15) as usize;
        let rd = ((op >> 12) & 15) as usize;
        let load = op & 0x0010_0000 != 0;
        let byte = op & 0x0040_0000 != 0;
        let off = if op & 0x0200_0000 != 0 {
            self.cpu.shift_imm(self.cpu.r[(op & 15) as usize], (op >> 5) & 3, (op >> 7) & 31).0
        } else {
            op & 0xFFF
        };
        let (a, wb) = self.arm_addr(op, rn, off);
        if load {
            let v = if byte { self.r8(a) } else { self.ldr_rot(a) };
            if let Some(w) = wb {
                if rn != rd {
                    self.cpu.r[rn] = w;
                }
            }
            if rd == 15 {
                self.cpu.pc = v & 0xFFFF_FFFC;
                5 + self.wt32(a)
            } else {
                self.cpu.r[rd] = v;
                2 + self.wt32(a)
            }
        } else {
            let val = self.cpu.r[rd].wrapping_add(if rd == 15 { 4 } else { 0 });
            if byte {
                self.cw8(a, val & 255);
            } else {
                self.cw32(a, val);
            }
            if let Some(w) = wb {
                self.cpu.r[rn] = w;
            }
            1 + self.wt32(a)
        }
    }

    fn arm_half(&mut self, op: u32) -> u64 {
        let rn = ((op >> 16) & 15) as usize;
        let rd = ((op >> 12) & 15) as usize;
        let load = op & 0x0010_0000 != 0;
        let sh = (op >> 5) & 3;
        let off = if op & 0x0040_0000 != 0 {
            ((op >> 4) & 0xF0) | (op & 15)
        } else {
            self.cpu.r[(op & 15) as usize]
        };
        let (a, wb) = self.arm_addr(op, rn, off);
        if load {
            let v = match sh {
                1 => self.ldrh_rot(a),
                2 => self.ldrsb(a),
                _ => self.ldrsh(a),
            };
            if let Some(w) = wb {
                if rn != rd {
                    self.cpu.r[rn] = w;
                }
            }
            if rd == 15 {
                self.cpu.pc = v & 0xFFFF_FFFC;
                5 + self.wt16(a)
            } else {
                self.cpu.r[rd] = v;
                2 + self.wt16(a)
            }
        } else {
            let val = self.cpu.r[rd].wrapping_add(if rd == 15 { 4 } else { 0 });
            if sh == 1 {
                self.cw16(a, val & 0xFFFF);
            }
            if let Some(w) = wb {
                self.cpu.r[rn] = w;
            }
            1 + self.wt16(a)
        }
    }

    fn arm_block(&mut self, op: u32) -> u64 {
        let rn = ((op >> 16) & 15) as usize;
        let load = op & 0x0010_0000 != 0;
        let wb = op & 0x0020_0000 != 0;
        let user = op & 0x0040_0000 != 0;
        let up = op & 0x0080_0000 != 0;
        let pre = op & 0x0100_0000 != 0;
        let mut rlist = op & 0xFFFF;
        let (n, size) = if rlist == 0 {
            rlist = 0x8000; // ARM7 quirk: transfers r15, base moves 0x40
            (1u32, 0x40u32)
        } else {
            (rlist.count_ones(), 4 * rlist.count_ones())
        };
        let base = self.cpu.r[rn];
        let (start, new) = if up {
            (if pre { base.wrapping_add(4) } else { base }, base.wrapping_add(size))
        } else {
            let s = base.wrapping_sub(size).wrapping_add(if pre { 0 } else { 4 });
            (s, base.wrapping_sub(size))
        };
        let mut a = start;
        let w = self.wt32(a);
        let has15 = rlist & 0x8000 != 0;
        let user_bank = user && !(load && has15);
        if load {
            if wb && rlist & (1 << rn) == 0 {
                self.cpu.r[rn] = new;
            }
            for i in 0..16usize {
                if rlist >> i & 1 == 0 {
                    continue;
                }
                let v = self.r32(a);
                if i == 15 {
                    if user {
                        self.cpu.ret_spsr(v);
                    } else {
                        self.cpu.pc = v & 0xFFFF_FFFC;
                    }
                } else if user_bank && i >= 8 {
                    self.cpu.set_user_reg(i, v);
                } else {
                    self.cpu.r[i] = v;
                }
                a = a.wrapping_add(4);
            }
        } else {
            let first = rlist.trailing_zeros() as usize;
            for i in 0..16usize {
                if rlist >> i & 1 == 0 {
                    continue;
                }
                let val = if i == 15 {
                    self.cpu.r[15].wrapping_add(4)
                } else if i == rn && wb && i != first {
                    new
                } else if user_bank && i >= 8 {
                    self.cpu.user_reg(i)
                } else {
                    self.cpu.r[i]
                };
                self.cw32(a, val);
                a = a.wrapping_add(4);
            }
            if wb {
                self.cpu.r[rn] = new;
            }
        }
        let n = n as u64;
        n + if load { 3 } else { 1 } + n * w
    }

    fn arm_bx(&mut self, op: u32) -> u64 {
        let v = self.cpu.r[(op & 15) as usize];
        if v & 1 != 0 {
            self.cpu.thumb = true;
            self.cpu.pc = v & 0xFFFF_FFFE;
            self.cpu.limit = 0;
        } else {
            self.cpu.pc = v & 0xFFFF_FFFC;
        }
        2
    }

    // ------------------------------------------------------------ THUMB
    pub fn exec_thumb(&mut self, op: u32) -> u64 {
        match op >> 13 {
            0 => {
                if (op >> 11) == 3 {
                    self.t_addsub(op)
                } else {
                    let typ = (op >> 11) & 3;
                    let amt = (op >> 6) & 31;
                    let rs = ((op >> 3) & 7) as usize;
                    let rd = (op & 7) as usize;
                    let (res, sc) = self.cpu.shift_imm(self.cpu.r[rs], typ, amt);
                    self.cpu.f = nz(res) | (sc << 1) | (self.cpu.f & 1);
                    self.cpu.r[rd] = res;
                    0
                }
            }
            1 => {
                let opc = (op >> 11) & 3;
                let rd = ((op >> 8) & 7) as usize;
                let arm = [13, 10, 4, 2][opc as usize];
                let a = self.cpu.r[rd];
                let res = self.cpu.alu(arm, a, op & 0xFF, true, self.cpu.carry());
                if arm != 10 {
                    self.cpu.r[rd] = res;
                }
                0
            }
            2 => {
                if (op >> 10) == 0x10 {
                    self.t_alu(op)
                } else if (op >> 10) == 0x11 {
                    self.t_hireg(op)
                } else if (op >> 11) == 9 {
                    let rd = ((op >> 8) & 7) as usize;
                    let a = (self.cpu.r[15] & 0xFFFF_FFFC).wrapping_add((op & 0xFF) << 2);
                    let v = self.r32(a);
                    self.cpu.r[rd] = v;
                    2 + self.wt32(a)
                } else {
                    self.t_ldst_reg(op)
                }
            }
            3 => {
                let byte = op & 0x1000 != 0;
                let load = op & 0x0800 != 0;
                let mut off = (op >> 6) & 31;
                if !byte {
                    off <<= 2;
                }
                let a = self.cpu.r[((op >> 3) & 7) as usize].wrapping_add(off);
                self.t_access((op & 7) as usize, a, load, byte)
            }
            4 => {
                if op & 0x1000 != 0 {
                    let load = op & 0x0800 != 0;
                    let rd = ((op >> 8) & 7) as usize;
                    let a = self.cpu.r[13].wrapping_add((op & 0xFF) << 2);
                    self.t_access(rd, a, load, false)
                } else {
                    let load = op & 0x0800 != 0;
                    let rd = (op & 7) as usize;
                    let a = self.cpu.r[((op >> 3) & 7) as usize].wrapping_add(((op >> 6) & 31) << 1);
                    if load {
                        let v = self.ldrh_rot(a);
                        self.cpu.r[rd] = v;
                        2 + self.wt16(a)
                    } else {
                        let v = self.cpu.r[rd] & 0xFFFF;
                        self.cw16(a, v);
                        1 + self.wt16(a)
                    }
                }
            }
            5 => {
                if op & 0x1000 == 0 {
                    let rd = ((op >> 8) & 7) as usize;
                    let src = if op & 0x0800 != 0 { self.cpu.r[13] } else { self.cpu.r[15] & 0xFFFF_FFFC };
                    self.cpu.r[rd] = src.wrapping_add((op & 0xFF) << 2);
                    0
                } else if (op >> 8) == 0xB0 {
                    let off = (op & 0x7F) << 2;
                    self.cpu.r[13] = if op & 0x80 != 0 {
                        self.cpu.r[13].wrapping_sub(off)
                    } else {
                        self.cpu.r[13].wrapping_add(off)
                    };
                    0
                } else if (op & 0x0600) == 0x0400 {
                    self.t_pushpop(op)
                } else {
                    self.undefined()
                }
            }
            6 => {
                if op & 0x1000 == 0 {
                    return self.t_ldm_stm(op);
                }
                let c = (op >> 8) & 15;
                if c == 15 {
                    return self.swi(op & 0xFF);
                }
                if c == 14 {
                    return self.undefined();
                }
                if cond(c, self.cpu.f) {
                    let off = ((op & 0xFF) as u8 as i8 as i32 as u32) << 1;
                    self.cpu.pc = self.cpu.r[15].wrapping_add(off);
                    2
                } else {
                    0
                }
            }
            _ => match (op >> 11) & 3 {
                0 => {
                    let off = ((((op & 0x7FF) << 21) as i32) >> 20) as u32;
                    self.cpu.pc = self.cpu.r[15].wrapping_add(off);
                    2
                }
                2 => {
                    let off = ((((op & 0x7FF) << 21) as i32) >> 9) as u32;
                    self.cpu.r[14] = self.cpu.r[15].wrapping_add(off);
                    0
                }
                3 => {
                    let nxt = self.cpu.r[15].wrapping_sub(2);
                    self.cpu.pc = self.cpu.r[14].wrapping_add((op & 0x7FF) << 1);
                    self.cpu.r[14] = nxt | 1;
                    2
                }
                _ => self.undefined(),
            },
        }
    }

    fn t_addsub(&mut self, op: u32) -> u64 {
        let rn = (op >> 6) & 7;
        let rs = ((op >> 3) & 7) as usize;
        let rd = (op & 7) as usize;
        let a = self.cpu.r[rs];
        let b = if op & 0x0400 != 0 { rn } else { self.cpu.r[rn as usize] };
        let res = self.cpu.alu(if op & 0x0200 != 0 { 2 } else { 4 }, a, b, true, 0);
        self.cpu.r[rd] = res;
        0
    }

    fn t_alu(&mut self, op: u32) -> u64 {
        let opc = (op >> 6) & 15;
        let rs = ((op >> 3) & 7) as usize;
        let rd = (op & 7) as usize;
        let a = self.cpu.r[rd];
        let b = self.cpu.r[rs];
        match opc {
            2 | 3 | 4 | 7 => {
                let typ = match opc {
                    2 => 0,
                    3 => 1,
                    4 => 2,
                    _ => 3,
                };
                let (res, sc) = self.cpu.shift_reg(a, b & 255, typ);
                self.cpu.f = nz(res) | (sc << 1) | (self.cpu.f & 1);
                self.cpu.r[rd] = res;
                1
            }
            9 => {
                let res = self.cpu.alu(2, 0, b, true, 0);
                self.cpu.r[rd] = res;
                0
            }
            13 => {
                let res = a.wrapping_mul(b);
                self.cpu.f = nz(res) | (self.cpu.f & 3);
                self.cpu.r[rd] = res;
                2
            }
            _ => {
                let c = self.cpu.carry();
                let res = self.cpu.alu(opc, a, b, true, c);
                if !(8..=11).contains(&opc) {
                    self.cpu.r[rd] = res;
                }
                0
            }
        }
    }

    fn t_hireg(&mut self, op: u32) -> u64 {
        let opc = (op >> 8) & 3;
        let rd = ((op & 7) | ((op >> 4) & 8)) as usize;
        let rs = ((op >> 3) & 15) as usize;
        match opc {
            3 => {
                let v = self.cpu.r[rs];
                if v & 1 != 0 {
                    self.cpu.pc = v & 0xFFFF_FFFE;
                } else {
                    self.cpu.thumb = false;
                    self.cpu.pc = v & 0xFFFF_FFFC;
                    self.cpu.limit = 0;
                }
                2
            }
            1 => {
                let (a, b) = (self.cpu.r[rd], self.cpu.r[rs]);
                self.cpu.alu(10, a, b, true, 0);
                0
            }
            _ => {
                let res = if opc == 0 {
                    self.cpu.r[rd].wrapping_add(self.cpu.r[rs])
                } else {
                    self.cpu.r[rs]
                };
                if rd == 15 {
                    self.cpu.pc = res & 0xFFFF_FFFE;
                    2
                } else {
                    self.cpu.r[rd] = res;
                    0
                }
            }
        }
    }

    fn t_ldst_reg(&mut self, op: u32) -> u64 {
        let ro = ((op >> 6) & 7) as usize;
        let rb = ((op >> 3) & 7) as usize;
        let rd = (op & 7) as usize;
        let a = self.cpu.r[rb].wrapping_add(self.cpu.r[ro]);
        if op & 0x0200 != 0 {
            let kind = (op >> 10) & 3;
            if kind == 0 {
                let v = self.cpu.r[rd] & 0xFFFF;
                self.cw16(a, v);
                return 1 + self.wt16(a);
            }
            let v = match kind {
                1 => self.ldrsb(a),
                2 => self.ldrh_rot(a),
                _ => self.ldrsh(a),
            };
            self.cpu.r[rd] = v;
            return 2 + self.wt16(a);
        }
        self.t_access(rd, a, op & 0x0800 != 0, op & 0x0400 != 0)
    }

    #[inline(always)]
    fn t_access(&mut self, rd: usize, a: u32, load: bool, byte: bool) -> u64 {
        if load {
            if byte {
                let v = self.r8(a);
                self.cpu.r[rd] = v;
                2 + self.wt16(a)
            } else {
                let v = self.ldr_rot(a);
                self.cpu.r[rd] = v;
                2 + self.wt32(a)
            }
        } else if byte {
            let v = self.cpu.r[rd] & 255;
            self.cw8(a, v);
            1 + self.wt16(a)
        } else {
            let v = self.cpu.r[rd];
            self.cw32(a, v);
            1 + self.wt32(a)
        }
    }

    fn t_pushpop(&mut self, op: u32) -> u64 {
        let load = op & 0x0800 != 0;
        let extra = op & 0x0100 != 0;
        let mut rlist = op & 0xFF;
        if extra {
            rlist |= if load { 0x8000 } else { 0x4000 };
        }
        let (n, size) = if rlist == 0 {
            rlist = 0x8000;
            (1u32, 0x40u32)
        } else {
            (rlist.count_ones(), 4 * rlist.count_ones())
        };
        let w;
        if load {
            let mut a = self.cpu.r[13];
            w = self.wt32(a);
            for i in 0..16usize {
                if rlist >> i & 1 == 0 {
                    continue;
                }
                let v = self.r32(a);
                if i == 15 {
                    self.cpu.pc = v & 0xFFFF_FFFE;
                } else {
                    self.cpu.r[i] = v;
                }
                a = a.wrapping_add(4);
            }
            self.cpu.r[13] = self.cpu.r[13].wrapping_add(size);
        } else {
            let mut a = self.cpu.r[13].wrapping_sub(size);
            w = self.wt32(a);
            for i in 0..16usize {
                if rlist >> i & 1 == 0 {
                    continue;
                }
                let val = if i == 15 { self.cpu.r[15].wrapping_add(2) } else { self.cpu.r[i] };
                self.cw32(a, val);
                a = a.wrapping_add(4);
            }
            self.cpu.r[13] = self.cpu.r[13].wrapping_sub(size);
        }
        let n = n as u64;
        n + if load { 2 } else { 0 } + n * w
    }

    fn t_ldm_stm(&mut self, op: u32) -> u64 {
        let load = op & 0x0800 != 0;
        let rb = ((op >> 8) & 7) as usize;
        let mut rlist = op & 0xFF;
        let (n, size) = if rlist == 0 {
            rlist = 0x8000;
            (1u32, 0x40u32)
        } else {
            (rlist.count_ones(), 4 * rlist.count_ones())
        };
        let base = self.cpu.r[rb];
        let new = base.wrapping_add(size);
        let mut a = base;
        let w = self.wt32(a);
        if load {
            if rlist & (1 << rb) == 0 {
                self.cpu.r[rb] = new;
            }
            for i in 0..16usize {
                if rlist >> i & 1 == 0 {
                    continue;
                }
                let v = self.r32(a);
                if i == 15 {
                    self.cpu.pc = v & 0xFFFF_FFFE;
                } else {
                    self.cpu.r[i] = v;
                }
                a = a.wrapping_add(4);
            }
        } else {
            let first = rlist.trailing_zeros() as usize;
            for i in 0..16usize {
                if rlist >> i & 1 == 0 {
                    continue;
                }
                let val = if i == 15 {
                    self.cpu.r[15].wrapping_add(2)
                } else if i == rb && i != first {
                    new
                } else {
                    self.cpu.r[i]
                };
                self.cw32(a, val);
                a = a.wrapping_add(4);
            }
            self.cpu.r[rb] = new;
        }
        let n = n as u64;
        n + if load { 2 } else { 0 } + n * w
    }
}

#[inline(always)]
fn rd32_(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]])
}

#[inline(always)]
fn rd16_(b: &[u8], o: usize) -> u32 {
    u16::from_le_bytes([b[o], b[o + 1]]) as u32
}
