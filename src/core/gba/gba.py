"""GBA system: wires CPU, bus, PPU, timers, DMA, keypad and runs the event loop.

Time is the CPU cycle counter (16.78 MHz). The CPU runs until the earliest
pending event (PPU line phase, timer overflow, audio sample), then that
component catches up.
"""
from pathlib import Path

from src.core.gba.apu import APU
from src.core.gba.bios import hle_swi
from src.core.gba.bus import Bus
from src.core.gba.cartridge import Cartridge
from src.core.gba.common import (
    BIOS_LATCH_STARTUP, SP_USR, SP_IRQ, SP_SVC, ROM_START, BUTTON_NAMES, CPU_HZ,
    REG_SOUNDBIAS, REG_POSTFLG, REG_BG2PA, REG_BG3PA,
)
from src.core.gba.cpu import CPU
from src.core.gba.dma import DMA
from src.core.gba.ppu import PPU
from src.core.gba.timer import Timers


class GBA:
    def __init__(self, rom: str | Path | bytes, bios: str | Path | None = None):
        if isinstance(rom, (bytes, bytearray)):
            self.cart = Cartridge(rom)
        else:
            self.cart = Cartridge.from_file(rom)
        bios_data = Path(bios).read_bytes() if bios is not None else None
        self.bus = bus = Bus(self.cart.rom, bios_data, self.cart.save, self.cart.rtc)
        self.cpu = cpu = CPU(bus)
        bus.cpu = cpu
        self.timers = Timers(bus)
        bus.timers = self.timers
        self.dma = DMA(bus)
        bus.dma = self.dma
        self.ppu = PPU(bus)
        self.ppu.dma = self.dma
        self.apu = APU(bus)
        self.timers.on_overflow = self.apu.timer_overflow
        self._frames_since_save = 0
        if bios_data is None:
            cpu.hle_swi = hle_swi
            cpu.reset_skip_bios(ROM_START, SP_USR, SP_IRQ, SP_SVC)
            cpu.bios_latch = BIOS_LATCH_STARTUP
            bus.io[REG_POSTFLG] = 1
            bus.io16[REG_SOUNDBIAS >> 1] = 0x200
            for pa in (REG_BG2PA, REG_BG3PA):            # BIOS leaves identity affine
                bus.io16[pa >> 1] = 0x100                # PA
                bus.io16[(pa >> 1) + 3] = 0x100          # PD
        else:
            cpu.pc = 0                       # real BIOS boots from the reset vector

    # ------------------------------------------------------------ input
    def press(self, button: str | int) -> None:
        bit = BUTTON_NAMES[button] if isinstance(button, str) else button
        self.bus.keys &= ~(1 << bit)

    def release(self, button: str | int) -> None:
        bit = BUTTON_NAMES[button] if isinstance(button, str) else button
        self.bus.keys |= 1 << bit

    # ------------------------------------------------------------ running
    def run_until(self, t_end: int) -> None:
        cpu, ppu, timers = self.cpu, self.ppu, self.timers
        while cpu.now < t_end:
            nxt = ppu.next_event
            if timers.next_event < nxt:
                nxt = timers.next_event
            if t_end < nxt:
                nxt = t_end
            cpu.run(nxt)
            now = cpu.now
            if timers.next_event <= now:
                timers.update(now)
            while ppu.next_event <= now:
                ppu.event()

    def run_frame(self) -> None:
        cpu, ppu, timers = self.cpu, self.ppu, self.timers
        target = ppu.frame + 1
        while ppu.frame < target:
            nxt = ppu.next_event
            if timers.next_event < nxt:
                nxt = timers.next_event
            cpu.run(nxt)
            now = cpu.now
            if timers.next_event <= now:
                timers.update(now)
            while ppu.next_event <= now:
                ppu.event()
        self.apu.end_frame()
        self._frames_since_save += 1
        if self._frames_since_save >= 600:
            self._frames_since_save = 0
            self.cart.save_ram()

    def run_frames(self, n: int) -> None:
        for _ in range(n):
            self.run_frame()

    def save(self) -> bool:
        return self.cart.save_ram()

    @property
    def seconds(self) -> float:
        return self.cpu.now / CPU_HZ
