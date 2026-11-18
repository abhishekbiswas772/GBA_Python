from pathlib import Path

import pytest

from tests.helpers import CYCLE_BUDGET, run_serial

ROMS = sorted((Path(__file__).parent / "roms" / "cpu_instrs").glob("*.gb"))
ROMS.append(Path(__file__).parent / "roms" / "other" / "instr_timing.gb")
ROMS.append(Path(__file__).parent / "roms" / "other" / "cpu_instrs.gb")


@pytest.mark.slow
@pytest.mark.parametrize("cgb", [False, True], ids=["dmg", "cgb"])
@pytest.mark.parametrize("rom", ROMS, ids=lambda p: p.name)
def test_blargg_rom(rom, cgb):
    # the combined ROM runs all 11 sub-tests: give it a 3x budget
    text, cycles, wall, done = run_serial(
        rom, budget=CYCLE_BUDGET * (3 if rom.name == "cpu_instrs.gb" else 1), cgb=cgb)
    assert done, f"TIMEOUT after {cycles} cycles; serial: {text!r}"
    assert "Passed" in text, f"serial: {text!r}"
