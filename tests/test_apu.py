"""APU unit tests (fast, headless; never init the mixer)."""
import os
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from src.core.gameboy.apu import APU, CYCLES_PER_SECOND, FS_PERIOD, NoiseChannel, PulseChannel
from src.core.gameboy.cartridge import Cartridge
from src.core.gameboy.mmu import MMU
from tests.helpers import blank_rom


def make_apu():
    mmu = MMU(Cartridge(bytes(blank_rom())))
    apu = APU()
    mmu.apu = apu
    return apu, mmu


def powered():
    apu, mmu = make_apu()
    mmu.write(0xFF26, 0x80)
    return apu, mmu


# --- readback masks ---------------------------------------------------------

POWER_OFF_READS = {
    0xFF10: 0x80, 0xFF11: 0x3F, 0xFF12: 0x00, 0xFF13: 0xFF, 0xFF14: 0xBF,
    0xFF15: 0xFF, 0xFF16: 0x3F, 0xFF17: 0x00, 0xFF18: 0xFF, 0xFF19: 0xBF,
    0xFF1A: 0x7F, 0xFF1B: 0xFF, 0xFF1C: 0x9F, 0xFF1D: 0xFF, 0xFF1E: 0xBF,
    0xFF1F: 0xFF, 0xFF20: 0xFF, 0xFF21: 0x00, 0xFF22: 0x00, 0xFF23: 0xBF,
    0xFF24: 0x00, 0xFF25: 0x00, 0xFF26: 0x70,
}

POWER_ON_READS = {
    0xFF10: 0xFF, 0xFF11: 0xFF, 0xFF12: 0xFF, 0xFF13: 0xFF, 0xFF14: 0xFF,
    0xFF15: 0xFF, 0xFF16: 0xFF, 0xFF17: 0xFF, 0xFF18: 0xFF, 0xFF19: 0xFF,
    0xFF1A: 0xFF, 0xFF1B: 0xFF, 0xFF1C: 0xFF, 0xFF1D: 0xFF, 0xFF1E: 0xFF,
    0xFF1F: 0xFF, 0xFF20: 0xFF, 0xFF21: 0xFF, 0xFF22: 0xFF, 0xFF23: 0xFF,
    0xFF24: 0xFF, 0xFF25: 0xFF, 0xFF26: 0xFF,
}


def test_read_masks_power_off():
    apu, mmu = make_apu()
    for addr, want in POWER_OFF_READS.items():
        assert mmu.read(addr) == want, hex(addr)
    for addr in range(0xFF27, 0xFF30):
        assert mmu.read(addr) == 0xFF


def test_read_masks_power_on():
    apu, mmu = powered()
    for addr in range(0xFF10, 0xFF26):
        mmu.write(addr, 0xFF)                 # triggers all channels (DACs on)
    for addr, want in POWER_ON_READS.items():
        assert mmu.read(addr) == want, hex(addr)
    assert mmu.read(0xFF26) == 0xFF           # power + all status bits


def test_nrx4_only_length_enable_reads_back():
    apu, mmu = powered()
    for addr in (0xFF14, 0xFF19, 0xFF1E, 0xFF23):
        mmu.write(addr, 0x87)                 # trigger + freq-high: write-only
        assert mmu.read(addr) == 0xBF, hex(addr)
        mmu.write(addr, 0x47)                 # length enable is readable
        assert mmu.read(addr) == 0xFF, hex(addr)
        mmu.write(addr, 0x00)
        assert mmu.read(addr) == 0xBF, hex(addr)


# --- mmu routing ------------------------------------------------------------

def test_mmu_apu_routing():
    apu, mmu = make_apu()
    mmu.write(0xFF26, 0x80)
    mmu.write(0xFF12, 0xF3)
    assert mmu.read(0xFF12) == 0xF3 and apu.regs[0x02] == 0xF3
    mmu.write(0xFF13, 0x5A)
    assert mmu.read(0xFF13) == 0xFF           # write-only register
    for i in range(16):
        mmu.write(0xFF30 + i, 0x40 + i)
    for i in range(16):
        assert mmu.read(0xFF30 + i) == 0x40 + i
    assert bytes(apu.ch3.wave) == bytes(range(0x40, 0x50))


# --- duty / frequency -------------------------------------------------------

def test_duty_patterns():
    patterns = [[0, 0, 0, 0, 0, 0, 0, 1],
                [1, 0, 0, 0, 0, 0, 0, 1],
                [1, 0, 0, 0, 0, 1, 1, 1],
                [0, 1, 1, 1, 1, 1, 1, 0]]
    for duty, want in enumerate(patterns):
        apu, mmu = powered()
        mmu.write(0xFF12, 0xF0)
        mmu.write(0xFF11, duty << 6)
        mmu.write(0xFF13, 0xFF)               # freq 2047 -> 4 cycles/duty step
        mmu.write(0xFF14, 0x87)               # trigger
        ch = apu.ch1
        trace = []
        for _ in range(8):
            trace.append(1 if ch.sample() else 0)
            ch.step(4)
        assert trace == want, (duty, trace)


def test_frequency_timer_period():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF13, 0xFF)
    mmu.write(0xFF14, 0x87)                   # freq 2047 -> (2048-2047)*4 = 4
    ch = apu.ch1
    ch.step(3)
    assert ch.duty_pos == 0
    ch.step(1)
    assert ch.duty_pos == 1
    mmu.write(0xFF13, 2040 & 0xFF)            # freq 2040 -> 32 cycles/step
    mmu.write(0xFF14, 2040 >> 8)              # no trigger
    ch.timer = 0
    ch.duty_pos = 0
    ch.step(31)
    assert ch.duty_pos == 0
    ch.step(1)
    assert ch.duty_pos == 1
    mmu.write(0xFF1D, 0xFF)                   # wave: freq 2047 -> 2 cycles/step
    mmu.write(0xFF1E, 0x87)
    w = apu.ch3
    w.step(1)
    assert w.pos == 0
    w.step(1)
    assert w.pos == 1


def test_wave_frequency_period():
    apu, mmu = powered()
    mmu.write(0xFF1A, 0x80)
    mmu.write(0xFF1D, 2046 & 0xFF)            # (2048-2046)*2 = 4 cycles/sample
    mmu.write(0xFF1E, 0x80 | (2046 >> 8))
    w = apu.ch3
    w.step(3)
    assert w.pos == 0
    w.step(1)
    assert w.pos == 1


# --- envelope ---------------------------------------------------------------

def test_envelope_increase_saturates():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xA9)                   # init 10, increase, period 1
    mmu.write(0xFF14, 0x80)                   # trigger (freq 0)
    ch = apu.ch1
    assert ch.volume == 10
    for want in (11, 12, 13, 14, 15, 15):
        apu.step(FS_PERIOD * 8)               # one envelope tick (64 Hz)
        assert ch.volume == want


def test_envelope_decrease():
    apu, mmu = powered()
    mmu.write(0xFF12, 0x91)                   # init 9, decrease, period 1
    mmu.write(0xFF14, 0x80)
    ch = apu.ch1
    for want in (8, 7, 6):
        apu.step(FS_PERIOD * 8)
        assert ch.volume == want
    for _ in range(10):
        apu.step(FS_PERIOD * 8)
    assert ch.volume == 0


def test_envelope_period_zero_holds():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xA0)                   # period 0: no envelope
    mmu.write(0xFF14, 0x80)
    apu.step(FS_PERIOD * 8 * 16)
    assert apu.ch1.volume == 10


def test_envelope_reloads_on_trigger():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xA9)
    mmu.write(0xFF14, 0x80)
    apu.step(FS_PERIOD * 8 * 5)
    assert apu.ch1.volume == 15
    mmu.write(0xFF12, 0x51)                   # init 5 (env state kept until trigger)
    mmu.write(0xFF14, 0x80)
    assert apu.ch1.volume == 5


# --- length counters --------------------------------------------------------

def test_length_expires_at_256hz():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF11, 0x3E)                   # length = 64 - 62 = 2
    mmu.write(0xFF14, 0xC0)                   # length enable + trigger
    ch = apu.ch1
    assert ch.enabled and ch.length == 2
    apu.step(FS_PERIOD)                       # 1st length clock (t = 8192)
    assert ch.enabled and ch.length == 1
    apu.step(16384 - 1)                       # 256 Hz: next clock at +16384
    assert ch.enabled and ch.length == 1
    apu.step(1)
    assert not ch.enabled and ch.length == 0
    assert mmu.read(0xFF26) == 0xF0           # status bit 0 clear


def test_length_disabled_never_expires():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF11, 0x3F)                   # length = 1
    mmu.write(0xFF14, 0x80)                   # trigger, length disabled
    apu.step(FS_PERIOD * 100)
    assert apu.ch1.enabled and apu.ch1.length == 1


def test_trigger_reloads_zero_length():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF11, 0x3F)                   # length = 1
    mmu.write(0xFF14, 0xC0)
    apu.step(FS_PERIOD)
    assert not apu.ch1.enabled
    mmu.write(0xFF14, 0x80)                   # trigger again: reloads length
    assert apu.ch1.enabled and apu.ch1.length == 64


def test_length_load_variants():
    apu, mmu = powered()
    mmu.write(0xFF11, 0x00)
    assert apu.ch1.length == 64
    mmu.write(0xFF11, 0x3F)
    assert apu.ch1.length == 1
    mmu.write(0xFF1B, 0x00)
    assert apu.ch3.length == 256
    mmu.write(0xFF1B, 0xFF)
    assert apu.ch3.length == 1
    mmu.write(0xFF20, 0x3F)
    assert apu.ch4.length == 1


def test_wave_length_expires():
    apu, mmu = powered()
    mmu.write(0xFF1A, 0x80)
    mmu.write(0xFF1B, 0xFF)                   # length = 1
    mmu.write(0xFF1E, 0xC0)                   # length enable + trigger
    apu.step(FS_PERIOD - 1)
    assert apu.ch3.enabled
    apu.step(1)
    assert not apu.ch3.enabled


# --- sweep ------------------------------------------------------------------

def test_sweep_updates_frequency():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF10, 0x21)                   # period 2, negate off, shift 1
    mmu.write(0xFF13, 500 & 0xFF)
    mmu.write(0xFF14, 0x80 | (500 >> 8))      # trigger
    ch = apu.ch1
    assert ch.freq == 500
    apu.step(FS_PERIOD * 8)                   # 1st sweep perform (t = 57344)
    assert ch.freq == 750 and ch.enabled      # 500 + 500>>1
    apu.step(FS_PERIOD * 8)
    assert ch.freq == 1125 and ch.enabled     # 750 + 750>>1


def test_sweep_negate():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF10, 0x19)                   # period 1, negate, shift 1
    mmu.write(0xFF13, 1000 & 0xFF)
    mmu.write(0xFF14, 0x80 | (1000 >> 8))
    apu.step(FS_PERIOD * 3)                   # sweep clock at FS step 2
    assert apu.ch1.freq == 500 and apu.ch1.enabled


def test_sweep_overflow_mutes():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF10, 0x21)                   # period 2, shift 1
    mmu.write(0xFF13, 1000 & 0xFF)
    mmu.write(0xFF14, 0x80 | (1000 >> 8))
    ch = apu.ch1
    apu.step(FS_PERIOD * 8)                   # 1000+500 then 1500+750 > 2047
    assert not ch.enabled
    assert mmu.read(0xFF26) == 0xF0


def test_sweep_overflow_on_trigger():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF10, 0x13)                   # period 1, shift 3
    mmu.write(0xFF13, 2040 & 0xFF)
    mmu.write(0xFF14, 0x80 | (2040 >> 8))
    assert not apu.ch1.enabled                # 2040 + 255 > 2047 at trigger


# --- noise LFSR -------------------------------------------------------------

def test_lfsr_sequence_and_period():
    ch = NoiseChannel()
    ch.dac = True
    ch.enabled = True
    ch.volume = 15
    bit0 = []
    for _ in range(16):
        bit0.append(ch.lfsr & 1)
        ch.step(8)
    assert bit0 == [1] * 15 + [0]
    ch = NoiseChannel()
    steps = 0
    while True:
        ch.step(8)
        steps += 1
        if ch.lfsr == 0x7FFF:
            break
        assert steps < 40000
    assert steps == 32767


def test_lfsr_width7_copies_feedback():
    ch = NoiseChannel()
    ch.width7 = True
    ch.lfsr = 0x7FFF
    for _ in range(50):
        fb = (ch.lfsr ^ (ch.lfsr >> 1)) & 1
        ch.step(8)
        assert ch.lfsr >> 14 & 1 == fb
        assert ch.lfsr >> 6 & 1 == fb


def test_lfsr_reset_on_trigger():
    apu, mmu = powered()
    mmu.write(0xFF22, 0x00)                   # fastest clock: 8 cycles/step
    mmu.write(0xFF21, 0xF0)
    mmu.write(0xFF23, 0x80)                   # trigger
    ch = apu.ch4
    assert ch.lfsr == 0x7FFF
    ch.step(8 * 100)
    assert ch.lfsr != 0x7FFF
    mmu.write(0xFF23, 0x80)
    assert ch.lfsr == 0x7FFF


def test_noise_clock_period():
    apu, mmu = powered()
    mmu.write(0xFF21, 0xF0)
    mmu.write(0xFF22, 0x01)                   # divisor code 1 -> 16 cycles
    mmu.write(0xFF23, 0x80)
    ch = apu.ch4
    before = ch.lfsr
    ch.step(15)
    assert ch.lfsr == before
    ch.step(1)
    assert ch.lfsr != before


# --- wave RAM playback ------------------------------------------------------

def test_wave_playback_and_volume_shift():
    apu, mmu = powered()
    mmu.write(0xFF1A, 0x80)                   # DAC on
    mmu.write(0xFF30, 0xAB)
    mmu.write(0xFF1C, 0x20)                   # volume code 1 (100%)
    mmu.write(0xFF1D, 0xFF)                   # freq 2047 -> 2 cycles/sample
    mmu.write(0xFF1E, 0x87)                   # trigger
    w = apu.ch3
    trace = []
    for _ in range(4):
        trace.append(w.sample())
        w.step(2)
    assert trace == [0xA, 0xB, 0, 0]
    mmu.write(0xFF1C, 0x40)                   # 50%
    assert w.sample() == 0                    # pos 4: wave RAM is zeroed
    w.pos = 0
    assert w.sample() == 0xA >> 1
    mmu.write(0xFF1C, 0x60)                   # 25%
    assert w.sample() == 0xA >> 2
    mmu.write(0xFF1C, 0x00)                   # mute
    assert w.sample() == 0


def test_wave_dac_disable_silences():
    apu, mmu = powered()
    mmu.write(0xFF1A, 0x80)
    mmu.write(0xFF30, 0xFF)
    mmu.write(0xFF1C, 0x20)
    mmu.write(0xFF1E, 0x80)
    assert apu.ch3.enabled
    mmu.write(0xFF1A, 0x00)                   # DAC off
    assert not apu.ch3.enabled
    assert apu.ch3.sample() == 0


def test_pulse_dac_disable_silences():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF14, 0x80)
    assert apu.ch1.enabled
    mmu.write(0xFF12, 0x00)                   # DAC off
    assert not apu.ch1.enabled


# --- NR52 power / status ----------------------------------------------------

def test_power_off_clears_and_ignores():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF11, 0x80)
    mmu.write(0xFF14, 0x87)
    assert apu.ch1.enabled
    mmu.write(0xFF26, 0x00)                   # power off
    assert not apu.ch1.enabled
    assert mmu.read(0xFF26) == 0x70
    assert mmu.read(0xFF12) == 0x00
    mmu.write(0xFF12, 0xF3)                   # ignored while off
    assert mmu.read(0xFF12) == 0x00
    mmu.write(0xFF14, 0x80)                   # trigger ignored
    assert not apu.ch1.enabled
    mmu.write(0xFF30, 0x99)                   # wave RAM still writable
    assert mmu.read(0xFF30) == 0x99
    mmu.write(0xFF26, 0x80)                   # power on
    assert mmu.read(0xFF26) == 0xF0
    assert apu.ch1.enabled is False


def test_power_off_length_quirk_loads():
    apu, mmu = powered()
    mmu.write(0xFF26, 0x00)                   # off
    mmu.write(0xFF11, 0x20)                   # DMG: length still loads
    assert apu.ch1.length == 64 - 0x20
    mmu.write(0xFF1B, 0x77)
    assert apu.ch3.length == 256 - 0x77
    mmu.write(0xFF14, 0x40)                   # NRx4 is ignored while off
    assert not apu.ch1.length_enabled
    mmu.write(0xFF16, 0xC5)                   # NRx1 duty bits ignored, length loads
    assert apu.ch2.length == 64 - 0x05 and apu.ch2.duty == 0


def test_power_off_resets_mixer_and_holds_sequencer():
    apu, mmu = powered()
    mmu.write(0xFF24, 0x77)
    mmu.write(0xFF25, 0xFF)
    mmu.write(0xFF26, 0x00)
    assert (apu.master_l, apu.master_r, apu.nr51) == (0, 0, 0)
    assert mmu.read(0xFF24) == 0x00 and mmu.read(0xFF25) == 0x00
    mmu.write(0xFF11, 0x3F)                   # length 1
    apu.step(FS_PERIOD * 8)                   # sequencer held: no length clocks
    assert apu.ch1.length == 1


def test_post_boot_state():
    apu, mmu = make_apu()
    apu.post_boot()
    assert mmu.read(0xFF26) == 0xF1           # power on, ch1 on
    assert mmu.read(0xFF24) == 0x77
    assert mmu.read(0xFF25) == 0xF3
    assert mmu.read(0xFF10) == 0x80
    assert mmu.read(0xFF11) == 0xBF
    assert mmu.read(0xFF12) == 0xF3
    assert mmu.read(0xFF14) == 0xBF
    assert (apu.master_l, apu.master_r) == (7, 7)


def test_sweep_negate_to_add_after_calc_disables():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF10, 0x19)                   # period 1, negate, shift 1
    mmu.write(0xFF13, 0x00)
    mmu.write(0xFF14, 0x84)                   # trigger: negate calc runs
    assert apu.ch1.enabled
    mmu.write(0xFF10, 0x11)                   # clear negate after a negate calc
    assert not apu.ch1.enabled


def test_sweep_negate_clear_before_calc_keeps_channel():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF10, 0x08)                   # negate, shift 0: no calc on trigger
    mmu.write(0xFF14, 0x84)
    mmu.write(0xFF10, 0x00)
    assert apu.ch1.enabled


def test_noise_shift_14_gets_no_clocks():
    apu, mmu = powered()
    mmu.write(0xFF21, 0xF0)
    mmu.write(0xFF22, 0xE0)                   # shift 14
    mmu.write(0xFF23, 0x80)
    apu.sampling = True
    apu.step(CYCLES_PER_SECOND // 10)
    assert apu.ch4.lfsr == 0x7FFF


def test_disabled_channel_with_dac_on_outputs_dc_level():
    apu, mmu = powered()
    mmu.write(0xFF24, 0x77)
    mmu.write(0xFF25, 0x11)                   # ch1 both sides
    mmu.write(0xFF12, 0x08)                   # DAC on, volume 0
    assert not apu.ch1.enabled and apu.ch1.dac
    apu.sampling = True
    apu.step(200)
    on = apu.take_samples()
    mmu.write(0xFF12, 0x00)                   # DAC off
    apu.step(200)
    off = apu.take_samples()
    assert on[0] < 0                          # digital 0 -> negative DC level
    assert off[-1] > on[-1]                   # DAC off: drops out (towards 0)


def test_status_bits_follow_channels():
    apu, mmu = powered()
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF14, 0x80)                   # trigger ch1 only
    assert mmu.read(0xFF26) == 0xF1
    mmu.write(0xFF21, 0xF0)
    mmu.write(0xFF23, 0x80)
    assert mmu.read(0xFF26) == 0xF9           # bits 0 (ch1) + 3 (ch4)
    mmu.write(0xFF26, 0x00)
    mmu.write(0xFF26, 0x80)
    assert mmu.read(0xFF26) == 0xF0           # all off after power cycle


def test_trigger_with_dac_off_stays_silent():
    apu, mmu = powered()
    mmu.write(0xFF12, 0x00)                   # DAC off
    mmu.write(0xFF14, 0x80)
    assert not apu.ch1.enabled
    assert mmu.read(0xFF26) == 0xF0


# --- mixing / panning -------------------------------------------------------

def collect(apu, mmu, cycles):
    mmu.apu.step(cycles)
    return apu.take_samples()


def test_panning_left_only():
    apu, mmu = powered()
    apu.sampling = True
    mmu.write(0xFF24, 0x77)
    mmu.write(0xFF25, 0x10)                   # ch1 -> left only
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF11, 0x80)
    mmu.write(0xFF13, 0xFF)
    mmu.write(0xFF14, 0x87)                   # freq 2047: fast square wave
    data = collect(apu, mmu, 100_000)
    left = data[0::2]
    right = data[1::2]
    assert any(left) and all(v == 0 for v in right)


def test_panning_right_only():
    apu, mmu = powered()
    apu.sampling = True
    mmu.write(0xFF24, 0x77)
    mmu.write(0xFF25, 0x01)                   # ch1 -> right only
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF13, 0xFF)
    mmu.write(0xFF14, 0x87)
    data = collect(apu, mmu, 100_000)
    left = data[0::2]
    right = data[1::2]
    assert all(v == 0 for v in left) and any(right)


def test_panning_both():
    apu, mmu = powered()
    apu.sampling = True
    mmu.write(0xFF24, 0x77)
    mmu.write(0xFF25, 0x11)                   # ch1 -> both
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF13, 0xFF)
    mmu.write(0xFF14, 0x87)
    data = collect(apu, mmu, 100_000)
    assert any(data[0::2]) and any(data[1::2])


def test_master_volume_scales():
    def peak_for(vol):
        apu, mmu = powered()
        apu.sampling = True
        mmu.write(0xFF24, vol << 4 | vol)
        mmu.write(0xFF25, 0x11)
        mmu.write(0xFF12, 0xF0)
        mmu.write(0xFF13, 0xFF)
        mmu.write(0xFF14, 0x87)
        data = collect(apu, mmu, 100_000)
        return max(abs(v) for v in data)
    p0, p7 = peak_for(0), peak_for(7)
    assert p0 > 0 and 6 * p0 <= p7 <= 10 * p0    # linear (n+1)/8 master volume


def test_sample_rate_and_format():
    apu, mmu = powered()
    apu.sampling = True
    mmu.write(0xFF25, 0xFF)
    mmu.write(0xFF24, 0x77)
    mmu.write(0xFF12, 0xF0)
    mmu.write(0xFF13, 0xFF)
    mmu.write(0xFF14, 0x87)
    apu.step(CYCLES_PER_SECOND)               # exactly 1 second
    data = apu.take_samples()
    assert len(data) == 44100 * 2             # interleaved stereo frames
    assert all(-32768 <= v <= 32767 for v in data)


# --- GameBoy integration ----------------------------------------------------

def test_gameboy_ticks_apu(tmp_path):
    from src.core.gameboy.gameboy import GameBoy
    rom = tmp_path / "blank.gb"
    rom.write_bytes(bytes(blank_rom()))
    gb = GameBoy(rom)
    assert gb.apu.sampling is False           # headless: no sample generation
    for _ in range(3):
        gb.run_frame()
    assert gb.apu.cycles == gb.total_cycles
    assert len(gb.apu.buffer) == 0
