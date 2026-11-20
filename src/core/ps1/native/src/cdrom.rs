//! CD-ROM controller: the register interface (index, parameter / response / data
//! FIFOs, interrupt flags), the drive (spin, seek, read, CD-DA play) and the command
//! set, reading sectors from a [`Disc`].
//!
//! Timings come from the console measurements in ps1-tests `cdrom/timing`: a first
//! response ~45k cycles after a command, Init done after ~0.48M, one sector every
//! 451,584 cycles (75/s; half that at double speed), the first sector ~0.7–1M after
//! ReadN, Pause done after ~1M while reading. Responses follow psx-spx / DuckStation.
//!
//! Not emulated yet: XA-ADPCM decoding (real-time audio sectors are skipped, as the
//! drive does when it plays them), multisession discs, subchannel other than Q.

use std::collections::VecDeque;
use std::sync::Arc;

use crate::disc::{bcd, from_bcd, msf, Disc, LEAD_IN, LEAD_OUT_TRACK};

const CPU_HZ: i32 = 33_868_800;
const SECTOR_TICKS: i32 = CPU_HZ / 75;
const ACK: i32 = 45_000;
const ACK_INIT: i32 = 72_000;
/// Pause acknowledges sooner than other commands (28k-44k cycles on hardware)
const ACK_PAUSE: i32 = 35_000;
/// gap between an acknowledged interrupt and the next queued one (as DuckStation models it)
const DELIVER_DELAY: i32 = 2_000;

const STAT_ERROR: u8 = 0x01;
const STAT_MOTOR: u8 = 0x02;
const STAT_SEEK_ERROR: u8 = 0x04;
const STAT_ID_ERROR: u8 = 0x08;
const STAT_SHELL_OPEN: u8 = 0x10;
const STAT_READING: u8 = 0x20;
const STAT_SEEKING: u8 = 0x40;
const STAT_PLAYING: u8 = 0x80;

const ERR_INVALID_ARG: u8 = 0x10;
const ERR_PARAM_COUNT: u8 = 0x20;
const ERR_INVALID_CMD: u8 = 0x40;
const ERR_NOT_READY: u8 = 0x80;

const MODE_CDDA: u8 = 0x01;
const MODE_AUTOPAUSE: u8 = 0x02;
const MODE_REPORT: u8 = 0x04;
const MODE_XA_FILTER: u8 = 0x08;
const MODE_SIZE_2340: u8 = 0x20;
const MODE_XA_ADPCM: u8 = 0x40;
const MODE_DOUBLE: u8 = 0x80;

#[derive(Clone, Copy, PartialEq, Eq, Debug, serde::Serialize, serde::Deserialize)]
enum Drive {
    Idle,
    Seeking { logical: bool, then: After },
    Reading,
    Playing,
}

#[derive(Clone, Copy, PartialEq, Eq, Debug, serde::Serialize, serde::Deserialize)]
enum After {
    Nothing,
    Read,
    Play,
}

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct Cdrom {
    index: u8,
    params: VecDeque<u8>,
    resp: VecDeque<u8>,
    int_enable: u8,
    int_flag: u8,
    /// the command being executed and the cycles until its first response
    cmd: Option<(u8, i32)>,
    cmd_params: Vec<u8>,
    /// a command's second response: (cycles, interrupt, bytes; empty = the status at that time)
    second: Option<(i32, u8, Vec<u8>)>,
    /// interrupts waiting for the CPU to acknowledge the current one
    queue: VecDeque<(u8, Vec<u8>)>,
    pub irq: bool,
    motor: bool,
    shell_open: bool,
    mode: u8,
    filter: (u8, u8),
    muted: bool,
    setloc: u32,
    setloc_pending: bool,
    /// the next sector under the head
    pos: u32,
    drive: Drive,
    drive_ticks: i32,
    sectors_read: u32,
    last_header: Option<[u8; 8]>,
    sector: Vec<u8>,
    data: VecDeque<u8>,
    /// audio volume matrix: L->L, L->R, R->R, R->L (applied), and the staged values
    vol: [u8; 4],
    vol_next: [u8; 4],
    /// CD-DA output at 44.1 kHz for the SPU
    pub audio: VecDeque<(i16, i16)>,
    /// cycles before a queued response may follow an acknowledged one
    deliver_delay: i32,
    #[serde(skip)]
    disc: Option<Arc<Disc>>,
}

impl Cdrom {
    pub fn new() -> Self {
        Cdrom {
            index: 0, params: VecDeque::new(), resp: VecDeque::new(), int_enable: 0, int_flag: 0, cmd: None,
            cmd_params: Vec::new(), second: None, queue: VecDeque::new(), irq: false, motor: false,
            shell_open: true, mode: 0, filter: (0, 0), muted: false, setloc: 0, setloc_pending: false, pos: 0,
            drive: Drive::Idle, drive_ticks: 0, sectors_read: 0, last_header: None, sector: Vec::new(),
            data: VecDeque::new(), vol: [0x80, 0, 0x80, 0], vol_next: [0x80, 0, 0x80, 0], audio: VecDeque::new(),
            deliver_delay: 0, disc: None,
        }
    }

    /// Put a disc in the drive (lid closed, motor spinning, as after the BIOS boot).
    pub fn insert(&mut self, disc: Arc<Disc>) {
        self.disc = Some(disc);
        self.shell_open = false;
        self.motor = true;
    }

    /// Re-attach the disc after a state load (it is never stored in a state).
    pub fn attach(&mut self, disc: Option<Arc<Disc>>) {
        self.disc = disc;
    }

    pub fn disc(&self) -> Option<&Arc<Disc>> {
        self.disc.as_ref()
    }

    fn stat(&self) -> u8 {
        let mut s = 0;
        if self.motor {
            s |= STAT_MOTOR;
        }
        if self.shell_open {
            s |= STAT_SHELL_OPEN;
        }
        match self.drive {
            Drive::Reading => s |= STAT_READING,
            Drive::Playing => s |= STAT_PLAYING,
            Drive::Seeking { .. } => s |= STAT_SEEKING,
            Drive::Idle => {}
        }
        s
    }

    fn ready(&self) -> bool {
        self.disc.is_some() && !self.shell_open
    }

    // ---------------------------------------------------------------- registers

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
                if !self.data.is_empty() {
                    s |= 1 << 6;
                }
                if self.cmd.is_some() {
                    s |= 1 << 7;
                }
                s
            }
            (1, _) => self.resp.pop_front().unwrap_or(0),
            (2, _) => self.data.pop_front().unwrap_or(0),
            (3, 0) | (3, 2) => self.int_enable | 0xE0,
            (3, _) => self.int_flag | 0xE0,
            _ => 0,
        }
    }

    pub fn write(&mut self, reg: u32, v: u8) {
        match (reg, self.index) {
            (0, _) => self.index = v & 3,
            (1, 0) => {
                self.cmd_params = self.params.drain(..).collect();
                self.cmd = Some((v, match v { 0x0A => ACK_INIT, 0x09 => ACK_PAUSE, _ => ACK }));
            }
            (2, 0) => {
                if self.params.len() < 16 {
                    self.params.push_back(v)
                }
            }
            (3, 0) => {
                if v & 0x80 != 0 {
                    if self.data.is_empty() {
                        self.data = self.sector.iter().copied().collect();
                    }
                } else {
                    self.data.clear();
                }
            }
            (2, 1) => {
                self.int_enable = v & 0x1F;
                self.update_irq();
            }
            (3, 1) => {
                let pending = self.int_flag & 7 != 0;
                self.int_flag &= !(v & 0x1F);
                if pending && self.int_flag & 7 == 0 {
                    // the program reads the response after the ack: the next one waits a little
                    self.deliver_delay = DELIVER_DELAY;
                }
                if v & 0x40 != 0 {
                    self.params.clear();
                }
                // the response FIFO stays readable after the ack: the next response replaces it
            }
            (2, 2) => self.vol_next[0] = v,
            (3, 2) => self.vol_next[1] = v,
            (1, 3) => self.vol_next[2] = v,
            (2, 3) => self.vol_next[3] = v,
            (3, 3) => {
                if v & 0x20 != 0 {
                    self.vol = self.vol_next;
                }
            }
            _ => {}
        }
    }

    pub fn dma_read(&mut self) -> u32 {
        let mut w = 0u32;
        for k in 0..4 {
            w |= (self.data.pop_front().unwrap_or(0) as u32) << (8 * k);
        }
        w
    }

    fn update_irq(&mut self) {
        if self.int_flag & self.int_enable & 0x1F != 0 {
            self.irq = true;
        }
    }

    fn respond(&mut self, int: u8, bytes: Vec<u8>) {
        self.queue.push_back((int, bytes));
    }

    fn error(&mut self, reason: u8) {
        let s = self.stat() | STAT_ERROR;
        self.respond(5, vec![s, reason]);
    }

    fn ack(&mut self) {
        let s = self.stat();
        self.respond(3, vec![s]);
    }

    // ---------------------------------------------------------------- commands

    fn execute(&mut self, cmd: u8) {
        let p = std::mem::take(&mut self.cmd_params);
        let need = |n: usize| p.len() >= n;
        match cmd {
            0x01 => {
                self.ack();
                if self.disc.is_some() {
                    self.shell_open = false; // the "lid was opened" bit clears once read
                }
            }
            0x02 => {
                if !need(3) {
                    return self.error(ERR_PARAM_COUNT);
                }
                let ok = |v: u8, max: u8| v & 0xF <= 9 && v < max;
                if !(ok(p[0], 0x9A) && ok(p[1], 0x60) && ok(p[2], 0x75)) {
                    return self.error(ERR_INVALID_ARG);
                }
                let abs = from_bcd(p[0]) * 4500 + from_bcd(p[1]) * 75 + from_bcd(p[2]);
                self.setloc = abs.saturating_sub(LEAD_IN);
                self.setloc_pending = true;
                self.ack();
            }
            0x03 => {
                if !self.ready() {
                    return self.error(ERR_NOT_READY);
                }
                self.ack();
                let track = p.first().map(|&t| from_bcd(t) as u8).unwrap_or(0);
                if track > 0 {
                    if let Some(t) = self.disc.as_ref().and_then(|d| d.track(track).map(|t| t.index1)) {
                        self.setloc = t;
                        self.setloc_pending = true;
                    }
                }
                if self.setloc_pending || self.drive != Drive::Playing {
                    self.begin_seek(true, After::Play);
                }
            }
            0x04 | 0x05 => self.ack(),
            0x06 | 0x1B => {
                if !self.ready() {
                    return self.error(ERR_NOT_READY);
                }
                self.ack();
                let continuing = !self.setloc_pending && self.drive == Drive::Reading;
                if !continuing {
                    self.begin_seek(true, After::Read);
                }
            }
            0x07 => {
                self.ack();
                self.motor = true;
                self.second = Some((SECTOR_TICKS, 2, Vec::new()));
            }
            0x08 => {
                let was = self.motor;
                self.ack();
                self.drive = Drive::Idle;
                self.motor = false;
                self.second = Some((if was { CPU_HZ } else { 7_000 }, 2, Vec::new()));
            }
            0x09 => {
                let busy = matches!(self.drive, Drive::Reading | Drive::Playing | Drive::Seeking { .. });
                self.ack();
                self.drive = Drive::Idle;
                // a spinning drive takes about 1M cycles to pause, a bit longer at double speed
                let t = match (busy, self.mode & MODE_DOUBLE != 0) {
                    (false, _) => 7_000,
                    (true, false) => 1_014_000,
                    (true, true) => 1_037_000,
                };
                self.second = Some((t, 2, Vec::new()));
            }
            0x0A => {
                self.ack();
                self.mode = 0;
                self.drive = Drive::Idle;
                self.motor = true;
                self.setloc_pending = false;
                self.second = Some((404_000, 2, Vec::new()));
            }
            0x0B => {
                self.muted = true;
                self.ack();
            }
            0x0C => {
                self.muted = false;
                self.ack();
            }
            0x0D => {
                if !need(2) {
                    return self.error(ERR_PARAM_COUNT);
                }
                self.filter = (p[0], p[1]);
                self.ack();
            }
            0x0E => {
                if !need(1) {
                    return self.error(ERR_PARAM_COUNT);
                }
                self.mode = p[0];
                self.ack();
            }
            0x0F => {
                let s = self.stat();
                self.respond(3, vec![s, self.mode, 0, self.filter.0, self.filter.1]);
            }
            0x10 => match self.last_header {
                Some(h) if self.ready() => self.respond(3, h.to_vec()),
                _ => self.error(ERR_NOT_READY),
            },
            0x11 => {
                let Some(d) = self.disc.clone().filter(|_| self.ready()) else {
                    return self.error(ERR_NOT_READY);
                };
                let q = d.position(self.pos);
                let (rm, rs, rf) = msf(q.relative);
                let (am, as_, af) = msf(q.absolute);
                let track = if q.track == LEAD_OUT_TRACK { LEAD_OUT_TRACK } else { bcd(q.track as u32) };
                self.respond(3, vec![track, bcd(q.index as u32), bcd(rm), bcd(rs), bcd(rf), bcd(am), bcd(as_), bcd(af)]);
            }
            0x12 => {
                self.ack();
                self.second = Some((SECTOR_TICKS * 2, 2, Vec::new()));
            }
            0x13 => {
                let Some(d) = self.disc.clone().filter(|_| self.ready()) else {
                    return self.error(ERR_NOT_READY);
                };
                let (first, last) = (d.tracks.first().map(|t| t.number).unwrap_or(1),
                                     d.tracks.last().map(|t| t.number).unwrap_or(1));
                let s = self.stat();
                self.respond(3, vec![s, bcd(first as u32), bcd(last as u32)]);
            }
            0x14 => {
                let Some(d) = self.disc.clone().filter(|_| self.ready()) else {
                    return self.error(ERR_NOT_READY);
                };
                let n = p.first().map(|&t| from_bcd(t) as u8).unwrap_or(0);
                let at = if n == 0 { Some(d.sectors()) } else { d.track(n).map(|t| t.index1) };
                match at {
                    Some(lba) => {
                        let (m, s, _) = msf(lba + LEAD_IN);
                        let st = self.stat();
                        self.respond(3, vec![st, bcd(m), bcd(s)]);
                    }
                    None => self.error(ERR_INVALID_ARG),
                }
            }
            0x15 | 0x16 => {
                if !self.ready() {
                    return self.error(ERR_NOT_READY);
                }
                self.ack();
                self.begin_seek(cmd == 0x15, After::Nothing);
            }
            0x19 => match p.first().copied().unwrap_or(0) {
                0x20 => {
                    // controller BIOS version: 1994-09-19, version C0 (as SCPH-1001 consoles)
                    self.respond(3, vec![0x94, 0x09, 0x19, 0xC0])
                }
                _ => self.ack(),
            },
            0x1A => {
                if self.disc.is_none() {
                    self.ack();
                    self.second = Some((ACK, 5, vec![STAT_ID_ERROR, 0x40, 0, 0, 0, 0, 0, 0]));
                    return;
                }
                if self.shell_open {
                    return self.error(ERR_NOT_READY);
                }
                self.ack();
                let d = self.disc.clone().unwrap();
                let s = self.stat();
                let resp = if d.is_audio(0) {
                    (5, vec![s | STAT_ID_ERROR, 0x90, 0, 0, 0, 0, 0, 0])
                } else if let Some(r) = d.region() {
                    let r = r.as_bytes();
                    (2, vec![s, 0x00, 0x20, 0x00, r[0], r[1], r[2], r[3]])
                } else {
                    (5, vec![s | STAT_ID_ERROR, 0x80, 0x20, 0x00, 0, 0, 0, 0]) // unlicensed data disc
                };
                self.second = Some((SECTOR_TICKS / 12, resp.0, resp.1));
            }
            0x1C => {
                let disc = self.disc.take();
                let (en, fl) = (self.int_enable, self.int_flag);
                *self = Cdrom::new();
                self.int_enable = en;
                self.int_flag = fl;
                if let Some(d) = disc {
                    self.insert(d);
                }
                self.ack();
            }
            0x1E => {
                if !self.ready() {
                    return self.error(ERR_NOT_READY);
                }
                self.ack();
                self.second = Some((CPU_HZ / 2, 2, Vec::new()));
            }
            _ => self.error(ERR_INVALID_CMD),
        }
    }

    fn sector_ticks(&self) -> i32 {
        if self.mode & MODE_DOUBLE != 0 { SECTOR_TICKS / 2 } else { SECTOR_TICKS }
    }

    fn begin_seek(&mut self, logical: bool, then: After) {
        let target = if self.setloc_pending { self.setloc } else { self.pos };
        self.setloc_pending = false;
        let dist = (target as i64 - self.pos as i64).unsigned_abs() as i64;
        self.setloc = target;
        self.motor = true;
        self.drive = Drive::Seeking { logical, then };
        self.drive_ticks = (500_000 + dist * 3).min(1_500_000) as i32;
    }

    fn finish_seek(&mut self, logical: bool, then: After) {
        let Some(d) = self.disc.clone() else { return };
        // seeks a little into the lead-out work; far past the end of the disc they fail
        if self.setloc > d.sectors() + 4 * 4500 {
            self.drive = Drive::Idle;
            self.motor = false;
            self.respond(5, vec![STAT_SEEK_ERROR, 0x04]);
            return;
        }
        self.pos = self.setloc;
        if logical && !d.is_audio(self.pos) {
            let raw = d.read(self.pos);
            self.last_header = Some(raw[12..20].try_into().unwrap());
        }
        match then {
            After::Nothing => {
                self.drive = Drive::Idle;
                let s = self.stat();
                self.respond(2, vec![s]);
            }
            After::Read => {
                self.drive = Drive::Reading;
                self.drive_ticks = self.sector_ticks();
            }
            After::Play => {
                self.drive = Drive::Playing;
                self.drive_ticks = self.sector_ticks();
            }
        }
    }

    fn read_sector(&mut self) {
        let Some(d) = self.disc.clone() else { return };
        let raw = d.read(self.pos);
        self.pos += 1;
        self.sectors_read += 1;
        if !d.is_audio(self.pos - 1) {
            self.last_header = Some(raw[12..20].try_into().unwrap());
            // real-time XA audio sectors go to the ADPCM decoder, not to the CPU
            let submode = raw[18];
            if self.mode & MODE_XA_ADPCM != 0 && raw[15] == 2 && submode & 0x04 != 0 {
                return;
            }
            if self.mode & MODE_XA_FILTER != 0 && raw[15] == 2 && submode & 0x04 != 0
                && (raw[16] != self.filter.0 || raw[17] != self.filter.1)
            {
                return;
            }
        } else if self.mode & MODE_CDDA == 0 {
            // ReadN on an audio track without CD-DA mode: the drive reports an error
            self.drive = Drive::Idle;
            return self.error(ERR_INVALID_CMD);
        }
        self.sector = if self.mode & MODE_SIZE_2340 != 0 { raw[12..12 + 2340].to_vec() } else { raw[24..24 + 2048].to_vec() };
        // one INT1 per sector; if the CPU hasn't taken the previous one, the newer sector replaces it
        self.queue.retain(|(i, _)| *i != 1);
        let s = self.stat();
        self.respond(1, vec![s]);
    }

    fn play_sector(&mut self) {
        let Some(d) = self.disc.clone() else { return };
        let lba = self.pos;
        let end_of_track = d.track_at(lba).map(|t| t.start + t.length).unwrap_or(0);
        if lba >= d.sectors() || (self.mode & MODE_AUTOPAUSE != 0 && lba >= end_of_track) {
            self.drive = Drive::Idle;
            let s = self.stat();
            self.respond(4, vec![s]); // DataEnd
            return;
        }
        self.pos += 1;
        let raw = d.read(lba);
        if d.is_audio(lba) {
            let [ll, lr, rr, rl] = self.vol.map(|v| v as i32);
            for f in raw.chunks_exact(4) {
                let l = i16::from_le_bytes([f[0], f[1]]) as i32;
                let r = i16::from_le_bytes([f[2], f[3]]) as i32;
                let (ol, or) = if self.muted {
                    (0, 0)
                } else {
                    (((l * ll + r * rl) >> 7).clamp(-32768, 32767), ((r * rr + l * lr) >> 7).clamp(-32768, 32767))
                };
                self.audio.push_back((ol as i16, or as i16));
            }
            while self.audio.len() > 44_100 {
                self.audio.pop_front();
            }
        }
        if self.mode & MODE_REPORT != 0 && lba % 10 == 0 {
            let q = d.position(lba);
            let (m, s, f) = msf(q.absolute);
            let st = self.stat();
            self.queue.retain(|(i, _)| *i != 1);
            self.respond(1, vec![st, bcd(q.track as u32), bcd(q.index as u32), bcd(m), bcd(s), bcd(f), 0, 0]);
        }
    }

    pub fn tick(&mut self, cycles: u32) {
        let c = cycles as i32;
        if let Some((cmd, t)) = self.cmd.as_mut() {
            *t -= c;
            if *t <= 0 {
                let cmd = *cmd;
                self.cmd = None;
                self.execute(cmd);
            }
        }
        if let Some(sec) = self.second.as_mut() {
            sec.0 -= c;
            if sec.0 <= 0 {
                let (_, int, bytes) = self.second.take().unwrap();
                let bytes = if bytes.is_empty() { vec![self.stat()] } else { bytes };
                self.respond(int, bytes);
            }
        }
        if self.drive != Drive::Idle {
            self.drive_ticks -= c;
            while self.drive_ticks <= 0 && self.drive != Drive::Idle {
                match self.drive {
                    Drive::Seeking { logical, then } => self.finish_seek(logical, then),
                    Drive::Reading => {
                        self.read_sector();
                        self.drive_ticks += self.sector_ticks();
                    }
                    Drive::Playing => {
                        self.play_sector();
                        self.drive_ticks += self.sector_ticks();
                    }
                    Drive::Idle => {}
                }
                if matches!(self.drive, Drive::Seeking { .. }) {
                    break;
                }
            }
        }
        if self.deliver_delay > 0 {
            self.deliver_delay -= c;
        }
        if self.int_flag & 7 == 0 && self.deliver_delay <= 0 {
            if let Some((int, bytes)) = self.queue.pop_front() {
                self.resp = bytes.into();
                self.int_flag = (self.int_flag & !7) | int;
                self.update_irq();
            }
        }
    }
}

impl Default for Cdrom {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::disc::build_iso;

    fn drive() -> Cdrom {
        let mut c = Cdrom::new();
        c.insert(Arc::new(Disc::from_raw_sectors(build_iso(&[("A.TXT", b"hello")], 400))));
        c.write(0, 1);
        c.write(2, 0x1F); // all interrupts on
        c.write(0, 0);
        c
    }

    /// Send a command and run until its first response; returns (interrupt, bytes, cycles).
    fn send(c: &mut Cdrom, cmd: u8, params: &[u8]) -> (u8, Vec<u8>, u32) {
        for &p in params {
            c.write(2, p);
        }
        c.write(1, cmd);
        wait(c)
    }

    fn wait(c: &mut Cdrom) -> (u8, Vec<u8>, u32) {
        let mut t = 0;
        while c.int_flag & 7 == 0 {
            c.tick(64);
            t += 64;
            assert!(t < 40_000_000, "no interrupt");
        }
        let int = c.int_flag & 7;
        let mut bytes = Vec::new();
        while c.read(0) & 0x20 != 0 {
            bytes.push(c.read(1));
        }
        c.write(0, 1);
        c.write(3, 0x1F);
        c.write(0, 0);
        (int, bytes, t)
    }

    #[test]
    fn getid_licensed_disc() {
        let mut c = drive();
        assert_eq!(send(&mut c, 0x1A, &[]).0, 3);
        assert_eq!(wait(&mut c).1, vec![0x02, 0x00, 0x20, 0x00, b'S', b'C', b'E', b'A']);
    }

    #[test]
    fn no_disc_is_not_ready() {
        let mut c = Cdrom::new();
        c.write(0, 1);
        c.write(2, 0x1F);
        c.write(0, 0);
        let (int, b, _) = send(&mut c, 0x06, &[]);
        assert_eq!((int, b[1]), (5, ERR_NOT_READY));
        assert_ne!(b[0] & STAT_SHELL_OPEN, 0);
    }

    #[test]
    fn toc_commands() {
        let mut c = drive();
        assert_eq!(send(&mut c, 0x13, &[]).1, vec![0x02, 0x01, 0x01]);
        assert_eq!(send(&mut c, 0x14, &[0x00]).1, vec![0x02, 0x00, 0x07], "lead-out at 400+150 sectors = 00:07");
        assert_eq!(send(&mut c, 0x14, &[0x01]).1, vec![0x02, 0x00, 0x02]);
        assert_eq!(send(&mut c, 0x14, &[0x05]).0, 5, "no track 5");
    }

    #[test]
    fn read_sectors_and_data_fifo() {
        let mut c = drive();
        send(&mut c, 0x02, &[0x00, 0x02, 0x24]); // LBA 24: the file A.TXT
        let (_, _, ack) = send(&mut c, 0x06, &[]);
        assert!((20_000..100_000).contains(&ack));
        let (int, st, first) = wait(&mut c);
        assert_eq!(int, 1);
        assert_eq!(st[0], STAT_MOTOR | STAT_READING);
        assert!((600_000..1_100_000).contains(&first), "first sector after {first}");
        c.write(3, 0x80); // BFRD: load the sector into the data FIFO
        assert_ne!(c.read(0) & 0x40, 0);
        let mut five = [0u8; 5];
        for b in &mut five {
            *b = c.read(2);
        }
        assert_eq!(&five, b"hello");
        let (int, _, next) = wait(&mut c);
        assert_eq!(int, 1);
        assert!((440_000..460_000).contains(&next), "75 sectors/s: {next}");
        // GetlocL: the header of the last sector (00:02:25, mode 2)
        let h = send(&mut c, 0x10, &[]).1;
        assert_eq!(&h[..4], &[0x00, 0x02, 0x25, 0x02]);
        // double speed halves the sector period
        send(&mut c, 0x0E, &[0x80]);
        wait(&mut c); // the sector already under way at the old speed
        let t = wait(&mut c).2;
        assert!((215_000..235_000).contains(&t), "150 sectors/s: {t}");
        // pause: ack, then complete about 1M cycles later
        send(&mut c, 0x09, &[]);
        while c.int_flag & 7 == 0 && c.drive != Drive::Idle {
            c.tick(64);
        }
        let (int, _, done) = wait(&mut c);
        assert_eq!(int, 2);
        assert!(done > 900_000);
    }

    #[test]
    fn getlocp_and_seek_errors() {
        let mut c = drive();
        send(&mut c, 0x02, &[0x00, 0x02, 0x16]);
        assert_eq!(send(&mut c, 0x15, &[]).0, 3);
        assert_eq!(wait(&mut c).0, 2, "SeekL completes with INT2");
        let q = send(&mut c, 0x11, &[]).1;
        assert_eq!(q, vec![0x01, 0x01, 0x00, 0x00, 0x16, 0x00, 0x02, 0x16]);
        send(&mut c, 0x02, &[0x74, 0x30, 0x00]);
        send(&mut c, 0x15, &[]);
        let (int, b, _) = wait(&mut c);
        assert_eq!((int, b[0]), (5, STAT_SEEK_ERROR));
        assert_eq!(send(&mut c, 0x02, &[0x00, 0x60, 0x00]).0, 5, "seconds must be < 60");
    }
}
