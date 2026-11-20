//! Disc images: `.cue` sheets with raw 2352-byte `.bin` tracks (data and audio),
//! plain 2048-byte `.iso` files, and in-memory images built by tests.
//!
//! Addresses are LBAs from the start of the program area: LBA 0 is MSF 00:02:00
//! (absolute time includes the 2-second lead-in, so `msf = lba + 150`). Sectors are
//! read from the file on demand; a 700 MB image is never held in memory.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

pub const SECTOR: usize = 2352;
pub const LEAD_IN: u32 = 150;
pub const LEAD_OUT_TRACK: u8 = 0xAA;

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Kind {
    Audio,
    Mode1,
    Mode2,
}

#[derive(Clone, Debug)]
pub struct Track {
    pub number: u8,
    pub kind: Kind,
    /// first sector of the track (index 0, i.e. including its pregap)
    pub start: u32,
    /// index 1: where the track's own time is 00:00:00
    pub index1: u32,
    pub length: u32, // sectors, from `start`
    source: usize,
    /// sector in the source file that holds `start`; None for sectors not in the file (PREGAP)
    file_sector: i64,
    /// bytes per sector in the source (2352 raw, 2048 for cooked .iso / MODE1/2048)
    sector_bytes: usize,
}

enum Source {
    File(Mutex<File>),
    Memory(Vec<u8>),
}

pub struct Disc {
    pub tracks: Vec<Track>,
    sources: Vec<Source>,
    pub path: Option<PathBuf>,
}

impl std::fmt::Debug for Disc {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "Disc({:?}, {} tracks)", self.path, self.tracks.len())
    }
}

pub fn bcd(v: u32) -> u8 {
    (((v / 10) << 4) | (v % 10)) as u8
}

pub fn from_bcd(v: u8) -> u32 {
    ((v >> 4) as u32) * 10 + (v & 0xF) as u32
}

/// (minute, second, frame) of a sector count
pub fn msf(n: u32) -> (u32, u32, u32) {
    (n / 4500, (n / 75) % 60, n % 75)
}

fn parse_msf(s: &str) -> Option<u32> {
    let p: Vec<u32> = s.split(':').map(|x| x.trim().parse().ok()).collect::<Option<_>>()?;
    if p.len() != 3 {
        return None;
    }
    Some(p[0] * 4500 + p[1] * 75 + p[2])
}

/// Q subchannel position: (track, index, relative sectors, absolute sectors incl. lead-in).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Position {
    pub track: u8,
    pub index: u8,
    pub relative: u32,
    pub absolute: u32,
}

impl Disc {
    /// Open a `.cue` (with its `.bin` files) or a 2048-byte `.iso` / `.bin` image.
    pub fn open(path: &Path) -> Result<Disc, String> {
        let ext = path.extension().and_then(|e| e.to_str()).unwrap_or("").to_ascii_lowercase();
        if ext == "cue" {
            let text = std::fs::read_to_string(path).map_err(|e| format!("can't read {}: {e}", path.display()))?;
            return Self::from_cue(&text, path.parent().unwrap_or(Path::new(".")), Some(path.to_path_buf()));
        }
        let f = File::open(path).map_err(|e| format!("can't open {}: {e}", path.display()))?;
        let len = f.metadata().map_err(|e| e.to_string())?.len();
        // a lone .bin is raw 2352; an .iso is 2048
        let raw = ext != "iso" && len % SECTOR as u64 == 0;
        let sb = if raw { SECTOR } else { 2048 };
        let sectors = (len / sb as u64) as u32;
        Ok(Disc {
            tracks: vec![Track { number: 1, kind: if raw { Kind::Mode2 } else { Kind::Mode1 }, start: 0, index1: 0,
                                 length: sectors, source: 0, file_sector: 0, sector_bytes: sb }],
            sources: vec![Source::File(Mutex::new(f))],
            path: Some(path.to_path_buf()),
        })
    }

    /// Parse a cue sheet; FILE paths are relative to `dir`.
    pub fn from_cue(text: &str, dir: &Path, path: Option<PathBuf>) -> Result<Disc, String> {
        struct Pending {
            number: u8,
            kind: Kind,
            sector_bytes: usize,
            source: usize,
            index0: Option<u32>,
            index1: Option<u32>,
            pregap: u32,
        }
        let mut files: Vec<(Source, u64)> = Vec::new(); // (source, sectors)
        let mut pend: Vec<Pending> = Vec::new();
        for raw in text.lines() {
            let line = raw.trim();
            let upper = line.to_ascii_uppercase();
            if upper.starts_with("FILE ") {
                let name = if let (Some(a), Some(b)) = (line.find('"'), line.rfind('"')) {
                    if b > a { line[a + 1..b].to_string() } else { String::new() }
                } else {
                    line.split_whitespace().nth(1).unwrap_or("").to_string()
                };
                let p = dir.join(&name);
                let f = File::open(&p).map_err(|e| format!("cue: can't open track file {}: {e}", p.display()))?;
                let len = f.metadata().map_err(|e| e.to_string())?.len();
                files.push((Source::File(Mutex::new(f)), len));
            } else if upper.starts_with("TRACK ") {
                let mut it = upper.split_whitespace().skip(1);
                let number: u8 = it.next().and_then(|n| n.parse().ok()).ok_or("cue: bad TRACK line")?;
                let ty = it.next().unwrap_or("");
                let (kind, sb) = match ty {
                    "AUDIO" => (Kind::Audio, SECTOR),
                    "MODE1/2352" => (Kind::Mode1, SECTOR),
                    "MODE2/2352" => (Kind::Mode2, SECTOR),
                    "MODE1/2048" => (Kind::Mode1, 2048),
                    other => return Err(format!("cue: track type {other} isn't supported")),
                };
                if files.is_empty() {
                    return Err("cue: TRACK before FILE".into());
                }
                pend.push(Pending { number, kind, sector_bytes: sb, source: files.len() - 1, index0: None,
                                    index1: None, pregap: 0 });
            } else if upper.starts_with("INDEX ") {
                let mut it = line.split_whitespace().skip(1);
                let n: u32 = it.next().and_then(|n| n.parse().ok()).ok_or("cue: bad INDEX line")?;
                let t = it.next().and_then(parse_msf).ok_or("cue: bad INDEX time")?;
                let cur = pend.last_mut().ok_or("cue: INDEX before TRACK")?;
                match n {
                    0 => cur.index0 = Some(t),
                    1 => cur.index1 = Some(t),
                    _ => {}
                }
            } else if upper.starts_with("PREGAP ") {
                let t = line.split_whitespace().nth(1).and_then(parse_msf).ok_or("cue: bad PREGAP")?;
                pend.last_mut().ok_or("cue: PREGAP before TRACK")?.pregap = t;
            }
        }
        if pend.is_empty() {
            return Err("cue: no tracks".into());
        }
        // lay the tracks out on the disc
        let mut tracks: Vec<Track> = Vec::new();
        let mut file_base = vec![0u32; files.len()]; // disc LBA of each file's sector 0
        let mut lba_shift = 0u32; // PREGAP sectors inserted so far (not in any file)
        let mut next_file_lba = 0u32;
        for (i, f) in files.iter().enumerate() {
            file_base[i] = next_file_lba;
            let sb = pend.iter().find(|p| p.source == i).map(|p| p.sector_bytes).unwrap_or(SECTOR);
            let gaps: u32 = pend.iter().filter(|p| p.source == i).map(|p| p.pregap).sum();
            next_file_lba += (f.1 / sb as u64) as u32 + gaps;
        }
        let mut last_source = usize::MAX;
        for p in &pend {
            if p.source != last_source {
                lba_shift = 0;
                last_source = p.source;
            }
            let i1 = p.index1.ok_or(format!("cue: track {} has no INDEX 01", p.number))?;
            lba_shift += p.pregap;
            let base = file_base[p.source] + lba_shift;
            let in_file_start = p.index0.unwrap_or(i1);
            let start = base + in_file_start - p.pregap;
            tracks.push(Track {
                number: p.number, kind: p.kind, start, index1: base + i1, length: 0, source: p.source,
                file_sector: in_file_start as i64 - p.pregap as i64, sector_bytes: p.sector_bytes,
            });
        }
        // lengths: up to the next track, or the end of the file
        for k in 0..tracks.len() {
            let end = if k + 1 < tracks.len() && tracks[k + 1].source == tracks[k].source {
                tracks[k + 1].start
            } else {
                let src = tracks[k].source;
                let sectors = (files[src].1 / tracks[k].sector_bytes as u64) as u32;
                let gaps: u32 = pend.iter().filter(|p| p.source == src).map(|p| p.pregap).sum();
                file_base[src] + sectors + gaps
            };
            tracks[k].length = end.saturating_sub(tracks[k].start);
        }
        // the first track's pregap (the 2 s before 00:02:00) isn't part of the program area
        Ok(Disc { tracks, sources: files.into_iter().map(|f| f.0).collect(), path })
    }

    /// An in-memory image of raw 2352-byte sectors as one MODE2 track (for tests).
    pub fn from_raw_sectors(data: Vec<u8>) -> Disc {
        let n = (data.len() / SECTOR) as u32;
        Disc {
            tracks: vec![Track { number: 1, kind: Kind::Mode2, start: 0, index1: 0, length: n, source: 0,
                                 file_sector: 0, sector_bytes: SECTOR }],
            sources: vec![Source::Memory(data)],
            path: None,
        }
    }

    /// Sectors in the program area.
    pub fn sectors(&self) -> u32 {
        self.tracks.last().map(|t| t.start + t.length).unwrap_or(0)
    }

    pub fn track_at(&self, lba: u32) -> Option<&Track> {
        self.tracks.iter().rev().find(|t| lba >= t.start && lba < t.start + t.length)
    }

    pub fn track(&self, number: u8) -> Option<&Track> {
        self.tracks.iter().find(|t| t.number == number)
    }

    pub fn is_audio(&self, lba: u32) -> bool {
        self.track_at(lba).map(|t| t.kind == Kind::Audio).unwrap_or(false)
    }

    /// The raw 2352 bytes of a sector (sync + header added for cooked images; zeros outside the disc).
    pub fn read(&self, lba: u32) -> Vec<u8> {
        let mut out = vec![0u8; SECTOR];
        let Some(t) = self.track_at(lba) else {
            return out;
        };
        let fs = t.file_sector + (lba - t.start) as i64;
        if fs < 0 {
            return out; // PREGAP silence, not stored in the file
        }
        let sb = t.sector_bytes;
        let off = fs as u64 * sb as u64;
        let dst = if sb == SECTOR { &mut out[..] } else { &mut out[16..16 + 2048] };
        match &self.sources[t.source] {
            Source::File(f) => {
                let mut f = f.lock().unwrap();
                if f.seek(SeekFrom::Start(off)).is_ok() {
                    let _ = f.read(dst);
                }
            }
            Source::Memory(m) => {
                let o = off as usize;
                if o < m.len() {
                    let n = sb.min(m.len() - o);
                    dst[..n].copy_from_slice(&m[o..o + n]);
                }
            }
        }
        if sb != SECTOR {
            write_header(&mut out, lba, 1);
        }
        out
    }

    pub fn position(&self, lba: u32) -> Position {
        let absolute = lba + LEAD_IN;
        match self.track_at(lba) {
            Some(t) if lba < t.index1 => Position { track: t.number, index: 0, relative: t.index1 - lba, absolute },
            Some(t) => Position { track: t.number, index: 1, relative: lba - t.index1, absolute },
            None => {
                let end = self.sectors();
                Position { track: LEAD_OUT_TRACK, index: 1, relative: lba.saturating_sub(end), absolute }
            }
        }
    }

    // ---------------------------------------------------------------- ISO9660

    /// User data (2048 bytes) of a data sector: mode 1 at +16, mode 2 form 1 at +24.
    pub fn user_data(&self, lba: u32) -> Vec<u8> {
        let s = self.read(lba);
        let at = if s[15] == 1 { 16 } else { 24 };
        s[at..at + 2048].to_vec()
    }

    /// A file by its ISO9660 path ("SYSTEM.CNF", "\\DIR\\FILE.EXE;1"), case-insensitive.
    pub fn read_file(&self, path: &str) -> Option<Vec<u8>> {
        let pvd = self.user_data(16);
        if &pvd[1..6] != b"CD001" {
            return None;
        }
        let le = |b: &[u8], o: usize| u32::from_le_bytes([b[o], b[o + 1], b[o + 2], b[o + 3]]);
        let (mut lba, mut size) = (le(&pvd, 156 + 2), le(&pvd, 156 + 10));
        let parts: Vec<String> = path.trim_start_matches(['\\', '/']).split(['\\', '/'])
            .filter(|p| !p.is_empty()).map(|p| p.to_ascii_uppercase()).collect();
        for (i, want) in parts.iter().enumerate() {
            let last = i + 1 == parts.len();
            let want = want.split(';').next().unwrap_or("").to_string();
            let mut found = None;
            'dir: for s in 0..size.div_ceil(2048) {
                let d = self.user_data(lba + s);
                let mut o = 0;
                while o < 2048 && d[o] != 0 {
                    let len = d[o] as usize;
                    if o + 33 > 2048 || len < 34 {
                        break;
                    }
                    let nl = d[o + 32] as usize;
                    let name = String::from_utf8_lossy(&d[o + 33..(o + 33 + nl).min(2048)]).to_ascii_uppercase();
                    let name = name.split(';').next().unwrap_or("").to_string();
                    let is_dir = d[o + 25] & 2 != 0;
                    if name == want && is_dir != last {
                        found = Some((le(&d, o + 2), le(&d, o + 10)));
                        break 'dir;
                    }
                    o += len;
                }
            }
            let (l, sz) = found?;
            lba = l;
            size = sz;
        }
        let mut out = Vec::with_capacity(size as usize);
        for s in 0..size.div_ceil(2048) {
            out.extend(self.user_data(lba + s));
        }
        out.truncate(size as usize);
        Some(out)
    }

    /// The disc's license text region ("Sony Computer Entertainment Amer…") from sector 4.
    pub fn region(&self) -> Option<&'static str> {
        let d = self.user_data(4);
        let text = String::from_utf8_lossy(&d[..0x60]).to_string();
        if text.contains("Sony Computer Entertainment Amer") {
            Some("SCEA")
        } else if text.contains("Sony Computer Entertainment Euro") {
            Some("SCEE")
        } else if text.contains("Sony Computer Entertainment Inc") {
            Some("SCEI")
        } else {
            None
        }
    }
}

/// Sync pattern and header (absolute MSF in BCD, mode) for a sector at `lba`.
pub fn write_header(s: &mut [u8], lba: u32, mode: u8) {
    s[0] = 0;
    s[1..11].fill(0xFF);
    s[11] = 0;
    let (m, sec, f) = msf(lba + LEAD_IN);
    s[12] = bcd(m);
    s[13] = bcd(sec);
    s[14] = bcd(f);
    s[15] = mode;
}

/// Build a small ISO9660 data disc in memory (MODE2/2352, form 1 sectors, licensed as
/// SCEA) holding `files` in the root directory. For tests and examples.
pub fn build_iso(files: &[(&str, &[u8])], total_sectors: u32) -> Vec<u8> {
    let mut user: Vec<Vec<u8>> = vec![vec![0u8; 2048]; total_sectors as usize];
    // license area (sector 4)
    let lic = b"          Licensed  by          Sony Computer Entertainment Amer  ica ";
    user[4][..lic.len()].copy_from_slice(lic);
    // files start at sector 24; root directory at 22
    let root = 22u32;
    let mut next = 24u32;
    let mut dir = Vec::new();
    let rec = |name: &[u8], lba: u32, size: u32, is_dir: bool| {
        let nl = name.len();
        let len = 33 + nl + (1 - nl % 2);
        let mut r = vec![0u8; len];
        r[0] = len as u8;
        r[2..6].copy_from_slice(&lba.to_le_bytes());
        r[6..10].copy_from_slice(&lba.to_be_bytes());
        r[10..14].copy_from_slice(&size.to_le_bytes());
        r[14..18].copy_from_slice(&size.to_be_bytes());
        r[25] = if is_dir { 2 } else { 0 };
        r[28] = 1;
        r[32] = nl as u8;
        r[33..33 + nl].copy_from_slice(name);
        r
    };
    dir.extend(rec(&[0], root, 2048, true));
    dir.extend(rec(&[1], root, 2048, true));
    let mut placed = Vec::new();
    for (name, data) in files {
        let n = (data.len() as u32).div_ceil(2048).max(1);
        placed.push((format!("{name};1"), next, data.len() as u32));
        for (k, chunk) in data.chunks(2048).enumerate() {
            user[(next as usize) + k][..chunk.len()].copy_from_slice(chunk);
        }
        next += n;
    }
    for (name, lba, size) in &placed {
        dir.extend(rec(name.as_bytes(), *lba, *size, false));
    }
    user[root as usize][..dir.len()].copy_from_slice(&dir);
    // primary volume descriptor (16) and terminator (17)
    let pvd = &mut user[16];
    pvd[0] = 1;
    pvd[1..6].copy_from_slice(b"CD001");
    pvd[6] = 1;
    pvd[40..48].copy_from_slice(b"RETROPY ");
    pvd[80..84].copy_from_slice(&total_sectors.to_le_bytes());
    let root_rec = rec(&[0], root, 2048, true);
    pvd[156..156 + root_rec.len()].copy_from_slice(&root_rec);
    user[17][0] = 255;
    user[17][1..6].copy_from_slice(b"CD001");
    // raw sectors
    let mut out = vec![0u8; total_sectors as usize * SECTOR];
    for (lba, u) in user.iter().enumerate() {
        let s = &mut out[lba * SECTOR..(lba + 1) * SECTOR];
        write_header(s, lba as u32, 2);
        s[18] = 0x08; // submode: data (both copies)
        s[22] = 0x08;
        s[24..24 + 2048].copy_from_slice(u);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn iso_files_round_trip() {
        let exe = vec![7u8; 5000];
        let d = Disc::from_raw_sectors(build_iso(&[("SYSTEM.CNF", b"BOOT = cdrom:\\GAME.EXE;1\r\n"),
                                                   ("GAME.EXE", &exe)], 64));
        assert_eq!(d.read_file("SYSTEM.CNF").unwrap(), b"BOOT = cdrom:\\GAME.EXE;1\r\n");
        assert_eq!(d.read_file("\\game.exe;1").unwrap(), exe);
        assert!(d.read_file("MISSING.TXT").is_none());
        assert_eq!(d.region(), Some("SCEA"));
        let s = d.read(30);
        assert_eq!(&s[12..16], &[0x00, 0x02, 0x30, 2]); // 00:02:30, mode 2
    }

    #[test]
    fn cue_layout_with_pregaps_and_files() {
        let dir = std::env::temp_dir().join(format!("retropy-cue-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("a.bin"), vec![0u8; SECTOR * 100]).unwrap();
        std::fs::write(dir.join("b.bin"), vec![1u8; SECTOR * 50]).unwrap();
        let cue = "FILE \"a.bin\" BINARY\n  TRACK 01 MODE2/2352\n    INDEX 01 00:00:00\n  TRACK 02 AUDIO\n    \
                   INDEX 00 00:01:00\n    INDEX 01 00:01:10\nFILE \"b.bin\" BINARY\n  TRACK 03 AUDIO\n    PREGAP 00:00:05\n    \
                   INDEX 01 00:00:00\n";
        let d = Disc::from_cue(cue, &dir, None).unwrap();
        let t: Vec<(u8, u32, u32, u32)> = d.tracks.iter().map(|t| (t.number, t.start, t.index1, t.length)).collect();
        assert_eq!(t, vec![(1, 0, 0, 75), (2, 75, 85, 25), (3, 100, 105, 55)]);
        assert_eq!(d.sectors(), 155);
        assert!(d.is_audio(90) && !d.is_audio(10));
        assert_eq!(d.position(80), Position { track: 2, index: 0, relative: 5, absolute: 230 });
        assert_eq!(d.position(90), Position { track: 2, index: 1, relative: 5, absolute: 240 });
        assert_eq!(d.read(102)[0], 0, "PREGAP sectors are silence");
        assert_eq!(d.read(105)[0], 1, "track 3 data starts at INDEX 01");
        assert_eq!(d.position(160).track, LEAD_OUT_TRACK);
        std::fs::remove_dir_all(&dir).ok();
    }
}
