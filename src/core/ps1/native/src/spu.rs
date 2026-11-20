//! SPU: 24 ADPCM voices with ADSR envelopes, volume sweeps, pitch modulation,
//! noise, Gaussian interpolation, reverb, CD audio input, capture buffers and the
//! sound RAM IRQ, mixed to 44.1 kHz stereo.
//!
//! The arithmetic (envelope steps, ADPCM filters, interpolation rounding, the reverb
//! and its 39-tap resampler) follows psx-spx and DuckStation's hardware-tested SPU.
//! Sound RAM transfers land at once; the DMA channel's duration is modelled on the
//! bus side.

use std::collections::VecDeque;

use crate::spu_gauss::GAUSS;

const RAM_SIZE: usize = 512 * 1024;
const RAM_MASK: u32 = (RAM_SIZE - 1) as u32;
pub const CYCLES_PER_SAMPLE: u32 = 768;
const CAPTURE_SIZE: u32 = 0x400;

// register indices (halfword offset from 0x1F801C00)
const MAIN_VOL_L: usize = 0x180 / 2;
const MAIN_VOL_R: usize = 0x182 / 2;
const REV_VOL_L: usize = 0x184 / 2;
const REV_VOL_R: usize = 0x186 / 2;
const KON: usize = 0x188 / 2;
const KOFF: usize = 0x18C / 2;
const PMON: usize = 0x190 / 2;
const NON: usize = 0x194 / 2;
const EON: usize = 0x198 / 2;
const ENDX: usize = 0x19C / 2;
const MBASE: usize = 0x1A2 / 2;
const IRQ_ADDR: usize = 0x1A4 / 2;
const TADDR: usize = 0x1A6 / 2;
const FIFO: usize = 0x1A8 / 2;
const CTRL: usize = 0x1AA / 2;
const STAT: usize = 0x1AE / 2;
const CD_VOL_L: usize = 0x1B0 / 2;
const CD_VOL_R: usize = 0x1B2 / 2;
const CUR_MAIN_L: usize = 0x1B8 / 2;
const CUR_MAIN_R: usize = 0x1BA / 2;
const REVERB: usize = 0x1C0 / 2; // 32 registers
const CUR_VOICE_VOL: usize = 0x200 / 2; // 24 x (left, right)

// reverb register numbers inside the 32-entry block
const FB_SRC_A: usize = 0;
const FB_SRC_B: usize = 1;
const IIR_ALPHA: usize = 2;
const ACC_COEF_A: usize = 3;
const IIR_COEF: usize = 7;
const FB_ALPHA: usize = 8;
const FB_X: usize = 9;
const IIR_DEST_A: usize = 10;
const ACC_SRC_A: usize = 12;
const ACC_SRC_B: usize = 14;
const IIR_SRC_A: usize = 16;
const IIR_DEST_B: usize = 18;
const ACC_SRC_C: usize = 20;
const ACC_SRC_D: usize = 22;
const IIR_SRC_B: usize = 24;
const MIX_DEST_A: usize = 26;
const MIX_DEST_B: usize = 28;
const IN_COEF: usize = 30;

/// Non-zero taps of the reverb's 39-tap half-band resampler (the odd taps are zero, the middle is 0x4000).
const RESAMPLE: [i32; 20] = [
    -0x0001, 0x0002, -0x000A, 0x0023, -0x0067, 0x010A, -0x0268, 0x0534, -0x0B90, 0x2806, 0x2806, -0x0B90, 0x0534,
    -0x0268, 0x010A, -0x0067, 0x0023, -0x000A, 0x0002, -0x0001,
];

fn clamp16(v: i32) -> i32 {
    v.clamp(-0x8000, 0x7FFF)
}

fn apply_volume(sample: i32, vol: i16) -> i32 {
    (sample * vol as i32) >> 15
}

// ------------------------------------------------------------------ envelopes

/// The shared rate/step engine behind ADSR and volume sweeps.
#[derive(Clone, Copy, Default, serde::Serialize, serde::Deserialize)]
struct Envelope {
    counter: u32,
    increment: u16,
    step: i16,
    rate: u8,
    decreasing: bool,
    exponential: bool,
    phase_invert: bool,
}

impl Envelope {
    fn reset(&mut self, rate: u8, mask: u8, decreasing: bool, exponential: bool, phase_invert: bool) {
        self.rate = rate;
        self.decreasing = decreasing;
        self.exponential = exponential;
        self.phase_invert = phase_invert;
        self.counter = 0;
        self.increment = 0x8000;
        let base: i16 = 7 - (rate & 3) as i16;
        let mut step = if (decreasing ^ phase_invert) | (decreasing & exponential) { !base } else { base };
        if rate < 44 {
            step <<= 11 - (rate >> 2);
        } else if rate >= 48 {
            self.increment = (0x8000u32 >> ((rate >> 2) - 11)) as u16;
            if rate & mask != mask {
                self.increment = self.increment.max(1);
            }
        }
        self.step = step;
    }

    /// One sample's tick; false once the envelope stops moving.
    fn tick(&mut self, level: &mut i16) -> bool {
        let mut inc = self.increment as u32;
        let mut step = self.step as i32;
        let cur = *level as i32;
        if self.exponential {
            if self.decreasing {
                step = (step * cur) >> 15;
            } else if cur >= 0x6000 {
                if self.rate < 40 {
                    step >>= 2;
                } else if self.rate >= 44 {
                    inc >>= 2;
                } else {
                    step >>= 1;
                    inc >>= 1;
                }
            }
        }
        self.counter += inc;
        if self.counter & 0x8000 == 0 {
            return true;
        }
        self.counter = 0;
        let new = cur + step;
        if !self.decreasing {
            *level = new.clamp(-32768, 32767) as i16;
            *level as i32 != if step < 0 { -32768 } else { 32767 }
        } else if self.phase_invert && (!self.exponential || cur < 0) {
            *level = new.clamp(-32768, 0) as i16;
            *level < 0 && step != 0
        } else {
            *level = new.max(0) as i16;
            *level > 0
        }
    }
}

/// A volume register: fixed, or sweeping.
#[derive(Clone, Copy, Default, serde::Serialize, serde::Deserialize)]
struct Sweep {
    env: Envelope,
    level: i16,
    active: bool,
}

impl Sweep {
    fn reset(&mut self, reg: u16) {
        if reg & 0x8000 == 0 {
            self.level = ((((reg as i32) << 17) >> 17) * 2) as i16; // 15-bit signed, halved in the register
            self.active = false;
            return;
        }
        self.env.reset((reg & 0x7F) as u8, 0x7F, reg & (1 << 13) != 0, reg & (1 << 14) != 0, reg & (1 << 12) != 0);
        self.active = self.env.increment > 0;
    }

    fn tick(&mut self) {
        if self.active {
            self.active = self.env.tick(&mut self.level);
        }
    }
}

// ------------------------------------------------------------------ voices

#[derive(Clone, Copy, PartialEq, Eq, Default, serde::Serialize, serde::Deserialize)]
enum Phase {
    #[default]
    Off,
    Attack,
    Decay,
    Sustain,
    Release,
}

#[derive(Clone, Default, serde::Serialize, serde::Deserialize)]
struct Voice {
    /// vol L, vol R, pitch, start, ADSR lo, ADSR hi, ADSR volume, repeat address
    regs: [u16; 8],
    left: Sweep,
    right: Sweep,
    phase: Phase,
    adsr: Envelope,
    adsr_target: i16,
    /// bits 4..11: interpolation index, bits 12..16: sample index in the block
    counter: u32,
    address: u16,
    has_samples: bool,
    ignore_loop: bool,
    key_on_delay: u8,
    flags: u8,
    /// 3 samples of the previous block, then the 28 of this one
    samples: [i16; 31],
    last: [i16; 2],
}

impl Voice {
    fn on(&self) -> bool {
        self.phase != Phase::Off
    }

    fn adsr_bits(&self) -> u32 {
        self.regs[4] as u32 | ((self.regs[5] as u32) << 16)
    }

    fn update_adsr(&mut self) {
        let a = self.adsr_bits();
        match self.phase {
            Phase::Off => {
                self.adsr_target = 0;
                self.adsr.reset(0, 0, false, false, false);
            }
            Phase::Attack => {
                self.adsr_target = 32767;
                self.adsr.reset(((a >> 8) & 0x7F) as u8, 0x7F, false, a & (1 << 15) != 0, false);
            }
            Phase::Decay => {
                self.adsr_target = (((a & 0xF) + 1) * 0x800).min(32767) as i16;
                self.adsr.reset((((a >> 4) & 0xF) << 2) as u8, 0x1F << 2, true, true, false);
            }
            Phase::Sustain => {
                self.adsr_target = 0;
                self.adsr.reset(((a >> 22) & 0x7F) as u8, 0x7F, a & (1 << 30) != 0, a & (1 << 31) != 0, false);
            }
            Phase::Release => {
                self.adsr_target = 0;
                self.adsr.reset((((a >> 16) & 0x1F) << 2) as u8, 0x1F << 2, true, a & (1 << 21) != 0, false);
            }
        }
    }

    fn tick_adsr(&mut self) {
        if self.adsr.increment > 0 {
            let mut v = self.regs[6] as i16;
            self.adsr.tick(&mut v);
            self.regs[6] = v as u16;
        }
        if self.phase != Phase::Sustain {
            let v = self.regs[6] as i16;
            let reached = if self.phase == Phase::Decay {
                v < self.adsr_target
            } else if self.adsr.decreasing {
                v <= self.adsr_target
            } else {
                v >= self.adsr_target
            };
            if reached {
                self.phase = match self.phase {
                    Phase::Attack => Phase::Decay,
                    Phase::Decay | Phase::Sustain => Phase::Sustain,
                    _ => Phase::Off,
                };
                self.update_adsr();
            }
        }
    }

    fn key_on(&mut self) {
        self.address = self.regs[3] & !1;
        self.counter = 3 << 12;
        self.regs[6] = 0;
        self.last = [0, 0];
        self.samples[28..31].fill(0);
        self.has_samples = false;
        self.ignore_loop = false;
        self.key_on_delay = 4;
        self.phase = Phase::Attack;
        self.update_adsr();
    }

    fn key_off(&mut self) {
        if matches!(self.phase, Phase::Off | Phase::Release) {
            return;
        }
        self.phase = Phase::Release;
        self.update_adsr();
    }

    fn force_off(&mut self) {
        self.key_on_delay = 0;
        if self.phase == Phase::Off {
            return;
        }
        self.regs[6] = 0;
        self.phase = Phase::Off;
    }

    /// Decode one 16-byte ADPCM block into 28 samples.
    fn decode(&mut self, block: &[u8; 16]) {
        const POS: [i32; 16] = [0, 60, 115, 98, 122, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
        const NEG: [i32; 16] = [0, 0, -52, -55, -60, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
        self.samples[0] = self.samples[28];
        self.samples[1] = self.samples[29];
        self.samples[2] = self.samples[30];
        let shift = match block[0] & 0xF {
            s if s > 12 => 9,
            s => s,
        };
        let f = (block[0] >> 4) as usize;
        let (fp, fn_) = (POS[f], NEG[f]);
        let (mut s0, mut s1) = (self.last[0] as i32, self.last[1] as i32); // newest, older
        for i in 0..28 {
            let nib = (block[2 + i / 2] >> ((i & 1) * 4)) & 0xF;
            let raw = (((nib as u16) << 12) as i16 >> shift) as i32;
            let s = clamp16(raw + ((s0 * fp) >> 6) + ((s1 * fn_) >> 6));
            self.samples[3 + i] = s as i16;
            s1 = s0;
            s0 = s;
        }
        self.last = [s0 as i16, s1 as i16];
        self.flags = block[1];
    }

    fn interpolate(&self) -> i32 {
        let i = ((self.counter >> 4) & 0xFF) as usize;
        let s = 3 + ((self.counter >> 12) & 0x1F) as usize;
        let w = [GAUSS[0xFF - i], GAUSS[0x1FF - i], GAUSS[0x100 + i], GAUSS[i]];
        let mut sum = 0i32;
        for k in 0..4 {
            sum += (self.samples[s - 3 + k] as i32 * w[k] as i32) >> 14;
        }
        sum >> 1
    }
}

// ------------------------------------------------------------------ the SPU

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct Spu {
    pub regs: Vec<u16>,
    pub ram: Vec<u8>,
    addr: u32,
    voices: Vec<Voice>,
    key_on: u32,
    key_off: u32,
    endx: u32,
    main_l: Sweep,
    main_r: Sweep,
    noise_level: u32,
    noise_count: u32,
    capture_pos: u32,
    reverb_base: u32,
    reverb_cur: u32,
    reverb_pos: u32,
    reverb_down: Vec<Vec<i16>>, // 2 x 128 (duplicated so the 39-tap window never wraps)
    reverb_up: Vec<Vec<i16>>,   // 2 x 64
    irq_flag: bool,
    cycles: u32,
    /// CD-DA / XA audio at 44.1 kHz from the drive (left, right)
    #[serde(default)]
    pub cd_audio: VecDeque<(i16, i16)>,
    /// SPUSTAT bit 10: a DMA transfer is still running (set by the bus)
    #[serde(default)]
    pub transfer_busy: bool,
    /// edge: the SPU raised its interrupt
    #[serde(skip)]
    pub irq: bool,
}

impl Spu {
    pub fn new() -> Self {
        Spu {
            regs: vec![0; 0x200],
            ram: vec![0; RAM_SIZE],
            addr: 0,
            voices: vec![Voice::default(); 24],
            key_on: 0,
            key_off: 0,
            endx: 0,
            main_l: Sweep::default(),
            main_r: Sweep::default(),
            noise_level: 1,
            noise_count: 0,
            capture_pos: 0,
            reverb_base: 0,
            reverb_cur: 0,
            reverb_pos: 0,
            reverb_down: vec![vec![0; 128]; 2],
            reverb_up: vec![vec![0; 64]; 2],
            irq_flag: false,
            cycles: 0,
            cd_audio: VecDeque::new(),
            transfer_busy: false,
            irq: false,
        }
    }

    fn idx(p: u32) -> usize {
        ((p - 0x1F80_1C00) as usize / 2) & 0x1FF
    }

    fn ctrl(&self) -> u16 {
        self.regs[CTRL]
    }

    // ---------------------------------------------------------- IRQ

    fn irq_armed(&self) -> bool {
        self.ctrl() & (1 << 6) != 0 && !self.irq_flag
    }

    fn check_irq(&mut self, byte_addr: u32) {
        if self.irq_armed() && (self.regs[IRQ_ADDR] as u32) * 8 == byte_addr {
            self.irq_flag = true;
            self.irq = true;
        }
    }

    // ---------------------------------------------------------- registers

    pub fn read16(&mut self, p: u32) -> u16 {
        let i = Self::idx(p);
        match i {
            0..=0xBF => {
                let (v, r) = (i / 8, i % 8);
                self.voices[v].regs[r]
            }
            KON => self.key_on as u16,
            0xC5 => (self.key_on >> 16) as u16,
            KOFF => self.key_off as u16,
            0xC7 => (self.key_off >> 16) as u16,
            ENDX => self.endx as u16,
            0xCF => (self.endx >> 16) as u16,
            FIFO => 0xFFFF,
            STAT => {
                let mode = self.ctrl() & 0x3F;
                let transfer = (self.ctrl() >> 4) & 3;
                let mut s = mode;
                if self.irq_flag {
                    s |= 1 << 6;
                }
                match transfer {
                    2 => s |= (1 << 7) | (1 << 9), // DMA write request
                    3 => s |= (1 << 7) | (1 << 8), // DMA read request
                    _ => {}
                }
                if self.transfer_busy {
                    s |= 1 << 10;
                }
                if self.capture_pos >= CAPTURE_SIZE / 2 {
                    s |= 1 << 11;
                }
                s
            }
            CUR_MAIN_L => self.main_l.level as u16,
            CUR_MAIN_R => self.main_r.level as u16,
            0x100..=0x12F => {
                let v = &self.voices[(i - CUR_VOICE_VOL) / 2];
                (if i & 1 == 0 { v.left.level } else { v.right.level }) as u16
            }
            _ => self.regs[i],
        }
    }

    pub fn write16(&mut self, p: u32, v: u16) {
        let i = Self::idx(p);
        match i {
            0..=0xBF => {
                let (n, r) = (i / 8, i % 8);
                let voice = &mut self.voices[n];
                voice.regs[r] = v;
                match r {
                    0 => voice.left.reset(v),
                    1 => voice.right.reset(v),
                    4 | 5 if voice.on() => voice.update_adsr(),
                    7 => voice.ignore_loop = true,
                    _ => {}
                }
            }
            MAIN_VOL_L => {
                self.regs[i] = v;
                self.main_l.reset(v);
            }
            MAIN_VOL_R => {
                self.regs[i] = v;
                self.main_r.reset(v);
            }
            // Key on/off are latched every second sample. Pending writes accumulate so two
            // quick writes (one voice each) both start: with the HLE kernel a printf between
            // them takes far fewer cycles than on a console.
            KON => self.key_on |= v as u32,
            0xC5 => self.key_on |= (v as u32) << 16,
            KOFF => self.key_off |= v as u32,
            0xC7 => self.key_off |= (v as u32) << 16,
            ENDX | 0xCF => {} // read-only
            MBASE => {
                self.regs[i] = v;
                self.reverb_base = ((v as u32) << 2) & 0x3FFFF;
                self.reverb_cur = self.reverb_base;
            }
            IRQ_ADDR => {
                self.regs[i] = v;
                let a = self.addr;
                self.check_irq(a);
            }
            TADDR => {
                self.regs[i] = v;
                self.addr = (v as u32) * 8;
                let a = self.addr;
                self.check_irq(a);
            }
            FIFO => self.write_ram(v),
            CTRL => {
                if v & 0x8000 == 0 && self.ctrl() & 0x8000 != 0 {
                    for voice in &mut self.voices {
                        voice.force_off(); // hardware mutes at once on disable
                    }
                }
                self.regs[i] = v;
                if v & (1 << 6) == 0 {
                    self.irq_flag = false;
                }
            }
            STAT => {}
            CUR_MAIN_L => self.main_l.level = v as i16,
            CUR_MAIN_R => self.main_r.level = v as i16,
            0x100..=0x12F => {
                let voice = &mut self.voices[(i - CUR_VOICE_VOL) / 2];
                if i & 1 == 0 { voice.left.level = v as i16 } else { voice.right.level = v as i16 }
            }
            _ => self.regs[i] = v,
        }
    }

    /// The SPU's DMA request line: set in the DMA write / DMA read transfer modes.
    pub fn dma_request(&self) -> bool {
        (self.ctrl() >> 4) & 3 >= 2
    }

    fn write_ram(&mut self, h: u16) {
        let a = (self.addr & 0x7FFFE) as usize;
        self.ram[a..a + 2].copy_from_slice(&h.to_le_bytes());
        self.addr = (self.addr + 2) & RAM_MASK;
        let a = self.addr;
        self.check_irq(a);
    }

    fn read_ram(&mut self) -> u16 {
        let a = (self.addr & 0x7FFFE) as usize;
        let h = u16::from_le_bytes([self.ram[a], self.ram[a + 1]]);
        self.addr = (self.addr + 2) & RAM_MASK;
        let a = self.addr;
        self.check_irq(a);
        h
    }

    pub fn dma_write(&mut self, w: u32) {
        self.write_ram(w as u16);
        self.write_ram((w >> 16) as u16);
    }

    pub fn dma_read(&mut self) -> u32 {
        let lo = self.read_ram() as u32;
        lo | ((self.read_ram() as u32) << 16)
    }

    // ---------------------------------------------------------- sample generation

    /// Run `cycles` CPU cycles worth of SPU; output frames go to `out` when given.
    pub fn tick(&mut self, cycles: u32, mut out: Option<&mut Vec<i16>>) {
        self.cycles += cycles;
        while self.cycles >= CYCLES_PER_SAMPLE {
            self.cycles -= CYCLES_PER_SAMPLE;
            let (l, r) = self.sample();
            if let Some(o) = out.as_deref_mut() {
                o.push(l);
                o.push(r);
            }
        }
    }

    fn read_block(&mut self, address: u16) -> [u8; 16] {
        let a = ((address as u32) * 8) & RAM_MASK;
        self.check_irq(a);
        self.check_irq((a + 8) & RAM_MASK);
        let mut b = [0u8; 16];
        for (k, x) in b.iter_mut().enumerate() {
            *x = self.ram[((a + k as u32) & RAM_MASK) as usize];
        }
        b
    }

    /// One voice's sample: (left, right, post-ADSR mono) for the mix, capture and the next voice's pitch modulation.
    fn sample_voice(&mut self, n: usize, noise: bool, pmod: bool, prev: i32) -> (i32, i32, i32) {
        let irq9 = self.ctrl() & (1 << 6) != 0;
        if !self.voices[n].on() && !irq9 {
            return (0, 0, 0);
        }
        if !self.voices[n].has_samples {
            if self.voices[n].key_on_delay > 0 {
                if self.voices[n].key_on_delay == 4 {
                    self.endx &= !(1 << n);
                }
                if self.voices[n].key_on_delay >= 2 {
                    let a = self.voices[n].address;
                    self.voices[n].ignore_loop = false;
                    if self.ram[(((a as u32) * 8 + 1) & RAM_MASK) as usize] & 4 != 0 {
                        self.voices[n].regs[7] = a;
                    }
                }
                self.voices[n].key_on_delay -= 1;
                return (0, 0, 0);
            }
            let a = self.voices[n].address;
            let block = self.read_block(a);
            let v = &mut self.voices[n];
            v.decode(&block);
            v.has_samples = true;
            if v.flags & 4 != 0 && !v.ignore_loop {
                v.regs[7] = v.address;
            }
        }
        let noise_level = self.noise_level as u16 as i16 as i32;
        let v = &mut self.voices[n];
        let adsr_vol = v.regs[6] as i16;
        let volume = if adsr_vol != 0 {
            let s = if noise { noise_level } else { v.interpolate() };
            apply_volume(s, adsr_vol)
        } else {
            0
        };
        if v.on() {
            v.tick_adsr();
        }
        let mut step = v.regs[2];
        if pmod {
            let factor = prev.clamp(-0x8000, 0x7FFF) + 0x8000;
            step = ((step as i16 as i32 * factor) >> 15) as u16;
        }
        step = step.min(0x3FFF);
        v.counter += step as u32;
        let mut ended = false;
        if (v.counter >> 12) & 0x1F >= 28 {
            v.counter -= 28 << 12;
            v.has_samples = false;
            v.address = v.address.wrapping_add(2);
            if v.flags & 1 != 0 {
                ended = true;
                v.address = v.regs[7] & !1;
                if v.flags & 2 == 0 && !noise {
                    v.force_off();
                }
            }
        }
        let l = apply_volume(volume, v.left.level);
        let r = apply_volume(volume, v.right.level);
        v.left.tick();
        v.right.tick();
        if ended {
            self.endx |= 1 << n;
        }
        (l, r, volume)
    }

    fn update_noise(&mut self) {
        const EIGHTHS: [u32; 4] = [0x55, 0x57, 0x77, 0x7F];
        let clock = ((self.ctrl() >> 8) & 0x3F) as u32;
        let shift = clock >> 2;
        let pos = self.noise_count;
        self.noise_count = self.noise_count.wrapping_add(1);
        if shift < 15 {
            let es = 14 - shift;
            if pos & ((1 << es) - 1) != 0 || EIGHTHS[(clock & 3) as usize] & (1 << ((pos >> es) & 7)) == 0 {
                return;
            }
        }
        let l = self.noise_level;
        self.noise_level = (l << 1) | (((l >> 15) ^ (l >> 12) ^ (l >> 11) ^ (l >> 10) ^ 1) & 1);
    }

    fn sample(&mut self) -> (i16, i16) {
        let (mut left, mut right, mut rev_l, mut rev_r) = (0i32, 0i32, 0i32, 0i32);
        let mut noise = (self.regs[NON] as u32) | ((self.regs[NON + 1] as u32) << 16);
        let mut pmon = ((self.regs[PMON] as u32) | ((self.regs[PMON + 1] as u32) << 16)) & !1;
        let mut eon = (self.regs[EON] as u32) | ((self.regs[EON + 1] as u32) << 16);
        let (mut prev, mut cap1, mut cap3) = (0, 0, 0);
        for n in 0..24 {
            let (l, r, vol) = self.sample_voice(n, noise & 1 != 0, pmon & 1 != 0, prev);
            left += l;
            right += r;
            if eon & 1 != 0 {
                rev_l += l;
                rev_r += r;
            }
            if n == 1 {
                cap1 = vol;
            }
            if n == 3 {
                cap3 = vol;
            }
            prev = vol;
            noise >>= 1;
            pmon >>= 1;
            eon >>= 1;
        }
        let ctrl = self.ctrl();
        if ctrl & (1 << 14) == 0 {
            // muted
            left = 0;
            right = 0;
            rev_l = 0;
            rev_r = 0;
        }
        self.update_noise();
        let (cd_l, cd_r) = self.cd_audio.pop_front().unwrap_or((0, 0));
        if ctrl & 1 != 0 {
            let l = apply_volume(cd_l as i32, self.regs[CD_VOL_L] as i16);
            let r = apply_volume(cd_r as i32, self.regs[CD_VOL_R] as i16);
            left += l;
            right += r;
            if ctrl & 4 != 0 {
                rev_l += l;
                rev_r += r;
            }
        }
        let (ol, or) = self.reverb(clamp16(rev_l), clamp16(rev_r));
        left += ol;
        right += or;
        let out_l = apply_volume(clamp16(left), self.main_l.level) as i16;
        let out_r = apply_volume(clamp16(right), self.main_r.level) as i16;
        self.main_l.tick();
        self.main_r.tick();
        self.capture([cd_l, cd_r, clamp16(cap1) as i16, clamp16(cap3) as i16]);
        // key on / off take effect every second sample
        if self.capture_pos & 2 == 0 && (self.key_on | self.key_off) != 0 {
            let (on, off) = (std::mem::take(&mut self.key_on), std::mem::take(&mut self.key_off));
            for n in 0..24 {
                if off & (1 << n) != 0 {
                    self.voices[n].key_off();
                }
                if on & (1 << n) != 0 {
                    self.voices[n].key_on();
                }
            }
        }
        (out_l, out_r)
    }

    fn capture(&mut self, values: [i16; 4]) {
        let pos = self.capture_pos;
        for (k, v) in values.iter().enumerate() {
            let a = (k as u32 * CAPTURE_SIZE) | pos;
            self.ram[a as usize..a as usize + 2].copy_from_slice(&v.to_le_bytes());
            self.check_irq(a);
        }
        self.capture_pos = (pos + 2) & (CAPTURE_SIZE - 1);
    }

    // ---------------------------------------------------------- reverb

    fn rev(&self, r: usize) -> u16 {
        self.regs[REVERB + r]
    }

    fn rev_s(&self, r: usize) -> i32 {
        self.regs[REVERB + r] as i16 as i32
    }

    /// Halfword offset in RAM of a reverb buffer address (wrapped inside the work area).
    fn rev_addr(&self, addr: u32) -> usize {
        const MASK: u32 = (RAM_SIZE as u32 - 1) / 2;
        let mut off = self.reverb_cur + (addr & MASK);
        if off & (1 << 18) != 0 {
            off += self.reverb_base;
        }
        ((off & MASK) * 2) as usize
    }

    fn rev_read(&self, reg_value: u16, offset: i32) -> i32 {
        let a = self.rev_addr((((reg_value as u32) << 2) as i32 + offset) as u32);
        i16::from_le_bytes([self.ram[a], self.ram[a + 1]]) as i32
    }

    fn rev_write(&mut self, reg_value: u32, v: i32) {
        let a = self.rev_addr(reg_value << 2);
        self.ram[a..a + 2].copy_from_slice(&(v as i16).to_le_bytes());
    }

    fn reverb(&mut self, in_l: i32, in_r: i32) -> (i32, i32) {
        let pos = self.reverb_pos as usize;
        for (ch, v) in [in_l, in_r].into_iter().enumerate() {
            self.reverb_down[ch][pos] = v as i16;
            self.reverb_down[ch][pos | 0x40] = v as i16;
        }
        let master = self.ctrl() & (1 << 7) != 0;
        let mut out = [0i32; 2];
        if pos & 1 != 0 {
            let mut down = [0i32; 2];
            if master {
                for (ch, d) in down.iter_mut().enumerate() {
                    let base = (pos.wrapping_sub(38)) & 0x3F;
                    let src = &self.reverb_down[ch][base..base + 39];
                    let mut acc = 0i32;
                    for k in 0..20 {
                        acc += RESAMPLE[k] * src[2 * k] as i32;
                    }
                    *d = clamp16((acc + 0x4000 * src[19] as i32) >> 15);
                }
            }
            let iir_alpha = self.rev_s(IIR_ALPHA);
            let iiasm = |s: i32| -> i32 {
                if iir_alpha == -32768 {
                    if s == -32768 { 0 } else { s * -65536 }
                } else {
                    s * (32768 - iir_alpha)
                }
            };
            let neg = |s: i32| if s == -32768 { 0x7FFF } else { -s };
            for ch in 0..2 {
                if master {
                    let in_coef = self.rev_s(IN_COEF + ch);
                    let iir_coef = self.rev_s(IIR_COEF);
                    let ia = clamp16(
                        (((self.rev_read(self.rev(IIR_SRC_A + ch), 0) * iir_coef) >> 14) + ((down[ch] * in_coef) >> 14))
                            >> 1,
                    );
                    let ib = clamp16(
                        (((self.rev_read(self.rev(IIR_SRC_B + (ch ^ 1)), 0) * iir_coef) >> 14)
                            + ((down[ch] * in_coef) >> 14))
                            >> 1,
                    );
                    let a = clamp16(
                        (((ia * iir_alpha) >> 14) + (iiasm(self.rev_read(self.rev(IIR_DEST_A + ch), -1)) >> 14)) >> 1,
                    );
                    let b = clamp16(
                        (((ib * iir_alpha) >> 14) + (iiasm(self.rev_read(self.rev(IIR_DEST_B + ch), -1)) >> 14)) >> 1,
                    );
                    self.rev_write(self.rev(IIR_DEST_A + ch) as u32, a);
                    self.rev_write(self.rev(IIR_DEST_B + ch) as u32, b);
                }
                let mut acc = 0i32;
                for (k, src) in [ACC_SRC_A, ACC_SRC_B, ACC_SRC_C, ACC_SRC_D].into_iter().enumerate() {
                    acc += (self.rev_read(self.rev(src + ch), 0) * self.rev_s(ACC_COEF_A + k)) >> 14;
                }
                let fb_alpha = self.rev_s(FB_ALPHA);
                let fb_x = self.rev_s(FB_X);
                let fb_a = self.rev_read(self.rev(MIX_DEST_A + ch).wrapping_sub(self.rev(FB_SRC_A)), 0);
                let fb_b = self.rev_read(self.rev(MIX_DEST_B + ch).wrapping_sub(self.rev(FB_SRC_B)), 0);
                let mda = clamp16((acc + ((fb_a * neg(fb_alpha)) >> 14)) >> 1);
                let mdb = clamp16(fb_a + ((((mda * fb_alpha) >> 14) + ((fb_b * neg(fb_x)) >> 14)) >> 1));
                let o = clamp16(fb_b + ((mdb * fb_x) >> 15)) as i16;
                self.reverb_up[ch][pos >> 1] = o;
                self.reverb_up[ch][(pos >> 1) | 0x20] = o;
                if master {
                    self.rev_write(self.rev(MIX_DEST_A + ch) as u32, mda);
                    self.rev_write(self.rev(MIX_DEST_B + ch) as u32, mdb);
                }
            }
            self.reverb_cur = (self.reverb_cur + 1) & 0x3FFFF;
            if self.reverb_cur == 0 {
                self.reverb_cur = self.reverb_base;
            }
            for (ch, o) in out.iter_mut().enumerate() {
                if self.regs[REV_VOL_L + ch] == 0 {
                    continue;
                }
                let base = ((pos >> 1).wrapping_sub(19)) & 0x1F;
                let src = &self.reverb_up[ch][base..base + 20];
                let acc: i32 = (0..20).map(|k| src[k] as i32 * RESAMPLE[k]).sum();
                *o = clamp16(acc >> 14);
            }
        } else {
            let idx = (((pos >> 1).wrapping_sub(19)) & 0x1F) + 9;
            out = [self.reverb_up[0][idx] as i32, self.reverb_up[1][idx] as i32];
        }
        self.reverb_pos = ((pos + 1) & 0x3F) as u32;
        (apply_volume(out[0], self.regs[REV_VOL_L] as i16), apply_volume(out[1], self.regs[REV_VOL_R] as i16))
    }
}

impl Default for Spu {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const BASE: u32 = 0x1F80_1C00;

    /// A one-block looping sample: filter 0, shift 0, every nibble = 3 -> constant 0x3000.
    fn looping_block(spu: &mut Spu, at: usize) {
        spu.ram[at] = 0x00; // shift 0, filter 0
        spu.ram[at + 1] = 0x07; // loop start + loop end + repeat
        for b in &mut spu.ram[at + 2..at + 16] {
            *b = 0x33;
        }
    }

    fn voice0(spu: &mut Spu, adsr_lo: u16, adsr_hi: u16) {
        spu.write16(BASE + 0x1AA, 0xC000); // enable, unmute
        spu.write16(BASE + 0x180, 0x3FFF);
        spu.write16(BASE + 0x182, 0x3FFF);
        spu.write16(BASE, 0x3FFF);
        spu.write16(BASE + 2, 0x3FFF);
        spu.write16(BASE + 4, 0x1000); // 44.1 kHz
        spu.write16(BASE + 6, 0x1000 / 8);
        spu.write16(BASE + 8, adsr_lo);
        spu.write16(BASE + 10, adsr_hi);
        spu.write16(BASE + 0x188, 1);
    }

    fn run(spu: &mut Spu, samples: u32) -> Vec<i16> {
        let mut out = Vec::new();
        spu.tick(samples * CYCLES_PER_SAMPLE, Some(&mut out));
        out
    }

    #[test]
    fn adpcm_filters() {
        let mut v = Voice::default();
        let mut b = [0u8; 16];
        b[0] = 0x0C; // shift 12, filter 0: nibble 7 -> 7
        b[2] = 0x07;
        v.decode(&b);
        assert_eq!(v.samples[3], 7);
        assert_eq!(v.samples[4], 0);
        // filter 1 (60/64 of the previous sample) decays a single impulse
        let mut b = [0u8; 16];
        b[0] = 0x10; // shift 0, filter 1
        b[2] = 0x01; // first sample 0x1000
        v.last = [0, 0];
        v.decode(&b);
        assert_eq!(v.samples[3], 0x1000);
        assert_eq!(v.samples[4] as i32, (0x1000 * 60) >> 6);
        assert_eq!(v.samples[5] as i32, (((0x1000 * 60) >> 6) * 60) >> 6);
        // reserved shifts 13..15 act as 9
        let mut b = [0u8; 16];
        b[0] = 0x0F;
        b[2] = 0x01;
        v.last = [0, 0];
        v.decode(&b);
        assert_eq!(v.samples[3], 0x1000 >> 9);
    }

    #[test]
    fn gaussian_weights_sum_to_unity() {
        // the four weights of every phase add up to ~1.0 (0x8000 in the table's scale)
        for i in 0..256 {
            let s = GAUSS[0xFF - i] as i32 + GAUSS[0x1FF - i] as i32 + GAUSS[0x100 + i] as i32 + GAUSS[i] as i32;
            assert!((0x7F00..=0x8100).contains(&s), "phase {i}: {s:#x}");
        }
    }

    #[test]
    fn key_on_attack_sustain_release() {
        let mut spu = Spu::new();
        looping_block(&mut spu, 0x1000);
        // attack rate 0 (fastest, linear), slowest decay, sustain level 15 (max), sustain rate 0x7F (hold)
        voice0(&mut spu, 0x00FF, 0x1FC0);
        run(&mut spu, 4);
        assert_eq!(spu.endx & 1, 0);
        let out = run(&mut spu, 200);
        assert!(spu.voices[0].phase == Phase::Sustain && spu.voices[0].regs[6] > 0x7F00, "attack reached the top, held");
        let tail = &out[out.len() - 20..];
        assert!(tail.iter().all(|&s| s > 0x1000), "the constant sample is heard: {tail:?}");
        assert_eq!(spu.endx & 1, 1, "a looped block sets ENDX");
        // key off -> release (rate 0) back to silence and the voice turns off
        spu.write16(BASE + 0x18C, 1);
        run(&mut spu, 400);
        assert_eq!(spu.voices[0].regs[6], 0);
        assert!(!spu.voices[0].on());
        assert!(run(&mut spu, 10).iter().all(|&s| s == 0));
    }

    #[test]
    fn end_without_repeat_stops_the_voice() {
        let mut spu = Spu::new();
        looping_block(&mut spu, 0x1000);
        spu.ram[0x1001] = 0x01; // loop end, no repeat
        voice0(&mut spu, 0x000F, 0x1FC0);
        run(&mut spu, 60);
        assert!(!spu.voices[0].on());
        assert_eq!(spu.endx & 1, 1);
    }

    #[test]
    fn exponential_decay_and_sustain_level() {
        let mut spu = Spu::new();
        looping_block(&mut spu, 0x1000);
        // attack 0, decay rate 8 (exponential), sustain level 7 -> 0x4000, sustain hold. Decay goes
        // one step past the threshold (hardware), so the level ends just under 0x4000.
        voice0(&mut spu, 0x0087, 0x1FC0);
        run(&mut spu, 3000);
        let v = spu.voices[0].regs[6] as i16;
        assert!(spu.voices[0].phase == Phase::Sustain, "decay ended");
        assert!((0x3FC0..0x4000).contains(&v), "sustain level reached: {v:#x}");
    }

    #[test]
    fn ram_irq_fires_on_transfer_address() {
        let mut spu = Spu::new();
        spu.write16(BASE + 0x1A4, 0x0200); // IRQ at 0x1000
        spu.write16(BASE + 0x1AA, 0x8040); // enable + IRQ9 enable
        spu.write16(BASE + 0x1A6, 0x01FF); // transfer at 0xFF8
        for _ in 0..3 {
            spu.write16(BASE + 0x1A8, 0);
        }
        assert!(!spu.irq);
        spu.write16(BASE + 0x1A8, 0);
        assert!(spu.irq, "the 4th halfword reaches 0x1000");
        assert_ne!(spu.read16(BASE + 0x1AE) & (1 << 6), 0);
        spu.write16(BASE + 0x1AA, 0x8000); // acknowledge
        assert_eq!(spu.read16(BASE + 0x1AE) & (1 << 6), 0);
    }

    #[test]
    fn noise_lfsr_runs_at_max_clock() {
        let mut spu = Spu::new();
        spu.write16(BASE + 0x1AA, 0xC000 | (0x3F << 8));
        let a = spu.noise_level;
        run(&mut spu, 4);
        assert_ne!(spu.noise_level, a);
    }

    #[test]
    fn muted_spu_is_silent() {
        let mut spu = Spu::new();
        looping_block(&mut spu, 0x1000);
        voice0(&mut spu, 0x000F, 0x1FC0);
        spu.write16(BASE + 0x1AA, 0x8000); // enabled but muted
        assert!(run(&mut spu, 200).iter().all(|&s| s == 0));
    }

    #[test]
    fn state_round_trip() {
        let mut spu = Spu::new();
        looping_block(&mut spu, 0x1000);
        voice0(&mut spu, 0x000F, 0x1FC0);
        run(&mut spu, 50);
        let mut copy: Spu = bincode::deserialize(&bincode::serialize(&spu).unwrap()).unwrap();
        assert_eq!(run(&mut spu, 100), run(&mut copy, 100));
    }
}
