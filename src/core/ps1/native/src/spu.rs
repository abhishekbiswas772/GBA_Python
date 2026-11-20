//! SPU: register file, 512 KB sound RAM and manual/DMA transfers.
//! Voices and mixing come in a later milestone (milestone 4); it outputs silence
//! at 44.1 kHz so the audio pipeline and recordings already run.

#[derive(Clone, serde::Serialize, serde::Deserialize)]
pub struct Spu {
    pub regs: Vec<u16>,
    pub ram: Vec<u8>,
    addr: u32,
}

const CTRL: usize = (0x1AA) / 2;
const STAT: usize = (0x1AE) / 2;
const TADDR: usize = (0x1A6) / 2;
const FIFO: usize = (0x1A8) / 2;

impl Spu {
    pub fn new() -> Self {
        Spu { regs: vec![0; 0x200], ram: vec![0; 512 * 1024], addr: 0 }
    }

    fn idx(p: u32) -> usize {
        ((p - 0x1F80_1C00) as usize / 2) & 0x1FF
    }

    pub fn read16(&mut self, p: u32) -> u16 {
        let i = Self::idx(p);
        match i {
            STAT => (self.regs[CTRL] & 0x3F) | (self.regs[STAT] & !0x3F),
            _ => self.regs[i],
        }
    }

    pub fn write16(&mut self, p: u32, v: u16) {
        let i = Self::idx(p);
        match i {
            TADDR => {
                self.regs[i] = v;
                self.addr = (v as u32) * 8;
            }
            FIFO => {
                // data FIFO (we write through immediately)
                let a = (self.addr & 0x7FFFE) as usize;
                self.ram[a..a + 2].copy_from_slice(&v.to_le_bytes());
                self.addr = (self.addr + 2) & 0x7FFFF;
            }
            STAT => {}
            _ => self.regs[i] = v,
        }
    }

    pub fn dma_write(&mut self, w: u32) {
        for h in [w as u16, (w >> 16) as u16] {
            let a = (self.addr & 0x7FFFE) as usize;
            self.ram[a..a + 2].copy_from_slice(&h.to_le_bytes());
            self.addr = (self.addr + 2) & 0x7FFFF;
        }
    }

    pub fn dma_read(&mut self) -> u32 {
        let mut out = 0u32;
        for k in 0..2 {
            let a = (self.addr & 0x7FFFE) as usize;
            out |= (u16::from_le_bytes([self.ram[a], self.ram[a + 1]]) as u32) << (16 * k);
            self.addr = (self.addr + 2) & 0x7FFFF;
        }
        out
    }
}

impl Default for Spu {
    fn default() -> Self {
        Self::new()
    }
}
