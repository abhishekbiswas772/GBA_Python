from src.gameboy.cartridge import Cartridge
from src.gameboy.cpu import CPU
from src.gameboy.mmu import MMU
from src.gameboy.timer import Timer
from pathlib import Path

class GameBoy:
    def __init__(self, rom_path: str | Path):
        self.cart = Cartridge.from_file(rom_path)
        self.mmu = MMU(self.cart)
        self.timer = Timer(self.mmu)
        self.mmu.timer = self.timer
        self.cpu = CPU(self.mmu)
        self.total_cycles = 0

    def steps(self) -> int:
        cycles = self.cpu.step()
        self.timer.tick(cycles)
        self.total_cycles += cycles
        return cycles


    def run(self, max_steps = None, doctor_log = None, stop_on_serial = ("Passed", "Failed")):
        steps = 0
        printed = 0
        while max_steps is None or steps < max_steps:
            if doctor_log is not None and not self.cpu.halted:
                doctor_log.write(self.cpu.doctor_line() + "\n")
            self.steps()
            steps += 1
            out =  self.mmu.serial_output
            if len(out) > printed:
                print("".join(out[printed:]), end="", flush=True)
                printed = len(out)
                text = self.mmu.serial_text()
                if any(word in text for word in stop_on_serial):
                    break
        return steps