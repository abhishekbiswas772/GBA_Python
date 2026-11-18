from pathlib import Path

import pytest

from tests.helpers import run_serial

ROMS = sorted((Path(__file__).parent / "roms" / "cpu_instrs").glob("*.gb"))
ROMS.append(Path(__file__).parent / "roms" / "other" / "instr_timing.gb")


@pytest.mark.slow
@pytest.mark.parametrize("rom", ROMS, ids=lambda p: p.name)
def test_blargg_rom(rom):
    text, cycles, wall, done = run_serial(rom)
    assert done, f"TIMEOUT after {cycles} cycles; serial: {text!r}"
    assert "Passed" in text, f"serial: {text!r}"
