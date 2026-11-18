"""GBA sound: 4 PSG channels (the Game Boy APU, reused) + Direct Sound A/B.

PSG registers 0x060-0x084 / wave RAM 0x090-0x09F are re-mapped byte-wise onto
the GB register file 0xFF10-0xFF3F; the GB APU runs at a quarter of the CPU
clock (4.19 MHz) and produces 44.1 kHz stereo.

Direct Sound: each FIFO holds 32 signed 8-bit samples, popped on overflow of
the timer selected in SOUNDCNT_H; when 16 or fewer remain, DMA1/2 is asked to
refill it. While sampling, each channel's output changes are recorded with
their CPU timestamp and mixed into the PSG stream in take_samples().
"""
from array import array
from collections import deque

from src.core.gameboy.apu import APU as PSG
from src.core.gameboy.common import SAMPLE_RATE
from src.core.gba.common import (
    CPU_HZ, REG_SOUND_FIRST, REG_SOUNDCNT_H, REG_SOUNDCNT_X, REG_SOUNDBIAS,
    REG_WAVE_RAM, REG_FIFO_A, REG_FIFO_B,
)

INF = 1 << 62
# GBA sound register byte -> GB register address
PSG_MAP = {
    0x60: 0xFF10, 0x62: 0xFF11, 0x63: 0xFF12, 0x64: 0xFF13, 0x65: 0xFF14,
    0x68: 0xFF16, 0x69: 0xFF17, 0x6C: 0xFF18, 0x6D: 0xFF19,
    0x70: 0xFF1A, 0x72: 0xFF1B, 0x73: 0xFF1C, 0x74: 0xFF1D, 0x75: 0xFF1E,
    0x78: 0xFF20, 0x79: 0xFF21, 0x7C: 0xFF22, 0x7D: 0xFF23,
    0x80: 0xFF24, 0x81: 0xFF25,
}
PSG_RATIO = (0.25, 0.5, 1.0, 0.0)
PSG_GAIN = 0.5          # GB APU full scale -> GBA PSG level relative to Direct Sound
DMA_GAIN = 64           # 8-bit sample (x2 at 100%) -> int16


class APU:
    def __init__(self, bus):
        self.bus = bus
        self.psg = PSG()
        self.psg.write(0xFF26, 0x80)
        self.psg_cycles = 0               # CPU cycles already passed to the PSG
        self.next_event = INF
        self.fifo = (deque(), deque())
        self.current = [0, 0]             # last popped sample (signed) per FIFO
        self.history = ([], [])           # (cpu_time, value) changes while sampling
        self.sample_k = 0                 # index of the next output sample to mix
        self.cnt_h = 0
        self.enabled = True
        bus.apu = self
        rd, wr = bus.io_read, bus.io_write
        for off in range(REG_SOUND_FIRST, REG_SOUNDCNT_H, 2):
            wr[off] = (lambda v, m, o=off: self._w_psg(o, v, m))
        wr[REG_SOUNDCNT_H] = self._w_cnt_h
        rd[REG_SOUNDCNT_H] = lambda: self.cnt_h
        wr[REG_SOUNDCNT_X] = self._w_cnt_x
        rd[REG_SOUNDCNT_X] = self._r_cnt_x
        for off in range(REG_WAVE_RAM, REG_WAVE_RAM + 16, 2):
            wr[off] = (lambda v, m, o=off: self._w_wave(o, v, m))
        for k, base in enumerate((REG_FIFO_A, REG_FIFO_B)):
            for half in (0, 2):
                wr[base + half] = (lambda v, m, k=k: self._w_fifo(k, v, m))
                rd[base + half] = lambda: 0

    # ------------------------------------------------------------ clocking
    def _sync(self) -> None:
        now = self.bus.cpu.now
        delta = (now - self.psg_cycles) >> 2
        if delta > 0:
            self.psg.step(delta)
            self.psg_cycles += delta << 2

    def end_frame(self) -> None:
        self._sync()

    @property
    def sampling(self) -> bool:
        return self.psg.sampling

    @sampling.setter
    def sampling(self, on: bool) -> None:
        self._sync()
        self.psg.sampling = on
        if on:
            self.sample_k = self.psg.sample_k
            self.history = ([], [])

    # ------------------------------------------------------------ registers
    def _w_psg(self, off: int, v: int, m: int) -> None:
        io16 = self.bus.io16
        io16[off >> 1] = (io16[off >> 1] & ~m) | v
        self._sync()
        for byte in (0, 1):
            if m >> (8 * byte) & 0xFF:
                gb = PSG_MAP.get(off + byte)
                if gb is not None:
                    self.psg.write(gb, (v >> (8 * byte)) & 0xFF)

    def _w_wave(self, off: int, v: int, m: int) -> None:
        io16 = self.bus.io16
        io16[off >> 1] = (io16[off >> 1] & ~m) | v
        self._sync()
        i = off - REG_WAVE_RAM
        if m & 0xFF:
            self.psg.write(0xFF30 + i, v & 0xFF)
        if m & 0xFF00:
            self.psg.write(0xFF31 + i, (v >> 8) & 0xFF)

    def _w_cnt_h(self, v: int, m: int) -> None:
        if v & 0x0800:
            self.fifo[0].clear()
        if v & 0x8000:
            self.fifo[1].clear()
        self.cnt_h = ((self.cnt_h & ~m) | v) & 0x770F

    def _w_cnt_x(self, v: int, m: int) -> None:
        if m & 0xFF:
            self._sync()
            self.enabled = bool(v & 0x80)
            self.psg.write(0xFF26, v & 0x80)
            if not self.enabled:
                for f in self.fifo:
                    f.clear()

    def _r_cnt_x(self) -> int:
        self._sync()
        return (self.psg.read(0xFF26) & 0x8F)

    def _w_fifo(self, k: int, v: int, m: int) -> None:
        f = self.fifo[k]
        for byte in (0, 1):
            if m >> (8 * byte) & 0xFF and len(f) < 32:
                b = (v >> (8 * byte)) & 0xFF
                f.append(b - 256 if b & 0x80 else b)

    # ------------------------------------------------------------ Direct Sound
    def timer_overflow(self, timer: int, count: int) -> None:
        """Hooked to Timers.on_overflow (timers 0 and 1)."""
        if not self.enabled:
            return
        h = self.cnt_h
        for k, sel_bit in ((0, 0x0400), (1, 0x4000)):
            if bool(h & sel_bit) != bool(timer):
                continue
            f = self.fifo[k]
            for _ in range(min(count, 32)):
                if f:
                    self.current[k] = f.popleft()
            if self.psg.sampling:
                self.history[k].append((self.bus.cpu.now, self.current[k]))
            if len(f) <= 16:
                dma = self.bus.dma
                if dma is not None:
                    dma.fifo_request(0x04000000 | (REG_FIFO_A if k == 0 else REG_FIFO_B))

    # ------------------------------------------------------------ output
    def take_samples(self) -> array:
        self._sync()
        psg = self.psg.take_samples()
        n = len(psg) // 2
        if n == 0:
            return psg
        h = self.cnt_h
        ratio = PSG_RATIO[h & 3] * PSG_GAIN
        ga = DMA_GAIN * (2 if h & 0x04 else 1)
        gb = DMA_GAIN * (2 if h & 0x08 else 1)
        a_r, a_l = bool(h & 0x0100), bool(h & 0x0200)
        b_r, b_l = bool(h & 0x1000), bool(h & 0x2000)
        hist_a, hist_b = self.history
        ia = ib = 0
        cur_a = self._start_value(0)
        cur_b = self._start_value(1)
        out = array("h", bytes(4 * n))
        k = self.sample_k
        for j in range(n):
            t = (k + j) * CPU_HZ // SAMPLE_RATE
            while ia < len(hist_a) and hist_a[ia][0] <= t:
                cur_a = hist_a[ia][1]
                ia += 1
            while ib < len(hist_b) and hist_b[ib][0] <= t:
                cur_b = hist_b[ib][1]
                ib += 1
            da = cur_a * ga
            db = cur_b * gb
            left = psg[2 * j] * ratio + (da if a_l else 0) + (db if b_l else 0)
            right = psg[2 * j + 1] * ratio + (da if a_r else 0) + (db if b_r else 0)
            out[2 * j] = max(-32768, min(32767, int(left)))
            out[2 * j + 1] = max(-32768, min(32767, int(right)))
        self.sample_k = k + n
        self._last = [cur_a, cur_b]
        self.history = ([], [])
        return out

    def _start_value(self, k: int) -> int:
        last = getattr(self, "_last", None)
        return last[k] if last is not None else self.current[k]
