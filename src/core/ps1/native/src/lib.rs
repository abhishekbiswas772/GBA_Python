//! Experimental PlayStation (PS1) core for RetroPy.
//!
//! Status: CPU, bus, DMA, timers, IRQs, GPU, GTE, SPU, MDEC, CD-ROM, pad,
//! PS-X EXE and disc booting (HLE kernel or a real BIOS), save states.
//! Still to come: XA-ADPCM audio and memory cards.

pub mod bus;
pub mod cdrom;
pub mod cpu;
pub mod disc;
pub mod gpu;
pub mod gte;
pub mod hle;
pub mod mdec;
pub mod sio;
pub mod spu;
mod spu_gauss;
pub mod system;
pub mod timers;

#[cfg(feature = "python")]
mod python {
    use super::system::System;
    use pyo3::exceptions::PyValueError;
    use pyo3::prelude::*;
    use pyo3::types::PyBytes;

    /// One PlayStation. `bios`: BIOS image bytes or None (HLE kernel, EXEs only);
    /// `exe`: a PS-X EXE to run (sideloaded at the BIOS shell when a BIOS is given).
    #[pyclass(name = "Ps1", module = "src.core.ps1._ps1")]
    pub struct Ps1 {
        sys: System,
    }

    /// Run one GTE command on a fresh GTE: `regs` = 64 values written in order
    /// (data 0..31 with MTC2, then control 0..31 with CTC2); returns the 64 registers
    /// read back with MFC2 / CFC2. For tests against hardware logs.
    #[pyfunction]
    pub fn gte_exec(regs: Vec<u32>, op: u32) -> PyResult<Vec<u32>> {
        if regs.len() != 64 {
            return Err(PyValueError::new_err("need 64 register values"));
        }
        let mut g = crate::gte::Gte::default();
        for (r, &v) in regs.iter().enumerate() {
            if r < 32 { g.write_data(r, v) } else { g.write_ctrl(r - 32, v) }
        }
        g.command(op);
        Ok((0..64).map(|r| if r < 32 { g.read_data(r) } else { g.read_ctrl(r - 32) }).collect())
    }

    /// A raw (2352-byte sector) ISO9660 data disc holding `files` in its root, licensed
    /// as SCEA: for tests and homebrew. Write it as a .bin next to a one-track .cue.
    #[pyfunction]
    pub fn make_disc_image(files: Vec<(String, Vec<u8>)>, sectors: u32) -> Vec<u8> {
        let refs: Vec<(&str, &[u8])> = files.iter().map(|(n, d)| (n.as_str(), d.as_slice())).collect();
        crate::disc::build_iso(&refs, sectors)
    }

    #[pymethods]
    impl Ps1 {
        #[new]
        #[pyo3(signature = (bios=None, exe=None, disc=None))]
        fn new(bios: Option<Vec<u8>>, exe: Option<Vec<u8>>, disc: Option<std::path::PathBuf>) -> PyResult<Self> {
            let disc = match disc {
                Some(p) => Some(std::sync::Arc::new(crate::disc::Disc::open(&p).map_err(PyValueError::new_err)?)),
                None => None,
            };
            System::with_disc(bios, exe, disc).map(|sys| Ps1 { sys }).map_err(PyValueError::new_err)
        }

        /// A short id of the disc in the drive (its SYSTEM.CNF and volume name), or None.
        fn disc_id<'py>(&self, py: Python<'py>) -> Option<Bound<'py, PyBytes>> {
            let d = self.sys.bus.cdrom.disc()?;
            let mut id = d.user_data(16)[40..72].to_vec();
            id.extend(d.read_file("SYSTEM.CNF").unwrap_or_default());
            Some(PyBytes::new(py, &id))
        }

        /// Machine state as bytes (no BIOS inside).
        fn save_state<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
            PyBytes::new(py, &self.sys.save_state())
        }

        /// Restore bytes from `save_state` into this machine.
        fn load_state(&mut self, data: &[u8]) -> PyResult<()> {
            self.sys.load_state(data).map_err(PyValueError::new_err)
        }

        /// Emulate one video frame (the GIL is released while it runs).
        fn run_frame(&mut self, py: Python<'_>) {
            let sys = &mut self.sys;
            py.allow_threads(|| sys.run_frame());
        }

        fn run_frames(&mut self, py: Python<'_>, n: u32) {
            for _ in 0..n {
                self.run_frame(py);
            }
        }

        /// (width, height, RGB888 bytes) of the displayed area.
        fn frame_rgb<'py>(&self, py: Python<'py>) -> (u32, u32, Bound<'py, PyBytes>) {
            let (w, h, rgb) = self.sys.bus.gpu.frame_rgb();
            (w, h, PyBytes::new(py, &rgb))
        }

        /// The displayed area scaled (nearest) onto a fixed `w × h` RGB888 canvas.
        fn frame_canvas<'py>(&self, py: Python<'py>, w: u32, h: u32) -> Bound<'py, PyBytes> {
            let (sw, sh, rgb) = self.sys.bus.gpu.frame_rgb();
            if (sw, sh) == (w, h) {
                return PyBytes::new(py, &rgb);
            }
            let mut out = vec![0u8; (w * h * 3) as usize];
            let xs: Vec<usize> = (0..w).map(|x| ((x * sw / w).min(sw - 1) * 3) as usize).collect();
            for y in 0..h {
                let sy = ((y * sh / h).min(sh - 1) * sw * 3) as usize;
                let o = (y * w * 3) as usize;
                for (x, &sx) in xs.iter().enumerate() {
                    out[o + x * 3..o + x * 3 + 3].copy_from_slice(&rgb[sy + sx..sy + sx + 3]);
                }
            }
            PyBytes::new(py, &out)
        }

        /// All of VRAM: 1024×512 little-endian 15-bit pixels.
        fn vram<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
            let v = &self.sys.bus.gpu.vram;
            let mut out = Vec::with_capacity(v.len() * 2);
            for p in v {
                out.extend_from_slice(&p.to_le_bytes());
            }
            PyBytes::new(py, &out)
        }

        /// The 512 KB of sound RAM.
        fn spu_ram<'py>(&self, py: Python<'py>) -> Bound<'py, PyBytes> {
            PyBytes::new(py, &self.sys.bus.spu.ram)
        }

        /// TTY text printed since the last call.
        fn take_tty<'py>(&mut self, py: Python<'py>) -> Bound<'py, PyBytes> {
            let t = std::mem::take(&mut self.sys.bus.tty);
            PyBytes::new(py, &t)
        }

        /// int16 stereo samples at 44.1 kHz produced since the last call.
        fn take_samples<'py>(&mut self, py: Python<'py>) -> Bound<'py, PyBytes> {
            let s = std::mem::take(&mut self.sys.samples);
            let mut out = Vec::with_capacity(s.len() * 2);
            for v in s {
                out.extend_from_slice(&v.to_le_bytes());
            }
            PyBytes::new(py, &out)
        }

        #[getter]
        fn sampling(&self) -> bool {
            self.sys.sampling
        }

        #[setter]
        fn set_sampling(&mut self, on: bool) {
            self.sys.sampling = on;
            if !on {
                self.sys.samples.clear();
            }
        }

        /// Pressed pad buttons, PS1 bit order (bit 0 Select … bit 15 Square).
        fn set_buttons(&mut self, mask: u16) {
            self.sys.bus.sio.buttons = mask;
        }

        #[getter]
        fn fps(&self) -> f64 {
            self.sys.fps()
        }

        #[getter]
        fn frames(&self) -> u64 {
            self.sys.bus.gpu.frames
        }

        #[getter]
        fn cycles(&self) -> u64 {
            self.sys.cycles
        }

        #[getter]
        fn pc(&self) -> u32 {
            self.sys.cpu.pc
        }

        /// COP0 status and cause registers (debugging).
        #[getter]
        fn cop0(&self) -> (u32, u32, u32, u64) {
            (self.sys.cpu.sr, self.sys.cpu.cause, self.sys.cpu.epc, self.sys.cpu.irqs_taken)
        }

        #[getter]
        fn hle(&self) -> bool {
            self.sys.hle.is_some()
        }

        /// BIOS functions the HLE kernel didn't know, as (table, function) pairs.
        fn hle_unknown(&self) -> Vec<(u8, u8)> {
            self.sys.hle.as_ref().map(|h| h.unknown.clone()).unwrap_or_default()
        }

        /// Feed GP0 / GP1 directly (tests and tools).
        fn gp0(&mut self, w: u32) {
            self.sys.bus.gpu.gp0(w);
        }

        fn gp1(&mut self, w: u32) {
            self.sys.bus.gpu.gp1(w);
        }

        #[getter]
        fn gpustat(&self) -> u32 {
            self.sys.bus.gpu.status()
        }

        fn read32(&mut self, addr: u32) -> u32 {
            self.sys.bus.read32(addr)
        }
    }

    #[pymodule]
    fn _ps1(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add("VERSION", env!("CARGO_PKG_VERSION"))?;
        m.add("SAMPLE_RATE", super::system::SAMPLE_RATE)?;
        m.add_class::<Ps1>()?;
        m.add_function(wrap_pyfunction!(gte_exec, m)?)?;
        m.add_function(wrap_pyfunction!(make_disc_image, m)?)?;
        Ok(())
    }
}
