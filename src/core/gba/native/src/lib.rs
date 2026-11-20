//! Native Game Boy Advance core for RetroPy.
//!
//! A cycle-exact port of the Python core in `src/core/gba/` (CPU, bus, HLE
//! BIOS, timers, DMA, PPU, APU, cartridge saves, RTC). Python assembles the
//! machine once (ROM, BIOS, .sav contents, clock) and then calls in once per
//! frame; inside a frame every component calls the others directly.

pub mod apu;
pub mod bios;
pub mod bus;
pub mod cart;
pub mod cpu;
pub mod dma;
pub mod gba;
pub mod ppu;
pub mod psg;
pub mod rtc;
pub mod timers;

/// RGB888 from little-endian BGR555 (5-to-8 bit: v << 3 | v >> 2).
pub fn bgr555_to_rgb888(fb: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(fb.len() / 2 * 3);
    for px in fb.chunks_exact(2) {
        let v = u16::from_le_bytes([px[0], px[1]]);
        for c in [v & 31, (v >> 5) & 31, (v >> 10) & 31] {
            out.push(((c << 3) | (c >> 2)) as u8);
        }
    }
    out
}

#[cfg(feature = "python")]
mod python {
    use super::gba::Gba as Machine;
    use pyo3::exceptions::PyValueError;
    use pyo3::prelude::*;
    use pyo3::types::PyBytes;

    /// One GBA. `rom`: cartridge bytes; `bios`: a real BIOS image or None (HLE BIOS);
    /// `save`: the .sav contents or None.
    #[pyclass(name = "Gba", module = "src.core.gba._gba")]
    pub struct Gba {
        g: Box<Machine>,
        rom: Vec<u8>,
        bios: Option<Vec<u8>>,
    }

    fn bytes<'py>(py: Python<'py>, b: &[u8]) -> Bound<'py, PyBytes> {
        PyBytes::new(py, b)
    }

    #[pymethods]
    impl Gba {
        #[new]
        #[pyo3(signature = (rom, bios=None, save=None))]
        fn new(rom: Vec<u8>, bios: Option<Vec<u8>>, save: Option<Vec<u8>>) -> PyResult<Self> {
            let g = Machine::new(&rom, bios.clone(), save.as_deref()).map_err(PyValueError::new_err)?;
            Ok(Gba { g: Box::new(g), rom, bios })
        }

        // -------------------------------------------------------- running
        /// Emulate one video frame (the GIL is released while it runs).
        #[pyo3(signature = (render=true))]
        fn run_frame(&mut self, py: Python<'_>, render: bool) {
            let g = &mut self.g;
            py.allow_threads(|| {
                g.ppu.skip_render = !render;
                g.run_frame();
                g.ppu.skip_render = false;
            });
        }

        fn run_frames(&mut self, py: Python<'_>, n: u32) {
            let g = &mut self.g;
            py.allow_threads(|| {
                for _ in 0..n {
                    g.run_frame();
                }
            });
        }

        fn run_until(&mut self, t: u64) {
            self.g.run_until(t);
        }

        /// One instruction with frame-loop event handling (lockstep tests).
        fn step_instr(&mut self) {
            self.g.step_instr();
        }

        /// `cpu.step()`: one instruction, no PPU / timer events.
        fn step(&mut self) -> u64 {
            self.g.step()
        }

        /// Run a BIOS call directly (HLE only), like `cpu.swi(n)`.
        fn swi(&mut self, n: u32) -> u64 {
            self.g.hle_swi(n)
        }

        // -------------------------------------------------------- input
        fn set_key(&mut self, bit: u32, down: bool) {
            self.g.set_key(bit, down);
        }

        #[getter]
        fn keys(&self) -> u32 {
            self.g.keys
        }

        // -------------------------------------------------------- video / audio
        fn frame_bgr555<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
            bytes(py, &self.g.ppu.fb)
        }

        fn frame_rgb<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
            bytes(py, &super::bgr555_to_rgb888(&self.g.ppu.fb))
        }

        /// Draw one scanline into the framebuffer now (tests).
        fn render_line(&mut self, y: usize) -> PyResult<()> {
            if y >= crate::ppu::H {
                return Err(PyValueError::new_err("line out of range"));
            }
            self.g.render_line(y);
            Ok(())
        }

        /// Reload BG2 (0) / BG3 (1)'s internal affine reference point from its registers.
        fn latch_ref(&mut self, bg: usize) {
            self.g.ppu_latch_ref(bg & 1);
        }

        #[getter]
        fn frame(&self) -> u64 {
            self.g.ppu.frame
        }

        #[getter]
        fn sampling(&self) -> bool {
            self.g.apu_sampling()
        }

        #[setter]
        fn set_sampling(&mut self, on: bool) {
            self.g.apu_set_sampling(on);
        }

        /// int16 stereo samples (little-endian bytes) at 44.1 kHz since the last call.
        fn take_samples<'py>(&mut self, py: Python<'py>) -> Bound<'py, PyBytes> {
            let s = self.g.apu_take_samples();
            let mut out = Vec::with_capacity(s.len() * 2);
            for v in s {
                out.extend_from_slice(&v.to_le_bytes());
            }
            bytes(py, &out)
        }

        // -------------------------------------------------------- cartridge
        #[getter]
        fn save_kind(&self) -> &'static str {
            self.g.save.kind().name()
        }

        #[getter]
        fn save_dirty(&self) -> bool {
            self.g.save.dirty()
        }

        fn save_data<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
            self.g.save.data().map(|d| bytes(py, d))
        }

        fn clear_save_dirty(&mut self) {
            self.g.save.clear_dirty();
        }

        #[getter]
        fn has_rtc(&self) -> bool {
            self.g.rtc.is_some()
        }

        /// Local time = UTC + `seconds` (the host's UTC offset).
        fn set_rtc_offset(&mut self, seconds: i64) {
            if let Some(r) = &mut self.g.rtc {
                r.utc_offset = seconds;
            }
        }

        /// Pin the RTC to (year, month, day, weekday Mon=0, hour, minute, second), or None.
        fn set_rtc_fixed(&mut self, t: Option<[i64; 7]>) {
            if let Some(r) = &mut self.g.rtc {
                r.fixed = t;
            }
        }

        // -------------------------------------------------------- save states
        fn save_state<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyBytes>> {
            let b = bincode::serialize(&*self.g).map_err(|e| PyValueError::new_err(e.to_string()))?;
            Ok(bytes(py, &b))
        }

        fn load_state(&mut self, data: &[u8]) -> PyResult<()> {
            let mut g: Machine = bincode::deserialize(data).map_err(|e| PyValueError::new_err(e.to_string()))?;
            g.adopt_images(&self.rom, self.bios.clone().unwrap_or_else(super::gba::hle_bios_image));
            self.g = Box::new(g);
            Ok(())
        }

        // -------------------------------------------------------- CPU / bus (tests, tools)
        #[getter]
        fn regs(&self) -> Vec<u32> {
            self.g.cpu.r.to_vec()
        }

        fn set_reg(&mut self, i: usize, v: u32) {
            self.g.cpu.r[i & 15] = v;
        }

        #[getter]
        fn cpsr(&self) -> u32 {
            self.g.cpu.get_cpsr()
        }

        #[getter]
        fn pc(&self) -> u32 {
            self.g.cpu.pc
        }

        #[getter]
        fn thumb(&self) -> bool {
            self.g.cpu.thumb
        }

        #[getter]
        fn halted(&self) -> bool {
            self.g.cpu.halted
        }

        #[getter]
        fn intrwait(&self) -> bool {
            self.g.cpu.intrwait
        }

        #[getter]
        fn now(&self) -> u64 {
            self.g.cpu.now
        }

        fn read8(&mut self, a: u32) -> u32 {
            self.g.read8(a)
        }
        fn read16(&mut self, a: u32) -> u32 {
            self.g.read16(a)
        }
        fn read32(&mut self, a: u32) -> u32 {
            self.g.read32(a)
        }
        fn write8(&mut self, a: u32, v: u32) {
            self.g.write8(a, v)
        }
        fn write16(&mut self, a: u32, v: u32) {
            self.g.write16(a, v)
        }
        fn write32(&mut self, a: u32, v: u32) {
            self.g.write32(a, v)
        }

        /// Raw memory: "ewram", "iwram", "io", "pal", "vram", "oam" or "rom" (padded).
        fn memory<'py>(&self, py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyBytes>> {
            let g = &self.g;
            let b: &[u8] = match name {
                "ewram" => &g.ewram,
                "iwram" => &g.iwram,
                "io" => &g.io,
                "pal" => &g.pal,
                "vram" => &g.vram,
                "oam" => &g.oam,
                "rom" => &g.rom,
                _ => return Err(PyValueError::new_err(format!("no memory named {name}"))),
            };
            Ok(bytes(py, b))
        }

        /// Write raw bytes into one of the plain memories (tests).
        fn poke(&mut self, name: &str, offset: usize, data: &[u8]) -> PyResult<()> {
            let g = &mut self.g;
            let b: &mut Vec<u8> = match name {
                "ewram" => &mut g.ewram,
                "iwram" => &mut g.iwram,
                "pal" => &mut g.pal,
                "vram" => &mut g.vram,
                "oam" => &mut g.oam,
                _ => return Err(PyValueError::new_err(format!("can't poke {name}"))),
            };
            if offset + data.len() > b.len() {
                return Err(PyValueError::new_err("out of range"));
            }
            b[offset..offset + data.len()].copy_from_slice(data);
            Ok(())
        }
    }

    #[pymodule]
    fn _gba(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add("VERSION", env!("CARGO_PKG_VERSION"))?;
        m.add("SAMPLE_RATE", 44_100)?;
        m.add_class::<Gba>()?;
        Ok(())
    }
}
