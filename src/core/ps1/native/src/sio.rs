//! SIO0: controller port 1 with a digital pad (SCPH-1080). Memory cards come later.
//!
//! Protocol: the host selects the port (JOY_CTRL DTR), then exchanges bytes:
//!   01 → (hi-z)   42 → 41 (ID lo)   00 → 5A (ID hi)   00 → buttons lo   00 → buttons hi
//! The pad pulls /ACK after every byte but the last, which raises IRQ7.

pub const ACK_DELAY: i32 = 500; // CPU cycles from the end of a byte to the IRQ

#[derive(Clone)]
pub struct Sio {
    pub ctrl: u16,
    pub mode: u16,
    pub baud: u16,
    stat_irq: bool,
    rx: Option<u8>,
    step: u8,
    ack_timer: i32,
    pub irq: bool,
    /// Pressed buttons, PS1 bit order: Select L3 R3 Start Up Right Down Left L2 R2 L1 R1 △ ○ ✕ □
    pub buttons: u16,
    pub connected: bool,
}

impl Sio {
    pub fn new() -> Self {
        Sio { ctrl: 0, mode: 0, baud: 0, stat_irq: false, rx: None, step: 0, ack_timer: -1, irq: false,
              buttons: 0, connected: true }
    }

    pub fn read(&mut self, off: u32, _width: u32) -> u32 {
        match off {
            0x0..=0x3 => match self.rx.take() {
                Some(b) => b as u32,
                None => 0xFF,
            },
            0x4 => {
                let mut s = 1 | 4; // TX ready 1 + TX finished
                if self.rx.is_some() {
                    s |= 2;
                }
                if self.stat_irq {
                    s |= 1 << 9;
                }
                s
            }
            0x8 => self.mode as u32,
            0xA => self.ctrl as u32,
            0xE => self.baud as u32,
            _ => 0,
        }
    }

    pub fn write(&mut self, off: u32, v: u32, _width: u32) {
        match off {
            0x0 => self.transfer(v as u8),
            0x8 => self.mode = v as u16,
            0xA => {
                if v & 0x40 != 0 {
                    // reset
                    self.ctrl = 0;
                    self.stat_irq = false;
                    self.rx = None;
                    self.step = 0;
                } else {
                    self.ctrl = (v as u16) & !0x10;
                    if v & 0x10 != 0 {
                        self.stat_irq = false; // acknowledge
                    }
                    if v & 2 == 0 {
                        self.step = 0; // deselected
                    }
                }
            }
            0xE => self.baud = v as u16,
            _ => {}
        }
    }

    fn transfer(&mut self, b: u8) {
        let selected = self.ctrl & 2 != 0;
        let port2 = self.ctrl & 0x2000 != 0;
        let (reply, ack) = if !selected || port2 || !self.connected {
            (0xFF, false)
        } else {
            match (self.step, b) {
                (0, 0x01) => {
                    self.step = 1;
                    (0xFF, true)
                }
                (1, 0x42) => {
                    self.step = 2;
                    (0x41, true)
                }
                (2, _) => {
                    self.step = 3;
                    (0x5A, true)
                }
                (3, _) => {
                    self.step = 4;
                    (!(self.buttons as u8), true)
                }
                (4, _) => {
                    self.step = 0;
                    (!((self.buttons >> 8) as u8), false)
                }
                _ => {
                    self.step = 0;
                    (0xFF, false)
                }
            }
        };
        self.rx = Some(reply);
        if ack && self.ctrl & 0x1000 != 0 {
            self.ack_timer = ACK_DELAY;
        }
    }

    pub fn tick(&mut self, cycles: u32) {
        if self.ack_timer >= 0 {
            self.ack_timer -= cycles as i32;
            if self.ack_timer < 0 {
                self.stat_irq = true;
                self.irq = true;
            }
        }
    }
}

impl Default for Sio {
    fn default() -> Self {
        Self::new()
    }
}
