"""NES system: wires the 2A03 CPU, 2C02 PPU, APU, cartridge and joypads.

The CPU runs in chunks up to the next point where something must happen on
time: the end of a scanline, the vblank NMI at line 241 dot 1, the MMC3
scanline clock at dot 260 (only on carts that use it) and the APU frame IRQ.
Between chunks the PPU catches up (``ppu.sync``); register accesses also sync
it to the exact CPU cycle.
"""
from __future__ import annotations

from pathlib import Path

from src.core.nes.apu import APU
from src.core.nes.cartridge import Cartridge
from src.core.nes.common import BUTTON_BITS, CPU_HZ, VBLANK_LINE, VISIBLE_LINES, PRERENDER_LINE
from src.core.nes.cpu import CPU
from src.core.nes.ppu import PPU


NMI_POLL = 1           # dots: NMI after the 2nd-to-last cycle of an instruction waits one more


class Joypad:
    def __init__(self):
        self.state = 0          # bit set = pressed (A, B, Select, Start, Up, Down, Left, Right)
        self.shift = 0
        self.strobe = False

    def write(self, v: int) -> None:
        self.strobe = bool(v & 1)
        if self.strobe:
            self.shift = 0

    def read(self) -> int:
        if self.strobe:
            return 0x40 | (self.state & 1)
        if self.shift >= 8:
            return 0x41
        bit = (self.state >> self.shift) & 1
        self.shift += 1
        return 0x40 | bit


class NES:
    def __init__(self, rom: str | Path | bytes):
        if isinstance(rom, (bytes, bytearray)):
            self.cart = Cartridge(bytes(rom))
        else:
            self.cart = Cartridge.from_file(rom)
        cart = self.cart
        self.mapper = mapper = cart.mapper
        self.ram = bytearray(0x800)
        self.pads = (Joypad(), Joypad())
        self.ppu = PPU(mapper, cart.chr, cart.chr_ram)
        if cart.header.four_screen:
            mapper.nt[:] = [0, 0x400, 0x800, 0xC00]
        self.cpu = cpu = CPU(self.ram, cart.prg, mapper.prg_banks, self._rd, self._wr, self._peek)
        self.apu = APU(cpu.read)
        self.ppu.nmi = self._nmi
        self.ppu.nmi_cancel = self._nmi_cancel
        mapper.on_irq = self._update_irq
        self.apu.on_irq = self._update_irq
        self._scanline_irq = mapper.wants_scanlines
        self._frames_since_save = 0
        cpu.reset()

    # ------------------------------------------------------------ interrupts
    def _nmi(self) -> None:
        self.cpu.nmi_pending = True
        self.cpu.signal = True

    def _nmi_cancel(self) -> None:
        self.cpu.nmi_pending = False

    def _update_irq(self) -> None:
        line = self.mapper.irq or self.apu.irq
        cpu = self.cpu
        if line != cpu.irq_line:
            cpu.irq_line = line
            if line:
                cpu.signal = True

    # ------------------------------------------------------------ bus ($2000-$7FFF and writes above)
    def _rd(self, a: int) -> int:
        if a < 0x4000:
            if a < 0x2000:
                return self.ram[a & 0x7FF]
            self.ppu.sync(self.cpu.cyc)
            return self.ppu.read_reg(a & 7)
        if a < 0x4020:
            if a == 0x4016:
                return self.pads[0].read()
            if a == 0x4017:
                return self.pads[1].read()
            if a == 0x4015:
                return self.apu.read_status(self.cpu.cyc)
            return 0x40
        if a >= 0x6000:
            if a < 0x8000:
                c = self.cart
                return c.prg_ram[a & 0x1FFF] if c.prg_ram_enabled else (a >> 8)
            return self.cart.prg[self.mapper.prg_banks[(a >> 13) & 3] + (a & 0x1FFF)]
        return a >> 8                     # open bus

    def _wr(self, a: int, v: int) -> None:
        cpu = self.cpu
        if a < 0x4000:
            if a < 0x2000:
                self.ram[a & 0x7FF] = v
                return
            self.ppu.sync(cpu.cyc)
            if self.ppu.write_reg(a & 7, v):
                cpu.signal = True
                cpu.nmi_delay = True          # taken after the next instruction
            return
        if a < 0x4020:
            if a == 0x4014:                                   # OAM DMA
                self.ppu.sync(cpu.cyc)
                base = v << 8
                rd = cpu.read
                self.ppu.oam_dma(bytes(rd(base + k) for k in range(256)) if base >= 0x2000
                                 else bytes(self.ram[base & 0x7FF:(base & 0x7FF) + 256]))
                cpu.stall += 513 + (cpu.cyc & 1)
                cpu.signal = True
            elif a == 0x4016:
                self.pads[0].write(v)
                self.pads[1].write(v)
            elif a != 0x4018:
                self.apu.write(a, v, cpu.cyc)
            return
        if a < 0x8000:
            if a >= 0x6000:
                c = self.cart
                if c.prg_ram_enabled and c.prg_ram_writable:
                    i = a & 0x1FFF
                    if c.prg_ram[i] != v:
                        c.prg_ram[i] = v
                        c.ram_dirty = True
            return
        self.ppu.sync(cpu.cyc)                                # bank switch: draw what came before
        self.mapper.write(a, v)

    def _peek(self, a: int) -> int:
        """Side-effect free read (instruction fetch outside PRG ROM)."""
        if a < 0x2000:
            return self.ram[a & 0x7FF]
        if a >= 0x8000:
            return self.cart.prg[self.mapper.prg_banks[(a >> 13) & 3] + (a & 0x1FFF)]
        if a >= 0x6000:
            return self.cart.prg_ram[a & 0x1FFF]
        return 0

    # ------------------------------------------------------------ input
    def press(self, button: str) -> None:
        self.pads[0].state |= 1 << BUTTON_BITS[button.lower()]

    def release(self, button: str) -> None:
        self.pads[0].state &= ~(1 << BUTTON_BITS[button.lower()])

    # ------------------------------------------------------------ running
    def _next_stop(self) -> int:
        ppu = self.ppu
        line, start = ppu.line, ppu.line_start
        if line == VBLANK_LINE and ppu.ev == 0:
            dot = start + 1                                   # vblank NMI
        elif (self._scanline_irq and (line < VISIBLE_LINES or line == PRERENDER_LINE)
              and ppu.mask & 0x18 and ppu.a12_dot()
              and ppu.line_start + ppu.a12_dot() >= ppu.now):
            dot = start + ppu.a12_dot() + 1                   # just after the MMC3 clock
        else:
            dot = start + ppu._line_len(line)
        return (dot + 2) // 3

    def run_frame(self) -> None:
        cpu, ppu, apu = self.cpu, self.ppu, self.apu
        target = ppu.frame + 1
        while ppu.frame < target:
            stop = self._next_stop()
            irq_at = apu.next_irq_cycle()
            if irq_at < stop:
                stop = irq_at
            if stop <= cpu.cyc:
                stop = cpu.cyc + 1
            cpu.run(stop)
            ppu.sync(cpu.cyc)
            # NMI polling: an NMI raised after the instruction's second-to-last cycle
            # is only taken after the following instruction
            if cpu.nmi_pending and not cpu.nmi_delay and ppu.nmi_dot > (cpu.cyc - 2) * 3 + NMI_POLL:
                cpu.nmi_delay = True
            if cpu.cyc >= irq_at:
                apu.sync(cpu.cyc)
        apu.end_frame(cpu.cyc)
        self._frames_since_save += 1
        if self._frames_since_save >= 600:
            self._frames_since_save = 0
            self.cart.save_ram()

    def run_frames(self, n: int) -> None:
        for _ in range(n):
            self.run_frame()

    def reset(self) -> None:
        """Console reset button (RAM and save RAM survive)."""
        self.apu.write(0x4015, 0, self.cpu.cyc)
        self.cpu.reset()

    def save(self) -> bool:
        return self.cart.save_ram()

    @property
    def seconds(self) -> float:
        return self.cpu.cyc / CPU_HZ
