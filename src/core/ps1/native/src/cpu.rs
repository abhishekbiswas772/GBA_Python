//! R3000A (MIPS I) interpreter: load and branch delay slots, COP0 exceptions,
//! cache isolation, and COP2 (GTE) dispatch.
//!
//! Load delay: a load issued by instruction N lands after instruction N+1 has
//! executed. N+1 still reads the old value, and if N+1 writes that register
//! itself, its write wins. `pend` holds the load from the previous instruction
//! while the current one runs.

use crate::bus::Bus;
use crate::gte::Gte;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Exc {
    Interrupt = 0x0,
    LoadAddr = 0x4,
    BusInstr = 0x6,
    StoreAddr = 0x5,
    Syscall = 0x8,
    Break = 0x9,
    Reserved = 0xA,
    CopUnusable = 0xB,
    Overflow = 0xC,
}

/// HLE trap: `HLE_TRAP | id`, placed only at the kernel vectors (0x80..0xD0). It is an
/// SWC3 encoding, so it is honoured only there and only while the HLE kernel runs.
pub const HLE_TRAP: u32 = 0xEC00_0000;

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct Cpu {
    pub regs: [u32; 32],
    pub pc: u32,
    pub next_pc: u32,
    pub cur_pc: u32,
    pub hi: u32,
    pub lo: u32,
    load: (usize, u32),
    pend: (usize, u32),
    branch: bool,
    delay_slot: bool,
    pub sr: u32,
    pub cause: u32,
    pub epc: u32,
    pub bad_vaddr: u32,
    pub cop0: [u32; 16],
    pub gte: Gte,
    /// HLE trap id hit by the last instruction, if any.
    pub hle_hit: Option<u32>,
    /// True when the HLE kernel is installed (no BIOS).
    pub hle_traps: bool,
}

impl Default for Cpu {
    fn default() -> Self {
        Self::new()
    }
}

#[inline(always)]
fn rs(i: u32) -> u32 {
    (i >> 21) & 31
}
#[inline(always)]
fn rt(i: u32) -> u32 {
    (i >> 16) & 31
}
#[inline(always)]
fn rd(i: u32) -> u32 {
    (i >> 11) & 31
}
#[inline(always)]
fn imm(i: u32) -> u32 {
    i & 0xFFFF
}
#[inline(always)]
fn imm_se(i: u32) -> u32 {
    (i as i16) as i32 as u32
}

impl Cpu {
    pub fn new() -> Self {
        Cpu {
            regs: [0; 32],
            pc: 0xBFC0_0000,
            next_pc: 0xBFC0_0004,
            cur_pc: 0xBFC0_0000,
            hi: 0,
            lo: 0,
            load: (0, 0),
            pend: (0, 0),
            branch: false,
            delay_slot: false,
            sr: 0x0040_0000, // BEV: exception vectors in ROM
            cause: 0,
            epc: 0,
            bad_vaddr: 0,
            cop0: [0; 16],
            gte: Gte::default(),
            hle_hit: None,
            hle_traps: false,
        }
    }

    /// Continue execution at `pc` (EXE load, HLE return). Settles any pending load.
    pub fn set_pc(&mut self, pc: u32) {
        self.flush_load();
        self.pc = pc;
        self.next_pc = pc.wrapping_add(4);
        self.branch = false;
        self.delay_slot = false;
    }

    /// Apply a pending delayed load immediately.
    pub fn flush_load(&mut self) {
        let (r, v) = self.load;
        if r != 0 {
            self.regs[r] = v;
        }
        self.load = (0, 0);
    }

    /// Write a register from outside (HLE), cancelling a pending load to it.
    pub fn write_reg(&mut self, i: usize, v: u32) {
        if self.load.0 == i {
            self.load = (0, 0);
        }
        if i != 0 {
            self.regs[i] = v;
        }
    }

    #[inline(always)]
    fn reg(&self, i: u32) -> u32 {
        self.regs[i as usize]
    }

    #[inline(always)]
    fn set_reg(&mut self, i: u32, v: u32) {
        let i = i as usize;
        if i != 0 {
            self.regs[i] = v;
        }
        if self.pend.0 == i {
            self.pend = (0, 0);
        }
    }

    #[inline(always)]
    fn delayed_load(&mut self, r: u32, v: u32) {
        let r = r as usize;
        if r != 0 {
            self.load = (r, v);
        }
    }

    #[inline(always)]
    fn branch_to(&mut self, target: u32) {
        self.next_pc = target;
        self.branch = true;
    }

    /// Execute one instruction (or enter an interrupt). Returns CPU cycles used.
    #[inline]
    pub fn step(&mut self, bus: &mut Bus) -> u32 {
        if bus.irq_line() {
            self.cause |= 0x400;
        } else {
            self.cause &= !0x400;
        }
        self.pend = self.load;
        self.load = (0, 0);

        if (self.sr & 1) != 0 && (self.cause & self.sr & 0xFF00) != 0 {
            self.cur_pc = self.pc;
            self.delay_slot = self.branch;
            self.branch = false;
            self.finish_pend();
            self.exception(Exc::Interrupt);
            return 1;
        }

        let pc = self.pc;
        self.cur_pc = pc;
        self.delay_slot = self.branch;
        self.branch = false;
        if pc & 3 != 0 {
            self.bad_vaddr = pc;
            self.finish_pend();
            self.exception(Exc::LoadAddr);
            return 1;
        }
        let instr = match bus.fetch(pc) {
            Some(w) => w,
            None => {
                // instruction bus error: code can't run from the scratchpad or I/O
                self.finish_pend();
                self.exception(Exc::BusInstr);
                return 1;
            }
        };
        self.pc = self.next_pc;
        self.next_pc = self.pc.wrapping_add(4);
        let cycles = self.execute(instr, bus);
        self.finish_pend();
        cycles
    }

    #[inline(always)]
    fn finish_pend(&mut self) {
        let (r, v) = self.pend;
        if r != 0 {
            self.regs[r] = v;
        }
        self.pend = (0, 0);
    }

    pub fn exception(&mut self, exc: Exc) {
        self.exception_cop(exc, 0);
    }

    fn exception_cop(&mut self, exc: Exc, cop: u32) {
        let handler = if self.sr & (1 << 22) != 0 { 0xBFC0_0180 } else { 0x8000_0080 };
        // push the KU/IE mode stack
        let mode = self.sr & 0x3F;
        self.sr = (self.sr & !0x3F) | ((mode << 2) & 0x3F);
        self.cause = (self.cause & !0xB000_007C) | ((exc as u32) << 2) | ((cop & 3) << 28);
        if self.delay_slot {
            self.epc = self.cur_pc.wrapping_sub(4);
            self.cause |= 1 << 31;
        } else {
            self.epc = self.cur_pc;
        }
        self.pc = handler;
        self.next_pc = handler.wrapping_add(4);
        self.branch = false;
        self.delay_slot = false;
    }

    // ------------------------------------------------------------ memory helpers
    #[inline(always)]
    fn isolated(&self) -> bool {
        self.sr & 0x10000 != 0
    }

    fn load_addr_error(&mut self, addr: u32) {
        self.bad_vaddr = addr;
        self.exception(Exc::LoadAddr);
    }

    fn store_addr_error(&mut self, addr: u32) {
        self.bad_vaddr = addr;
        self.exception(Exc::StoreAddr);
    }

    #[inline]
    fn execute(&mut self, i: u32, bus: &mut Bus) -> u32 {
        match i >> 26 {
            0x00 => self.special(i, bus),
            0x01 => {
                // BcondZ: bit 16 = GEZ, rt & 0x1E == 0x10 links
                let s = self.reg(rs(i)) as i32;
                let ge = (i >> 16) & 1 != 0;
                let link = (rt(i) & 0x1E) == 0x10;
                let taken = if ge { s >= 0 } else { s < 0 };
                if link {
                    let ra = self.next_pc;
                    self.set_reg(31, ra);
                }
                if taken {
                    let t = self.pc.wrapping_add(imm_se(i) << 2);
                    self.branch_to(t);
                }
            }
            0x02 => {
                let t = (self.pc & 0xF000_0000) | ((i & 0x03FF_FFFF) << 2);
                self.branch_to(t);
            }
            0x03 => {
                let ra = self.next_pc;
                self.set_reg(31, ra);
                let t = (self.pc & 0xF000_0000) | ((i & 0x03FF_FFFF) << 2);
                self.branch_to(t);
            }
            0x04 => {
                if self.reg(rs(i)) == self.reg(rt(i)) {
                    let t = self.pc.wrapping_add(imm_se(i) << 2);
                    self.branch_to(t);
                }
            }
            0x05 => {
                if self.reg(rs(i)) != self.reg(rt(i)) {
                    let t = self.pc.wrapping_add(imm_se(i) << 2);
                    self.branch_to(t);
                }
            }
            0x06 => {
                if (self.reg(rs(i)) as i32) <= 0 {
                    let t = self.pc.wrapping_add(imm_se(i) << 2);
                    self.branch_to(t);
                }
            }
            0x07 => {
                if (self.reg(rs(i)) as i32) > 0 {
                    let t = self.pc.wrapping_add(imm_se(i) << 2);
                    self.branch_to(t);
                }
            }
            0x08 => {
                let s = self.reg(rs(i)) as i32;
                match s.checked_add(imm_se(i) as i32) {
                    Some(v) => self.set_reg(rt(i), v as u32),
                    None => self.exception(Exc::Overflow),
                }
            }
            0x09 => {
                let v = self.reg(rs(i)).wrapping_add(imm_se(i));
                self.set_reg(rt(i), v);
            }
            0x0A => {
                let v = ((self.reg(rs(i)) as i32) < (imm_se(i) as i32)) as u32;
                self.set_reg(rt(i), v);
            }
            0x0B => {
                let v = (self.reg(rs(i)) < imm_se(i)) as u32;
                self.set_reg(rt(i), v);
            }
            0x0C => {
                let v = self.reg(rs(i)) & imm(i);
                self.set_reg(rt(i), v);
            }
            0x0D => {
                let v = self.reg(rs(i)) | imm(i);
                self.set_reg(rt(i), v);
            }
            0x0E => {
                let v = self.reg(rs(i)) ^ imm(i);
                self.set_reg(rt(i), v);
            }
            0x0F => self.set_reg(rt(i), imm(i) << 16),
            0x10 => self.cop0_op(i),
            0x11 => self.cop_absent(1),
            0x12 => self.cop2_op(i),
            0x13 => self.cop_absent(3),
            0x20 => {
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                let v = bus.read8(a) as i8 as i32 as u32;
                self.delayed_load(rt(i), v);
                return 1 + bus.take_stall();
            }
            0x21 => {
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                if a & 1 != 0 {
                    self.load_addr_error(a);
                } else {
                    let v = bus.read16(a) as i16 as i32 as u32;
                    self.delayed_load(rt(i), v);
                }
                return 1 + bus.take_stall();
            }
            0x22 => {
                // LWL: merges with a load still in flight to rt
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                let cur = if self.pend.0 == rt(i) as usize { self.pend.1 } else { self.reg(rt(i)) };
                let w = bus.read32(a & !3);
                let v = match a & 3 {
                    0 => (cur & 0x00FF_FFFF) | (w << 24),
                    1 => (cur & 0x0000_FFFF) | (w << 16),
                    2 => (cur & 0x0000_00FF) | (w << 8),
                    _ => w,
                };
                self.delayed_load(rt(i), v);
                return 1 + bus.take_stall();
            }
            0x23 => {
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                if a & 3 != 0 {
                    self.load_addr_error(a);
                } else {
                    let v = bus.read32(a);
                    self.delayed_load(rt(i), v);
                }
                return 1 + bus.take_stall();
            }
            0x24 => {
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                let v = bus.read8(a) as u32;
                self.delayed_load(rt(i), v);
                return 1 + bus.take_stall();
            }
            0x25 => {
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                if a & 1 != 0 {
                    self.load_addr_error(a);
                } else {
                    let v = bus.read16(a) as u32;
                    self.delayed_load(rt(i), v);
                }
                return 1 + bus.take_stall();
            }
            0x26 => {
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                let cur = if self.pend.0 == rt(i) as usize { self.pend.1 } else { self.reg(rt(i)) };
                let w = bus.read32(a & !3);
                let v = match a & 3 {
                    0 => w,
                    1 => (cur & 0xFF00_0000) | (w >> 8),
                    2 => (cur & 0xFFFF_0000) | (w >> 16),
                    _ => (cur & 0xFFFF_FF00) | (w >> 24),
                };
                self.delayed_load(rt(i), v);
                return 1 + bus.take_stall();
            }
            0x28 => {
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                if !self.isolated() {
                    bus.write8(a, self.reg(rt(i)) as u8);
                }
            }
            0x29 => {
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                if a & 1 != 0 {
                    self.store_addr_error(a);
                } else if !self.isolated() {
                    bus.write16(a, self.reg(rt(i)) as u16);
                }
            }
            0x2A => {
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                if !self.isolated() {
                    let v = self.reg(rt(i));
                    let al = a & !3;
                    let m = bus.read32(al);
                    let w = match a & 3 {
                        0 => (m & 0xFFFF_FF00) | (v >> 24),
                        1 => (m & 0xFFFF_0000) | (v >> 16),
                        2 => (m & 0xFF00_0000) | (v >> 8),
                        _ => v,
                    };
                    bus.write32(al, w);
                }
            }
            0x2B => {
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                if a & 3 != 0 {
                    self.store_addr_error(a);
                } else if !self.isolated() {
                    bus.write32(a, self.reg(rt(i)));
                }
            }
            0x2E => {
                let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                if !self.isolated() {
                    let v = self.reg(rt(i));
                    let al = a & !3;
                    let m = bus.read32(al);
                    let w = match a & 3 {
                        0 => v,
                        1 => (m & 0x0000_00FF) | (v << 8),
                        2 => (m & 0x0000_FFFF) | (v << 16),
                        _ => (m & 0x00FF_FFFF) | (v << 24),
                    };
                    bus.write32(al, w);
                }
            }
            // the trap shares SWC3's encoding: only the HLE kernel's vectors use it
            0x3B if self.hle_traps && i & 0xFC00_0000 == HLE_TRAP && self.cur_pc & 0x1FFF_FFFF < 0x100 => {
                self.hle_hit = Some(i & 0xFF);
            }
            0x32 => {
                // LWC2
                if self.sr & (1 << 30) == 0 {
                    self.cop_unusable(2);
                } else {
                    let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                    if a & 3 != 0 {
                        self.load_addr_error(a);
                    } else {
                        let v = bus.read32(a);
                        self.gte.write_data(rt(i) as usize, v);
                    }
                }
            }
            0x3A => {
                // SWC2
                if self.sr & (1 << 30) == 0 {
                    self.cop_unusable(2);
                } else {
                    let a = self.reg(rs(i)).wrapping_add(imm_se(i));
                    if a & 3 != 0 {
                        self.store_addr_error(a);
                    } else if !self.isolated() {
                        let v = self.gte.read_data(rt(i) as usize);
                        bus.write32(a, v);
                    }
                }
            }
            // LWCn / SWCn for COP0, 1, 3: nothing there; only checks the CU bit
            0x30 | 0x31 | 0x33 | 0x38 | 0x39 | 0x3B => self.cop_absent((i >> 26) & 3),
            _ => self.exception(Exc::Reserved),
        }
        1
    }

    fn cop_unusable(&mut self, n: u32) {
        self.exception_cop(Exc::CopUnusable, n);
    }

    /// A coprocessor that doesn't exist (or a COP0 load/store): an exception only
    /// when its CU bit is clear, otherwise the instruction does nothing.
    fn cop_absent(&mut self, n: u32) {
        if self.sr & (1 << (28 + n)) == 0 {
            self.cop_unusable(n);
        }
    }

    fn special(&mut self, i: u32, _bus: &mut Bus) {
        let sh = (i >> 6) & 31;
        match i & 0x3F {
            0x00 => {
                let v = self.reg(rt(i)) << sh;
                self.set_reg(rd(i), v);
            }
            0x02 => {
                let v = self.reg(rt(i)) >> sh;
                self.set_reg(rd(i), v);
            }
            0x03 => {
                let v = ((self.reg(rt(i)) as i32) >> sh) as u32;
                self.set_reg(rd(i), v);
            }
            0x04 => {
                let v = self.reg(rt(i)) << (self.reg(rs(i)) & 31);
                self.set_reg(rd(i), v);
            }
            0x06 => {
                let v = self.reg(rt(i)) >> (self.reg(rs(i)) & 31);
                self.set_reg(rd(i), v);
            }
            0x07 => {
                let v = ((self.reg(rt(i)) as i32) >> (self.reg(rs(i)) & 31)) as u32;
                self.set_reg(rd(i), v);
            }
            0x08 => {
                let t = self.reg(rs(i));
                self.branch_to(t);
            }
            0x09 => {
                let t = self.reg(rs(i));
                let ra = self.next_pc;
                self.set_reg(rd(i), ra);
                self.branch_to(t);
            }
            0x0C => self.exception(Exc::Syscall),
            0x0D => self.exception(Exc::Break),
            0x10 => {
                let v = self.hi;
                self.set_reg(rd(i), v);
            }
            0x11 => self.hi = self.reg(rs(i)),
            0x12 => {
                let v = self.lo;
                self.set_reg(rd(i), v);
            }
            0x13 => self.lo = self.reg(rs(i)),
            0x18 => {
                let v = (self.reg(rs(i)) as i32 as i64) * (self.reg(rt(i)) as i32 as i64);
                self.hi = (v >> 32) as u32;
                self.lo = v as u32;
            }
            0x19 => {
                let v = (self.reg(rs(i)) as u64) * (self.reg(rt(i)) as u64);
                self.hi = (v >> 32) as u32;
                self.lo = v as u32;
            }
            0x1A => {
                let n = self.reg(rs(i)) as i32;
                let d = self.reg(rt(i)) as i32;
                if d == 0 {
                    self.hi = n as u32;
                    self.lo = if n >= 0 { 0xFFFF_FFFF } else { 1 };
                } else if n as u32 == 0x8000_0000 && d == -1 {
                    self.hi = 0;
                    self.lo = 0x8000_0000;
                } else {
                    self.hi = (n % d) as u32;
                    self.lo = (n / d) as u32;
                }
            }
            0x1B => {
                let n = self.reg(rs(i));
                let d = self.reg(rt(i));
                if d == 0 {
                    self.hi = n;
                    self.lo = 0xFFFF_FFFF;
                } else {
                    self.hi = n % d;
                    self.lo = n / d;
                }
            }
            0x20 => {
                match (self.reg(rs(i)) as i32).checked_add(self.reg(rt(i)) as i32) {
                    Some(v) => self.set_reg(rd(i), v as u32),
                    None => self.exception(Exc::Overflow),
                }
            }
            0x21 => {
                let v = self.reg(rs(i)).wrapping_add(self.reg(rt(i)));
                self.set_reg(rd(i), v);
            }
            0x22 => {
                match (self.reg(rs(i)) as i32).checked_sub(self.reg(rt(i)) as i32) {
                    Some(v) => self.set_reg(rd(i), v as u32),
                    None => self.exception(Exc::Overflow),
                }
            }
            0x23 => {
                let v = self.reg(rs(i)).wrapping_sub(self.reg(rt(i)));
                self.set_reg(rd(i), v);
            }
            0x24 => {
                let v = self.reg(rs(i)) & self.reg(rt(i));
                self.set_reg(rd(i), v);
            }
            0x25 => {
                let v = self.reg(rs(i)) | self.reg(rt(i));
                self.set_reg(rd(i), v);
            }
            0x26 => {
                let v = self.reg(rs(i)) ^ self.reg(rt(i));
                self.set_reg(rd(i), v);
            }
            0x27 => {
                let v = !(self.reg(rs(i)) | self.reg(rt(i)));
                self.set_reg(rd(i), v);
            }
            0x2A => {
                let v = ((self.reg(rs(i)) as i32) < (self.reg(rt(i)) as i32)) as u32;
                self.set_reg(rd(i), v);
            }
            0x2B => {
                let v = (self.reg(rs(i)) < self.reg(rt(i))) as u32;
                self.set_reg(rd(i), v);
            }
            _ => self.exception(Exc::Reserved),
        }
    }

    fn cop0_read(&self, r: u32) -> u32 {
        match r {
            12 => self.sr,
            13 => self.cause,
            14 => self.epc,
            8 => self.bad_vaddr,
            15 => 0x0000_0002, // PRId
            3 | 5 | 6 | 7 | 9 | 11 => self.cop0[r as usize],
            _ => 0,
        }
    }

    fn cop0_op(&mut self, i: u32) {
        match rs(i) {
            0x00 => {
                let v = self.cop0_read(rd(i));
                self.delayed_load(rt(i), v);
            }
            0x04 => {
                let v = self.reg(rt(i));
                match rd(i) {
                    12 => self.sr = v,
                    13 => self.cause = (self.cause & !0x300) | (v & 0x300),
                    3 | 5 | 6 | 7 | 9 | 11 => self.cop0[rd(i) as usize] = v,
                    _ => {}
                }
            }
            0x10 if i & 0x3F == 0x10 => {
                // RFE: pop the mode stack
                let mode = self.sr & 0x3F;
                self.sr = (self.sr & !0xF) | (mode >> 2);
            }
            _ => {} // other COP0 operations are ignored by the R3000A
        }
    }

    fn cop2_op(&mut self, i: u32) {
        if self.sr & (1 << 30) == 0 {
            self.cop_unusable(2);
            return;
        }
        if i & (1 << 25) != 0 {
            self.gte.command(i);
            return;
        }
        match rs(i) {
            0x00 => {
                let v = self.gte.read_data(rd(i) as usize);
                self.delayed_load(rt(i), v);
            }
            0x02 => {
                let v = self.gte.read_ctrl(rd(i) as usize);
                self.delayed_load(rt(i), v);
            }
            0x04 => {
                let v = self.reg(rt(i));
                self.gte.write_data(rd(i) as usize, v);
            }
            0x06 => {
                let v = self.reg(rt(i));
                self.gte.write_ctrl(rd(i) as usize, v);
            }
            _ => self.exception(Exc::Reserved),
        }
    }
}
