//! A minimal high-level kernel, for running PS-X EXEs without a BIOS.
//!
//! It covers what PSn00bSDK-built test programs use: the exception entry with
//! a custom exit hook (`B(19h)`), `ReturnFromException`, the critical-section
//! syscalls, TTY output (`putchar`/`puts`/`printf`), root-counter auto-clear
//! (`C(0Ah)`), and the pad driver (`B(12h)`/`B(13h)`). Anything else returns 0
//! and is counted in `unknown` for diagnostics.
//!
//! The vectors at 0x80, 0xA0, 0xB0 and 0xC0 hold HLE trap words, so a program
//! that installs its own handler there simply runs its own code.

use crate::bus::Bus;
use crate::cpu::{Cpu, HLE_TRAP};

pub const TRAP_EXCEPTION: u32 = 0;
pub const TRAP_A: u32 = 1;
pub const TRAP_B: u32 = 2;
pub const TRAP_C: u32 = 3;
pub const TRAP_RETURN: u32 = 4;

// Kernel structures, at the addresses the real BIOS uses (programs read them).
const A_TABLE: u32 = 0x8000_0200; // RAM copy of the A(xxh) function table
const PCB_PTR: u32 = 0x8000_0108; // -> process control block
const TCB_PTR: u32 = 0x8000_0110; // -> thread control blocks
const RETURN_STUB: u32 = 0x8000_00D0;
const PCB: u32 = 0x8000_E000;
const TCB: u32 = 0x8000_E100;
const KERNEL_SP: u32 = 0x8000_DFF0;
// TCB layout: +0 status, +4 mode, +8 r0..r31, +88h epc, +8Ch hi, +90h lo, +94h sr, +98h cause
const T_REGS: u32 = 0x08;
const T_EPC: u32 = 0x88;
const T_HI: u32 = 0x8C;
const T_LO: u32 = 0x90;
const T_SR: u32 = 0x94;
const T_CAUSE: u32 = 0x98;

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct Hle {
    custom_exit: u32,
    clear_rcnt: [bool; 4],
    pad_buf: [(u32, u32); 2],
    pad_started: bool,
    pub unknown: Vec<(u8, u8)>,
}

impl Hle {
    pub fn new() -> Self {
        Hle { custom_exit: 0, clear_rcnt: [true; 4], pad_buf: [(0, 0); 2], pad_started: false, unknown: Vec::new() }
    }

    /// Put the trap words and kernel structures in RAM (before the EXE loads).
    pub fn install(bus: &mut Bus) {
        for (addr, id) in [(0x80u32, TRAP_EXCEPTION), (0xA0, TRAP_A), (0xB0, TRAP_B), (0xC0, TRAP_C),
                           (RETURN_STUB & 0xFF, TRAP_RETURN)] {
            bus.write32(addr, HLE_TRAP | id);
            bus.write32(addr + 4, 0); // nop
        }
        bus.write32(PCB_PTR, PCB);
        bus.write32(PCB_PTR + 4, 4);
        bus.write32(TCB_PTR, TCB);
        bus.write32(TCB_PTR + 4, 0xC0);
        bus.write32(PCB, TCB);
        bus.write32(TCB, 0x4000); // in use
        bus.write32(TCB + 4, 0x1000);
    }

    pub fn dispatch(&mut self, id: u32, cpu: &mut Cpu, bus: &mut Bus) {
        cpu.flush_load();
        match id {
            TRAP_EXCEPTION => self.exception(cpu, bus),
            TRAP_RETURN => self.return_from_exception(cpu, bus),
            TRAP_A | TRAP_B | TRAP_C => {
                let table = [0, 0xA0u8, 0xB0, 0xC0][id as usize];
                let f = cpu.regs[9] as u8; // t1
                let ret = self.call(table, f, cpu, bus);
                if let Some(v) = ret {
                    cpu.write_reg(2, v);
                    let ra = cpu.regs[31];
                    cpu.set_pc(ra);
                }
            }
            _ => {}
        }
    }

    fn tcb(bus: &mut Bus) -> u32 {
        let pcb = bus.read32(PCB_PTR);
        bus.read32(pcb)
    }

    // ------------------------------------------------------------ exceptions
    fn exception(&mut self, cpu: &mut Cpu, bus: &mut Bus) {
        let t = Self::tcb(bus);
        for r in 0..32u32 {
            bus.write32(t + T_REGS + 4 * r, cpu.regs[r as usize]);
        }
        bus.write32(t + T_HI, cpu.hi);
        bus.write32(t + T_LO, cpu.lo);
        bus.write32(t + T_SR, cpu.sr);
        bus.write32(t + T_CAUSE, cpu.cause);
        let mut epc = cpu.epc;
        let code = (cpu.cause >> 2) & 0x1F;
        match code {
            0 => {
                // an interrupted GTE command already executed on hardware: skip it
                let w = bus.read32(epc);
                if (w >> 25) & 0x7F == 0x25 {
                    epc = epc.wrapping_add(4);
                }
                bus.write32(t + T_EPC, epc);
                self.irq_defaults(bus);
                if self.custom_exit != 0 {
                    self.jump_custom_exit(cpu, bus);
                    return;
                }
                self.return_from_exception(cpu, bus);
            }
            8 => {
                // syscall: a0 selects the function; return after the syscall
                bus.write32(t + T_EPC, epc.wrapping_add(4));
                let sr = bus.read32(t + T_SR);
                match cpu.regs[4] {
                    1 => {
                        bus.write32(t + T_REGS + 8, ((sr & 0x404) == 0x404) as u32);
                        bus.write32(t + T_SR, sr & !0x404);
                    }
                    2 => bus.write32(t + T_SR, sr | 0x404),
                    _ => {}
                }
                self.return_from_exception(cpu, bus);
            }
            _ => {
                bus.write32(t + T_EPC, epc);
                // SystemErrorUnresolvedException: programs hook it via the A-table
                let hook = bus.read32(A_TABLE + 0x40 * 4);
                if hook != 0 {
                    cpu.write_reg(29, KERNEL_SP);
                    cpu.write_reg(31, RETURN_STUB);
                    cpu.set_pc(hook);
                    return;
                }
                bus.write32(t + T_EPC, epc.wrapping_add(4)); // skip the faulting instruction
                self.return_from_exception(cpu, bus);
            }
        }
    }

    fn irq_defaults(&mut self, bus: &mut Bus) {
        let pending = bus.i_stat & bus.i_mask;
        if pending & 1 != 0 {
            self.pad_vblank(bus);
        }
        // root counters 0..2 are timers (IRQ 4..6), RCnt 3 is vblank (IRQ 0)
        for (k, bit) in [(0usize, 1u32 << 4), (1, 1 << 5), (2, 1 << 6), (3, 1)] {
            if self.clear_rcnt[k] && pending & bit != 0 {
                bus.i_stat &= !bit;
            }
        }
    }

    fn pad_vblank(&mut self, bus: &mut Bus) {
        if !self.pad_started {
            return;
        }
        let b = bus.sio.buttons;
        for (k, (addr, size)) in self.pad_buf.into_iter().enumerate() {
            if addr == 0 || size < 2 {
                continue;
            }
            let connected = k == 0 && bus.sio.connected;
            bus.write8(addr, if connected { 0x00 } else { 0xFF });
            if connected && size >= 4 {
                bus.write8(addr + 1, 0x41);
                bus.write8(addr + 2, !(b as u8));
                bus.write8(addr + 3, !((b >> 8) as u8));
            }
        }
    }

    fn jump_custom_exit(&mut self, cpu: &mut Cpu, bus: &mut Bus) {
        // struct: ra(pc), sp, fp, s0..s7, gp
        let p = self.custom_exit;
        let w = |k: u32, bus: &mut Bus| bus.read32(p + 4 * k);
        let pc = w(0, bus);
        cpu.write_reg(29, w(1, bus));
        cpu.write_reg(30, w(2, bus));
        for k in 0..8 {
            cpu.write_reg(16 + k, w(3 + k as u32, bus));
        }
        cpu.write_reg(28, w(11, bus));
        cpu.write_reg(2, 1);
        cpu.set_pc(pc);
    }

    /// Restore the thread from its TCB in RAM (so handlers can change EPC etc.) and RFE.
    fn return_from_exception(&mut self, cpu: &mut Cpu, bus: &mut Bus) {
        let t = Self::tcb(bus);
        for r in 1..32u32 {
            let v = bus.read32(t + T_REGS + 4 * r);
            cpu.write_reg(r as usize, v);
        }
        cpu.hi = bus.read32(t + T_HI);
        cpu.lo = bus.read32(t + T_LO);
        let sr = bus.read32(t + T_SR);
        cpu.sr = (sr & !0xF) | ((sr & 0x3F) >> 2); // rfe
        let epc = bus.read32(t + T_EPC);
        cpu.set_pc(epc);
    }

    // ------------------------------------------------------------ BIOS calls
    /// Some(v0) to return to the caller; None when the call already set the PC.
    fn call(&mut self, table: u8, f: u8, cpu: &mut Cpu, bus: &mut Bus) -> Option<u32> {
        let a = |i: usize, cpu: &Cpu| cpu.regs[4 + i];
        match (table, f) {
            (0xA0, 0x3C) | (0xB0, 0x3D) => {
                bus.tty.push(a(0, cpu) as u8);
                Some(a(0, cpu))
            }
            (0xA0, 0x3E) | (0xB0, 0x3F) => {
                let s = read_cstr(bus, a(0, cpu));
                bus.tty.extend_from_slice(&s);
                bus.tty.push(b'\n');
                Some(s.len() as u32)
            }
            (0xA0, 0x3F) => {
                let out = printf(cpu, bus);
                let n = out.len() as u32;
                bus.tty.extend_from_slice(&out);
                Some(n)
            }
            (0xB0, 0x17) => {
                self.return_from_exception(cpu, bus);
                None
            }
            (0xB0, 0x18) => {
                self.custom_exit = 0;
                Some(0)
            }
            (0xB0, 0x19) => {
                self.custom_exit = a(0, cpu);
                Some(0)
            }
            (0xC0, 0x0A) => {
                let t = (a(0, cpu) & 3) as usize;
                let old = self.clear_rcnt[t];
                self.clear_rcnt[t] = a(1, cpu) != 0;
                Some(old as u32)
            }
            (0xB0, 0x12) => {
                self.pad_buf = [(a(0, cpu), a(1, cpu)), (a(2, cpu), a(3, cpu))];
                Some(1)
            }
            (0xB0, 0x13) => {
                self.pad_started = true;
                bus.i_mask |= 1; // the pad driver runs on vblank
                Some(1)
            }
            (0xB0, 0x14) => {
                self.pad_started = false;
                Some(1)
            }
            (0xB0, 0x5B) => Some(0), // ChangeClearPad
            (0xA0, 0x72) => Some(0), // CdRemove
            (0xA0, 0x44) | (0xA0, 0x49) => Some(0), // FlushCache, GPU_cw
            (0xB0, 0x5C) => Some(0),
            _ => {
                if self.unknown.len() < 256 {
                    self.unknown.push((table, f));
                }
                Some(0)
            }
        }
    }
}

fn read_cstr(bus: &mut Bus, mut p: u32) -> Vec<u8> {
    let mut s = Vec::new();
    while s.len() < 4096 {
        let c = bus.read8(p);
        if c == 0 {
            break;
        }
        s.push(c);
        p = p.wrapping_add(1);
    }
    s
}

/// The BIOS printf: %d %i %u %x %X %o %c %s %p %%, with flags `-0+ #`, width,
/// precision and the `l`/`h` length modifiers (ignored: all arguments are 32-bit).
fn printf(cpu: &Cpu, bus: &mut Bus) -> Vec<u8> {
    let fmt = read_cstr(bus, cpu.regs[4]);
    let sp = cpu.regs[29];
    let mut argn = 1usize;
    let mut next_arg = |bus: &mut Bus| -> u32 {
        let v = if argn < 4 { cpu.regs[4 + argn] } else { bus.read32(sp + 4 * argn as u32) };
        argn += 1;
        v
    };
    let mut out = Vec::new();
    let mut i = 0;
    while i < fmt.len() {
        let c = fmt[i];
        i += 1;
        if c != b'%' {
            out.push(c);
            continue;
        }
        // the BIOS accepts flags and width in any order ("%2-d" left-justifies)
        let (mut left, mut zero, mut plus, mut space, mut alt) = (false, false, false, false, false);
        let mut width = 0usize;
        while i < fmt.len() {
            match fmt[i] {
                b'-' => left = true,
                b'0' if width == 0 => zero = true,
                b'+' => plus = true,
                b' ' => space = true,
                b'#' => alt = true,
                b'*' => width = next_arg(bus) as usize,
                d if d.is_ascii_digit() => width = width * 10 + (d - b'0') as usize,
                _ => break,
            }
            i += 1;
        }
        let mut prec: Option<usize> = None;
        if i < fmt.len() && fmt[i] == b'.' {
            i += 1;
            let mut p = 0;
            while i < fmt.len() && fmt[i].is_ascii_digit() {
                p = p * 10 + (fmt[i] - b'0') as usize;
                i += 1;
            }
            prec = Some(p);
        }
        while i < fmt.len() && matches!(fmt[i], b'l' | b'h') {
            i += 1;
        }
        if i >= fmt.len() {
            break;
        }
        let conv = fmt[i];
        i += 1;
        let mut body: Vec<u8>;
        let mut sign = Vec::new();
        let numeric = matches!(conv, b'd' | b'i' | b'u' | b'x' | b'X' | b'o' | b'p');
        match conv {
            b'%' => {
                out.push(b'%');
                continue;
            }
            b'c' => body = vec![next_arg(bus) as u8],
            b's' => {
                let p = next_arg(bus);
                body = read_cstr(bus, p);
                if let Some(p) = prec {
                    body.truncate(p);
                }
            }
            b'd' | b'i' => {
                let v = next_arg(bus) as i32;
                if v < 0 {
                    sign = b"-".to_vec();
                } else if plus {
                    sign = b"+".to_vec();
                } else if space {
                    sign = b" ".to_vec();
                }
                body = (v as i64).unsigned_abs().to_string().into_bytes();
            }
            b'u' => body = next_arg(bus).to_string().into_bytes(),
            b'x' | b'p' => {
                let v = next_arg(bus);
                body = format!("{v:x}").into_bytes();
                if alt && v != 0 {
                    sign = b"0x".to_vec();
                }
            }
            b'X' => {
                let v = next_arg(bus);
                body = format!("{v:X}").into_bytes();
                if alt && v != 0 {
                    sign = b"0X".to_vec();
                }
            }
            b'o' => body = format!("{:o}", next_arg(bus)).into_bytes(),
            _ => {
                out.push(b'%');
                out.push(conv);
                continue;
            }
        }
        if numeric {
            if let Some(p) = prec {
                while body.len() < p {
                    body.insert(0, b'0');
                }
            }
        }
        let len = sign.len() + body.len();
        let pad = width.saturating_sub(len);
        if left {
            out.extend_from_slice(&sign);
            out.extend_from_slice(&body);
            out.extend(std::iter::repeat_n(b' ', pad));
        } else if zero && numeric && prec.is_none() {
            out.extend_from_slice(&sign);
            out.extend(std::iter::repeat_n(b'0', pad));
            out.extend_from_slice(&body);
        } else {
            out.extend(std::iter::repeat_n(b' ', pad));
            out.extend_from_slice(&sign);
            out.extend_from_slice(&body);
        }
    }
    out
}

impl Default for Hle {
    fn default() -> Self {
        Self::new()
    }
}
