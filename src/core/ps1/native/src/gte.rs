//! GTE (COP2) register file with the hardware read/write quirks.
//! The geometry commands themselves come in a later milestone (milestone 3);
//! for now `command` only clears FLAG so code that polls it keeps running.

#[derive(Clone, Default)]
pub struct Gte {
    pub data: [u32; 32],
    pub ctrl: [u32; 32],
}

impl Gte {
    pub fn read_data(&self, r: usize) -> u32 {
        let d = &self.data;
        match r {
            // VZ0/VZ1/VZ2, IR0..IR3: sign-extended 16-bit
            1 | 3 | 5 | 8 | 9 | 10 | 11 => d[r] as i16 as i32 as u32,
            // OTZ, SZ0..SZ3: zero-extended 16-bit
            7 | 16 | 17 | 18 | 19 => d[r] & 0xFFFF,
            15 => d[14], // SXYP mirrors SXY2
            28 | 29 => {
                // IRGB/ORGB: IR1..IR3 saturated to 0..0x1F
                let c = |v: u32| ((v as i16 as i32) >> 7).clamp(0, 0x1F) as u32;
                c(d[9]) | (c(d[10]) << 5) | (c(d[11]) << 10)
            }
            31 => {
                // LZCR: leading zeros (or ones, for negative LZCS)
                let v = d[30];
                if (v as i32) < 0 { v.leading_ones() } else { v.leading_zeros() }
            }
            _ => d[r],
        }
    }

    pub fn write_data(&mut self, r: usize, v: u32) {
        match r {
            15 => {
                // SXYP pushes the screen XY FIFO
                self.data[12] = self.data[13];
                self.data[13] = self.data[14];
                self.data[14] = v;
            }
            28 => {
                self.data[28] = v & 0x7FFF;
                self.data[9] = (v & 0x1F) << 7;
                self.data[10] = ((v >> 5) & 0x1F) << 7;
                self.data[11] = ((v >> 10) & 0x1F) << 7;
            }
            29 | 31 => {} // ORGB and LZCR are read-only
            _ => self.data[r] = v,
        }
    }

    pub fn read_ctrl(&self, r: usize) -> u32 {
        match r {
            // 16-bit signed control registers read back sign-extended
            4 | 12 | 20 | 26 | 27 | 29 | 30 => self.ctrl[r] as i16 as i32 as u32,
            _ => self.ctrl[r],
        }
    }

    pub fn write_ctrl(&mut self, r: usize, v: u32) {
        self.ctrl[r] = match r {
            31 => {
                let f = v & 0x7FFF_F000;
                f | if f & 0x7F87_E000 != 0 { 0x8000_0000 } else { 0 }
            }
            _ => v,
        };
    }

    pub fn command(&mut self, _op: u32) {
        self.ctrl[31] = 0;
    }
}
