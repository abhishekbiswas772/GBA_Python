"""Cartridge GPIO + S-3511 RTC, driven through the bus exactly like a game does."""
import os

import pytest

from src.core import gba as gba_core

if os.environ.get("GBA_CORE_REQUIRED") and not gba_core.AVAILABLE:
    raise RuntimeError(f"GBA core required but not built: {gba_core.IMPORT_ERROR}")
pytestmark = pytest.mark.skipif(not gba_core.AVAILABLE, reason="GBA core not built")

from src.core.gba import GbaCore as GBA

DATA, DIR, CTRL = 0x080000C4, 0x080000C6, 0x080000C8
FIXED = [2024, 3, 5, 1, 14, 7, 9]           # Tue 2024-03-05 14:07:09 (weekday Mon=0)


def rtc_rom(sig: bool = True) -> bytes:
    rom = bytearray(0x400)
    rom[0xC4:0xCA] = b"\x11\x22\x33\x44\x55\x66"        # ROM bytes under the GPIO port
    if sig:
        rom[0x200:0x20B] = b"SIIRTC_V001"
    return bytes(rom)


def make(sig: bool = True) -> GBA:
    g = GBA(rtc_rom(sig))
    if g.cart.rtc:
        g.gba.set_rtc_fixed(FIXED)
    return g


def start(bus):
    bus.write16(CTRL, 1)          # GPIO readable
    bus.write16(DIR, 7)           # SCK, SIO, CS driven by the GBA
    bus.write16(DATA, 1)          # SCK high, CS low
    bus.write16(DATA, 5)          # CS high: selected


def send(bus, byte):
    for i in range(8):            # LSB first, latched on the SCK rising edge
        bit = (byte >> i) & 1
        bus.write16(DATA, 4 | bit << 1)
        bus.write16(DATA, 5 | bit << 1)


def recv(bus, n):
    bus.write16(DIR, 5)           # SIO becomes an input
    out = []
    for _ in range(n):
        v = 0
        for i in range(8):
            bus.write16(DATA, 4)
            bus.write16(DATA, 5)
            v |= ((bus.read16(DATA) >> 1) & 1) << i
        out.append(v)
    bus.write16(DIR, 7)
    return out


def stop(bus):
    bus.write16(DATA, 1)          # CS low ends the transfer


def test_rtc_detected_from_library_string():
    assert make().cart.rtc
    assert not make(sig=False).cart.rtc


def test_datetime_read_bcd():
    g = make()
    bus = g.bus
    start(bus)
    send(bus, 0x06 | 2 << 4 | 0x80)          # read date+time (7 bytes)
    got = recv(bus, 7)
    stop(bus)
    assert got == [0x24, 0x03, 0x05, 2, 0x14, 0x07, 0x09]   # 24h mode: plain BCD hour


def test_time_read_and_control_register():
    g = make()
    bus = g.bus
    start(bus)
    send(bus, 0x06 | 4 << 4)                 # write control register
    send(bus, 0x00)                          # 12-hour mode
    stop(bus)
    start(bus)
    send(bus, 0x06 | 4 << 4 | 0x80)          # read control back
    assert recv(bus, 1) == [0x00]
    stop(bus)
    start(bus)
    send(bus, 0x06 | 6 << 4 | 0x80)          # read time (3 bytes)
    assert recv(bus, 3) == [0x02 | 0x80, 0x07, 0x09]   # 14:07 -> 2 PM
    stop(bus)


def test_gpio_reads_rom_until_enabled():
    g = make()
    bus = g.bus
    assert bus.read16(DATA) == 0x2211        # control bit clear: plain ROM
    bus.write16(CTRL, 1)
    assert bus.read16(CTRL) == 1
    assert make(sig=False).bus.read16(DATA) == 0x2211
