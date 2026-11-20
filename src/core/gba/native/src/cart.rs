//! Cartridge backup memory (`src/core/gba/cartridge.py`): save-type detection
//! from the SDK library ID strings, SRAM, Flash 64/128 KB and serial EEPROM.
//! The .sav file itself is read and written by Python.

use serde::{Deserialize, Serialize};

const SRAM_SIZE: usize = 0x8000;
const FLASH_BANK: usize = 0x10000;
const EEPROM_SMALL: usize = 0x200;
const EEPROM_LARGE: usize = 0x2000;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    None,
    Sram,
    Flash64,
    Flash128,
    Eeprom,
}

impl Kind {
    pub fn name(self) -> &'static str {
        match self {
            Kind::None => "none",
            Kind::Sram => "sram",
            Kind::Flash64 => "flash64",
            Kind::Flash128 => "flash128",
            Kind::Eeprom => "eeprom",
        }
    }
}

pub fn contains(hay: &[u8], needle: &[u8]) -> bool {
    hay.windows(needle.len()).any(|w| w == needle)
}

pub fn detect_save(rom: &[u8]) -> Kind {
    const SIGS: [(&[u8], Kind); 6] = [
        (b"EEPROM_V", Kind::Eeprom),
        (b"SRAM_F_V", Kind::Sram),
        (b"SRAM_V", Kind::Sram),
        (b"FLASH1M_V", Kind::Flash128),
        (b"FLASH512_V", Kind::Flash64),
        (b"FLASH_V", Kind::Flash64),
    ];
    for (sig, kind) in SIGS {
        if contains(rom, sig) {
            return kind;
        }
    }
    Kind::None
}

#[derive(Serialize, Deserialize)]
pub struct Sram {
    pub data: Vec<u8>,
    pub dirty: bool,
}

impl Sram {
    pub fn read8(&self, a: u32) -> u32 {
        self.data[a as usize & (SRAM_SIZE - 1)] as u32
    }
    pub fn write8(&mut self, a: u32, v: u32) {
        self.data[a as usize & (SRAM_SIZE - 1)] = v as u8;
        self.dirty = true;
    }
}

#[derive(Serialize, Deserialize)]
pub struct Flash {
    pub data: Vec<u8>,
    pub ident: [u8; 2],
    pub bank: usize,
    pub state: u32,
    pub id_mode: bool,
    pub erase_armed: bool,
    pub write_next: bool,
    pub bank_next: bool,
    pub dirty: bool,
}

impl Flash {
    pub fn new(size: usize) -> Flash {
        Flash {
            data: vec![0xFF; size],
            ident: if size > FLASH_BANK { [0x62, 0x13] } else { [0x32, 0x1B] },
            bank: 0,
            state: 0,
            id_mode: false,
            erase_armed: false,
            write_next: false,
            bank_next: false,
            dirty: false,
        }
    }

    pub fn read8(&self, a: u32) -> u32 {
        let a = (a & 0xFFFF) as usize;
        if self.id_mode && a < 2 {
            return self.ident[a] as u32;
        }
        self.data[self.bank * FLASH_BANK + a] as u32
    }

    pub fn write8(&mut self, a: u32, v: u32) {
        let a = (a & 0xFFFF) as usize;
        let v = v as u8;
        if self.write_next {
            self.data[self.bank * FLASH_BANK + a] &= v;
            self.write_next = false;
            self.dirty = true;
            return;
        }
        if self.bank_next {
            if a == 0 {
                self.bank = v as usize & (self.data.len() / FLASH_BANK - 1);
            }
            self.bank_next = false;
            return;
        }
        if self.state == 0 && a == 0x5555 && v == 0xAA {
            self.state = 1;
        } else if self.state == 1 && a == 0x2AAA && v == 0x55 {
            self.state = 2;
        } else if self.state == 2 {
            self.state = 0;
            if self.erase_armed {
                self.erase_armed = false;
                if a == 0x5555 && v == 0x10 {
                    self.data.fill(0xFF);
                    self.dirty = true;
                } else if v == 0x30 {
                    let start = self.bank * FLASH_BANK + (a & 0xF000);
                    self.data[start..start + 0x1000].fill(0xFF);
                    self.dirty = true;
                }
                return;
            }
            if a != 0x5555 {
                return;
            }
            match v {
                0x90 => self.id_mode = true,
                0xF0 => self.id_mode = false,
                0x80 => self.erase_armed = true,
                0xA0 => self.write_next = true,
                0xB0 if self.data.len() > FLASH_BANK => self.bank_next = true,
                _ => {}
            }
        } else {
            self.state = 0;
            if v == 0xF0 {
                self.id_mode = false;
            }
        }
    }
}

/// Serial EEPROM on the ROM bus (bit 0 of 16-bit accesses, normally via DMA3).
#[derive(Serialize, Deserialize)]
pub struct Eeprom {
    pub data: Vec<u8>,
    pub size: usize,
    pub bits: Vec<u8>,
    pub expect: u32,
    pub out: std::collections::VecDeque<u8>,
    pub dirty: bool,
}

impl Eeprom {
    pub fn new() -> Eeprom {
        Eeprom {
            data: vec![0xFF; EEPROM_LARGE],
            size: 0,
            bits: Vec::new(),
            expect: 0,
            out: Default::default(),
            dirty: false,
        }
    }

    fn addr_bits(&self) -> usize {
        if self.size == EEPROM_SMALL {
            6
        } else {
            14
        }
    }

    pub fn begin(&mut self, count: u32) {
        if self.size == 0 {
            if count == 9 || count == 73 {
                self.size = EEPROM_SMALL;
                self.data.truncate(EEPROM_SMALL);
            } else if count == 17 || count == 81 {
                self.size = EEPROM_LARGE;
            }
        }
        self.expect = count;
        self.bits.clear();
    }

    fn addr(&self, ab: usize) -> usize {
        let mut a = 0usize;
        for &b in &self.bits[2..2 + ab] {
            a = (a << 1) | b as usize;
        }
        a & (self.data.len() / 8 - 1)
    }

    pub fn write_bit(&mut self, v: u32) {
        self.bits.push((v & 1) as u8);
        let n = self.bits.len();
        let ab = self.addr_bits();
        if n < 2 {
            return;
        }
        let cmd = (self.bits[0] << 1) | self.bits[1];
        if cmd == 3 && n == 2 + ab + 1 {
            let addr = self.addr(ab);
            self.out = [0u8, 0, 0, 0].into_iter().collect();
            let end = (addr * 8 + 8).min(self.data.len());
            let block: Vec<u8> = self.data[(addr * 8).min(end)..end].to_vec();
            for byte in block {
                for s in (0..8).rev() {
                    self.out.push_back((byte >> s) & 1);
                }
            }
            self.bits.clear();
        } else if cmd == 2 && n == 2 + ab + 64 + 1 {
            let addr = self.addr(ab);
            for k in 0..8 {
                let mut byte = 0u8;
                for &bit in &self.bits[2 + ab + k * 8..2 + ab + k * 8 + 8] {
                    byte = (byte << 1) | bit;
                }
                if addr * 8 + k < self.data.len() {
                    self.data[addr * 8 + k] = byte;
                }
            }
            self.dirty = true;
            self.out.clear();
            self.bits.clear();
        } else if cmd == 0 || cmd == 1 {
            self.bits.clear();
        }
    }

    pub fn read_bit(&mut self) -> u32 {
        self.out.pop_front().map(|b| b as u32).unwrap_or(1)
    }
}

impl Default for Eeprom {
    fn default() -> Self {
        Eeprom::new()
    }
}

#[derive(Serialize, Deserialize)]
pub enum Save {
    None,
    Sram(Sram),
    Flash(Flash),
    Eeprom(Eeprom),
}

impl Save {
    pub fn make(kind: Kind) -> Save {
        match kind {
            Kind::None => Save::None,
            Kind::Sram => Save::Sram(Sram { data: vec![0xFF; SRAM_SIZE], dirty: false }),
            Kind::Flash64 => Save::Flash(Flash::new(FLASH_BANK)),
            Kind::Flash128 => Save::Flash(Flash::new(FLASH_BANK * 2)),
            Kind::Eeprom => Save::Eeprom(Eeprom::new()),
        }
    }

    pub fn kind(&self) -> Kind {
        match self {
            Save::None => Kind::None,
            Save::Sram(_) => Kind::Sram,
            Save::Flash(f) => {
                if f.data.len() > FLASH_BANK {
                    Kind::Flash128
                } else {
                    Kind::Flash64
                }
            }
            Save::Eeprom(_) => Kind::Eeprom,
        }
    }

    /// Contents of a .sav file (same rules as `Cartridge._load`).
    pub fn load(&mut self, d: &[u8]) {
        match self {
            Save::None => {}
            Save::Eeprom(e) => {
                let take = |n: usize| {
                    let mut v = d[..d.len().min(n)].to_vec();
                    v.resize(n, 0xFF);
                    v
                };
                if d.len() <= EEPROM_SMALL {
                    e.size = EEPROM_SMALL;
                    e.data = take(EEPROM_SMALL);
                } else {
                    e.size = EEPROM_LARGE;
                    e.data = take(EEPROM_LARGE);
                }
            }
            Save::Sram(Sram { data, .. }) | Save::Flash(Flash { data, .. }) => {
                let n = d.len().min(data.len());
                data[..n].copy_from_slice(&d[..n]);
            }
        }
    }

    pub fn data(&self) -> Option<&[u8]> {
        match self {
            Save::None => None,
            Save::Sram(s) => Some(&s.data),
            Save::Flash(f) => Some(&f.data),
            Save::Eeprom(e) => Some(&e.data),
        }
    }

    pub fn dirty(&self) -> bool {
        match self {
            Save::None => false,
            Save::Sram(s) => s.dirty,
            Save::Flash(f) => f.dirty,
            Save::Eeprom(e) => e.dirty,
        }
    }

    pub fn clear_dirty(&mut self) {
        match self {
            Save::None => {}
            Save::Sram(s) => s.dirty = false,
            Save::Flash(f) => f.dirty = false,
            Save::Eeprom(e) => e.dirty = false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn save_detection() {
        let mut rom = vec![0u8; 64];
        rom.extend_from_slice(b"EEPROM_V124");
        assert_eq!(detect_save(&rom), Kind::Eeprom);
        assert_eq!(detect_save(b"FLASH1M_V103"), Kind::Flash128);
        assert_eq!(detect_save(b"SRAM_V113"), Kind::Sram);
        assert_eq!(detect_save(b"nothing here"), Kind::None);
    }

    fn bits(s: &str) -> Vec<u32> {
        s.bytes().map(|c| (c - b'0') as u32).collect()
    }

    #[test]
    fn eeprom_write_then_read_large() {
        let mut e = Eeprom::new();
        let addr = format!("{:014b}", 5);
        let payload: String = b"ZELDA!MC".iter().map(|b| format!("{b:08b}")).collect();
        let req = bits(&format!("10{addr}{payload}0"));
        e.begin(req.len() as u32);
        for &b in &req {
            e.write_bit(b);
        }
        assert!(e.size == 0x2000 && e.dirty);
        let req = bits(&format!("11{addr}0"));
        e.begin(req.len() as u32);
        for &b in &req {
            e.write_bit(b);
        }
        let out: Vec<u32> = (0..68).map(|_| e.read_bit()).collect();
        assert_eq!(&out[..4], &[0, 0, 0, 0]);
        let data: Vec<u8> = (0..8)
            .map(|k| out[4 + 8 * k..12 + 8 * k].iter().fold(0u8, |a, &b| (a << 1) | b as u8))
            .collect();
        assert_eq!(&data, b"ZELDA!MC");
        assert_eq!(e.read_bit(), 1); // ready afterwards
    }

    #[test]
    fn eeprom_small_size_from_dma_length() {
        let mut e = Eeprom::new();
        e.begin(9);
        assert_eq!(e.size, 0x200);
        assert_eq!(e.data.len(), 0x200);
    }

    #[test]
    fn flash_id_erase_and_bank_switch() {
        let mut f = Flash::new(0x20000);
        let cmd = |f: &mut Flash, v: u32| {
            f.write8(0x5555, 0xAA);
            f.write8(0x2AAA, 0x55);
            f.write8(0x5555, v);
        };
        cmd(&mut f, 0x90);
        assert_eq!((f.read8(0), f.read8(1)), (0x62, 0x13));
        cmd(&mut f, 0xF0);
        cmd(&mut f, 0xA0);
        f.write8(0x1234, 0x5A);
        assert_eq!(f.read8(0x1234), 0x5A);
        cmd(&mut f, 0xB0);
        f.write8(0x0000, 1);
        assert_eq!(f.read8(0x1234), 0xFF); // bank 1 still erased
        cmd(&mut f, 0x80);
        cmd(&mut f, 0x10); // chip erase
        f.bank = 0;
        assert_eq!(f.read8(0x1234), 0xFF);
    }

    #[test]
    fn sav_load_rules() {
        let mut s = Save::make(Kind::Eeprom);
        s.load(&[1, 2, 3]);
        assert_eq!(s.data().unwrap().len(), 0x200);
        let mut s = Save::make(Kind::Sram);
        s.load(&[7; 10]);
        assert_eq!(&s.data().unwrap()[..11], &[7, 7, 7, 7, 7, 7, 7, 7, 7, 7, 0xFF]);
    }
}
