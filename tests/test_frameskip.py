"""Frameskip (run_frame(render=False)) must not change emulation: after skipping
frames, the next drawn frame, the cycle count and the CPU state are identical
to drawing every frame."""
from pathlib import Path

import pytest

from src import bridge

ROMS = Path(__file__).parent / "roms"
CASES = [
    ROMS / "other" / "dmg-acid2.gb",          # window line counter (DMG)
    ROMS / "other" / "cgb-acid2.gbc",         # window line counter (CGB)
    ROMS / "gba" / "stripes.gba",
    ROMS / "gba" / "shades.gba",
    ROMS / "gba" / "hello.gba",
]


def _state(m):
    core = m.core
    if m.system == "GBA":
        return (core.fb, core.now, list(core.cpu.r), core.gba.save_state())
    p = core.ppu
    return (m.frame_rgb(), core.total_cycles, core.cpu.pc, p.ly, p.win_y, p.win_active)


@pytest.mark.parametrize("rom", CASES, ids=lambda p: p.name)
def test_skipped_frames_match_drawn_frames(rom):
    if not rom.is_file():
        pytest.skip(f"missing {rom.name}")
    drawn = bridge.load(rom)
    skipped = bridge.load(rom)
    for _ in range(3):                       # a few skip/draw rounds
        drawn.run_frames(20)
        for i in range(20):
            skipped.run_frame(render=(i == 19))
        assert _state(drawn) == _state(skipped)


def test_skip_leaves_last_frame_on_screen():
    rom = ROMS / "gba" / "stripes.gba"
    if not rom.is_file():
        pytest.skip("missing stripes.gba")
    m = bridge.load(rom)
    m.run_frames(10)
    before = m.frame_rgb()
    m.run_frame(render=False)
    assert m.frame_rgb() == before
