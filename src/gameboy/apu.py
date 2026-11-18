"""DMG APU: 2 pulse (CH1 + sweep), wave, noise; 512 Hz frame sequencer; stereo mixer."""
from array import array

CYCLES_PER_SECOND = 4_194_304
SAMPLE_RATE = 44_100
FS_PERIOD = 8_192
DUTY = (0b00000001, 0b10000001, 0b10000111, 0b01111110)
SCALE = 32_767 // (2 * 15 * 4 * 8)      # full -60..+60 swing * master vol 8 fits after DC block
HP = 0.001                              # DC blocker coefficient (~7 Hz)

# register file 0xFF10-0xFF26: write masks (stored bits) / read-as-one masks
# NRx4: only bit 6 (length enable) reads back; trigger + freq-high read as 1
_W = bytes((0x7F, 0xFF, 0xFF, 0xFF, 0x40, 0x00, 0xFF, 0xFF, 0xFF, 0x40,
            0x80, 0xFF, 0x60, 0xFF, 0x40, 0x00, 0xFF, 0xFF, 0xFF, 0x40,
            0xFF, 0xFF, 0x80))
_R = bytes((0x80, 0x3F, 0x00, 0xFF, 0xBF, 0xFF, 0x3F, 0x00, 0xFF, 0xBF,
            0x7F, 0xFF, 0x9F, 0xFF, 0xBF, 0xFF, 0xFF, 0x00, 0x00, 0xBF,
            0x00, 0x00, 0x70))


class PulseChannel:
    def __init__(self, has_sweep: bool):
        self.has_sweep = has_sweep
        self.duty = 0
        self.duty_pos = 0
        self.freq = 0
        self.timer = 0
        self.volume = 0
        self.env_init = 0
        self.env_dir = 0
        self.env_period = 0
        self.env_timer = 0
        self.length = 0
        self.length_enabled = False
        self.enabled = False
        self.dac = False
        self.sweep_period = 0
        self.sweep_negate = False
        self.sweep_shift = 0
        self.sweep_timer = 0
        self.shadow = 0
        self.sweep_on = False
        self.neg_used = False         # a negate-mode calc ran since trigger

    def step(self, cycles):
        p = (2048 - self.freq) << 2
        steps, self.timer = divmod(self.timer + cycles, p)
        if steps:
            self.duty_pos = (self.duty_pos + steps) & 7

    def sample(self):
        if self.enabled and (DUTY[self.duty] >> (7 - self.duty_pos)) & 1:
            return self.volume
        return 0

    def trigger(self):
        self.enabled = self.dac
        self.timer = 0                        # duty position is NOT reset by trigger
        if self.length == 0:
            self.length = 64
        self.volume = self.env_init
        self.env_timer = self.env_period
        if self.has_sweep:
            self.shadow = self.freq
            self.sweep_timer = self.sweep_period or 8
            self.sweep_on = self.sweep_period != 0 or self.sweep_shift != 0
            self.neg_used = False
            if self.sweep_shift:
                self._sweep_calc()

    def _sweep_calc(self):
        delta = self.shadow >> self.sweep_shift
        if self.sweep_negate:
            self.neg_used = True
            new = self.shadow - delta
        else:
            new = self.shadow + delta
        if new > 2047:
            self.enabled = False
        return new

    def clock_sweep(self):
        if self.sweep_timer > 0:
            self.sweep_timer -= 1
        if self.sweep_timer:
            return
        self.sweep_timer = self.sweep_period or 8
        if not self.sweep_on or self.sweep_period == 0:
            return
        new = self._sweep_calc()
        if new <= 2047 and self.sweep_shift:
            self.shadow = self.freq = new
            self._sweep_calc()

    def clock_length(self):
        if self.length_enabled and self.length:
            self.length -= 1
            if self.length == 0:
                self.enabled = False

    def clock_envelope(self):
        if self.env_period == 0 or self.env_timer == 0:
            return
        self.env_timer -= 1
        if self.env_timer:
            return
        self.env_timer = self.env_period
        if self.env_dir:
            if self.volume < 15:
                self.volume += 1
        elif self.volume > 0:
            self.volume -= 1

    def power_off(self):
        self.enabled = False
        self.dac = False
        self.duty = 0
        self.duty_pos = 0
        self.freq = 0
        self.volume = 0
        self.env_init = 0
        self.env_dir = 0
        self.env_period = 0
        self.length_enabled = False
        self.sweep_period = 0
        self.sweep_negate = False
        self.sweep_shift = 0
        self.sweep_on = False
        self.sweep_timer = 0
        self.neg_used = False


class WaveChannel:
    def __init__(self):
        self.wave = bytearray(16)
        self.pos = 0
        self.freq = 0
        self.timer = 0
        self.volume_code = 0
        self.length = 0
        self.length_enabled = False
        self.enabled = False
        self.dac = False

    def step(self, cycles):
        p = (2048 - self.freq) << 1
        steps, self.timer = divmod(self.timer + cycles, p)
        if steps:
            self.pos = (self.pos + steps) & 31

    def sample(self):
        if not self.enabled or self.volume_code == 0:
            return 0
        b = self.wave[self.pos >> 1]
        nib = b >> 4 if not self.pos & 1 else b & 0xF
        return nib >> (self.volume_code - 1)

    def trigger(self):
        self.enabled = self.dac
        self.timer = 0
        self.pos = 0
        if self.length == 0:
            self.length = 256

    def clock_length(self):
        if self.length_enabled and self.length:
            self.length -= 1
            if self.length == 0:
                self.enabled = False

    def clock_envelope(self):
        pass

    def power_off(self):
        self.enabled = False
        self.dac = False
        self.freq = 0
        self.volume_code = 0
        self.length_enabled = False


class NoiseChannel:
    def __init__(self):
        self.lfsr = 0x7FFF
        self.width7 = False
        self.shift = 0
        self.div_code = 0
        self.freq = 0
        self.timer = 0
        self.volume = 0
        self.env_init = 0
        self.env_dir = 0
        self.env_period = 0
        self.env_timer = 0
        self.length = 0
        self.length_enabled = False
        self.enabled = False
        self.dac = False

    def step(self, cycles):
        if self.shift >= 14:                  # shift 14/15: LFSR receives no clocks
            return
        p = (8 if self.div_code == 0 else self.div_code << 4) << self.shift
        steps, self.timer = divmod(self.timer + cycles, p)
        lfsr = self.lfsr
        for _ in range(steps):
            fb = (lfsr ^ (lfsr >> 1)) & 1
            lfsr = (lfsr >> 1) | (fb << 14)
            if self.width7:
                lfsr = (lfsr & ~0x40) | (fb << 6)
        self.lfsr = lfsr

    def sample(self):
        if self.enabled and not self.lfsr & 1:
            return self.volume
        return 0

    def trigger(self):
        self.enabled = self.dac
        self.timer = 0
        self.lfsr = 0x7FFF
        if self.length == 0:
            self.length = 64
        self.volume = self.env_init
        self.env_timer = self.env_period

    def clock_length(self):
        if self.length_enabled and self.length:
            self.length -= 1
            if self.length == 0:
                self.enabled = False

    def clock_envelope(self):
        if self.env_period == 0 or self.env_timer == 0:
            return
        self.env_timer -= 1
        if self.env_timer:
            return
        self.env_timer = self.env_period
        if self.env_dir:
            if self.volume < 15:
                self.volume += 1
        elif self.volume > 0:
            self.volume -= 1

    def power_off(self):
        self.enabled = False
        self.dac = False
        self.shift = 0
        self.div_code = 0
        self.width7 = False
        self.volume = 0
        self.env_init = 0
        self.env_dir = 0
        self.env_period = 0
        self.length_enabled = False


class APU:
    """The APU tracks absolute T-cycles; step() only accumulates them. Channel
    state is advanced lazily in chunks split at output-sample (44.1 kHz) and
    frame-sequencer (512 Hz) boundaries, with frequency timers computed in bulk
    (divmod), so generation cost is per sample (~735/frame), never per T-cycle,
    and headless runs (sampling off) only tick the frame sequencer."""

    def __init__(self):
        self.ch1 = PulseChannel(True)
        self.ch2 = PulseChannel(False)
        self.ch3 = WaveChannel()
        self.ch4 = NoiseChannel()
        self.channels = (self.ch1, self.ch2, self.ch3, self.ch4)
        self.regs = bytearray(0x17)
        self.power = False
        self.fs_step = 0
        self.master_l = 0
        self.master_r = 0
        self.nr51 = 0
        self.cycles = 0
        self.advanced = 0
        self.next_fs = FS_PERIOD
        self.sample_k = 1
        self.next_sample = CYCLES_PER_SECOND // SAMPLE_RATE
        self._sampling = False
        self._next_event = FS_PERIOD          # min(next_fs, next_sample if sampling)
        self.buffer = array('h')
        self.dc_l = 0.0
        self.dc_r = 0.0

    def post_boot(self):
        """Register state the DMG boot ROM leaves behind (Pan Docs "Power Up
        Sequence"): APU on, NR50=$77, NR51=$F3, NR52=$F1 (ch1 on, its boot
        beep long since decayed to volume 0), NR10=$80, NR11=$BF, NR12=$F3."""
        self.write(0xFF26, 0x80)
        self.write(0xFF10, 0x80)
        self.write(0xFF11, 0xBF)
        self.write(0xFF12, 0xF3)
        self.write(0xFF24, 0x77)
        self.write(0xFF25, 0xF3)
        ch = self.ch1
        ch.enabled = True
        ch.volume = 0
        ch.env_timer = 0

    # -- timing -------------------------------------------------------------

    @property
    def sampling(self):
        return self._sampling

    @sampling.setter
    def sampling(self, on):
        on = bool(on)
        if on != self._sampling:
            self._sync()
            if on:
                k = -(-self.advanced * SAMPLE_RATE // CYCLES_PER_SECOND)
                self.sample_k = k or 1
                self.next_sample = (self.sample_k * CYCLES_PER_SECOND) // SAMPLE_RATE
        self._sampling = on
        self._update_next_event()

    def step(self, cycles):
        # hot path (called per instruction): one compare unless an event is due
        c = self.cycles + cycles
        self.cycles = c
        if c >= self._next_event:
            self._advance(c)

    def _sync(self):
        self._advance(self.cycles)

    def _update_next_event(self):
        n = self.next_fs
        if self._sampling and self.next_sample < n:
            n = self.next_sample
        self._next_event = n

    def _advance(self, end):
        if end < self.next_fs and (not self._sampling or end < self.next_sample):
            return
        if not self._sampling:
            while self.next_fs <= end:
                self.advanced = self.next_fs
                self._fs_tick()
                self.next_fs += FS_PERIOD
        else:
            self._advance_sampled(end)
        self._update_next_event()

    def _fs_tick(self):
        if not self.power:                    # sequencer is held while powered off
            return
        step = self.fs_step
        self.fs_step = (step + 1) & 7
        if not step & 1:
            for ch in self.channels:
                ch.clock_length()
        if step == 2 or step == 6:
            self.ch1.clock_sweep()
        if step == 7:
            for ch in self.channels:
                ch.clock_envelope()

    def _advance_sampled(self, end):
        ch1, ch2, ch3, ch4 = self.ch1, self.ch2, self.ch3, self.ch4
        duty_tab = DUTY
        buf_append = self.buffer.append
        k = self.sample_k
        nxt_s = self.next_sample
        nxt_f = self.next_fs
        advanced = self.advanced
        dc_l = self.dc_l
        dc_r = self.dc_r
        pan = self.nr51
        ml = self.master_l + 1
        mr = self.master_r + 1
        scale = SCALE
        hp = HP

        # channel state cached for the loop; flushed around frame-sequencer ticks
        f1 = ch1.freq; t1 = ch1.timer; dp1 = ch1.duty_pos
        duty1 = ch1.duty; vol1 = ch1.volume; en1 = ch1.enabled
        f2 = ch2.freq; t2 = ch2.timer; dp2 = ch2.duty_pos
        duty2 = ch2.duty; vol2 = ch2.volume; en2 = ch2.enabled
        wv = ch3.wave; f3 = ch3.freq; t3 = ch3.timer; p3 = ch3.pos
        code3 = ch3.volume_code; en3 = ch3.enabled
        t4 = ch4.timer; lfsr4 = ch4.lfsr; w7 = ch4.width7
        sh4 = ch4.shift; div4 = ch4.div_code; vol4 = ch4.volume; en4 = ch4.enabled
        # a disabled channel whose DAC is on still outputs digital 0 (-15 here);
        # only DAC-off channels drop out of the mix (avoids pops at length expiry)
        z1 = -15 if ch1.dac else 0
        z2 = -15 if ch2.dac else 0
        z3 = -15 if ch3.dac else 0
        z4 = -15 if ch4.dac else 0

        while True:
            nxt = nxt_f
            if nxt_s < nxt:
                nxt = nxt_s
            if nxt > end:
                break
            chunk = nxt - advanced
            advanced = nxt

            if en1:
                steps, t1 = divmod(t1 + chunk, (2048 - f1) << 2)
                if steps:
                    dp1 = (dp1 + steps) & 7
            if en2:
                steps, t2 = divmod(t2 + chunk, (2048 - f2) << 2)
                if steps:
                    dp2 = (dp2 + steps) & 7
            if en3:
                steps, t3 = divmod(t3 + chunk, (2048 - f3) << 1)
                if steps:
                    p3 = (p3 + steps) & 31
            if en4 and sh4 < 14:
                steps, t4 = divmod(t4 + chunk, (8 if div4 == 0 else div4 << 4) << sh4)
                lfsr = lfsr4
                for _ in range(steps):
                    fb = (lfsr ^ (lfsr >> 1)) & 1
                    lfsr = (lfsr >> 1) | (fb << 14)
                    if w7:
                        lfsr = (lfsr & ~0x40) | (fb << 6)
                lfsr4 = lfsr

            if nxt == nxt_s:
                l = r = 0
                s = (((vol1 if (duty_tab[duty1] >> (7 - dp1)) & 1 else 0) << 1) - 15) if en1 else z1
                if s:
                    if pan & 0x01:
                        r += s
                    if pan & 0x10:
                        l += s
                s = (((vol2 if (duty_tab[duty2] >> (7 - dp2)) & 1 else 0) << 1) - 15) if en2 else z2
                if s:
                    if pan & 0x02:
                        r += s
                    if pan & 0x20:
                        l += s
                if en3:
                    if code3:
                        b = wv[p3 >> 1]
                        s = (((b >> 4 if not p3 & 1 else b & 0xF) >> (code3 - 1)) << 1) - 15
                    else:
                        s = -15
                else:
                    s = z3
                if s:
                    if pan & 0x04:
                        r += s
                    if pan & 0x40:
                        l += s
                s = (((vol4 if not lfsr4 & 1 else 0) << 1) - 15) if en4 else z4
                if s:
                    if pan & 0x08:
                        r += s
                    if pan & 0x80:
                        l += s

                x = l * ml * scale
                dc_l += (x - dc_l) * hp
                y = int(x - dc_l)
                x = r * mr * scale
                dc_r += (x - dc_r) * hp
                z = int(x - dc_r)
                if y > 32767:
                    y = 32767
                elif y < -32768:
                    y = -32768
                if z > 32767:
                    z = 32767
                elif z < -32768:
                    z = -32768
                buf_append(y)
                buf_append(z)

                k += 1
                nxt_s = (k * CYCLES_PER_SECOND) // SAMPLE_RATE

            if nxt == nxt_f:
                ch1.timer = t1; ch1.duty_pos = dp1
                ch2.timer = t2; ch2.duty_pos = dp2
                ch3.timer = t3; ch3.pos = p3
                ch4.timer = t4; ch4.lfsr = lfsr4
                self._fs_tick()
                f1 = ch1.freq; vol1 = ch1.volume; en1 = ch1.enabled
                f2 = ch2.freq; vol2 = ch2.volume; en2 = ch2.enabled
                f3 = ch3.freq; code3 = ch3.volume_code; en3 = ch3.enabled
                vol4 = ch4.volume; en4 = ch4.enabled
                nxt_f += FS_PERIOD

        ch1.timer = t1; ch1.duty_pos = dp1
        ch2.timer = t2; ch2.duty_pos = dp2
        ch3.timer = t3; ch3.pos = p3
        ch4.timer = t4; ch4.lfsr = lfsr4
        self.sample_k = k
        self.next_sample = nxt_s
        self.next_fs = nxt_f
        self.advanced = advanced
        self.dc_l = dc_l
        self.dc_r = dc_r

    def take_samples(self):
        self._sync()
        out = self.buffer
        self.buffer = array('h')
        return out

    def read_samples(self, frames):
        self._sync()
        n = frames * 2
        out = self.buffer[:n]
        del self.buffer[:n]
        return out

    # -- registers ----------------------------------------------------------

    def read(self, addr):
        i = addr - 0xFF10
        if i >= 0x20:
            return self.ch3.wave[i - 0x20]
        if i > 0x16:
            return 0xFF
        self._sync()
        if i == 0x16:
            status = 0
            for bit, ch in enumerate(self.channels):
                if ch.enabled:
                    status |= 1 << bit
            return 0x70 | (0x80 if self.power else 0) | status
        return self.regs[i] | _R[i]

    def write(self, addr, value):
        i = addr - 0xFF10
        if i >= 0x20:
            self.ch3.wave[i - 0x20] = value
            return
        if i > 0x16:
            return
        self._sync()
        if i == 0x16:
            self._power(value)
            return
        if not self.power:
            self._write_off(i, value)
            return
        self.regs[i] = value & _W[i]
        if i == 0x00:
            ch = self.ch1
            ch.sweep_period = value >> 4 & 7
            ch.sweep_negate = value & 0x08
            ch.sweep_shift = value & 7
            if ch.neg_used and not ch.sweep_negate:
                ch.enabled = False            # negate -> add after a negate calc
        elif i == 0x01:
            self.ch1.duty = value >> 6
            self.ch1.length = 64 - (value & 0x3F)
        elif i == 0x02:
            self._envelope(self.ch1, value)
        elif i == 0x03:
            self.ch1.freq = self.ch1.freq & 0x700 | value
        elif i == 0x04:
            self._trigger(self.ch1, value)
        elif i == 0x06:
            self.ch2.duty = value >> 6
            self.ch2.length = 64 - (value & 0x3F)
        elif i == 0x07:
            self._envelope(self.ch2, value)
        elif i == 0x08:
            self.ch2.freq = self.ch2.freq & 0x700 | value
        elif i == 0x09:
            self._trigger(self.ch2, value)
        elif i == 0x0A:
            self.ch3.dac = value & 0x80
            if not self.ch3.dac:
                self.ch3.enabled = False
        elif i == 0x0B:
            self.ch3.length = 256 - value
        elif i == 0x0C:
            self.ch3.volume_code = value >> 5 & 3
        elif i == 0x0D:
            self.ch3.freq = self.ch3.freq & 0x700 | value
        elif i == 0x0E:
            self._trigger(self.ch3, value)
        elif i == 0x10:
            self.ch4.length = 64 - (value & 0x3F)
        elif i == 0x11:
            self._envelope(self.ch4, value)
        elif i == 0x12:
            self.ch4.shift = value >> 4
            self.ch4.width7 = value & 0x08
            self.ch4.div_code = value & 7
        elif i == 0x13:
            self._trigger(self.ch4, value)
        elif i == 0x14:
            self.master_l = value >> 4 & 7
            self.master_r = value & 7
        elif i == 0x15:
            self.nr51 = value

    def _envelope(self, ch, value):
        ch.env_init = value >> 4
        ch.env_dir = value & 0x08
        ch.env_period = value & 7
        ch.dac = value & 0xF8
        if not ch.dac:
            ch.enabled = False

    def _trigger(self, ch, value):
        ch.freq = ch.freq & 0xFF | ((value & 7) << 8)
        ch.length_enabled = value & 0x40
        if value & 0x80:
            ch.trigger()

    def _power(self, value):
        on = value & 0x80
        if bool(on) == self.power:
            return
        self.power = bool(on)
        self.fs_step = 0
        self.regs[0x16] = on
        if not on:
            for i in range(0x16):
                self.regs[i] = 0
            self.master_l = self.master_r = self.nr51 = 0
            for ch in self.channels:
                ch.power_off()

    def _write_off(self, i, value):
        if i == 0x01:
            self.ch1.length = 64 - (value & 0x3F)
        elif i == 0x06:
            self.ch2.length = 64 - (value & 0x3F)
        elif i == 0x0B:
            self.ch3.length = 256 - value
        elif i == 0x10:
            self.ch4.length = 64 - (value & 0x3F)
        # everything else (incl. NRx4) is ignored while powered off
