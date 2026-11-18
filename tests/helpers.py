import time
from pathlib import Path

REAL_HARDWARE_HZ = 4194304
CYCLE_BUDGET = 100_000_000


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
    from src.gameboy.cartridge import Cartridge
    from src.gameboy.cpu import CPU
    from src.gameboy.mmu import MMU
    from src.gameboy.timer import Timer
    mmu = MMU(Cartridge(rom))
    timer = Timer(mmu)
    mmu.timer = timer
    cpu = CPU(mmu)
    cpu.pc = 0x150
    return cpu, mmu, timer


def build_ppu():
    from src.gameboy.cartridge import Cartridge
    from src.gameboy.mmu import MMU
    from src.gameboy.ppu import PPU
    from src.gameboy.timer import Timer
    mmu = MMU(Cartridge(make_rom()))
    timer = Timer(mmu)
    mmu.timer = timer
    ppu = PPU(mmu)
    mmu.ppu = ppu
    return mmu, ppu


def run_serial(rom_path: str | Path, budget: int = CYCLE_BUDGET):
    """Run a ROM until it prints Passed/Failed on serial.

    Returns (text, cycles, wall_seconds, done).
    """
    from src.gameboy.gameboy import GameBoy
    gb = GameBoy(rom_path)
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
