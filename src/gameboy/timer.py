from src.gameboy.common import TIMER_FREQ_BITS as _FREQ_BITS


class Timer:
    def __init__(self, mmu):
        self.mmu = mmu
        self.div = 0
        self.tima = 0
        self.tma = 0
        self.tac = 0

    def _signal(self) -> bool:
        """The AND of TAC enable and the selected DIV bit; TIMA ticks on its falling edge."""
        return bool(self.tac & 0x04 and self.div & (1 << _FREQ_BITS[self.tac & 3]))

    def _inc_tima(self) -> None:
        if self.tima == 0xFF:
            self.tima = self.tma
            self.mmu.request_interrupt(0x04)
        else:
            self.tima += 1

    def tick(self, cycles: int) -> None:
        old = self.div
        self.div = (old + cycles) & 0xFFFF
        if self.tac & 0x04:
            b = _FREQ_BITS[self.tac & 3]
            edges = ((old + cycles) >> (b + 1)) - (old >> (b + 1))
            if edges:
                full = self.tima + edges
                if full > 0xFF:
                    self.tima = (self.tma + full) & 0xFF
                    self.mmu.request_interrupt(0x04)
                else:
                    self.tima = full

    def read(self, addr: int) -> int:
        if addr == 0xFF04:
            return self.div >> 8
        if addr == 0xFF05:
            return self.tima
        if addr == 0xFF06:
            return self.tma
        return self.tac | 0xF8

    def write(self, addr: int, value: int) -> None:
        if addr == 0xFF04:
            if self._signal():          # resetting DIV can make the selected bit fall
                self._inc_tima()
            self.div = 0
        elif addr == 0xFF05:
            self.tima = value & 0xFF
        elif addr == 0xFF06:
            self.tma = value & 0xFF
        else:
            was = self._signal()
            self.tac = value & 0x07
            if was and not self._signal():   # DMG TAC-write glitch
                self._inc_tima()
