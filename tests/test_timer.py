import pytest

from src.core.gameboy.cartridge import Cartridge
from src.core.gameboy.mmu import MMU
from src.core.gameboy.timer import Timer
from tests.helpers import blank_rom, build, make_rom


def make_timer():
    mmu = MMU(Cartridge(bytes(blank_rom())))
    timer = Timer(mmu)
    mmu.timer = timer
    return timer, mmu


def test_div_rate():
    timer, _ = make_timer()
    assert timer.read(0xFF04) == 0
    timer.tick(256)
    assert timer.read(0xFF04) == 1
    timer.tick(256)
    assert timer.read(0xFF04) == 2
    timer.tick(256 * 254)
    assert timer.read(0xFF04) == 0


def test_div_reset_on_write():
    timer, _ = make_timer()
    timer.tick(0x1234)
    assert timer.read(0xFF04) == 0x12
    timer.write(0xFF04, 0xAB)
    assert timer.read(0xFF04) == 0
    timer.tick(256)
    assert timer.read(0xFF04) == 1


@pytest.mark.parametrize("tac,period", [(0x04, 1024), (0x05, 16), (0x06, 64), (0x07, 256)])
def test_tac_frequencies(tac, period):
    timer, _ = make_timer()
    timer.write(0xFF07, tac)
    timer.tick(period)
    assert timer.tima == 1
    timer.tick(period - 1)
    assert timer.tima == 1
    timer.tick(1)
    assert timer.tima == 2


def test_tac_enable_bit():
    timer, _ = make_timer()
    timer.write(0xFF07, 0x01)          # bit 2 clear: disabled
    timer.tick(100_000)
    assert timer.tima == 0
    assert timer.read(0xFF04) == (100_000 & 0xFFFF) >> 8
    timer.write(0xFF07, 0x05)          # same freq, now enabled
    timer.tick(16)
    assert timer.tima == 1


def test_tac_write_no_glitch():
    timer, _ = make_timer()
    timer.tick(8)                      # counter = 8: bit 3 is high
    timer.write(0xFF07, 0x05)          # enable, selects bit 3
    assert timer.tima == 0             # no glitch increment
    timer.tick(8)                      # counter = 16: falling edge of bit 3
    assert timer.tima == 1


def test_counter_wraparound():
    timer, _ = make_timer()
    timer.write(0xFF07, 0x05)          # bit 3, every 16 cycles
    timer.tick(0xFFF8)
    assert timer.read(0xFF04) == 0xFF
    timer.write(0xFF05, 0x00)
    timer.tick(8)                      # counter wraps 0xFFF8 -> 0x0000
    assert timer.read(0xFF04) == 0x00
    assert timer.tima == 1             # falling edge across the wrap counted


def test_overflow_reloads_tma_and_requests_interrupt():
    timer, mmu = make_timer()
    timer.write(0xFF06, 0xAB)
    timer.write(0xFF05, 0xFF)
    timer.write(0xFF07, 0x05)
    timer.tick(16)
    assert timer.tima == 0xAB
    assert mmu.io[0x0F] & 0x04
    timer.tick(16)
    assert timer.tima == 0xAC


def test_tima_write_and_read():
    timer, _ = make_timer()
    timer.write(0xFF05, 0x42)
    timer.write(0xFF06, 0x11)
    assert timer.read(0xFF05) == 0x42
    assert timer.read(0xFF06) == 0x11


def test_mmu_io_delegation():
    _, mmu, _ = build(make_rom())
    mmu.write(0xFF06, 0x99)
    mmu.write(0xFF05, 0xFF)
    mmu.write(0xFF07, 0x05)
    assert mmu.read(0xFF06) == 0x99
    mmu.timer.tick(16)
    assert mmu.read(0xFF05) == 0x99
    assert mmu.io[0x0F] & 0x04
    mmu.write(0xFF04, 0xFF)
    assert mmu.read(0xFF04) == 0


def test_div_write_falling_edge_ticks_tima():
    timer, _ = make_timer()
    timer.write(0xFF07, 0x05)            # enabled, bit 3 (16 cycles)
    timer.tick(8)                        # bit 3 now high
    timer.write(0xFF04, 0)               # reset DIV -> bit falls -> TIMA+1
    assert timer.tima == 1


def test_tac_disable_glitch_ticks_tima():
    timer, _ = make_timer()
    timer.write(0xFF07, 0x05)
    timer.tick(8)
    timer.write(0xFF07, 0x01)            # disable while selected bit high
    assert timer.tima == 1


def test_stop_resets_div():
    cpu, mmu, timer = build(make_rom(b"\x10\x00"))
    timer.tick(0x1234)
    cpu.step()
    assert timer.read(0xFF04) == 0
