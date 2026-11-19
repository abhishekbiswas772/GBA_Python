//! CD-ROM controller: register interface, parameter/response FIFOs and the
//! interrupt handshake, answering with "no disc". Reading discs (cue/bin) comes
//! in milestone 2.

use std::collections::VecDeque;

#[derive(Clone)]
pub struct Cdrom {
    index: u8,
    params: VecDeque<u8>,
    resp: VecDeque<u8>,
    int_enable: u8,
    int_flag: u8,
    /// queued (delay in cycles, interrupt number, response bytes)
    pending: VecDeque<(i32, u8, Vec<u8>)>,
    pub irq: bool,
    stat: u8,
}

const STAT_SHELL_OPEN: u8 = 0x10;

impl Cdrom {
    pub fn new() -> Self {
        Cdrom { index: 0, params: VecDeque::new(), resp: VecDeque::new(), int_enable: 0, int_flag: 0,
                pending: VecDeque::new(), irq: false, stat: STAT_SHELL_OPEN }
    }

    pub fn read(&mut self, reg: u32) -> u8 {
        match (reg, self.index) {
            (0, _) => {
                let mut s = self.index;
                if self.params.is_empty() {
                    s |= 1 << 3;
                }
                if self.params.len() < 16 {
                    s |= 1 << 4;
                }
                if !self.resp.is_empty() {
                    s |= 1 << 5;
                }
                if !self.pending.is_empty() && self.int_flag & 7 == 0 {
                    s |= 1 << 7; // busy
                }
                s
            }
            (1, _) => self.resp.pop_front().unwrap_or(0),
            (2, _) => 0,
            (3, 0) | (3, 2) => self.int_enable | 0xE0,
            (3, _) => self.int_flag | 0xE0,
            _ => 0,
        }
    }

    pub fn write(&mut self, reg: u32, v: u8) {
        match (reg, self.index) {
            (0, _) => self.index = v & 3,
            (1, 0) => self.command(v),
            (2, 0) => {
                if self.params.len() < 16 {
                    self.params.push_back(v)
                }
            }
            (2, 1) => self.int_enable = v & 0x1F,
            (3, 1) => {
                self.int_flag &= !(v & 0x1F);
                if v & 0x40 != 0 {
                    self.params.clear();
                }
                if self.int_flag == 0 {
                    self.resp.clear();
                }
            }
            _ => {}
        }
    }

    fn command(&mut self, cmd: u8) {
        let s = self.stat;
        let ack = |d: i32, extra: Vec<u8>| (d, 3u8, extra);
        match cmd {
            0x01 => self.pending.push_back(ack(25_000, vec![s])),                 // GetStat
            0x19 => {
                let sub = self.params.front().copied().unwrap_or(0);
                let r = if sub == 0x20 { vec![0x94, 0x09, 0x19, 0xC0] } else { vec![s] };
                self.pending.push_back(ack(25_000, r));
            }
            0x1A => {
                // GetID with no disc: INT3 stat, then INT5 error
                self.pending.push_back(ack(25_000, vec![s]));
                self.pending.push_back((50_000, 5, vec![0x08, 0x40, 0, 0, 0, 0, 0, 0]));
            }
            0x0A => {
                // Init
                self.pending.push_back(ack(25_000, vec![s]));
                self.pending.push_back((100_000, 2, vec![s]));
            }
            _ => {
                // anything that needs a disc: error "no disc"
                self.pending.push_back((25_000, 5, vec![s | 1, 0x80]));
            }
        }
        self.params.clear();
    }

    pub fn tick(&mut self, cycles: u32) {
        if self.int_flag & 7 != 0 {
            return; // the CPU hasn't acknowledged the previous interrupt yet
        }
        if let Some(front) = self.pending.front_mut() {
            front.0 -= cycles as i32;
            if front.0 <= 0 {
                let (_, n, bytes) = self.pending.pop_front().unwrap();
                self.resp = bytes.into();
                self.int_flag = (self.int_flag & !7) | n;
                if self.int_flag & self.int_enable != 0 {
                    self.irq = true;
                }
            }
        }
    }

    pub fn dma_read(&mut self) -> u32 {
        0
    }
}

impl Default for Cdrom {
    fn default() -> Self {
        Self::new()
    }
}
