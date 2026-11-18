"""2A03 APU: 2 pulse, triangle, noise and DMC channels + frame sequencer.

Catch-up design: ``sync(cpu_cycle)`` advances from the last synced cycle to
now, splitting the span at frame-sequencer steps (envelopes, length counters,
sweeps, frame IRQ). Register writes sync first, so every change lands on the
right cycle. While ``sampling`` is on, each span is rendered straight to
44.1 kHz with phase accumulators, the NES's non-linear mixer and a DC-blocking
high-pass. Output matches the other cores: ``take_samples()`` -> array('h')
of interleaved stereo int16 (the same mono mix on both sides).
"""
from __future__ import annotations

from array import array

from src.core.nes.common import CPU_HZ, SAMPLE_RATE

INF = 1 << 62
STEP = CPU_HZ / SAMPLE_RATE                       # CPU cycles per output sample (~40.58)

LENGTHS = (10, 254, 20, 2, 40, 4, 80, 6, 160, 8, 60, 10, 14, 12, 26, 14,
           12, 16, 24, 18, 48, 20, 96, 22, 192, 24, 72, 26, 16, 28, 32, 30)
DUTY = ((0, 1, 0, 0, 0, 0, 0, 0), (0, 1, 1, 0, 0, 0, 0, 0),
        (0, 1, 1, 1, 1, 0, 0, 0), (1, 0, 0, 1, 1, 1, 1, 1))
TRIANGLE = tuple(list(range(15, -1, -1)) + list(range(16)))
NOISE_PERIOD = (4, 8, 16, 32, 64, 96, 128, 160, 202, 254, 380, 508, 762, 1016, 2034, 4068)
DMC_RATE = (428, 380, 340, 320, 286, 254, 226, 214, 190, 160, 142, 128, 106, 84, 72, 54)
# frame sequencer step cycles (4-step and 5-step modes)
SEQ4 = (7458, 14914, 22372, 29830)
IRQ4 = 29829                      # frame IRQ rises one cycle before the last step
SEQ5 = (7458, 14914, 22372, 29830, 37282)
PULSE_MIX = [0.0] + [95.52 / (8128.0 / n + 100) for n in range(1, 31)]
TND_MIX = [0.0] + [163.67 / (24329.0 / n + 100) for n in range(1, 203)]
GAIN = 30000


class _Env:
    """Envelope + length counter shared by pulse and noise."""
    __slots__ = ("start", "div", "decay", "period", "const", "loop", "length", "enabled")

    def __init__(self):
        self.start = False
        self.div = self.decay = self.period = 0
        self.const = False
        self.loop = False
        self.length = 0
        self.enabled = False

    def ctrl(self, v: int) -> None:
        self.loop = bool(v & 0x20)
        self.const = bool(v & 0x10)
        self.period = v & 0x0F

    def quarter(self) -> None:
        if self.start:
            self.start = False
            self.decay = 15
            self.div = self.period
        elif self.div == 0:
            self.div = self.period
            if self.decay:
                self.decay -= 1
            elif self.loop:
                self.decay = 15
        else:
            self.div -= 1

    def half_length(self) -> None:
        if self.length and not self.loop:
            self.length -= 1

    @property
    def volume(self) -> int:
        return self.period if self.const else self.decay


class Pulse:
    def __init__(self, ones_complement: bool):
        self.env = _Env()
        self.duty = 0
        self.timer = 0
        self.phase = 0.0
        self.sweep_en = False
        self.sweep_period = self.sweep_shift = self.sweep_div = 0
        self.sweep_neg = False
        self.sweep_reload = False
        self.ones = ones_complement

    def target(self) -> int:
        change = self.timer >> self.sweep_shift
        if self.sweep_neg:
            return self.timer - change - (1 if self.ones else 0)
        return self.timer + change

    def muted(self) -> bool:
        return self.timer < 8 or self.target() > 0x7FF

    def half(self) -> None:
        self.env.half_length()
        if self.sweep_div == 0 and self.sweep_en and self.sweep_shift and not self.muted():
            self.timer = max(0, self.target())
        if self.sweep_div == 0 or self.sweep_reload:
            self.sweep_div = self.sweep_period
            self.sweep_reload = False
        else:
            self.sweep_div -= 1

    def write(self, reg: int, v: int) -> None:
        if reg == 0:
            self.duty = v >> 6
            self.env.ctrl(v)
        elif reg == 1:
            self.sweep_en = bool(v & 0x80)
            self.sweep_period = (v >> 4) & 7
            self.sweep_neg = bool(v & 0x08)
            self.sweep_shift = v & 7
            self.sweep_reload = True
        elif reg == 2:
            self.timer = (self.timer & 0x700) | v
        else:
            self.timer = (self.timer & 0xFF) | ((v & 7) << 8)
            if self.env.enabled:
                self.env.length = LENGTHS[v >> 3]
            self.env.start = True
            self.phase = 0.0

    def level(self) -> int:
        """Current output volume (0-15) if audible, else 0 (sequencer handled by caller)."""
        if not self.env.length or self.muted():
            return 0
        return self.env.volume


class Triangle:
    def __init__(self):
        self.timer = 0
        self.phase = 0.0
        self.length = 0
        self.enabled = False
        self.linear = 0
        self.linear_reload = 0
        self.reload_flag = False
        self.control = False

    def quarter(self) -> None:
        if self.reload_flag:
            self.linear = self.linear_reload
        elif self.linear:
            self.linear -= 1
        if not self.control:
            self.reload_flag = False

    def half(self) -> None:
        if self.length and not self.control:
            self.length -= 1

    def write(self, reg: int, v: int) -> None:
        if reg == 0:
            self.control = bool(v & 0x80)
            self.linear_reload = v & 0x7F
        elif reg == 2:
            self.timer = (self.timer & 0x700) | v
        elif reg == 3:
            self.timer = (self.timer & 0xFF) | ((v & 7) << 8)
            if self.enabled:
                self.length = LENGTHS[v >> 3]
            self.reload_flag = True

    def active(self) -> bool:
        return bool(self.length and self.linear and self.timer >= 2)


class Noise:
    def __init__(self):
        self.env = _Env()
        self.mode = False
        self.period = NOISE_PERIOD[0]
        self.lfsr = 1
        self.acc = 0.0

    def write(self, reg: int, v: int) -> None:
        if reg == 0:
            self.env.ctrl(v)
        elif reg == 2:
            self.mode = bool(v & 0x80)
            self.period = NOISE_PERIOD[v & 0x0F]
        elif reg == 3:
            if self.env.enabled:
                self.env.length = LENGTHS[v >> 3]
            self.env.start = True


class DMC:
    """Delta modulation channel: memory reader -> 1-byte buffer -> shift register."""

    def __init__(self, read):
        self.read = read                 # CPU memory read for sample bytes
        self.irq_en = False
        self.loop = False
        self.rate = DMC_RATE[0]
        self.level = 0
        self.addr0 = 0xC000
        self.len0 = 1
        self.addr = 0xC000
        self.remaining = 0               # bytes left to fetch
        self.buffer = -1                 # sample buffer (-1 = empty)
        self.shift = 0
        self.bits = 8
        self.silent = True
        self.acc = 0.0                   # cycles consumed since the timer's last reload
        self.timer = DMC_RATE[0]         # countdown length in progress (rate changes apply on reload)
        self.irq = False

    @property
    def live(self) -> bool:
        return bool(self.remaining or self.buffer >= 0 or not self.silent)

    def write(self, reg: int, v: int) -> None:
        if reg == 0:
            self.irq_en = bool(v & 0x80)
            self.loop = bool(v & 0x40)
            self.rate = DMC_RATE[v & 0x0F]
            if not self.irq_en:
                self.irq = False
        elif reg == 1:
            self.level = v & 0x7F
        elif reg == 2:
            self.addr0 = 0xC000 | (v << 6)
        else:
            self.len0 = (v << 4) | 1

    def restart(self) -> None:
        self.addr = self.addr0
        self.remaining = self.len0

    def fetch(self) -> None:
        """Memory reader: refill the empty buffer from the sample."""
        if self.buffer >= 0 or not self.remaining:
            return
        self.buffer = self.read(self.addr)
        self.addr = 0x8000 | ((self.addr + 1) & 0x7FFF)
        self.remaining -= 1
        if self.remaining == 0:
            if self.loop:
                self.restart()
            elif self.irq_en:
                self.irq = True

    def clock(self, cycles: float) -> None:
        """Advance the output unit by `cycles` CPU cycles."""
        self.acc += cycles
        while self.acc >= self.timer:
            self.acc -= self.timer
            self.timer = self.rate
            if not self.silent:
                if self.shift & 1:
                    if self.level <= 125:
                        self.level += 2
                elif self.level >= 2:
                    self.level -= 2
            self.shift >>= 1
            self.bits -= 1
            if self.bits <= 0:
                self.bits = 8
                if self.buffer < 0:
                    self.silent = True
                else:
                    self.silent = False
                    self.shift = self.buffer
                    self.buffer = -1
                    self.fetch()


class APU:
    def __init__(self, read=None):
        self.p1 = Pulse(True)
        self.p2 = Pulse(False)
        self.tri = Triangle()
        self.noise = Noise()
        self.dmc = DMC(read or (lambda a: 0))
        self.cycle = 0                    # last synced CPU cycle
        self.seq_start = 0                # CPU cycle where the current sequence began
        self.seq_step = 0
        self.mode5 = False
        self.irq_inhibit = False
        self.frame_irq = False
        self._irq_fired = False           # this sequence's frame IRQ already raised
        self._irq_time = -10              # cycle it was raised (asserted for 3 cycles)
        self.on_irq = None                # callable() when the IRQ line may have changed
        self._sampling = False
        self._buf = array("h")
        self._next_sample = 0.0           # CPU cycle of the next output sample
        self._hp_x = 0.0                  # DC blocker state
        self._hp_y = 0.0

    # ------------------------------------------------------------ sampling contract
    @property
    def sampling(self) -> bool:
        return self._sampling

    @sampling.setter
    def sampling(self, on: bool) -> None:
        self._sampling = bool(on)
        self._next_sample = float(self.cycle)
        if not on:
            self._buf = array("h")

    def take_samples(self) -> array:
        out = self._buf
        self._buf = array("h")
        return out

    # ------------------------------------------------------------ IRQ
    @property
    def irq(self) -> bool:
        return self.frame_irq or self.dmc.irq

    def next_irq_cycle(self) -> int:
        """CPU cycle of the next frame IRQ (INF when it can't fire)."""
        if self.mode5 or self.irq_inhibit or self.frame_irq or self._irq_fired:
            return INF
        return self.seq_start + IRQ4

    def _irq_changed(self) -> None:
        if self.on_irq is not None:
            self.on_irq()

    # ------------------------------------------------------------ time
    def sync(self, now: int) -> None:
        while True:
            seq = SEQ5 if self.mode5 else SEQ4
            at = self.seq_start + seq[self.seq_step]
            irq_at = self.next_irq_cycle()
            if irq_at < at:                   # frame IRQ is its own event, before step 4
                if irq_at <= self.cycle:
                    self._irq_fired = True
                    self._irq_time = irq_at
                    self.frame_irq = True
                    self._irq_changed()
                    continue
                if irq_at <= now:
                    self._render(self.cycle, irq_at)
                    self.cycle = irq_at
                    continue
            if at <= self.cycle:              # a step is due (or overdue): take it
                self._step()
                continue
            if at > now:
                if now > self.cycle:
                    self._render(self.cycle, now)
                    self.cycle = now
                return
            self._render(self.cycle, at)
            self.cycle = at
            self._step()

    def end_frame(self, now: int) -> None:
        self.sync(now)

    def _step(self) -> None:
        k = self.seq_step
        if self.mode5:
            if k in (0, 1, 2, 4):
                self._quarter()
            if k in (1, 4):
                self._half()
            last = 4
        else:
            self._quarter()
            if k in (1, 3):
                self._half()
            last = 3
        if k == last:
            self._irq_fired = False
            self.seq_step = 0
            self.seq_start = self.seq_start + (SEQ5 if self.mode5 else SEQ4)[last]
        else:
            self.seq_step = k + 1

    def _quarter(self) -> None:
        self.p1.env.quarter()
        self.p2.env.quarter()
        self.noise.env.quarter()
        self.tri.quarter()

    def _half(self) -> None:
        self.p1.half()
        self.p2.half()
        self.noise.env.half_length()
        self.tri.half()

    # ------------------------------------------------------------ rendering
    def _render(self, start: int, end: int) -> None:
        dmc = self.dmc
        if not self._sampling:
            was = dmc.irq                     # the DMC timer runs even when silent
            dmc.clock(end - start)
            if dmc.irq != was:
                self._irq_changed()
            return
        t = self._next_sample
        if t >= end:
            return
        p1, p2, tri, nz = self.p1, self.p2, self.tri, self.noise
        v1, v2 = p1.level(), p2.level()
        d1, d2 = DUTY[p1.duty], DUTY[p2.duty]
        inc1 = STEP / (2 * (p1.timer + 1))
        inc2 = STEP / (2 * (p2.timer + 1))
        ph1, ph2 = p1.phase, p2.phase
        tri_on = tri.active()
        inc_t = STEP / (tri.timer + 1)
        ph_t = tri.phase
        nv = nz.env.volume if nz.env.length else 0
        nper = nz.period
        nacc = nz.acc
        lfsr = nz.lfsr
        tap = 6 if nz.mode else 1
        dmc_live = dmc.live
        irq_was = dmc.irq
        buf = self._buf
        hx, hy = self._hp_x, self._hp_y
        last = start
        pmix, tmix = PULSE_MIX, TND_MIX
        while t < end:
            if dmc_live:
                dmc.clock(t - last)
                last = t
            ph1 += inc1
            ph2 += inc2
            s1 = v1 if d1[int(ph1) & 7] else 0
            s2 = v2 if d2[int(ph2) & 7] else 0
            if tri_on:
                ph_t += inc_t
            tv = TRIANGLE[int(ph_t) & 31]
            if nv:
                nacc += STEP
                while nacc >= nper:
                    nacc -= nper
                    lfsr = (lfsr >> 1) | ((((lfsr ^ (lfsr >> tap)) & 1)) << 14)
                ns = 0 if lfsr & 1 else nv
            else:
                ns = 0
            x = pmix[s1 + s2] + tmix[3 * tv + 2 * ns + dmc.level]
            hy = 0.996 * (hy + x - hx)    # DC blocker: no clicks when channels start/stop
            hx = x
            s = int(hy * GAIN)
            if s > 32767:
                s = 32767
            elif s < -32768:
                s = -32768
            buf.append(s)
            buf.append(s)
            t += STEP
        if end > last:
            dmc.clock(end - last)             # (whole span at once while silent)
        p1.phase, p2.phase, tri.phase = ph1 % 8, ph2 % 8, ph_t % 32
        nz.acc, nz.lfsr = nacc, lfsr
        self._hp_x, self._hp_y = hx, hy
        self._next_sample = t
        if dmc.irq != irq_was:
            self._irq_changed()

    # ------------------------------------------------------------ registers
    def write(self, addr: int, v: int, now: int) -> None:
        self.sync(now)
        if addr < 0x4004:
            self.p1.write(addr & 3, v)
        elif addr < 0x4008:
            self.p2.write(addr & 3, v)
        elif addr < 0x400C:
            self.tri.write(addr & 3, v)
        elif addr < 0x4010:
            self.noise.write(addr & 3, v)
        elif addr < 0x4014:
            was = self.dmc.irq
            self.dmc.write(addr & 3, v)
            if was != self.dmc.irq:
                self._irq_changed()
        elif addr == 0x4015:
            for ch, bit in ((self.p1.env, 1), (self.p2.env, 2), (self.noise.env, 8)):
                ch.enabled = bool(v & bit)
                if not ch.enabled:
                    ch.length = 0
            self.tri.enabled = bool(v & 4)
            if not self.tri.enabled:
                self.tri.length = 0
            d = self.dmc
            if d.irq:
                d.irq = False
                self._irq_changed()
            if v & 0x10:
                if d.remaining == 0:
                    d.restart()
                d.fetch()                    # empty buffer fills immediately
                if d.irq:
                    self._irq_changed()
            else:
                d.remaining = 0
        elif addr == 0x4017:
            self.mode5 = bool(v & 0x80)
            self.irq_inhibit = bool(v & 0x40)
            if self.irq_inhibit and self.frame_irq:
                self.frame_irq = False
                self._irq_changed()
            # the sequencer restarts 3 CPU cycles after the write, or 4 when the
            # write lands between two APU cycles (even CPU cycle)
            self.seq_start = now + (3 if now & 1 else 2)
            self.seq_step = 0
            self._irq_fired = False
            if self.mode5:
                self._quarter()
                self._half()

    def read_status(self, now: int) -> int:
        self.sync(now)
        v = ((1 if self.p1.env.length else 0) | (2 if self.p2.env.length else 0)
             | (4 if self.tri.length else 0) | (8 if self.noise.env.length else 0)
             | (0x10 if self.dmc.remaining else 0)
             | (0x40 if self.frame_irq else 0) | (0x80 if self.dmc.irq else 0))
        if self.frame_irq and now > self._irq_time + 1:   # still being asserted: stays set
            self.frame_irq = False
            self._irq_changed()
        return v
