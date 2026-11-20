//! Cartridge GPIO port + Seiko S-3511 real-time clock (`src/core/gba/rtc.py`).
//!
//! Date and time come from the host clock: UTC from the system plus the local
//! UTC offset Python hands over (`utc_offset`), or a fixed time set for tests.

use serde::{Deserialize, Serialize};

const GPIO_DATA: u32 = 0x0C4;
const GPIO_DIRECTION: u32 = 0x0C6;
const GPIO_CONTROL: u32 = 0x0C8;
const RTC_MAGIC: u32 = 0x06;
const CMD_RESET: u32 = 0;
const CMD_DATETIME: u32 = 2;
const CMD_CONTROL: u32 = 4;
const CMD_TIME: u32 = 6;
const PARAM_BYTES: [i32; 8] = [0, 0, 7, 0, 1, 0, 3, 0];
const CONTROL_24H: u32 = 0x40;

fn bcd(v: u32) -> u32 {
    ((v / 10) << 4) | (v % 10)
}

/// (year, month 1-12, day, weekday Mon=0, hour, minute, second) like `time.struct_time`.
pub type Clock = [i64; 7];

fn civil(secs: i64) -> Clock {
    let days = secs.div_euclid(86400);
    let rem = secs.rem_euclid(86400);
    // days since 1970-01-01 -> civil date (Howard Hinnant's algorithm)
    let z = days + 719468;
    let era = z.div_euclid(146097);
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + if m <= 2 { 1 } else { 0 };
    let wday = (days + 3).rem_euclid(7); // 1970-01-01 was a Thursday (Mon=0)
    [y, m, d, wday, rem / 3600, rem / 60 % 60, rem % 60]
}

#[derive(Serialize, Deserialize, Clone)]
pub struct Rtc {
    pub pins: u32,
    pub direction: u32,
    pub readable: bool,
    pub step: u32,
    pub bits: u32,
    pub nbits: u32,
    pub command: u32,
    pub active: bool,
    pub remaining: i32,
    pub control: u32,
    pub regs: [u32; 7],
    pub utc_offset: i64,
    pub fixed: Option<Clock>,
}

impl Rtc {
    pub fn new() -> Rtc {
        Rtc {
            pins: 0,
            direction: 0,
            readable: false,
            step: 0,
            bits: 0,
            nbits: 0,
            command: 0,
            active: false,
            remaining: 0,
            control: CONTROL_24H,
            regs: [0; 7],
            utc_offset: 0,
            fixed: None,
        }
    }

    pub fn read16(&self, off: u32) -> Option<u32> {
        if !self.readable {
            return None;
        }
        match off {
            GPIO_DATA => Some(self.pins),
            GPIO_DIRECTION => Some(self.direction),
            GPIO_CONTROL => Some(1),
            _ => None,
        }
    }

    pub fn write16(&mut self, off: u32, v: u32) {
        match off {
            GPIO_DATA => {
                let d = self.direction;
                self.pins = ((self.pins & !d) | (v & d)) & 0xF;
                self.clock_pins();
            }
            GPIO_DIRECTION => self.direction = v & 0xF,
            GPIO_CONTROL => self.readable = v & 1 != 0,
            _ => {}
        }
    }

    fn drive(&mut self, value: u32) {
        let d = self.direction;
        self.pins = ((self.pins & d) | (value & !d)) & 0xF;
    }

    fn clock_pins(&mut self) {
        let p = self.pins & 5;
        if self.step == 0 {
            if p == 1 {
                self.step = 1;
            }
        } else if self.step == 1 {
            if p == 5 {
                self.step = 2;
            } else if p != 1 {
                self.step = 0;
            }
        } else if self.pins & 1 == 0 {
            if !(self.active && self.command & 0x80 != 0) {
                let bit = (self.pins >> 1) & 1;
                self.bits = (self.bits & !(1 << self.nbits)) | (bit << self.nbits);
            }
        } else if self.pins & 4 != 0 {
            if self.active && self.command & 0x80 != 0 {
                let b = self.output_bit();
                self.drive(5 | (b << 1));
                self.nbits += 1;
                if self.nbits == 8 {
                    self.nbits = 0;
                    self.remaining -= 1;
                    if self.remaining <= 0 {
                        self.active = false;
                        self.command = 0;
                    }
                }
            } else {
                self.nbits += 1;
                if self.nbits == 8 {
                    self.byte();
                }
            }
        } else {
            self.nbits = 0;
            self.remaining = 0;
            self.active = false;
            self.command = 0;
            self.step = self.pins & 1;
            self.drive(1);
        }
    }

    fn byte(&mut self) {
        let b = self.bits & 0xFF;
        if !self.active {
            if b & 0x0F == RTC_MAGIC {
                self.command = b;
                let reg = (b >> 4) & 7;
                self.remaining = PARAM_BYTES[reg as usize];
                self.active = self.remaining > 0;
                if reg == CMD_RESET {
                    self.control = 0;
                } else if reg == CMD_DATETIME || reg == CMD_TIME {
                    self.latch_clock();
                }
            }
        } else {
            if (self.command >> 4) & 7 == CMD_CONTROL {
                self.control = b;
            }
            self.remaining -= 1;
            if self.remaining <= 0 {
                self.active = false;
                self.command = 0;
            }
        }
        self.bits = 0;
        self.nbits = 0;
    }

    fn output_bit(&self) -> u32 {
        let reg = (self.command >> 4) & 7;
        let byte = if reg == CMD_CONTROL {
            self.control
        } else if reg == CMD_DATETIME || reg == CMD_TIME {
            let i = 7 - self.remaining;
            if (0..7).contains(&i) {
                self.regs[i as usize]
            } else {
                0
            }
        } else {
            0
        };
        (byte >> self.nbits) & 1
    }

    fn now(&self) -> Clock {
        if let Some(c) = self.fixed {
            return c;
        }
        let secs = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        civil(secs + self.utc_offset)
    }

    fn latch_clock(&mut self) {
        let t = self.now();
        let hour = t[4] as u32;
        let h = if self.control & CONTROL_24H != 0 {
            bcd(hour)
        } else {
            bcd(hour % 12) | if hour >= 12 { 0x80 } else { 0 }
        };
        self.regs = [
            bcd((t[0] % 100) as u32),
            bcd(t[1] as u32),
            bcd(t[2] as u32),
            ((t[3] + 1) % 7) as u32,
            h,
            bcd(t[5] as u32),
            bcd(t[6] as u32),
        ];
    }
}

impl Default for Rtc {
    fn default() -> Self {
        Rtc::new()
    }
}
