//! GBA sound (`src/core/gba/apu.py`): 4 PSG channels (the Game Boy APU at a
//! quarter of the CPU clock) + Direct Sound A/B FIFOs fed by timer overflows.

use std::collections::VecDeque;

use serde::{Deserialize, Serialize};

use crate::gba::{Gba, CPU_HZ};
use crate::psg::{Psg, SAMPLE_RATE};

const PSG_RATIO: [f64; 4] = [0.25, 0.5, 1.0, 0.0];
const PSG_GAIN: f64 = 0.5;
const DMA_GAIN: i64 = 64;

fn psg_map(off: u32) -> Option<u32> {
    Some(match off {
        0x60 => 0xFF10,
        0x62 => 0xFF11,
        0x63 => 0xFF12,
        0x64 => 0xFF13,
        0x65 => 0xFF14,
        0x68 => 0xFF16,
        0x69 => 0xFF17,
        0x6C => 0xFF18,
        0x6D => 0xFF19,
        0x70 => 0xFF1A,
        0x72 => 0xFF1B,
        0x73 => 0xFF1C,
        0x74 => 0xFF1D,
        0x75 => 0xFF1E,
        0x78 => 0xFF20,
        0x79 => 0xFF21,
        0x7C => 0xFF22,
        0x7D => 0xFF23,
        0x80 => 0xFF24,
        0x81 => 0xFF25,
        _ => return None,
    })
}

#[derive(Serialize, Deserialize)]
pub struct Apu {
    pub psg: Psg,
    pub psg_cycles: u64,
    pub fifo: [VecDeque<i32>; 2],
    pub current: [i32; 2],
    pub history: [Vec<(u64, i32)>; 2],
    pub sample_k: i64,
    pub cnt_h: u32,
    pub enabled: bool,
    pub last: Option<[i32; 2]>,
}

impl Apu {
    pub fn new() -> Apu {
        let mut psg = Psg::new();
        psg.write(0xFF26, 0x80);
        Apu {
            psg,
            psg_cycles: 0,
            fifo: [VecDeque::new(), VecDeque::new()],
            current: [0; 2],
            history: [Vec::new(), Vec::new()],
            sample_k: 0,
            cnt_h: 0,
            enabled: true,
            last: None,
        }
    }
}

impl Default for Apu {
    fn default() -> Self {
        Apu::new()
    }
}

impl Gba {
    pub fn apu_sync(&mut self) {
        let now = self.cpu.now;
        let delta = (now - self.apu.psg_cycles.min(now)) >> 2;
        if delta > 0 {
            self.apu.psg.step(delta as i64);
            self.apu.psg_cycles += delta << 2;
        }
    }

    pub fn apu_sampling(&self) -> bool {
        self.apu.psg.sampling()
    }

    pub fn apu_set_sampling(&mut self, on: bool) {
        self.apu_sync();
        self.apu.psg.set_sampling(on);
        if on {
            self.apu.sample_k = self.apu.psg.sample_k;
            self.apu.history = [Vec::new(), Vec::new()];
        }
    }

    pub fn apu_write_psg(&mut self, off: u32, v: u32, m: u32) {
        self.store_io(off, v, m);
        self.apu_sync();
        for byte in 0..2 {
            if m >> (8 * byte) & 0xFF != 0 {
                if let Some(gb) = psg_map(off + byte) {
                    self.apu.psg.write(gb, (v >> (8 * byte)) & 0xFF);
                }
            }
        }
    }

    pub fn apu_write_wave(&mut self, off: u32, v: u32, m: u32) {
        self.store_io(off, v, m);
        self.apu_sync();
        let i = off - 0x90;
        if m & 0xFF != 0 {
            self.apu.psg.write(0xFF30 + i, v & 0xFF);
        }
        if m & 0xFF00 != 0 {
            self.apu.psg.write(0xFF31 + i, (v >> 8) & 0xFF);
        }
    }

    pub fn apu_write_cnt_h(&mut self, v: u32, m: u32) {
        if v & 0x0800 != 0 {
            self.apu.fifo[0].clear();
        }
        if v & 0x8000 != 0 {
            self.apu.fifo[1].clear();
        }
        self.apu.cnt_h = ((self.apu.cnt_h & !m) | v) & 0x770F;
    }

    pub fn apu_write_cnt_x(&mut self, v: u32, m: u32) {
        if m & 0xFF != 0 {
            self.apu_sync();
            self.apu.enabled = v & 0x80 != 0;
            self.apu.psg.write(0xFF26, v & 0x80);
            if !self.apu.enabled {
                self.apu.fifo[0].clear();
                self.apu.fifo[1].clear();
            }
        }
    }

    pub fn apu_read_cnt_x(&mut self) -> u32 {
        self.apu_sync();
        self.apu.psg.read(0xFF26) & 0x8F
    }

    pub fn apu_write_fifo(&mut self, k: usize, v: u32, m: u32) {
        let f = &mut self.apu.fifo[k];
        for byte in 0..2 {
            if m >> (8 * byte) & 0xFF != 0 && f.len() < 32 {
                let b = ((v >> (8 * byte)) & 0xFF) as i32;
                f.push_back(if b & 0x80 != 0 { b - 256 } else { b });
            }
        }
    }

    /// Timers 0 and 1 overflowed `count` times.
    pub fn apu_timer_overflow(&mut self, timer: usize, count: i64) {
        if !self.apu.enabled {
            return;
        }
        let h = self.apu.cnt_h;
        for (k, sel_bit) in [(0usize, 0x0400u32), (1, 0x4000)] {
            if (h & sel_bit != 0) != (timer != 0) {
                continue;
            }
            for _ in 0..count.min(32) {
                if let Some(s) = self.apu.fifo[k].pop_front() {
                    self.apu.current[k] = s;
                }
            }
            if self.apu.psg.sampling() {
                let now = self.cpu.now;
                let cur = self.apu.current[k];
                self.apu.history[k].push((now, cur));
            }
            if self.apu.fifo[k].len() <= 16 {
                self.dma_fifo_request(0x0400_0000 | if k == 0 { 0xA0 } else { 0xA4 });
            }
        }
    }

    /// int16 stereo samples at 44.1 kHz since the last call.
    pub fn apu_take_samples(&mut self) -> Vec<i16> {
        self.apu_sync();
        let psg = self.apu.psg.take_samples();
        let n = psg.len() / 2;
        if n == 0 {
            return psg;
        }
        let a = &mut self.apu;
        let h = a.cnt_h;
        let ratio = PSG_RATIO[(h & 3) as usize] * PSG_GAIN;
        let ga = DMA_GAIN * if h & 0x04 != 0 { 2 } else { 1 };
        let gb = DMA_GAIN * if h & 0x08 != 0 { 2 } else { 1 };
        let (a_r, a_l) = (h & 0x0100 != 0, h & 0x0200 != 0);
        let (b_r, b_l) = (h & 0x1000 != 0, h & 0x2000 != 0);
        let (mut ia, mut ib) = (0, 0);
        let start = a.last.unwrap_or(a.current);
        let (mut cur_a, mut cur_b) = (start[0], start[1]);
        let mut out = vec![0i16; 2 * n];
        let k = a.sample_k;
        for j in 0..n {
            let t = ((k + j as i64) * CPU_HZ as i64).div_euclid(SAMPLE_RATE) as u64;
            while ia < a.history[0].len() && a.history[0][ia].0 <= t {
                cur_a = a.history[0][ia].1;
                ia += 1;
            }
            while ib < a.history[1].len() && a.history[1][ib].0 <= t {
                cur_b = a.history[1][ib].1;
                ib += 1;
            }
            let da = (cur_a as i64 * ga) as f64;
            let db = (cur_b as i64 * gb) as f64;
            let left = psg[2 * j] as f64 * ratio + if a_l { da } else { 0.0 } + if b_l { db } else { 0.0 };
            let right = psg[2 * j + 1] as f64 * ratio + if a_r { da } else { 0.0 } + if b_r { db } else { 0.0 };
            out[2 * j] = (left as i64).clamp(-32768, 32767) as i16;
            out[2 * j + 1] = (right as i64).clamp(-32768, 32767) as i16;
        }
        a.sample_k = k + n as i64;
        a.last = Some([cur_a, cur_b]);
        a.history = [Vec::new(), Vec::new()];
        out
    }
}
