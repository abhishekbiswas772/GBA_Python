//! Timers 0-3 (`src/core/gba/timer.py`): lazily evaluated counters with
//! scheduled overflow events. Overflows between two updates are folded together.

use serde::{Deserialize, Serialize};

use crate::gba::{Gba, INF};

const TIMER_SHIFT: [u32; 4] = [0, 6, 8, 10];
const IRQ_TIMER0: u32 = 0x0008;

#[derive(Serialize, Deserialize)]
pub struct Timers {
    pub reload: [u32; 4],
    pub ctrl: [u32; 4],
    pub base: [i64; 4],
    pub start: [i64; 4],
    pub next_event: u64,
}

impl Timers {
    pub fn new() -> Timers {
        Timers { reload: [0; 4], ctrl: [0; 4], base: [0; 4], start: [0; 4], next_event: INF }
    }

    #[inline]
    fn ticking(&self, i: usize) -> bool {
        let c = self.ctrl[i];
        c & 0x80 != 0 && !(i != 0 && c & 0x04 != 0)
    }

    #[inline]
    fn shift(&self, i: usize) -> u32 {
        TIMER_SHIFT[(self.ctrl[i] & 3) as usize]
    }
}

impl Default for Timers {
    fn default() -> Self {
        Timers::new()
    }
}

impl Gba {
    pub fn timer_read(&mut self, i: usize) -> u32 {
        if self.timers.ticking(i) {
            let now = self.cpu.now;
            self.timers_update(now);
            let t = &self.timers;
            return ((t.base[i] + ((self.cpu.now as i64 - t.start[i]) >> t.shift(i))) & 0xFFFF) as u32;
        }
        (self.timers.base[i] & 0xFFFF) as u32
    }

    fn timer_freeze(&mut self, i: usize, now: i64) {
        let t = &mut self.timers;
        if t.ticking(i) {
            t.base[i] += (now - t.start[i]) >> t.shift(i);
            t.start[i] = now;
        }
    }

    pub fn timer_write_ctrl(&mut self, i: usize, v: u32, m: u32) {
        let now = self.cpu.now;
        self.timers_update(now);
        self.timer_freeze(i, now as i64);
        let t = &mut self.timers;
        let old = t.ctrl[i];
        let new = ((old & !m) | v) & 0xC7;
        t.ctrl[i] = new;
        if new & 0x80 != 0 && old & 0x80 == 0 {
            t.base[i] = t.reload[i] as i64;
        }
        t.start[i] = now as i64;
        self.timers_schedule();
    }

    fn timers_schedule(&mut self) {
        let t = &mut self.timers;
        let mut nxt = INF;
        for i in 0..4 {
            if t.ticking(i) {
                let e = t.start[i] + ((0x10000 - t.base[i]) << t.shift(i));
                let e = e.max(0) as u64;
                if e < nxt {
                    nxt = e;
                }
            }
        }
        t.next_event = nxt;
        if nxt < self.cpu.deadline {
            self.cpu.deadline = nxt;
            self.cpu.limit = 0;
        }
    }

    pub fn timers_update(&mut self, now: u64) {
        if self.timers.next_event > now {
            return;
        }
        let nowi = now as i64;
        for i in 0..4 {
            if !self.timers.ticking(i) {
                continue;
            }
            let t = &mut self.timers;
            let shift = t.shift(i);
            let first = t.start[i] + ((0x10000 - t.base[i]) << shift);
            if first > nowi {
                continue;
            }
            let period = (0x10000 - t.reload[i] as i64) << shift;
            let count = 1 + (nowi - first) / period;
            let last = first + (count - 1) * period;
            t.base[i] = t.reload[i] as i64;
            t.start[i] = last;
            self.timer_overflowed(i, count);
        }
        self.timers_schedule();
    }

    fn timer_overflowed(&mut self, i: usize, count: i64) {
        if self.timers.ctrl[i] & 0x40 != 0 {
            self.request_irq(IRQ_TIMER0 << i);
        }
        if i < 2 {
            self.apu_timer_overflow(i, count);
        }
        if i < 3 {
            let j = i + 1;
            let c = self.timers.ctrl[j];
            if c & 0x80 != 0 && c & 0x04 != 0 {
                let t = &mut self.timers;
                let total = t.base[j] + count;
                if total >= 0x10000 {
                    let period = 0x10000 - t.reload[j] as i64;
                    let n = 1 + (total - 0x10000) / period;
                    t.base[j] = t.reload[j] as i64 + (total - 0x10000) % period;
                    self.timer_overflowed(j, n);
                } else {
                    t.base[j] = total;
                }
            }
        }
    }
}
