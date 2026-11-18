"""Timers 0-3: lazily evaluated counters with scheduled overflow events.

A running (non-cascade) timer's counter is base + ((now - start) >> shift).
Overflows between two updates are folded together: the counter is advanced
arithmetically, cascade timers are bumped by the overflow count, and the sound
FIFO hook is called once per overflow.
"""
from src.core.gba.common import REG_TM0CNT, TIMER_SHIFT, IRQ_TIMER0

INF = 1 << 62


class Timers:
    def __init__(self, bus):
        self.bus = bus
        self.reload = [0] * 4
        self.ctrl = [0] * 4
        self.base = [0] * 4             # counter value at `start`
        self.start = [0] * 4
        self.next_event = INF
        self.on_overflow = None          # callable(timer_index, count) for the APU
        for i in range(4):
            off = REG_TM0CNT + 4 * i
            bus.io_read[off] = (lambda i=i: self.read(i))
            bus.io_write[off] = (lambda v, m, i=i: self._w_reload(i, v, m))
            bus.io_read[off + 2] = (lambda i=i: self.ctrl[i])
            bus.io_write[off + 2] = (lambda v, m, i=i: self._w_ctrl(i, v, m))

    def _now(self) -> int:
        return self.bus.cpu.now

    def _ticking(self, i: int) -> bool:
        c = self.ctrl[i]
        return bool(c & 0x80) and not (i and c & 0x04)

    def read(self, i: int) -> int:
        if self._ticking(i):
            self.update(self._now())
            return (self.base[i] + ((self._now() - self.start[i]) >> TIMER_SHIFT[self.ctrl[i] & 3])) & 0xFFFF
        return self.base[i] & 0xFFFF

    def _freeze(self, i: int, now: int) -> None:
        if self._ticking(i):
            self.base[i] += (now - self.start[i]) >> TIMER_SHIFT[self.ctrl[i] & 3]
            self.start[i] = now

    def _w_reload(self, i: int, v: int, m: int) -> None:
        self.reload[i] = (self.reload[i] & ~m) | v

    def _w_ctrl(self, i: int, v: int, m: int) -> None:
        now = self._now()
        self.update(now)
        self._freeze(i, now)
        old = self.ctrl[i]
        new = ((old & ~m) | v) & 0xC7
        self.ctrl[i] = new
        if new & 0x80 and not old & 0x80:
            self.base[i] = self.reload[i]
        self.start[i] = now
        self._schedule()

    def _schedule(self) -> None:
        nxt = INF
        for i in range(4):
            if self._ticking(i):
                t = self.start[i] + ((0x10000 - self.base[i]) << TIMER_SHIFT[self.ctrl[i] & 3])
                if t < nxt:
                    nxt = t
        self.next_event = nxt
        cpu = self.bus.cpu
        if cpu is not None and nxt < cpu.deadline:
            cpu.deadline = nxt
            cpu.limit = 0

    def update(self, now: int) -> None:
        if self.next_event > now:
            return
        for i in range(4):
            if not self._ticking(i):
                continue
            shift = TIMER_SHIFT[self.ctrl[i] & 3]
            first = self.start[i] + ((0x10000 - self.base[i]) << shift)
            if first > now:
                continue
            period = (0x10000 - self.reload[i]) << shift
            count = 1 + (now - first) // period
            last = first + (count - 1) * period
            self.base[i] = self.reload[i]
            self.start[i] = last
            self._overflowed(i, count)
        self._schedule()

    def _overflowed(self, i: int, count: int) -> None:
        if self.ctrl[i] & 0x40:
            self.bus.request_irq(IRQ_TIMER0 << i)
        if self.on_overflow is not None and i < 2:
            self.on_overflow(i, count)
        if i < 3:
            j = i + 1
            c = self.ctrl[j]
            if c & 0x80 and c & 0x04:              # cascade
                total = self.base[j] + count
                if total >= 0x10000:
                    period = 0x10000 - self.reload[j]
                    n = 1 + (total - 0x10000) // period
                    self.base[j] = self.reload[j] + (total - 0x10000) % period
                    self._overflowed(j, n)
                else:
                    self.base[j] = total
