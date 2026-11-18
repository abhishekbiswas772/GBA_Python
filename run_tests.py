"""Headless runner for Blargg test ROMs."""
import sys
from pathlib import Path

from tests.helpers import CYCLE_BUDGET, REAL_HARDWARE_HZ, run_serial

ROOT = Path(__file__).parent

ROMS = [
    (Path("tests/roms/cpu_instrs/01-special.gb"), None),
    (Path("tests/roms/cpu_instrs/02-interrupts.gb"), None),
    (Path("tests/roms/cpu_instrs/03-op sp,hl.gb"), None),
    (Path("tests/roms/cpu_instrs/04-op r,imm.gb"), None),
    (Path("tests/roms/cpu_instrs/05-op rp.gb"), None),
    (Path("tests/roms/cpu_instrs/06-ld r,r.gb"), None),
    (Path("tests/roms/cpu_instrs/07-jr,jp,call,ret,rst.gb"), None),
    (Path("tests/roms/cpu_instrs/08-misc instrs.gb"), None),
    (Path("tests/roms/cpu_instrs/09-op r,r.gb"), None),
    (Path("tests/roms/cpu_instrs/10-bit ops.gb"), None),
    (Path("tests/roms/cpu_instrs/11-op a,(hl).gb"), None),
    (Path("tests/roms/other/instr_timing.gb"), None),
    (Path("tests/roms/other/cpu_instrs.gb"), None),
    (Path("tests/roms/other/dmg-acid2.gb"), "no serial output - tested via pytest --slow"),
]


def main() -> int:
    passed = failed = skipped = 0
    total_cycles = 0
    total_wall = 0.0

    for rel, skip in ROMS:
        name = rel.as_posix()
        if skip:
            print(f"SKIP  {name:<42} {skip}")
            skipped += 1
            continue
        text, cycles, wall, done = run_serial(ROOT / rel,
                                              budget=CYCLE_BUDGET * (3 if rel.name == "cpu_instrs.gb" else 1))
        total_cycles += cycles
        total_wall += wall
        if not done:
            result = "TIMEOUT"
        elif "Passed" in text:
            result = "PASS"
        else:
            result = "FAIL"
        if result == "PASS":
            passed += 1
        else:
            failed += 1
        speed = cycles / wall / REAL_HARDWARE_HZ * 100 if wall > 0 else 0.0
        print(f"{result:<5} {name:<42} {wall:6.1f}s  {speed:5.0f}% realtime")
        if result != "PASS":
            print(f"      serial: {text!r}")

    speed = total_cycles / total_wall / REAL_HARDWARE_HZ * 100 if total_wall > 0 else 0.0
    print(f"{passed} passed, {failed} failed, {skipped} skipped "
          f"in {total_wall:.1f}s ({speed:.0f}% of real hardware)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
