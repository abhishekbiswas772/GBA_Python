//! Memory map and interconnect: RAM, BIOS, scratchpad, I/O, DMA, IRQ controller.

use crate::cdrom::Cdrom;
use crate::gpu::Gpu;
use crate::sio::Sio;
use crate::spu::Spu;
use crate::timers::Timers;

pub const RAM_SIZE: usize = 2 * 1024 * 1024;
pub const BIOS_SIZE: usize = 512 * 1024;
const SPU_DMA_CYCLES_PER_WORD: u32 = 48;

pub mod irq {
    pub const VBLANK: u32 = 1 << 0;
    pub const GPU: u32 = 1 << 1;
    pub const CDROM: u32 = 1 << 2;
    pub const DMA: u32 = 1 << 3;
    pub const TIMER0: u32 = 1 << 4;
    pub const PAD: u32 = 1 << 7;
    pub const SPU: u32 = 1 << 9;
}

#[derive(Clone, Copy, Default, serde::Serialize, serde::Deserialize)]
pub struct DmaChan {
    pub madr: u32,
    pub bcr: u32,
    pub chcr: u32,
}

#[derive(serde::Serialize, serde::Deserialize)]
pub struct Bus {
    pub ram: Vec<u8>,
    #[serde(skip)] // passed in again on load (never stored in a state)
    pub bios: Vec<u8>,
    pub scratch: Vec<u8>,
    pub memctrl: [u32; 9],
    pub ram_size: u32,
    pub cache_ctrl: u32,
    pub i_stat: u32,
    pub i_mask: u32,
    pub dma: [DmaChan; 7],
    pub dpcr: u32,
    pub dicr: u32,
    pub gpu: Gpu,
    pub timers: Timers,
    pub spu: Spu,
    pub cdrom: Cdrom,
    pub sio: Sio,
    pub mdec: crate::mdec::Mdec,
    /// Text written to the TTY (DUART at 0x1F802023, or BIOS putchar).
    pub tty: Vec<u8>,
    pub post: u8,
    stall: u32,
    #[serde(skip)]
    dma_pending_mdec_out: bool,
    /// Cycles until a running block-mode SPU DMA reports completion (the CPU runs meanwhile).
    dma_busy: [u32; 7],
    /// CPU cycles not yet applied to the timers.
    pub timer_lag: u32,
    timer_dot_frac: u32,
    timer_video_frac: u32,
}

#[inline(always)]
fn phys(addr: u32) -> u32 {
    addr & 0x1FFF_FFFF
}

impl Bus {
    pub fn new(bios: Vec<u8>) -> Self {
        let mut b = bios;
        b.resize(BIOS_SIZE, 0);
        Bus {
            ram: vec![0; RAM_SIZE],
            bios: b,
            scratch: vec![0; 1024],
            memctrl: [0; 9],
            ram_size: 0x0000_0B88,
            cache_ctrl: 0,
            i_stat: 0,
            i_mask: 0,
            dma: [DmaChan::default(); 7],
            dpcr: 0x0765_4321,
            dicr: 0,
            gpu: Gpu::new(),
            timers: Timers::default(),
            spu: Spu::new(),
            cdrom: Cdrom::new(),
            sio: Sio::new(),
            mdec: crate::mdec::Mdec::default(),
            tty: Vec::new(),
            post: 0,
            stall: 0,
            dma_pending_mdec_out: false,
            dma_busy: [0; 7],
            timer_lag: 0,
            timer_dot_frac: 0,
            timer_video_frac: 0,
        }
    }

    #[inline(always)]
    pub fn irq_line(&self) -> bool {
        self.i_stat & self.i_mask != 0
    }

    #[inline(always)]
    pub fn raise(&mut self, bits: u32) {
        self.i_stat |= bits;
    }

    #[inline(always)]
    pub fn take_stall(&mut self) -> u32 {
        std::mem::take(&mut self.stall)
    }

    // ------------------------------------------------------------ fast paths
    /// Instruction fetch. None = bus error (no code runs from the scratchpad or I/O).
    #[inline(always)]
    pub fn fetch(&mut self, addr: u32) -> Option<u32> {
        let p = phys(addr);
        if p < 0x0080_0000 {
            let o = (p as usize) & (RAM_SIZE - 1);
            Some(u32::from_le_bytes([self.ram[o], self.ram[o + 1], self.ram[o + 2], self.ram[o + 3]]))
        } else if (0x1FC0_0000..0x1FC8_0000).contains(&p) {
            let o = (p - 0x1FC0_0000) as usize;
            Some(u32::from_le_bytes([self.bios[o], self.bios[o + 1], self.bios[o + 2], self.bios[o + 3]]))
        } else if (0x1F80_0000..=0x1F80_03FF).contains(&p)
            || (0x1F80_1070..=0x1F80_1077).contains(&p)
            || (0x1F80_1820..=0x1F80_182F).contains(&p)
        {
            None // the scratchpad, IRQ and MDEC ports can't feed the instruction bus
        } else {
            Some(self.read32(addr))
        }
    }

    /// Bring the timers up to the current cycle (before the CPU looks at them).
    pub fn sync_timers(&mut self) {
        let c = std::mem::take(&mut self.timer_lag);
        if c > 0 {
            // CPU cycles -> video clocks (x11/7) -> dots (/divider), carrying remainders
            let total = self.timer_dot_frac + c * 11;
            let video = total / 7;
            self.timer_dot_frac = total % 7;
            let div = self.gpu.dot_divider();
            let v = self.timer_video_frac + video;
            let dots = v / div;
            self.timer_video_frac = v % div;
            self.timers.tick(c, dots);
        }
    }

    pub fn read32(&mut self, addr: u32) -> u32 {
        if addr >= 0xFFFE_0000 {
            return if addr == 0xFFFE_0130 { self.cache_ctrl } else { 0 };
        }
        let p = phys(addr);
        match p {
            0x0000_0000..=0x007F_FFFF => {
                let o = (p as usize) & (RAM_SIZE - 1);
                u32::from_le_bytes([self.ram[o], self.ram[o + 1], self.ram[o + 2], self.ram[o + 3]])
            }
            0x1F80_0000..=0x1F80_03FF => {
                let o = (p & 0x3FF) as usize;
                u32::from_le_bytes([self.scratch[o], self.scratch[o + 1], self.scratch[o + 2], self.scratch[o + 3]])
            }
            0x1FC0_0000..=0x1FC7_FFFF => {
                let o = (p - 0x1FC0_0000) as usize;
                u32::from_le_bytes([self.bios[o], self.bios[o + 1], self.bios[o + 2], self.bios[o + 3]])
            }
            0x1F80_1000..=0x1F80_2FFF => self.io_read(p, 4),
            0x1F00_0000..=0x1F7F_FFFF => 0xFFFF_FFFF,
            _ => 0,
        }
    }

    pub fn read16(&mut self, addr: u32) -> u16 {
        let p = phys(addr);
        match p {
            0x0000_0000..=0x007F_FFFF => {
                let o = (p as usize) & (RAM_SIZE - 1);
                u16::from_le_bytes([self.ram[o], self.ram[o + 1]])
            }
            0x1F80_0000..=0x1F80_03FF => {
                let o = (p & 0x3FF) as usize;
                u16::from_le_bytes([self.scratch[o], self.scratch[o + 1]])
            }
            0x1FC0_0000..=0x1FC7_FFFF => {
                let o = (p - 0x1FC0_0000) as usize;
                u16::from_le_bytes([self.bios[o], self.bios[o + 1]])
            }
            0x1F80_1000..=0x1F80_2FFF => self.io_read(p, 2) as u16,
            0x1F00_0000..=0x1F7F_FFFF => 0xFFFF,
            _ => 0,
        }
    }

    pub fn read8(&mut self, addr: u32) -> u8 {
        let p = phys(addr);
        match p {
            0x0000_0000..=0x007F_FFFF => self.ram[(p as usize) & (RAM_SIZE - 1)],
            0x1F80_0000..=0x1F80_03FF => self.scratch[(p & 0x3FF) as usize],
            0x1FC0_0000..=0x1FC7_FFFF => self.bios[(p - 0x1FC0_0000) as usize],
            0x1F80_1000..=0x1F80_2FFF => self.io_read(p, 1) as u8,
            0x1F00_0000..=0x1F7F_FFFF => 0xFF,
            _ => 0,
        }
    }

    pub fn write32(&mut self, addr: u32, v: u32) {
        if addr >= 0xFFFE_0000 {
            if addr == 0xFFFE_0130 {
                self.cache_ctrl = v;
            }
            return;
        }
        let p = phys(addr);
        match p {
            0x0000_0000..=0x007F_FFFF => {
                let o = (p as usize) & (RAM_SIZE - 1);
                self.ram[o..o + 4].copy_from_slice(&v.to_le_bytes());
            }
            0x1F80_0000..=0x1F80_03FF => {
                let o = (p & 0x3FF) as usize;
                self.scratch[o..o + 4].copy_from_slice(&v.to_le_bytes());
            }
            0x1F80_1000..=0x1F80_2FFF => self.io_write(p, v, 4),
            _ => {}
        }
    }

    pub fn write16(&mut self, addr: u32, v: u16) {
        let p = phys(addr);
        match p {
            0x0000_0000..=0x007F_FFFF => {
                let o = (p as usize) & (RAM_SIZE - 1);
                self.ram[o..o + 2].copy_from_slice(&v.to_le_bytes());
            }
            0x1F80_0000..=0x1F80_03FF => {
                let o = (p & 0x3FF) as usize;
                self.scratch[o..o + 2].copy_from_slice(&v.to_le_bytes());
            }
            0x1F80_1000..=0x1F80_2FFF => self.io_write(p, v as u32, 2),
            _ => {}
        }
    }

    pub fn write8(&mut self, addr: u32, v: u8) {
        let p = phys(addr);
        match p {
            0x0000_0000..=0x007F_FFFF => self.ram[(p as usize) & (RAM_SIZE - 1)] = v,
            0x1F80_0000..=0x1F80_03FF => self.scratch[(p & 0x3FF) as usize] = v,
            0x1F80_1000..=0x1F80_2FFF => self.io_write(p, v as u32, 1),
            _ => {}
        }
    }

    // ------------------------------------------------------------ I/O
    fn io_read(&mut self, p: u32, width: u32) -> u32 {
        // sub-word reads of 32-bit registers return the shifted register
        let al = p & !3;
        let sh = (p & 3) * 8;
        match p {
            0x1F80_1040..=0x1F80_104F => return self.sio.read(p - 0x1F80_1040, width),
            0x1F80_1800..=0x1F80_1803 => return self.cdrom.read(p & 3) as u32,
            0x1F80_1C00..=0x1F80_1FFF => {
                let v = self.spu.read16(p & !1) as u32;
                return if width == 4 { v | ((self.spu.read16((p & !1) + 2) as u32) << 16) } else if p & 1 != 0 { v >> 8 } else { v };
            }
            0x1F80_2000..=0x1F80_2FFF => {
                return match p {
                    0x1F80_2021 => 0x04, // DUART: TX ready
                    _ => if width == 4 { 0xFFFF_FFFF } else if width == 2 { 0xFFFF } else { 0xFF },
                };
            }
            _ => {}
        }
        let v = match al {
            0x1F80_1000..=0x1F80_1020 => self.memctrl[((al - 0x1F80_1000) / 4) as usize],
            0x1F80_1060 => self.ram_size,
            0x1F80_1070 => self.i_stat,
            0x1F80_1074 => self.i_mask,
            0x1F80_1080..=0x1F80_10EF => {
                let ch = ((al - 0x1F80_1080) >> 4) as usize;
                match al & 0xF {
                    0x0 => self.dma[ch].madr,
                    0x4 => self.dma[ch].bcr,
                    _ => self.dma[ch].chcr,
                }
            }
            0x1F80_10F0 => self.dpcr,
            0x1F80_10F4 => self.dicr,
            0x1F80_1100..=0x1F80_112F => {
                self.sync_timers();
                self.timers.read(al - 0x1F80_1100)
            }
            0x1F80_1810 => self.gpu.read(),
            0x1F80_1814 => self.gpu.status(),
            0x1F80_1820 => self.mdec.read(),
            0x1F80_1824 => self.mdec.status(),
            _ => 0,
        };
        v >> sh
    }

    fn io_write(&mut self, p: u32, v: u32, width: u32) {
        match p {
            0x1F80_1040..=0x1F80_104F => {
                self.sio.write(p - 0x1F80_1040, v, width);
                return;
            }
            0x1F80_1800..=0x1F80_1803 => {
                self.cdrom.write(p & 3, v as u8);
                return;
            }
            0x1F80_1C00..=0x1F80_1FFF => {
                if width == 4 {
                    self.spu.write16(p, v as u16);
                    self.spu.write16(p + 2, (v >> 16) as u16);
                } else {
                    self.spu.write16(p & !1, v as u16);
                }
                return;
            }
            0x1F80_2000..=0x1F80_2FFF => {
                match p {
                    0x1F80_2023 => self.tty.push(v as u8),
                    0x1F80_2041 => self.post = v as u8,
                    _ => {}
                }
                return;
            }
            _ => {}
        }
        let al = p & !3;
        let v = if width < 4 { v << ((p & 3) * 8) } else { v };
        match al {
            0x1F80_1000..=0x1F80_1020 => self.memctrl[((al - 0x1F80_1000) / 4) as usize] = v,
            0x1F80_1060 => self.ram_size = v,
            0x1F80_1070 => self.i_stat &= v & 0x7FF,
            0x1F80_1074 => self.i_mask = v & 0x7FF,
            0x1F80_1080..=0x1F80_10EF => {
                let ch = ((al - 0x1F80_1080) >> 4) as usize;
                match al & 0xF {
                    0x0 => self.dma[ch].madr = v & 0x00FF_FFFF,
                    0x4 => self.dma[ch].bcr = v,
                    _ => {
                        self.dma[ch].chcr = if ch == 6 {
                            (v & 0x5100_0000) | 0x0000_0002
                        } else {
                            v & 0x7177_0703
                        };
                        self.dma_try(ch);
                    }
                }
            }
            0x1F80_10F0 => self.dpcr = v,
            0x1F80_10F4 => {
                let flags = (self.dicr & 0x7F00_0000) & !(v & 0x7F00_0000);
                self.dicr = flags | (v & 0x00FF_803F);
                self.dma_update_irq();
            }
            0x1F80_1100..=0x1F80_112F => {
                self.sync_timers();
                self.timers.write(al - 0x1F80_1100, v)
            }
            0x1F80_1810 => {
                self.gpu.gp0(v);
                self.collect_gpu_irq();
            }
            0x1F80_1814 => self.gpu.gp1(v),
            0x1F80_1820 => {
                self.mdec.write(v);
                self.dma_try(1);
            }
            0x1F80_1824 => self.mdec.control(v),
            _ => {}
        }
    }

    pub fn collect_gpu_irq(&mut self) {
        if std::mem::take(&mut self.gpu.irq_pending) {
            self.raise(irq::GPU);
        }
    }

    // ------------------------------------------------------------ DMA
    fn dma_update_irq(&mut self) {
        let d = self.dicr;
        let master = (d & (1 << 15)) != 0 || ((d & (1 << 23)) != 0 && ((d >> 24) & (d >> 16) & 0x7F) != 0);
        let was = d & (1 << 31) != 0;
        if master {
            self.dicr |= 1 << 31;
            if !was {
                self.raise(irq::DMA);
            }
        } else {
            self.dicr &= !(1 << 31);
        }
    }

    /// After DMA0 fed the decoder, a waiting DMA1 can run (done by the caller, outside dma_block).
    fn dma_try(&mut self, ch: usize) {
        self.dma_try_one(ch);
        if std::mem::take(&mut self.dma_pending_mdec_out) {
            self.dma_try_one(1);
        }
    }

    fn dma_try_one(&mut self, ch: usize) {
        let c = self.dma[ch].chcr;
        let enabled = (self.dpcr >> (ch * 4 + 3)) & 1 != 0;
        let sync = (c >> 9) & 3;
        // Sync mode 0 needs the manual trigger (bit 28) unless the device itself requests DMA:
        // the SPU in a DMA transfer mode does (seen on hardware, ps1-tests PR #2).
        let request = ch == 4 && self.spu.dma_request();
        let start = c & (1 << 24) != 0 && (sync != 0 || c & (1 << 28) != 0 || request);
        if !(start && enabled) || self.dma_busy[ch] > 0 {
            return;
        }
        if ch == 1 {
            // MDECout waits for the decoder: the whole transfer, or whatever an idle decoder holds
            let have = self.mdec.out.len() as u32;
            if have < self.dma_words(ch) && !(self.mdec.idle() && have > 0) {
                return;
            }
        }
        let words = self.dma_words(ch);
        if sync == 2 {
            self.dma_linked(ch);
        } else {
            self.dma_block(ch);
        }
        if ch == 4 && sync == 1 {
            // SPU block DMA goes one block per SPU request: the data is in place at once, but
            // the channel stays busy (CPU running) for the transfer time. Real consoles take
            // ~16 cycles per byte here (ps1-tests spu/memory-transfer accepts 10%..110% of that).
            self.dma_busy[ch] = words.max(1) * SPU_DMA_CYCLES_PER_WORD;
            self.spu.transfer_busy = true;
            return;
        }
        self.dma_finish(ch);
    }

    /// Advance running DMA transfers by `cycles`.
    pub fn tick_dma(&mut self, cycles: u32) {
        for ch in 0..7 {
            if self.dma_busy[ch] > 0 {
                self.dma_busy[ch] = self.dma_busy[ch].saturating_sub(cycles);
                if self.dma_busy[ch] == 0 {
                    self.dma_finish(ch);
                }
            }
        }
    }

    fn dma_finish(&mut self, ch: usize) {
        if ch == 4 {
            self.spu.transfer_busy = false;
        }
        let ch_c = &mut self.dma[ch];
        ch_c.chcr &= !((1 << 24) | (1 << 28));
        if self.dicr & (1 << (16 + ch)) != 0 {
            self.dicr |= 1 << (24 + ch);
        }
        self.dma_update_irq();
    }

    fn dma_words(&self, ch: usize) -> u32 {
        let DmaChan { bcr, chcr, .. } = self.dma[ch];
        if (chcr >> 9) & 3 == 0 {
            let n = bcr & 0xFFFF;
            if n == 0 { 0x1_0000 } else { n }
        } else {
            let bs = bcr & 0xFFFF;
            let bc = bcr >> 16;
            (if bs == 0 { 0x1_0000 } else { bs }) * (if bc == 0 { 0x1_0000 } else { bc })
        }
    }

    fn dma_block(&mut self, ch: usize) {
        let DmaChan { madr, chcr, .. } = self.dma[ch];
        let sync = (chcr >> 9) & 3;
        let mut words = self.dma_words(ch);
        if ch == 1 {
            words = words.min(self.mdec.out.len() as u32);
        }
        let step: u32 = if chcr & 2 != 0 { (-4i32) as u32 } else { 4 };
        let from_ram = chcr & 1 != 0;
        let mut addr = madr;
        for n in 0..words {
            let a = (addr & 0x1F_FFFC) as usize;
            if from_ram {
                let w = u32::from_le_bytes([self.ram[a], self.ram[a + 1], self.ram[a + 2], self.ram[a + 3]]);
                match ch {
                    2 => self.gpu.gp0(w),
                    4 => self.spu.dma_write(w),
                    0 => self.mdec.write(w),
                    _ => {}
                }
            } else {
                let w = match ch {
                    6 => {
                        if n == words - 1 { 0x00FF_FFFF } else { addr.wrapping_sub(4) & 0x1F_FFFF }
                    }
                    2 => self.gpu.read(),
                    1 => self.mdec.read_dma(),
                    3 => self.cdrom.dma_read(),
                    4 => self.spu.dma_read(),
                    _ => 0,
                };
                self.ram[a..a + 4].copy_from_slice(&w.to_le_bytes());
            }
            addr = addr.wrapping_add(step);
        }
        if sync == 1 {
            self.dma[ch].madr = addr & 0x00FF_FFFF;
            self.dma[ch].bcr &= 0xFFFF;
        }
        self.collect_gpu_irq();
        if ch == 0 {
            self.dma_pending_mdec_out = true;
        }
    }

    fn dma_linked(&mut self, ch: usize) {
        if ch != 2 {
            return;
        }
        let mut addr = self.dma[ch].madr & 0x1F_FFFC;
        // hardware would loop forever on a cyclic list; bound the work
        let mut budget: u32 = 4 * 1024 * 1024;
        loop {
            let a = addr as usize;
            let header = u32::from_le_bytes([self.ram[a], self.ram[a + 1], self.ram[a + 2], self.ram[a + 3]]);
            let count = header >> 24;
            for k in 0..count {
                let b = ((addr + 4 + k * 4) & 0x1F_FFFC) as usize;
                let w = u32::from_le_bytes([self.ram[b], self.ram[b + 1], self.ram[b + 2], self.ram[b + 3]]);
                self.gpu.gp0(w);
            }
            budget = budget.saturating_sub(count + 1);
            let next = header & 0x00FF_FFFF;
            if next & 0x80_0000 != 0 || budget == 0 {
                self.dma[ch].madr = next;
                break;
            }
            addr = next & 0x1F_FFFC;
        }
        self.collect_gpu_irq();
    }
}
