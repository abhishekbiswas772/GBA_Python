//! GTE (COP2): the geometry transformation engine. Register file with the hardware
//! read/write quirks, and every command: perspective transforms (RTPS/RTPT), lighting
//! (NC*/CC/CDP), depth cueing and interpolation (DPC*/INTPL/DCPL), MVMVA, SQR, OP,
//! NCLIP, AVSZ3/4, GPF/GPL. MAC1-3 are 44-bit with overflow flags at every addition,
//! division is the hardware's Unsigned Newton-Raphson (UNR) reciprocal.

#[derive(Clone, Default, serde::Serialize, serde::Deserialize)]
pub struct Gte {
    pub data: [u32; 32],
    pub ctrl: [u32; 32],
}

// FLAG bits
const F_MAC_POS: [u32; 4] = [1 << 16, 1 << 30, 1 << 29, 1 << 28];
const F_MAC_NEG: [u32; 4] = [1 << 15, 1 << 27, 1 << 26, 1 << 25];
const F_IR: [u32; 4] = [1 << 12, 1 << 24, 1 << 23, 1 << 22];
const F_COLOR: [u32; 3] = [1 << 21, 1 << 20, 1 << 19];
const F_SZ: u32 = 1 << 18;
const F_DIV: u32 = 1 << 17;
const F_SX: u32 = 1 << 14;
const F_SY: u32 = 1 << 13;

/// Reciprocal seed table of the UNR division (257 entries).
fn unr_table() -> &'static [u8; 257] {
    static T: std::sync::OnceLock<[u8; 257]> = std::sync::OnceLock::new();
    T.get_or_init(|| {
        let mut t = [0u8; 257];
        for (i, e) in t.iter_mut().enumerate() {
            *e = ((0x40000 / (i as i32 + 0x100) + 1) / 2 - 0x101).max(0) as u8;
        }
        t
    })
}

#[derive(Clone, Copy)]
enum Mat {
    Rot,
    Light,
    Color,
    Garbage,
}

type V3 = [i64; 3];

impl Gte {
    // ------------------------------------------------------------------ registers
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
            // 16-bit signed control registers read back sign-extended (H too: a hardware bug)
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

    // ------------------------------------------------------------------ field helpers
    fn flag(&mut self, f: u32) {
        self.ctrl[31] |= f;
    }

    fn ir(&self, i: usize) -> i64 {
        self.data[8 + i] as i16 as i64
    }

    fn vec(&self, n: usize) -> V3 {
        let xy = self.data[n * 2];
        [xy as i16 as i64, (xy >> 16) as i16 as i64, self.data[n * 2 + 1] as i16 as i64]
    }

    fn half(&self, idx: usize, base: usize) -> i64 {
        // `idx`-th signed 16-bit field of the packed matrix starting at ctrl[base]
        let w = self.ctrl[base + idx / 2];
        (if idx % 2 == 0 { w } else { w >> 16 }) as i16 as i64
    }

    fn matrix(&self, m: Mat) -> [[i64; 3]; 3] {
        let base = match m {
            Mat::Rot => 0,
            Mat::Light => 8,
            Mat::Color => 16,
            Mat::Garbage => {
                let r = (self.data[6] & 0xFF) as i64;
                let rt13 = self.half(2, 0);
                let rt22 = self.half(4, 0);
                return [[-(r << 4), r << 4, self.ir(0)], [rt13; 3], [rt22; 3]];
            }
        };
        let mut out = [[0; 3]; 3];
        for (k, row) in out.iter_mut().enumerate() {
            for (j, e) in row.iter_mut().enumerate() {
                *e = self.half(k * 3 + j, base);
            }
        }
        out
    }

    fn ctrl3(&self, base: usize) -> V3 {
        [self.ctrl[base] as i32 as i64, self.ctrl[base + 1] as i32 as i64, self.ctrl[base + 2] as i32 as i64]
    }

    fn rgbc(&self) -> V3 {
        let c = self.data[6];
        [(c & 0xFF) as i64, ((c >> 8) & 0xFF) as i64, ((c >> 16) & 0xFF) as i64]
    }

    // ------------------------------------------------------------------ arithmetic
    /// Check a MAC1-3 intermediate for 44-bit overflow and wrap it to 44 bits.
    fn ext44(&mut self, i: usize, v: i64) -> i64 {
        if v > 0x7FF_FFFF_FFFF {
            self.flag(F_MAC_POS[i]);
        } else if v < -0x800_0000_0000 {
            self.flag(F_MAC_NEG[i]);
        }
        (v << 20) >> 20
    }

    fn set_mac(&mut self, i: usize, v: i64, shift: u32) -> i64 {
        if v > 0x7FF_FFFF_FFFF {
            self.flag(F_MAC_POS[i]);
        } else if v < -0x800_0000_0000 {
            self.flag(F_MAC_NEG[i]);
        }
        let v = v >> shift;
        self.data[24 + i] = v as u32;
        v
    }

    fn set_ir(&mut self, i: usize, v: i64, lm: bool) {
        let lo = if lm { 0 } else { -0x8000 };
        let c = if v < lo {
            self.flag(F_IR[i]);
            lo
        } else if v > 0x7FFF {
            self.flag(F_IR[i]);
            0x7FFF
        } else {
            v
        };
        self.data[8 + i] = c as i32 as u32;
    }

    fn set_mac_ir(&mut self, i: usize, v: i64, shift: u32, lm: bool) {
        let m = self.set_mac(i, v, shift);
        self.set_ir(i, m as i32 as i64, lm);
    }

    fn set_mac0(&mut self, v: i64) -> i64 {
        if v > 0x7FFF_FFFF {
            self.flag(F_MAC_POS[0]);
        } else if v < -0x8000_0000 {
            self.flag(F_MAC_NEG[0]);
        }
        self.data[24] = v as u32;
        v
    }

    fn set_otz(&mut self, v: i64) {
        self.data[7] = self.sat_z(v) as u32;
    }

    fn sat_z(&mut self, v: i64) -> i64 {
        if v < 0 {
            self.flag(F_SZ);
            0
        } else if v > 0xFFFF {
            self.flag(F_SZ);
            0xFFFF
        } else {
            v
        }
    }

    fn push_sz(&mut self, v: i64) {
        let z = self.sat_z(v);
        self.data[16] = self.data[17];
        self.data[17] = self.data[18];
        self.data[18] = self.data[19];
        self.data[19] = z as u32;
    }

    fn push_sxy(&mut self, x: i64, y: i64) {
        let sat = |s: &mut Self, v: i64, f: u32| -> i64 {
            if v < -0x400 {
                s.flag(f);
                -0x400
            } else if v > 0x3FF {
                s.flag(f);
                0x3FF
            } else {
                v
            }
        };
        let x = sat(self, x, F_SX);
        let y = sat(self, y, F_SY);
        self.data[12] = self.data[13];
        self.data[13] = self.data[14];
        self.data[14] = (x as u32 & 0xFFFF) | ((y as u32 & 0xFFFF) << 16);
    }

    fn push_rgb_from_mac(&mut self) {
        let mut c = [0u32; 3];
        for (i, out) in c.iter_mut().enumerate() {
            let v = (self.data[25 + i] as i32) >> 4;
            *out = if v < 0 {
                self.flag(F_COLOR[i]);
                0
            } else if v > 0xFF {
                self.flag(F_COLOR[i]);
                0xFF
            } else {
                v as u32
            };
        }
        let code = self.data[6] & 0xFF00_0000;
        self.data[20] = self.data[21];
        self.data[21] = self.data[22];
        self.data[22] = c[0] | (c[1] << 8) | (c[2] << 16) | code;
    }

    /// H / SZ3 with the hardware's UNR reciprocal; saturates to 0x1FFFF (and flags) on overflow.
    fn divide(&mut self, h: u32, sz3: u32) -> u32 {
        if h >= sz3 * 2 {
            self.flag(F_DIV);
            return 0x1FFFF;
        }
        let z = sz3 as u16;
        let shift = z.leading_zeros();
        let n = (h as u64) << shift;
        let d = ((z as u32) << shift) as i64;
        let u = unr_table()[(((d & 0x7FFF) + 0x40) >> 7) as usize] as i64 + 0x101;
        let d2 = (0x200_0080 - d * u) >> 8;
        let recip = ((u * d2 + 0x80) >> 8) as u64;
        (((n * recip) + 0x8000) >> 16).min(0x1FFFF) as u32
    }

    /// [MAC] = (T*0x1000 + M*V) >> shift, [IR] = lim(MAC); the FC translation keeps its hardware bug.
    fn mul_mat_vec(&mut self, m: Mat, v: V3, t: Option<V3>, far_color_bug: bool, shift: u32, lm: bool) {
        let mm = self.matrix(m);
        let t = t.unwrap_or([0; 3]);
        for i in 0..3 {
            let row = mm[i];
            if far_color_bug {
                // the first product is computed (flags set) and dropped; IR flags with lm=0
                let a = self.ext44(i + 1, (t[i] << 12) + row[0] * v[0]);
                let _ = self.ext44(i + 1, a);
                self.set_ir(i + 1, (a >> shift) as i32 as i64, false);
                let b = self.ext44(i + 1, row[1] * v[1]);
                let c = self.ext44(i + 1, b + row[2] * v[2]);
                self.set_mac_ir(i + 1, c, shift, lm);
            } else {
                let a = self.ext44(i + 1, (t[i] << 12) + row[0] * v[0]);
                let b = self.ext44(i + 1, a + row[1] * v[1]);
                let c = b + row[2] * v[2];
                self.set_mac_ir(i + 1, c, shift, lm);
            }
        }
    }

    fn ir_vec(&self) -> V3 {
        [self.ir(1), self.ir(2), self.ir(3)]
    }

    /// [MAC] = in + (FC - in) * IR0, then IR = lim(MAC).
    fn interpolate(&mut self, inp: V3, shift: u32, lm: bool) {
        let fc = self.ctrl3(21);
        for i in 0..3 {
            self.set_mac_ir(i + 1, (fc[i] << 12) - inp[i], shift, false);
        }
        let ir0 = self.ir(0);
        for i in 0..3 {
            let v = self.ir(i + 1) * ir0 + inp[i];
            self.set_mac_ir(i + 1, v, shift, lm);
        }
    }

    // ------------------------------------------------------------------ commands
    fn rtp(&mut self, n: usize, shift: u32, lm: bool, last: bool) {
        let v = self.vec(n);
        let tr = self.ctrl3(5);
        let rt = self.matrix(Mat::Rot);
        let mut z = 0i64;
        for i in 0..3 {
            let a = self.ext44(i + 1, (tr[i] << 12) + rt[i][0] * v[0]);
            let b = self.ext44(i + 1, a + rt[i][1] * v[1]);
            let c = b + rt[i][2] * v[2];
            if i < 2 {
                self.set_mac_ir(i + 1, c, shift, lm);
            } else {
                // IR3 saturates from MAC3, but its flag follows MAC3 >> 12 whatever sf is
                let m = self.set_mac(3, c, shift);
                let c44 = (c << 20) >> 20;
                z = c44 >> 12;
                if !(-0x8000..=0x7FFF).contains(&z) {
                    self.flag(F_IR[3]);
                }
                let lo = if lm { 0 } else { -0x8000 };
                self.data[11] = (m as i32 as i64).clamp(lo, 0x7FFF) as i32 as u32;
            }
        }
        self.push_sz(z);
        let h = self.ctrl[26] & 0xFFFF;
        let q = self.divide(h, self.data[19] & 0xFFFF) as i64;
        let x = self.set_mac0(q * self.ir(1) + self.ctrl[24] as i32 as i64);
        let y = self.set_mac0(q * self.ir(2) + self.ctrl[25] as i32 as i64);
        self.push_sxy(x >> 16, y >> 16);
        if last {
            let dqa = self.ctrl[27] as i16 as i64;
            let dqb = self.ctrl[28] as i32 as i64;
            let m = self.set_mac0(q * dqa + dqb);
            let ir0 = m >> 12;
            let c = if ir0 < 0 {
                self.flag(F_IR[0]);
                0
            } else if ir0 > 0x1000 {
                self.flag(F_IR[0]);
                0x1000
            } else {
                ir0
            };
            self.data[8] = c as u32;
        }
    }

    fn nc(&mut self, n: usize, shift: u32, lm: bool) {
        let v = self.vec(n);
        self.mul_mat_vec(Mat::Light, v, None, false, shift, lm);
        let bk = self.ctrl3(13);
        let ir = self.ir_vec();
        self.mul_mat_vec(Mat::Color, ir, Some(bk), false, shift, lm);
        self.push_rgb_from_mac();
    }

    fn color_times_ir(&self) -> V3 {
        let c = self.rgbc();
        [(c[0] * self.ir(1)) << 4, (c[1] * self.ir(2)) << 4, (c[2] * self.ir(3)) << 4]
    }

    fn ncc_tail(&mut self, shift: u32, lm: bool) {
        let bk = self.ctrl3(13);
        let ir = self.ir_vec();
        self.mul_mat_vec(Mat::Color, ir, Some(bk), false, shift, lm);
        let ci = self.color_times_ir();
        for (i, v) in ci.iter().enumerate() {
            self.set_mac_ir(i + 1, *v, shift, lm);
        }
        self.push_rgb_from_mac();
    }

    fn ncc(&mut self, n: usize, shift: u32, lm: bool) {
        let v = self.vec(n);
        self.mul_mat_vec(Mat::Light, v, None, false, shift, lm);
        self.ncc_tail(shift, lm);
    }

    fn ncd_tail(&mut self, shift: u32, lm: bool) {
        let bk = self.ctrl3(13);
        let ir = self.ir_vec();
        self.mul_mat_vec(Mat::Color, ir, Some(bk), false, shift, lm);
        let ci = self.color_times_ir();
        self.interpolate(ci, shift, lm);
        self.push_rgb_from_mac();
    }

    fn ncd(&mut self, n: usize, shift: u32, lm: bool) {
        let v = self.vec(n);
        self.mul_mat_vec(Mat::Light, v, None, false, shift, lm);
        self.ncd_tail(shift, lm);
    }

    fn dpc(&mut self, c: V3, shift: u32, lm: bool) {
        self.interpolate([c[0] << 16, c[1] << 16, c[2] << 16], shift, lm);
        self.push_rgb_from_mac();
    }

    pub fn command(&mut self, i: u32) {
        self.ctrl[31] = 0;
        let shift = if i & (1 << 19) != 0 { 12 } else { 0 };
        let lm = i & (1 << 10) != 0;
        match i & 0x3F {
            0x01 => self.rtp(0, shift, lm, true),
            0x06 => {
                let s = |k: usize, d: &[u32; 32]| (d[12 + k] as i16 as i64, (d[12 + k] >> 16) as i16 as i64);
                let ((x0, y0), (x1, y1), (x2, y2)) = (s(0, &self.data), s(1, &self.data), s(2, &self.data));
                self.set_mac0(x0 * y1 + x1 * y2 + x2 * y0 - x0 * y2 - x1 * y0 - x2 * y1);
            }
            0x0C => {
                let d = [self.half(0, 0), self.half(4, 0), self.half(8, 0)];
                let ir = self.ir_vec();
                self.set_mac_ir(1, ir[2] * d[1] - ir[1] * d[2], shift, lm);
                self.set_mac_ir(2, ir[0] * d[2] - ir[2] * d[0], shift, lm);
                self.set_mac_ir(3, ir[1] * d[0] - ir[0] * d[1], shift, lm);
            }
            0x10 => {
                let c = self.rgbc();
                self.dpc(c, shift, lm);
            }
            0x11 => {
                let ir = self.ir_vec();
                self.interpolate([ir[0] << 12, ir[1] << 12, ir[2] << 12], shift, lm);
                self.push_rgb_from_mac();
            }
            0x12 => {
                let m = match (i >> 17) & 3 {
                    0 => Mat::Rot,
                    1 => Mat::Light,
                    2 => Mat::Color,
                    _ => Mat::Garbage,
                };
                let v = match (i >> 15) & 3 {
                    3 => self.ir_vec(),
                    n => self.vec(n as usize),
                };
                match (i >> 13) & 3 {
                    0 => self.mul_mat_vec(m, v, Some(self.ctrl3(5)), false, shift, lm),
                    1 => self.mul_mat_vec(m, v, Some(self.ctrl3(13)), false, shift, lm),
                    2 => self.mul_mat_vec(m, v, Some(self.ctrl3(21)), true, shift, lm),
                    _ => self.mul_mat_vec(m, v, None, false, shift, lm),
                }
            }
            0x13 => self.ncd(0, shift, lm),
            0x14 => self.ncd_tail(shift, lm),
            0x16 => {
                for n in 0..3 {
                    self.ncd(n, shift, lm);
                }
            }
            0x1B => self.ncc(0, shift, lm),
            0x1C => self.ncc_tail(shift, lm),
            0x1E => self.nc(0, shift, lm),
            0x20 => {
                for n in 0..3 {
                    self.nc(n, shift, lm);
                }
            }
            0x28 => {
                let ir = self.ir_vec();
                for k in 0..3 {
                    self.set_mac_ir(k + 1, ir[k] * ir[k], shift, lm);
                }
            }
            0x29 => {
                let ci = self.color_times_ir();
                self.interpolate(ci, shift, lm);
                self.push_rgb_from_mac();
            }
            0x2A => {
                for _ in 0..3 {
                    let c = self.data[20];
                    let c = [(c & 0xFF) as i64, ((c >> 8) & 0xFF) as i64, ((c >> 16) & 0xFF) as i64];
                    self.dpc(c, shift, lm);
                }
            }
            0x2D | 0x2E => {
                let sz = |k: usize| (self.data[16 + k] & 0xFFFF) as i64;
                let m = if i & 0x3F == 0x2D {
                    (self.ctrl[29] as i16 as i64) * (sz(1) + sz(2) + sz(3))
                } else {
                    (self.ctrl[30] as i16 as i64) * (sz(0) + sz(1) + sz(2) + sz(3))
                };
                let m = self.set_mac0(m);
                self.set_otz(m >> 12);
            }
            0x30 => {
                self.rtp(0, shift, lm, false);
                self.rtp(1, shift, lm, false);
                self.rtp(2, shift, lm, true);
            }
            0x3D => {
                let ir0 = self.ir(0);
                let ir = self.ir_vec();
                for k in 0..3 {
                    self.set_mac_ir(k + 1, ir[k] * ir0, shift, lm);
                }
                self.push_rgb_from_mac();
            }
            0x3E => {
                let ir0 = self.ir(0);
                let ir = self.ir_vec();
                for k in 0..3 {
                    let mac = (self.data[25 + k] as i32 as i64) << shift;
                    self.set_mac_ir(k + 1, mac + ir[k] * ir0, shift, lm);
                }
                self.push_rgb_from_mac();
            }
            0x3F => {
                for n in 0..3 {
                    self.ncc(n, shift, lm);
                }
            }
            _ => {}
        }
        if self.ctrl[31] & 0x7F87_E000 != 0 {
            self.ctrl[31] |= 0x8000_0000;
        }
    }
}
