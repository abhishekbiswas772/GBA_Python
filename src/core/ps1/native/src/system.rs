//! The whole console: CPU + bus, the frame loop, EXE loading and BIOS hooks.

use crate::bus::{irq, Bus};
use crate::cpu::Cpu;
use crate::disc::Disc;
use crate::hle::Hle;
use std::sync::Arc;

pub const CPU_HZ: u32 = 33_868_800;
pub const SAMPLE_RATE: u32 = 44_100;
const BATCH: u32 = 64; // cycles between device updates
const SHELL_ENTRY: u32 = 0x8003_0000;
/// Bumped whenever the state layout changes (a state from another layout is refused).
const STATE_MAGIC: &[u8] = b"PS1STATE\x03";

#[derive(serde::Serialize, serde::Deserialize)]
pub struct Exe {
    pub pc: u32,
    pub gp: u32,
    pub addr: u32,
    pub data: Vec<u8>,
    pub sp: u32,
    pub bss: (u32, u32),
}

impl Exe {
    pub fn parse(b: &[u8]) -> Result<Exe, String> {
        if b.len() < 0x800 || &b[..8] != b"PS-X EXE" {
            return Err("not a PS-X EXE (missing 'PS-X EXE' header)".into());
        }
        let w = |o: usize| u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]]);
        let size = w(0x1C) as usize;
        let end = (0x800 + size).min(b.len());
        let sp_base = w(0x30);
        Ok(Exe {
            pc: w(0x10),
            gp: w(0x14),
            addr: w(0x18),
            data: b[0x800..end].to_vec(),
            sp: if sp_base != 0 { sp_base.wrapping_add(w(0x34)) } else { 0x801F_FF00 },
            bss: (w(0x28), w(0x2C)),
        })
    }
}

#[derive(serde::Serialize, serde::Deserialize)]
pub struct System {
    pub cpu: Cpu,
    pub bus: Bus,
    pub hle: Option<Hle>,
    exe: Option<Exe>,
    video_frac: u32,
    #[serde(skip)]
    pub sampling: bool,
    #[serde(skip)]
    pub samples: Vec<i16>,
    pub cycles: u64,
}

impl System {
    /// `bios`: a real BIOS image, or None for the built-in HLE kernel (EXEs only).
    pub fn new(bios: Option<Vec<u8>>, exe: Option<Vec<u8>>) -> Result<System, String> {
        Self::with_disc(bios, exe, None)
    }

    /// Boot with a disc in the drive. With a BIOS the BIOS boots it; without one the
    /// HLE kernel loads the program named in the disc's SYSTEM.CNF (enough for homebrew
    /// and test discs; commercial games need the BIOS).
    pub fn with_disc(bios: Option<Vec<u8>>, exe: Option<Vec<u8>>, disc: Option<Arc<Disc>>) -> Result<System, String> {
        let mut exe = match exe {
            Some(b) => Some(Exe::parse(&b)?),
            None => None,
        };
        let have_bios = bios.is_some();
        if !have_bios && exe.is_none() {
            let Some(d) = disc.as_ref() else {
                return Err("a PS1 BIOS is needed to boot discs; without one only PS-X EXE files run".into());
            };
            exe = Some(boot_exe_from_disc(d)?);
        }
        let mut s = System {
            cpu: Cpu::new(),
            bus: Bus::new(bios.unwrap_or_default()),
            hle: None,
            exe,
            video_frac: 0,
            sampling: false,
            samples: Vec::new(),
            cycles: 0,
        };
        if let Some(d) = disc {
            s.bus.cdrom.insert(d);
        }
        if !have_bios {
            s.boot_hle();
        }
        Ok(s)
    }

    fn boot_hle(&mut self) {
        Hle::install(&mut self.bus);
        self.hle = Some(Hle::new());
        self.cpu.hle_traps = true;
        self.bus.i_mask = 0;
        self.bus.gpu.gp1(0);
        self.load_exe();
        // the BIOS starts programs with COP2 usable and interrupts enabled
        self.cpu.sr = 0x4000_0401;
    }

    fn load_exe(&mut self) {
        let Some(exe) = self.exe.take() else { return };
        let base = (exe.addr & 0x1F_FFFF) as usize;
        let end = (base + exe.data.len()).min(self.bus.ram.len());
        self.bus.ram[base..end].copy_from_slice(&exe.data[..end - base]);
        let (bss, bss_len) = exe.bss;
        if bss_len != 0 {
            let b = (bss & 0x1F_FFFF) as usize;
            let e = (b + bss_len as usize).min(self.bus.ram.len());
            self.bus.ram[b..e].fill(0);
        }
        self.cpu.write_reg(28, exe.gp);
        self.cpu.write_reg(29, exe.sp);
        self.cpu.write_reg(30, exe.sp);
        self.cpu.set_pc(exe.pc);
    }

    /// The whole machine state (minus the BIOS image), versioned.
    pub fn save_state(&self) -> Vec<u8> {
        let mut out = STATE_MAGIC.to_vec();
        out.extend(bincode::serialize(self).expect("state serializes"));
        out
    }

    /// Replace this machine with `data` from [`save_state`]; keeps the BIOS, the
    /// sampling switch and drops buffered audio.
    pub fn load_state(&mut self, data: &[u8]) -> Result<(), String> {
        let body = data.strip_prefix(STATE_MAGIC).ok_or("not a PS1 state (or from another core version)")?;
        let mut new: System = bincode::deserialize(body).map_err(|e| format!("bad PS1 state: {e}"))?;
        new.bus.bios = std::mem::take(&mut self.bus.bios);
        new.bus.cdrom.attach(self.bus.cdrom.disc().cloned());
        new.sampling = self.sampling;
        *self = new;
        Ok(())
    }

    pub fn fps(&self) -> f64 {
        let g = &self.bus.gpu;
        let (per_line, lines) = if g.pal() { (3406.0, 314.0) } else { (3413.0, 263.0) };
        CPU_HZ as f64 * 11.0 / 7.0 / (per_line * lines)
    }

    /// Run until the next vblank starts (one video frame).
    pub fn run_frame(&mut self) {
        self.bus.gpu.frame_done = false;
        let mut guard: u64 = 0;
        while !self.bus.gpu.frame_done {
            let mut used = 0;
            while used < BATCH {
                let c = self.cpu.step(&mut self.bus);
                used += c;
                self.bus.timer_lag += c;
                if let Some(id) = self.cpu.hle_hit.take() {
                    if let Some(h) = self.hle.as_mut() {
                        h.dispatch(id, &mut self.cpu, &mut self.bus);
                    }
                }
                self.bios_hooks();
            }
            self.advance(used);
            guard += used as u64;
            if guard > CPU_HZ as u64 {
                break; // display never reached vblank (GPU timing broken): don't hang
            }
        }
    }

    #[inline]
    fn bios_hooks(&mut self) {
        if self.hle.is_some() {
            return;
        }
        let pc = self.cpu.pc;
        match pc & 0x1FFF_FFFF {
            0xA0 if self.cpu.regs[9] == 0x3C => self.bus.tty.push(self.cpu.regs[4] as u8),
            0xB0 if self.cpu.regs[9] == 0x3D => self.bus.tty.push(self.cpu.regs[4] as u8),
            _ => {}
        }
        if pc == SHELL_ENTRY && self.exe.is_some() {
            self.load_exe();
        }
    }

    fn advance(&mut self, cycles: u32) {
        self.cycles += cycles as u64;
        let total = self.video_frac + cycles * 11;
        let video = total / 7;
        self.video_frac = total % 7;
        let bus = &mut self.bus;
        let div = bus.gpu.dot_divider();
        let (hbl, vb_on, vb_off) = bus.gpu.tick(video);
        let _ = div;
        bus.sync_timers();
        for _ in 0..hbl {
            bus.timers.hblank(true);
            bus.timers.hblank(false);
        }
        if vb_on {
            bus.raise(irq::VBLANK);
            bus.timers.vblank(true);
        }
        if vb_off {
            bus.timers.vblank(false);
        }
        let t = std::mem::take(&mut bus.timers.irq);
        bus.i_stat |= t;
        bus.sio.tick(cycles);
        if std::mem::take(&mut bus.sio.irq) {
            bus.raise(irq::PAD);
        }
        bus.cdrom.tick(cycles);
        if std::mem::take(&mut bus.cdrom.irq) {
            bus.raise(irq::CDROM);
        }
        bus.collect_gpu_irq();
        bus.tick_dma(cycles);
        if !bus.cdrom.audio.is_empty() {
            let a = std::mem::take(&mut bus.cdrom.audio);
            bus.spu.cd_audio.extend(a);
        }
        bus.spu.tick(cycles, if self.sampling { Some(&mut self.samples) } else { None });
        if std::mem::take(&mut bus.spu.irq) {
            bus.raise(irq::SPU);
        }
    }
}

/// The boot program of a disc: SYSTEM.CNF's `BOOT = cdrom:\NAME;1` (or PSX.EXE), with its STACK.
pub fn boot_exe_from_disc(d: &Disc) -> Result<Exe, String> {
    let (path, stack) = match d.read_file("SYSTEM.CNF") {
        Some(cnf) => {
            let text = String::from_utf8_lossy(&cnf).to_string();
            let mut boot = None;
            let mut stack = None;
            for line in text.lines() {
                let Some((k, v)) = line.split_once('=') else { continue };
                let (k, v) = (k.trim().to_ascii_uppercase(), v.trim());
                if k == "BOOT" {
                    let v = v.split_whitespace().next().unwrap_or("");
                    boot = Some(v.trim_start_matches("cdrom:").trim_start_matches("CDROM:").to_string());
                } else if k == "STACK" {
                    stack = u32::from_str_radix(v.trim_start_matches("0x"), 16).ok();
                }
            }
            (boot.ok_or("SYSTEM.CNF has no BOOT line")?, stack)
        }
        None => ("PSX.EXE;1".to_string(), None),
    };
    let data = d.read_file(&path).ok_or(format!("the disc's boot file {path} isn't on the disc"))?;
    let mut exe = Exe::parse(&data)?;
    if let Some(sp) = stack {
        exe.sp = sp;
    }
    Ok(exe)
}
