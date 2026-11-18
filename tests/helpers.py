import time
from pathlib import Path

REAL_HARDWARE_HZ = 4194304
CYCLE_BUDGET = 100_000_000

# Commercial ROM folders, in lookup order. "src/rom" is the legacy layout.
_ROM_SEARCH_DIRS = ("rom", "rom/gb", "rom/gb_color", "rom/gba", "src/rom")


def find_rom(name: str) -> Path | None:
    """Resolve a ROM by filename (or glob pattern) across the repo's ROM folders.

    Searches rom/, rom/gb/, rom/gb_color/, rom/gba/ (current layout) and the
    legacy src/rom/ (old layout). Returns the first match (glob matches in
    sorted order per folder) or None when nothing is found, so callers can
    pytest.skip() when a commercial ROM is absent. Read-only: callers must
    never write to or delete the returned ROM or its .sav.
    """
    root = Path(__file__).resolve().parent.parent
    is_pattern = any(ch in name for ch in "*?[")
    for sub in _ROM_SEARCH_DIRS:
        folder = root / sub
        if is_pattern:
            found = sorted(folder.glob(name))
            if found:
                return found[0]
        elif (folder / name).is_file():
            return folder / name
    return None


def blank_rom() -> bytearray:
    rom = bytearray(0x8000)
    rom[0x100:0x104] = b"\x00\xC3\x50\x01"
    return rom


def make_rom(program: bytes = b"", org: int = 0x150,
             patches: dict[int, bytes] | None = None) -> bytes:
    rom = blank_rom()
    rom[org:org + len(program)] = program
    for addr, data in (patches or {}).items():
        rom[addr:addr + len(data)] = data
    return bytes(rom)


def build(rom: bytes):
    from src.core.gameboy.cartridge import Cartridge
    from src.core.gameboy.cpu import CPU
    from src.core.gameboy.joypad import Joypad
    from src.core.gameboy.mmu import MMU
    from src.core.gameboy.timer import Timer
    mmu = MMU(Cartridge(rom))
    timer = Timer(mmu)
    mmu.timer = timer
    mmu.joypad = Joypad(mmu)
    cpu = CPU(mmu)
    cpu.pc = 0x150
    return cpu, mmu, timer


def build_ppu():
    from src.core.gameboy.cartridge import Cartridge
    from src.core.gameboy.mmu import MMU
    from src.core.gameboy.ppu import PPU
    from src.core.gameboy.timer import Timer
    mmu = MMU(Cartridge(make_rom()))
    timer = Timer(mmu)
    mmu.timer = timer
    ppu = PPU(mmu)
    mmu.ppu = ppu
    return mmu, ppu


def run_serial(rom_path: str | Path, budget: int = CYCLE_BUDGET, cgb: bool | None = False):
    """Run a ROM until it prints Passed/Failed on serial.

    cgb=False forces DMG mode (the Blargg ROMs are CGB-flagged), True forces CGB.
    Returns (text, cycles, wall_seconds, done).
    """
    from src.core.gameboy.gameboy import GameBoy
    gb = GameBoy(rom_path, cgb=cgb)
    t0 = time.perf_counter()
    steps = gb.steps
    serial = gb.mmu.serial_output
    cycles = 0
    last = 0
    text = ""
    done = False
    while cycles < budget:
        for _ in range(512):
            cycles += steps()
        n = len(serial)
        if n != last:
            last = n
            text = gb.mmu.serial_text()
            if "Passed" in text or "Failed" in text:
                done = True
                break
    return text, cycles, time.perf_counter() - t0, done
