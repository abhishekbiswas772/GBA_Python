"""NES core: CPU trace against nestest, blargg hardware tests, bridge + input."""
import re
from pathlib import Path

import pytest

from src.bridge import NESMachine, detect, load
from src.core.nes.nes import NES

ROMS = Path(__file__).parent / "roms" / "nes"


def _need(name: str) -> Path:
    p = ROMS / name
    if not p.exists():
        pytest.skip(f"missing test ROM {name}")
    return p


def test_nestest_matches_reference_log():
    """Every instruction (official + unofficial) matches the Nintendulator trace,
    registers and cycle count."""
    n = NES(_need("nestest.nes"))
    c = n.cpu
    c.pc, c.cyc, c.p, c.s = 0xC000, 7, 0x24, 0xFD
    pat = re.compile(r"^(\w{4}).*A:(\w\w) X:(\w\w) Y:(\w\w) P:(\w\w) SP:(\w\w).*CYC:(\d+)")
    lines = (ROMS / "nestest.log").read_text().splitlines()
    for k, line in enumerate(lines):
        m = pat.match(line)
        exp = (int(m[1], 16), int(m[2], 16), int(m[3], 16), int(m[4], 16),
               int(m[5], 16), int(m[6], 16), int(m[7]))
        got = (c.pc, c.a, c.x, c.y, c.p, c.s, c.cyc)
        assert got == exp, f"line {k + 1}: {line[:48]}"
        c.step()
    assert n.ram[2] == 0 and n.ram[3] == 0


def _blargg(path: Path, max_frames: int = 3000) -> tuple[int, str]:
    """Run a blargg test that reports through $6000 (status) / $6004 (text)."""
    n = NES(path)
    r = n.cart.prg_ram
    n.cart.save_ram = lambda *a, **k: False
    started = False
    for _ in range(max_frames):
        n.run_frame()
        if r[1:4] != b"\xde\xb0\x61":
            continue
        st = r[0]
        if st == 0x80:
            started = True
        elif st == 0x81:                   # test asks for a reset
            n.run_frames(10)
            n.reset()
        elif started or st < 0x80:
            text = bytes(r[4:r.index(0, 4)]).decode("latin-1")
            return st, " | ".join(text.split("\n"))
    return -1, "no result"


@pytest.mark.slow
@pytest.mark.parametrize("rom", [
    "official_only.nes", "all_instrs.nes", "1-instr_timing.nes", "2-branch_timing.nes",
    "apu_test.nes", "1-clocking.nes", "2-details.nes", "3-A12_clocking.nes",
])
def test_blargg_status_roms(rom):
    status, text = _blargg(_need(rom))
    assert status == 0, text


@pytest.mark.slow
@pytest.mark.parametrize("rom", [
    "01.basics.nes", "02.alignment.nes", "03.corners.nes", "04.flip.nes",
    "05.left_clip.nes", "06.right_edge.nes", "07.screen_bottom.nes", "08.double_height.nes",
])
def test_sprite_hit_roms(rom):
    n = NES(_need(rom))
    n.run_frames(240)
    assert n.ram[0xF8] == 1, f"result code {n.ram[0xF8]}"


@pytest.mark.slow
@pytest.mark.parametrize("rom", ["palette_ram.nes", "sprite_ram.nes", "vram_access.nes",
                                 "vbl_clear_time.nes"])
def test_old_ppu_roms(rom):
    n = NES(_need(rom))
    n.run_frames(240)
    assert n.ram[0xF0] == 1, f"result code {n.ram[0xF0]}"


def test_bridge_loads_nes():
    rom = _need("demo_ntsc.nes")
    assert detect(rom) == "NES"
    m = load(rom)
    assert isinstance(m, NESMachine)
    assert (m.system, m.width, m.height) == ("NES", 256, 240)
    assert 60.0 < m.fps < 60.2
    m.apu.sampling = True
    m.run_frame()
    assert len(m.frame_rgb()) == 256 * 240 * 3
    samples = m.apu.take_samples()
    assert samples.typecode == "h" and 1300 < len(samples) < 1600   # ~735 stereo pairs


def test_bridge_input_and_shoulders_are_noops():
    m = load(_need("demo_ntsc.nes"))
    m.press("Start")
    m.press("L")                       # no shoulder buttons on the NES pad
    assert m.core.pads[0].state == 1 << 3
    m.release("Start")
    m.release("R")
    assert m.core.pads[0].state == 0


def test_frameskip_keeps_last_frame():
    m = load(_need("full_palette.nes"))
    m.run_frames(30)
    drawn = m.frame_rgb()
    m.run_frame(render=False)
    assert m.frame_rgb() == drawn
    assert not m.core.ppu.skip_render


def test_joypad_shift_register():
    n = NES(_need("demo_ntsc.nes"))
    n.press("a")
    n.press("right")
    n._wr(0x4016, 1)
    n._wr(0x4016, 0)
    bits = [n._rd(0x4016) & 1 for _ in range(9)]
    assert bits == [1, 0, 0, 0, 0, 0, 0, 1, 1]       # A ... Right, then 1s


def test_unknown_mapper_is_rejected():
    rom = b"NES\x1a" + bytes([1, 1, 0xF0, 0xF0]) + bytes(8) + bytes(0x4000 + 0x2000)
    with pytest.raises(Exception):
        NES(rom)
