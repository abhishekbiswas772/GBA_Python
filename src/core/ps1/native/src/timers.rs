//! Root counters (timers 0-2): sysclk / dotclock / hblank / sysclk÷8 sources,
//! target and overflow IRQs, and the vblank/hblank sync modes.

#[derive(Clone, Copy, Default, serde::Serialize, serde::Deserialize)]
pub struct Timer {
    pub counter: u32,
    pub mode: u32,
    pub target: u32,
    fired: bool,
    paused: bool,
    frac: u32,
}

#[derive(Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct Timers {
    pub t: [Timer; 3],
    pub irq: u32,
    in_vblank: bool,
    in_hblank: bool,
}

impl Timer {
    fn add(&mut self, n: u32, idx: usize, irq: &mut u32) {
        if n == 0 || self.paused {
            return;
        }
        let old = self.counter;
        let mut c = old + n;
        let target = self.target;
        let mut hit = false;
        if old <= target && c >= target {
            self.mode |= 1 << 11;
            if self.mode & (1 << 4) != 0 {
                hit = true;
            }
        }
        if self.mode & (1 << 3) != 0 && target != 0 {
            if c > target {
                c = (c - target - 1) % (target + 1);
            }
        } else if c > 0xFFFF {
            self.mode |= 1 << 12;
            if self.mode & (1 << 5) != 0 {
                hit = true;
            }
            c &= 0xFFFF;
        }
        self.counter = c;
        if hit {
            self.fire(idx, irq);
        }
    }

    fn fire(&mut self, idx: usize, irq: &mut u32) {
        let repeat = self.mode & (1 << 6) != 0;
        if self.fired && !repeat {
            return;
        }
        self.fired = true;
        if self.mode & (1 << 7) != 0 {
            self.mode ^= 1 << 10; // toggle mode
            if self.mode & (1 << 10) == 0 {
                *irq |= 1 << (4 + idx);
            }
        } else {
            // pulse mode: bit 10 drops for a few cycles; report it already restored
            *irq |= 1 << (4 + idx);
        }
    }
}

impl Timers {
    pub fn read(&mut self, off: u32) -> u32 {
        let i = ((off >> 4) & 3) as usize;
        if i > 2 {
            return 0;
        }
        let t = &mut self.t[i];
        match off & 0xF {
            0x0 => t.counter,
            0x4 => {
                let v = t.mode;
                t.mode &= !((1 << 11) | (1 << 12));
                v
            }
            0x8 => t.target,
            _ => 0,
        }
    }

    pub fn write(&mut self, off: u32, v: u32) {
        let i = ((off >> 4) & 3) as usize;
        if i > 2 {
            return;
        }
        let vb = self.in_vblank;
        let hb = self.in_hblank;
        let t = &mut self.t[i];
        match off & 0xF {
            0x0 => t.counter = v & 0xFFFF,
            0x4 => {
                t.mode = (v & 0x3FF) | (1 << 10);
                t.counter = 0;
                t.fired = false;
                t.frac = 0;
                t.paused = false;
                if t.mode & 1 != 0 {
                    let sync = (t.mode >> 1) & 3;
                    match i {
                        0 => t.paused = (sync == 0 && hb) || (sync == 2 && !hb) || sync == 3,
                        1 => t.paused = (sync == 0 && vb) || (sync == 2 && !vb) || sync == 3,
                        _ => t.paused = sync == 0 || sync == 3,
                    }
                }
            }
            0x8 => t.target = v & 0xFFFF,
            _ => {}
        }
    }

    /// Advance by `cycles` CPU cycles; `dots` GPU dot-clock ticks elapsed in the same span.
    pub fn tick(&mut self, cycles: u32, dots: u32) {
        let mut irq = self.irq;
        // timer 0: sysclk or dotclock
        let t0src = (self.t[0].mode >> 8) & 1;
        let n0 = if t0src == 1 { dots } else { cycles };
        self.t[0].add(n0, 0, &mut irq);
        // timer 1: sysclk or hblank (hblank ticks arrive via `hblank`)
        if (self.t[1].mode >> 8) & 1 == 0 {
            self.t[1].add(cycles, 1, &mut irq);
        }
        // timer 2: sysclk or sysclk/8
        if (self.t[2].mode >> 9) & 1 == 0 {
            self.t[2].add(cycles, 2, &mut irq);
        } else {
            let t = &mut self.t[2];
            let total = t.frac + cycles;
            t.frac = total & 7;
            t.add(total >> 3, 2, &mut irq);
        }
        self.irq = irq;
    }

    pub fn hblank(&mut self, on: bool) {
        self.in_hblank = on;
        let mut irq = self.irq;
        let t = &mut self.t[0];
        if t.mode & 1 != 0 {
            match (t.mode >> 1) & 3 {
                0 => t.paused = on,
                1 => {
                    if on {
                        t.counter = 0
                    }
                }
                2 => {
                    if on {
                        t.counter = 0
                    }
                    t.paused = !on;
                }
                _ => {
                    if on {
                        t.paused = false;
                        t.mode &= !1;
                    }
                }
            }
        }
        if on && (self.t[1].mode >> 8) & 1 == 1 {
            self.t[1].add(1, 1, &mut irq);
        }
        self.irq = irq;
    }

    pub fn vblank(&mut self, on: bool) {
        self.in_vblank = on;
        let t = &mut self.t[1];
        if t.mode & 1 != 0 {
            match (t.mode >> 1) & 3 {
                0 => t.paused = on,
                1 => {
                    if on {
                        t.counter = 0
                    }
                }
                2 => {
                    if on {
                        t.counter = 0
                    }
                    t.paused = !on;
                }
                _ => {
                    if on {
                        t.paused = false;
                        t.mode &= !1;
                    }
                }
            }
        }
    }
}
