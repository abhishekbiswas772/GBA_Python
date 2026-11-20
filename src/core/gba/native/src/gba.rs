//! The whole machine. Python builds it once (the "linker" step: ROM, BIOS, save
//! data, RTC clock); after that every component talks to the others through
//! this struct, and Python calls in once per frame.
//!
//! Every behaviour here mirrors the Python core in `src/core/gba/` exactly,
//! cycle for cycle, so the two can be run in lockstep and compared.

use serde::{Deserialize, Serialize};

use crate::apu::Apu;
use crate::cart::{self, Save};
use crate::cpu::Cpu;
use crate::dma::Dma;
use crate::ppu::Ppu;
use crate::rtc::Rtc;
use crate::timers::Timers;

pub const CPU_HZ: u64 = 16_777_216;
pub const BIOS_SIZE: usize = 0x4000;
pub const EWRAM_SIZE: usize = 0x40000;
pub const IWRAM_SIZE: usize = 0x8000;
pub const IO_SIZE: usize = 0x400;
pub const PAL_SIZE: usize = 0x400;
pub const VRAM_SIZE: usize = 0x18000;
pub const OAM_SIZE: usize = 0x400;
pub const ROM_MAX: usize = 0x2000000;

pub const BIOS_LATCH_STARTUP: u32 = 0xE129F000;
pub const BIOS_LATCH_SWI: u32 = 0xE3A02004;
const BIOS_WORDS: [(usize, u32); 10] = [
    (0x018, 0xEA000042),
    (0x0E4, BIOS_LATCH_STARTUP),
    (0x128, 0xE92D500F),
    (0x12C, 0xE3A00301),
    (0x130, 0xE28FE000),
    (0x134, 0xE510F004),
    (0x138, 0xE8BD500F),
    (0x13C, 0xE25EF004),
    (0x144, 0xE55EC002),
    (0x190, BIOS_LATCH_SWI),
];
pub const SP_USR: u32 = 0x03007F00;
pub const SP_IRQ: u32 = 0x03007FA0;
pub const SP_SVC: u32 = 0x03007FE0;
pub const ROM_START: u32 = 0x08000000;

pub const REG_SOUNDBIAS: usize = 0x088;
pub const REG_POSTFLG: usize = 0x300;
pub const REG_BG2PA: usize = 0x020;
pub const REG_BG3PA: usize = 0x030;

pub const INF: u64 = 1 << 62;

#[inline(always)]
pub fn rd16(b: &[u8], o: usize) -> u32 {
    u16::from_le_bytes([b[o], b[o + 1]]) as u32
}

#[inline(always)]
pub fn rd32(b: &[u8], o: usize) -> u32 {
    u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]])
}

#[inline(always)]
pub fn wr16(b: &mut [u8], o: usize, v: u32) {
    b[o..o + 2].copy_from_slice(&(v as u16).to_le_bytes());
}

#[inline(always)]
pub fn wr32(b: &mut [u8], o: usize, v: u32) {
    b[o..o + 4].copy_from_slice(&v.to_le_bytes());
}

pub fn hle_bios_image() -> Vec<u8> {
    let mut bios = vec![0u8; BIOS_SIZE];
    for (addr, word) in BIOS_WORDS {
        wr32(&mut bios, addr, word);
    }
    bios
}

/// ROM padded to the full 32 MB window with open-bus data ((addr >> 1) & 0xFFFF).
pub fn pad_rom(rom: &[u8]) -> Vec<u8> {
    let size = (rom.len() + 3) & !3;
    let mut out = Vec::with_capacity(ROM_MAX.max(size));
    out.extend_from_slice(rom);
    out.resize(size, 0);
    let mut h = size / 2;
    while out.len() < ROM_MAX {
        out.extend_from_slice(&((h & 0xFFFF) as u16).to_le_bytes());
        h += 1;
    }
    out
}

#[derive(Serialize, Deserialize)]
pub struct Gba {
    pub cpu: Cpu,
    #[serde(skip)]
    pub bios: Vec<u8>,
    pub ewram: Vec<u8>,
    pub iwram: Vec<u8>,
    pub io: Vec<u8>,
    pub pal: Vec<u8>,
    pub vram: Vec<u8>,
    pub oam: Vec<u8>,
    #[serde(skip)]
    pub rom: Vec<u8>,
    pub rom_size: usize,
    pub ie: u32,
    pub if_: u32,
    pub ime: u32,
    pub keys: u32,
    pub wait16: Vec<u32>,
    pub wait32: Vec<u32>,
    pub code16: Vec<u32>,
    pub code32: Vec<u32>,
    pub timers: Timers,
    pub dma: Dma,
    pub ppu: Ppu,
    pub apu: Apu,
    pub save: Save,
    pub rtc: Option<Rtc>,
    pub eeprom_base: u32,
}

impl Gba {
    /// `bios`: a real BIOS image, or None for the HLE BIOS. `save_data`: the
    /// cartridge's .sav contents, if any.
    pub fn new(rom: &[u8], bios: Option<Vec<u8>>, save_data: Option<&[u8]>) -> Result<Gba, String> {
        if rom.len() < 0xC0 {
            return Err(format!("File is too small ({} bytes) - not a GBA ROM", rom.len()));
        }
        let hle = bios.is_none();
        let kind = cart::detect_save(rom);
        let mut save = Save::make(kind);
        if let Some(d) = save_data {
            save.load(d);
        }
        let rtc = if cart::contains(rom, b"SIIRTC_V") { Some(Rtc::new()) } else { None };
        let mut g = Gba {
            cpu: Cpu::new(),
            bios: bios.unwrap_or_else(hle_bios_image),
            ewram: vec![0; EWRAM_SIZE],
            iwram: vec![0; IWRAM_SIZE],
            io: vec![0; IO_SIZE],
            pal: vec![0; PAL_SIZE],
            vram: vec![0; VRAM_SIZE],
            oam: vec![0; OAM_SIZE],
            rom: pad_rom(rom),
            rom_size: rom.len(),
            ie: 0,
            if_: 0,
            ime: 0,
            keys: 0x3FF,
            wait16: vec![0; 256],
            wait32: vec![0; 256],
            code16: vec![1; 256],
            code32: vec![1; 256],
            timers: Timers::new(),
            dma: Dma::new(),
            ppu: Ppu::new(),
            apu: Apu::new(),
            save,
            rtc,
            eeprom_base: if rom.len() <= 0x1000000 { 0x0D000000 } else { 0x0DFFFF00 },
        };
        g.bios.resize(BIOS_SIZE, 0);
        g.set_waitcnt(0);
        if hle {
            g.cpu.hle = true;
            g.reset_skip_bios(ROM_START, SP_USR, SP_IRQ, SP_SVC);
            g.cpu.bios_latch = BIOS_LATCH_STARTUP;
            g.io[REG_POSTFLG] = 1;
            wr16(&mut g.io, REG_SOUNDBIAS, 0x200);
            for pa in [REG_BG2PA, REG_BG3PA] {
                wr16(&mut g.io, pa, 0x100);
                wr16(&mut g.io, pa + 6, 0x100);
            }
        } else {
            g.cpu.pc = 0;
        }
        Ok(g)
    }

    /// Put back what a save state leaves out (ROM, BIOS).
    pub fn adopt_images(&mut self, rom: &[u8], bios: Vec<u8>) {
        self.rom = pad_rom(rom);
        self.bios = bios;
        self.bios.resize(BIOS_SIZE, 0);
    }

    // ------------------------------------------------------------ running
    pub fn run_until(&mut self, t_end: u64) {
        while self.cpu.now < t_end {
            let mut nxt = self.ppu.next_event.min(self.timers.next_event);
            if t_end < nxt {
                nxt = t_end;
            }
            self.run(nxt);
            self.after_run();
        }
    }

    #[inline]
    fn after_run(&mut self) {
        let now = self.cpu.now;
        if self.timers.next_event <= now {
            self.timers_update(now);
        }
        while self.ppu.next_event <= now {
            self.ppu_event();
        }
    }

    pub fn run_frame(&mut self) {
        let target = self.ppu.frame + 1;
        while self.ppu.frame < target {
            let nxt = self.ppu.next_event.min(self.timers.next_event);
            self.run(nxt);
            self.after_run();
        }
        self.apu_sync();
    }

    /// One instruction (or one halted cycle), with the same event handling as
    /// `run_frame`: for lockstep comparison against the Python core.
    pub fn step_instr(&mut self) {
        let nxt = self.ppu.next_event.min(self.timers.next_event).min(self.cpu.now + 1);
        self.run(nxt);
        self.after_run();
    }

    pub fn set_key(&mut self, bit: u32, down: bool) {
        if down {
            self.keys &= !(1 << bit);
        } else {
            self.keys |= 1 << bit;
        }
    }
}
