from src.core.gameboy.apu import APU
from src.core.gameboy.cartridge import Cartridge
from src.core.gameboy.cpu import CPU
from src.core.gameboy.joypad import Joypad
from src.core.gameboy.mmu import MMU
from src.core.gameboy.ppu import PPU
from src.core.gameboy.timer import Timer
from pathlib import Path

class GameBoy:
    def __init__(self, rom_path: str | Path, cgb: bool | None = None):
        """cgb=None: auto-detect from the header (0x0143 bit 7); False forces DMG mode."""
        self.cart = Cartridge.from_file(rom_path)
        self.cgb = self.cart.header.supports_cgb if cgb is None else bool(cgb)
        self.mmu = MMU(self.cart, cgb=self.cgb)
        self.timer = Timer(self.mmu)
        self.mmu.timer = self.timer
        self.joypad = Joypad(self.mmu)
        self.mmu.joypad = self.joypad
        self.ppu = PPU(self.mmu, cgb=self.cgb)
        self.mmu.ppu = self.ppu
        self.apu = APU()
        self.apu.post_boot()                    # no boot ROM: start in post-boot state
        self.mmu.apu = self.apu
        self.cpu = CPU(self.mmu)
        if self.cgb:                       # CGB boot ROM hand-off state (A=0x11 = CGB)
            c = self.cpu
            c.a, c.f = 0x11, 0x80
            c.b, c.c = 0x00, 0x00
            c.d, c.e = 0xFF, 0x56
            c.h, c.l = 0x00, 0x0D
        self.total_cycles = 0              # real-time T-cycles (4.194304 MHz)
        self._frames_since_save = 0
        if not self.cgb:
            self.steps = self._steps_dmg   # no HDMA / double speed: skip those checks

    def save(self) -> bool:
        return self.cart.save_ram()

    def steps(self) -> int:
        """Run one CPU instruction; returns the CPU cycles it took.

        Timer/DIV run on the CPU clock; PPU and APU run in real time, so in CGB
        double-speed mode they get half the CPU cycles.
        """
        mmu = self.mmu
        cycles = self.cpu.step()
        if mmu.stall:                      # HDMA halted the CPU
            cycles += mmu.stall
            mmu.stall = 0
        self.timer.tick(cycles)
        rt = cycles >> mmu.double_speed
        self.ppu.step(rt)
        self.apu.step(rt)
        self.total_cycles += rt
        return cycles

    def _steps_dmg(self) -> int:
        cycles = self.cpu.step()
        self.timer.tick(cycles)
        self.ppu.step(cycles)
        self.apu.step(cycles)
        self.total_cycles += cycles
        return cycles

    def run_frame(self) -> None:
        target = self.ppu.frame_count + 1
        while self.ppu.frame_count < target:
            self.steps()
        self.ppu.frame_ready = False
        self._frames_since_save += 1
        if self._frames_since_save >= 600:      # autosave dirty battery RAM ~10s
            self._frames_since_save = 0
            self.cart.save_ram()


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
