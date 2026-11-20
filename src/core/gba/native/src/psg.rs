//! The Game Boy APU (`src/core/gameboy/apu.py`), reused as the GBA's four PSG
//! channels. It tracks absolute 4.19 MHz cycles; channel state advances lazily
//! in chunks split at output-sample (44.1 kHz) and frame-sequencer boundaries.

use serde::{Deserialize, Serialize};

pub const CYCLES_PER_SECOND: i64 = 4_194_304;
pub const SAMPLE_RATE: i64 = 44_100;
const FS_PERIOD: i64 = 8_192;
const DUTY: [u32; 4] = [0b00000001, 0b10000001, 0b10000111, 0b01111110];
const SCALE: i64 = 32_767 / (2 * 15 * 4 * 8);
const HP: f64 = 0.001;
const WMASK: [u8; 23] = [
    0x7F, 0xFF, 0xFF, 0xFF, 0x40, 0x00, 0xFF, 0xFF, 0xFF, 0x40, 0x80, 0xFF, 0x60, 0xFF, 0x40, 0x00, 0xFF, 0xFF, 0xFF,
    0x40, 0xFF, 0xFF, 0x80,
];
const ROR: [u8; 23] = [
    0x80, 0x3F, 0x00, 0xFF, 0xBF, 0xFF, 0x3F, 0x00, 0xFF, 0xBF, 0x7F, 0xFF, 0x9F, 0xFF, 0xBF, 0xFF, 0xFF, 0x00, 0x00,
    0xBF, 0x00, 0x00, 0x70,
];

#[derive(Serialize, Deserialize, Default, Clone)]
pub struct Pulse {
    has_sweep: bool,
    duty: u32,
    duty_pos: i64,
    freq: i64,
    timer: i64,
    volume: i64,
    env_init: i64,
    env_dir: bool,
    env_period: i64,
    env_timer: i64,
    length: i64,
    length_enabled: bool,
    pub enabled: bool,
    dac: bool,
    sweep_period: i64,
    sweep_negate: bool,
    sweep_shift: i64,
    sweep_timer: i64,
    shadow: i64,
    sweep_on: bool,
    neg_used: bool,
}

impl Pulse {
    fn trigger(&mut self) {
        self.enabled = self.dac;
        self.timer = 0;
        if self.length == 0 {
            self.length = 64;
        }
        self.volume = self.env_init;
        self.env_timer = self.env_period;
        if self.has_sweep {
            self.shadow = self.freq;
            self.sweep_timer = if self.sweep_period != 0 { self.sweep_period } else { 8 };
            self.sweep_on = self.sweep_period != 0 || self.sweep_shift != 0;
            self.neg_used = false;
            if self.sweep_shift != 0 {
                self.sweep_calc();
            }
        }
    }

    fn sweep_calc(&mut self) -> i64 {
        let delta = self.shadow >> self.sweep_shift;
        let new = if self.sweep_negate {
            self.neg_used = true;
            self.shadow - delta
        } else {
            self.shadow + delta
        };
        if new > 2047 {
            self.enabled = false;
        }
        new
    }

    fn clock_sweep(&mut self) {
        if self.sweep_timer > 0 {
            self.sweep_timer -= 1;
        }
        if self.sweep_timer != 0 {
            return;
        }
        self.sweep_timer = if self.sweep_period != 0 { self.sweep_period } else { 8 };
        if !self.sweep_on || self.sweep_period == 0 {
            return;
        }
        let new = self.sweep_calc();
        if new <= 2047 && self.sweep_shift != 0 {
            self.shadow = new;
            self.freq = new;
            self.sweep_calc();
        }
    }

    fn clock_length(&mut self) {
        if self.length_enabled && self.length != 0 {
            self.length -= 1;
            if self.length == 0 {
                self.enabled = false;
            }
        }
    }

    fn clock_envelope(&mut self) {
        if self.env_period == 0 || self.env_timer == 0 {
            return;
        }
        self.env_timer -= 1;
        if self.env_timer != 0 {
            return;
        }
        self.env_timer = self.env_period;
        if self.env_dir {
            if self.volume < 15 {
                self.volume += 1;
            }
        } else if self.volume > 0 {
            self.volume -= 1;
        }
    }

    fn power_off(&mut self) {
        let hs = self.has_sweep;
        let (length, timer, env_timer, shadow) = (self.length, self.timer, self.env_timer, self.shadow);
        *self = Pulse { has_sweep: hs, length, timer, env_timer, shadow, ..Default::default() };
    }
}

#[derive(Serialize, Deserialize, Default, Clone)]
pub struct Wave {
    wave: [u8; 16],
    pos: i64,
    freq: i64,
    timer: i64,
    volume_code: i64,
    length: i64,
    length_enabled: bool,
    pub enabled: bool,
    dac: bool,
}

impl Wave {
    fn trigger(&mut self) {
        self.enabled = self.dac;
        self.timer = 0;
        self.pos = 0;
        if self.length == 0 {
            self.length = 256;
        }
    }

    fn clock_length(&mut self) {
        if self.length_enabled && self.length != 0 {
            self.length -= 1;
            if self.length == 0 {
                self.enabled = false;
            }
        }
    }
}

#[derive(Serialize, Deserialize, Default, Clone)]
pub struct Noise {
    lfsr: u32,
    width7: bool,
    shift: i64,
    div_code: i64,
    timer: i64,
    volume: i64,
    env_init: i64,
    env_dir: bool,
    env_period: i64,
    env_timer: i64,
    length: i64,
    length_enabled: bool,
    pub enabled: bool,
    dac: bool,
}

impl Noise {
    fn trigger(&mut self) {
        self.enabled = self.dac;
        self.timer = 0;
        self.lfsr = 0x7FFF;
        if self.length == 0 {
            self.length = 64;
        }
        self.volume = self.env_init;
        self.env_timer = self.env_period;
    }

    fn clock_length(&mut self) {
        if self.length_enabled && self.length != 0 {
            self.length -= 1;
            if self.length == 0 {
                self.enabled = false;
            }
        }
    }

    fn clock_envelope(&mut self) {
        if self.env_period == 0 || self.env_timer == 0 {
            return;
        }
        self.env_timer -= 1;
        if self.env_timer != 0 {
            return;
        }
        self.env_timer = self.env_period;
        if self.env_dir {
            if self.volume < 15 {
                self.volume += 1;
            }
        } else if self.volume > 0 {
            self.volume -= 1;
        }
    }
}

#[derive(Serialize, Deserialize, Clone)]
pub struct Psg {
    ch1: Pulse,
    ch2: Pulse,
    ch3: Wave,
    ch4: Noise,
    regs: [u8; 0x17],
    power: bool,
    fs_step: u32,
    master_l: i64,
    master_r: i64,
    nr51: u32,
    cycles: i64,
    advanced: i64,
    next_fs: i64,
    pub sample_k: i64,
    next_sample: i64,
    sampling: bool,
    next_event: i64,
    pub buffer: Vec<i16>,
    dc_l: f64,
    dc_r: f64,
}

impl Psg {
    pub fn new() -> Psg {
        Psg {
            ch1: Pulse { has_sweep: true, ..Default::default() },
            ch2: Pulse::default(),
            ch3: Wave::default(),
            ch4: Noise { lfsr: 0x7FFF, ..Default::default() },
            regs: [0; 0x17],
            power: false,
            fs_step: 0,
            master_l: 0,
            master_r: 0,
            nr51: 0,
            cycles: 0,
            advanced: 0,
            next_fs: FS_PERIOD,
            sample_k: 1,
            next_sample: CYCLES_PER_SECOND / SAMPLE_RATE,
            sampling: false,
            next_event: FS_PERIOD,
            buffer: Vec::new(),
            dc_l: 0.0,
            dc_r: 0.0,
        }
    }

    pub fn sampling(&self) -> bool {
        self.sampling
    }

    pub fn set_sampling(&mut self, on: bool) {
        if on != self.sampling {
            self.sync();
            if on {
                let k = -((-self.advanced * SAMPLE_RATE).div_euclid(CYCLES_PER_SECOND));
                self.sample_k = if k != 0 { k } else { 1 };
                self.next_sample = (self.sample_k * CYCLES_PER_SECOND).div_euclid(SAMPLE_RATE);
            }
        }
        self.sampling = on;
        self.update_next_event();
    }

    pub fn step(&mut self, cycles: i64) {
        self.cycles += cycles;
        if self.cycles >= self.next_event {
            self.advance(self.cycles);
        }
    }

    fn sync(&mut self) {
        self.advance(self.cycles);
    }

    fn update_next_event(&mut self) {
        let mut n = self.next_fs;
        if self.sampling && self.next_sample < n {
            n = self.next_sample;
        }
        self.next_event = n;
    }

    fn advance(&mut self, end: i64) {
        if end < self.next_fs && (!self.sampling || end < self.next_sample) {
            return;
        }
        if !self.sampling {
            while self.next_fs <= end {
                self.advanced = self.next_fs;
                self.fs_tick();
                self.next_fs += FS_PERIOD;
            }
        } else {
            self.advance_sampled(end);
        }
        self.update_next_event();
    }

    fn fs_tick(&mut self) {
        if !self.power {
            return;
        }
        let step = self.fs_step;
        self.fs_step = (step + 1) & 7;
        if step & 1 == 0 {
            self.ch1.clock_length();
            self.ch2.clock_length();
            self.ch3.clock_length();
            self.ch4.clock_length();
        }
        if step == 2 || step == 6 {
            self.ch1.clock_sweep();
        }
        if step == 7 {
            self.ch1.clock_envelope();
            self.ch2.clock_envelope();
            self.ch4.clock_envelope();
        }
    }

    fn advance_sampled(&mut self, end: i64) {
        let mut k = self.sample_k;
        let mut nxt_s = self.next_sample;
        let mut nxt_f = self.next_fs;
        let mut advanced = self.advanced;
        let mut dc_l = self.dc_l;
        let mut dc_r = self.dc_r;
        let pan = self.nr51;
        let ml = self.master_l + 1;
        let mr = self.master_r + 1;
        let z1 = if self.ch1.dac { -15 } else { 0 };
        let z2 = if self.ch2.dac { -15 } else { 0 };
        let z3 = if self.ch3.dac { -15 } else { 0 };
        let z4 = if self.ch4.dac { -15 } else { 0 };
        loop {
            let nxt = nxt_f.min(nxt_s);
            if nxt > end {
                break;
            }
            let chunk = nxt - advanced;
            advanced = nxt;
            for ch in [&mut self.ch1, &mut self.ch2] {
                if ch.enabled {
                    let t = ch.timer + chunk;
                    let p = (2048 - ch.freq) << 2;
                    let steps = t.div_euclid(p);
                    ch.timer = t.rem_euclid(p);
                    if steps != 0 {
                        ch.duty_pos = (ch.duty_pos + steps) & 7;
                    }
                }
            }
            let c3 = &mut self.ch3;
            if c3.enabled {
                let t = c3.timer + chunk;
                let p = (2048 - c3.freq) << 1;
                let steps = t.div_euclid(p);
                c3.timer = t.rem_euclid(p);
                if steps != 0 {
                    c3.pos = (c3.pos + steps) & 31;
                }
            }
            let c4 = &mut self.ch4;
            if c4.enabled && c4.shift < 14 {
                let p = (if c4.div_code == 0 { 8 } else { c4.div_code << 4 }) << c4.shift;
                let t = c4.timer + chunk;
                let steps = t.div_euclid(p);
                c4.timer = t.rem_euclid(p);
                let mut lfsr = c4.lfsr;
                for _ in 0..steps {
                    let fb = (lfsr ^ (lfsr >> 1)) & 1;
                    lfsr = (lfsr >> 1) | (fb << 14);
                    if c4.width7 {
                        lfsr = (lfsr & !0x40) | (fb << 6);
                    }
                }
                c4.lfsr = lfsr;
            }

            if nxt == nxt_s {
                let (mut l, mut r) = (0i64, 0i64);
                let mut add = |s: i64, rb: u32, lb: u32| {
                    if s != 0 {
                        if pan & rb != 0 {
                            r += s;
                        }
                        if pan & lb != 0 {
                            l += s;
                        }
                    }
                };
                let pulse = |c: &Pulse, z: i64| {
                    if c.enabled {
                        ((if (DUTY[c.duty as usize] >> (7 - c.duty_pos)) & 1 != 0 { c.volume } else { 0 }) << 1) - 15
                    } else {
                        z
                    }
                };
                add(pulse(&self.ch1, z1), 0x01, 0x10);
                add(pulse(&self.ch2, z2), 0x02, 0x20);
                let c3 = &self.ch3;
                let s3 = if c3.enabled {
                    if c3.volume_code != 0 {
                        let b = c3.wave[(c3.pos >> 1) as usize] as i64;
                        let nib = if c3.pos & 1 == 0 { b >> 4 } else { b & 0xF };
                        ((nib >> (c3.volume_code - 1)) << 1) - 15
                    } else {
                        -15
                    }
                } else {
                    z3
                };
                add(s3, 0x04, 0x40);
                let c4 = &self.ch4;
                let s4 = if c4.enabled { ((if c4.lfsr & 1 == 0 { c4.volume } else { 0 }) << 1) - 15 } else { z4 };
                add(s4, 0x08, 0x80);

                let x = (l * ml * SCALE) as f64;
                dc_l += (x - dc_l) * HP;
                let y = ((x - dc_l) as i64).clamp(-32768, 32767);
                let x = (r * mr * SCALE) as f64;
                dc_r += (x - dc_r) * HP;
                let z = ((x - dc_r) as i64).clamp(-32768, 32767);
                self.buffer.push(y as i16);
                self.buffer.push(z as i16);
                k += 1;
                nxt_s = (k * CYCLES_PER_SECOND).div_euclid(SAMPLE_RATE);
            }
            if nxt == nxt_f {
                self.fs_tick();
                nxt_f += FS_PERIOD;
            }
        }
        self.sample_k = k;
        self.next_sample = nxt_s;
        self.next_fs = nxt_f;
        self.advanced = advanced;
        self.dc_l = dc_l;
        self.dc_r = dc_r;
    }

    pub fn take_samples(&mut self) -> Vec<i16> {
        self.sync();
        std::mem::take(&mut self.buffer)
    }

    // ------------------------------------------------------------ registers
    pub fn read(&mut self, addr: u32) -> u32 {
        let i = addr as usize - 0xFF10;
        if i >= 0x20 {
            return self.ch3.wave[i - 0x20] as u32;
        }
        if i > 0x16 {
            return 0xFF;
        }
        self.sync();
        if i == 0x16 {
            let status = self.ch1.enabled as u32
                | (self.ch2.enabled as u32) << 1
                | (self.ch3.enabled as u32) << 2
                | (self.ch4.enabled as u32) << 3;
            return 0x70 | if self.power { 0x80 } else { 0 } | status;
        }
        (self.regs[i] | ROR[i]) as u32
    }

    pub fn write(&mut self, addr: u32, value: u32) {
        let i = addr as usize - 0xFF10;
        let value = value & 0xFF;
        if i >= 0x20 {
            self.ch3.wave[i - 0x20] = value as u8;
            return;
        }
        if i > 0x16 {
            return;
        }
        self.sync();
        if i == 0x16 {
            self.power_write(value);
            return;
        }
        let v = value as i64;
        if !self.power {
            match i {
                0x01 => self.ch1.length = 64 - (v & 0x3F),
                0x06 => self.ch2.length = 64 - (v & 0x3F),
                0x0B => self.ch3.length = 256 - v,
                0x10 => self.ch4.length = 64 - (v & 0x3F),
                _ => {}
            }
            return;
        }
        self.regs[i] = value as u8 & WMASK[i];
        match i {
            0x00 => {
                let ch = &mut self.ch1;
                ch.sweep_period = v >> 4 & 7;
                ch.sweep_negate = v & 0x08 != 0;
                ch.sweep_shift = v & 7;
                if ch.neg_used && !ch.sweep_negate {
                    ch.enabled = false;
                }
            }
            0x01 => {
                self.ch1.duty = value >> 6;
                self.ch1.length = 64 - (v & 0x3F);
            }
            0x02 => envelope_p(&mut self.ch1, v),
            0x03 => self.ch1.freq = (self.ch1.freq & 0x700) | v,
            0x04 => {
                let ch = &mut self.ch1;
                ch.freq = (ch.freq & 0xFF) | ((v & 7) << 8);
                ch.length_enabled = v & 0x40 != 0;
                if v & 0x80 != 0 {
                    ch.trigger();
                }
            }
            0x06 => {
                self.ch2.duty = value >> 6;
                self.ch2.length = 64 - (v & 0x3F);
            }
            0x07 => envelope_p(&mut self.ch2, v),
            0x08 => self.ch2.freq = (self.ch2.freq & 0x700) | v,
            0x09 => {
                let ch = &mut self.ch2;
                ch.freq = (ch.freq & 0xFF) | ((v & 7) << 8);
                ch.length_enabled = v & 0x40 != 0;
                if v & 0x80 != 0 {
                    ch.trigger();
                }
            }
            0x0A => {
                self.ch3.dac = v & 0x80 != 0;
                if !self.ch3.dac {
                    self.ch3.enabled = false;
                }
            }
            0x0B => self.ch3.length = 256 - v,
            0x0C => self.ch3.volume_code = v >> 5 & 3,
            0x0D => self.ch3.freq = (self.ch3.freq & 0x700) | v,
            0x0E => {
                let ch = &mut self.ch3;
                ch.freq = (ch.freq & 0xFF) | ((v & 7) << 8);
                ch.length_enabled = v & 0x40 != 0;
                if v & 0x80 != 0 {
                    ch.trigger();
                }
            }
            0x10 => self.ch4.length = 64 - (v & 0x3F),
            0x11 => {
                let ch = &mut self.ch4;
                ch.env_init = v >> 4;
                ch.env_dir = v & 0x08 != 0;
                ch.env_period = v & 7;
                ch.dac = v & 0xF8 != 0;
                if !ch.dac {
                    ch.enabled = false;
                }
            }
            0x12 => {
                self.ch4.shift = v >> 4;
                self.ch4.width7 = v & 0x08 != 0;
                self.ch4.div_code = v & 7;
            }
            0x13 => {
                let ch = &mut self.ch4;
                ch.length_enabled = v & 0x40 != 0;
                if v & 0x80 != 0 {
                    ch.trigger();
                }
            }
            0x14 => {
                self.master_l = v >> 4 & 7;
                self.master_r = v & 7;
            }
            0x15 => self.nr51 = value,
            _ => {}
        }
    }

    fn power_write(&mut self, value: u32) {
        let on = value & 0x80 != 0;
        if on == self.power {
            return;
        }
        self.power = on;
        self.fs_step = 0;
        self.regs[0x16] = (value & 0x80) as u8;
        if !on {
            for i in 0..0x16 {
                self.regs[i] = 0;
            }
            self.master_l = 0;
            self.master_r = 0;
            self.nr51 = 0;
            self.ch1.power_off();
            self.ch2.power_off();
            let c3 = &mut self.ch3;
            c3.enabled = false;
            c3.dac = false;
            c3.freq = 0;
            c3.volume_code = 0;
            c3.length_enabled = false;
            let c4 = &mut self.ch4;
            c4.enabled = false;
            c4.dac = false;
            c4.shift = 0;
            c4.div_code = 0;
            c4.width7 = false;
            c4.volume = 0;
            c4.env_init = 0;
            c4.env_dir = false;
            c4.env_period = 0;
            c4.length_enabled = false;
        }
    }
}

impl Default for Psg {
    fn default() -> Self {
        Psg::new()
    }
}

fn envelope_p(ch: &mut Pulse, v: i64) {
    ch.env_init = v >> 4;
    ch.env_dir = v & 0x08 != 0;
    ch.env_period = v & 7;
    ch.dac = v & 0xF8 != 0;
    if !ch.dac {
        ch.enabled = false;
    }
}
