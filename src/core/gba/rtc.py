"""Cartridge GPIO port + Seiko S-3511 real-time clock (Pokemon R/S/E, Boktai, ...).

GPIO registers live in ROM space: 0x080000C4 data (4 pins), 0x080000C6 pin
direction (1 = GBA drives the pin), 0x080000C8 control (bit 0: registers
readable; otherwise the addresses read as normal ROM).

RTC pins: 0 = SCK, 1 = SIO, 2 = CS. A transfer starts with CS low -> high
(with SCK high); bits move on SCK rising edges, least significant bit first.
The first byte is the command: low nibble 0110 (magic), bits 4-6 the register,
bit 7 = read. Date/time come from the host clock as BCD.
"""
import time

from src.core.gba.common import (
    GPIO_DATA, GPIO_DIRECTION, GPIO_CONTROL, RTC_MAGIC, RTC_PARAM_BYTES,
    RTC_CMD_RESET, RTC_CMD_DATETIME, RTC_CMD_CONTROL, RTC_CMD_TIME, RTC_CONTROL_24H,
)


def _bcd(v: int) -> int:
    return ((v // 10) << 4) | (v % 10)


class RTC:
    def __init__(self, clock=time.localtime):
        self.clock = clock               # () -> struct_time (injectable for tests)
        self.pins = 0                    # pin levels as seen on the port
        self.direction = 0
        self.readable = False
        self.step = 0                    # 0 idle, 1 CS low seen, 2 selected
        self.bits = 0
        self.nbits = 0
        self.command = 0
        self.active = False
        self.remaining = 0
        self.control = RTC_CONTROL_24H
        self.regs = [0] * 7              # year month day weekday hour minute second (BCD)

    # ------------------------------------------------------------ GPIO registers
    def read16(self, off: int) -> int | None:
        """Register value for ROM offset `off`, or None if it reads as ROM."""
        if not self.readable:
            return None
        if off == GPIO_DATA:
            return self.pins
        if off == GPIO_DIRECTION:
            return self.direction
        if off == GPIO_CONTROL:
            return 1
        return None

    def write16(self, off: int, v: int) -> None:
        if off == GPIO_DATA:
            d = self.direction
            self.pins = (self.pins & ~d | v & d) & 0xF
            self._clock_pins()
        elif off == GPIO_DIRECTION:
            self.direction = v & 0xF
        elif off == GPIO_CONTROL:
            self.readable = bool(v & 1)

    def _drive(self, value: int) -> None:
        """The chip drives the pins the GBA has configured as inputs."""
        d = self.direction
        self.pins = (self.pins & d | value & ~d) & 0xF

    # ------------------------------------------------------------ serial protocol
    def _clock_pins(self) -> None:
        p = self.pins & 5                         # SCK + CS
        if self.step == 0:
            if p == 1:
                self.step = 1
        elif self.step == 1:
            if p == 5:
                self.step = 2
            elif p != 1:
                self.step = 0
        elif not self.pins & 1:                   # SCK low: sample SIO
            if not (self.active and self.command & 0x80):
                bit = (self.pins >> 1) & 1
                self.bits = (self.bits & ~(1 << self.nbits)) | (bit << self.nbits)
        elif self.pins & 4:                       # SCK rising edge, CS high
            if self.active and self.command & 0x80:
                self._drive(5 | (self._output_bit() << 1))
                self.nbits += 1
                if self.nbits == 8:
                    self.nbits = 0
                    self.remaining -= 1
                    if self.remaining <= 0:
                        self.active = False
                        self.command = 0
            else:
                self.nbits += 1
                if self.nbits == 8:
                    self._byte()
        else:                                     # CS dropped: end of transfer
            self.nbits = 0
            self.remaining = 0
            self.active = False
            self.command = 0
            self.step = self.pins & 1
            self._drive(1)

    def _byte(self) -> None:
        b = self.bits & 0xFF
        if not self.active:
            if b & 0x0F == RTC_MAGIC:
                self.command = b
                reg = (b >> 4) & 7
                self.remaining = RTC_PARAM_BYTES[reg]
                self.active = self.remaining > 0
                if reg == RTC_CMD_RESET:
                    self.control = 0
                elif reg in (RTC_CMD_DATETIME, RTC_CMD_TIME):
                    self._latch_clock()
        else:
            if (self.command >> 4) & 7 == RTC_CMD_CONTROL:
                self.control = b
            self.remaining -= 1
            if self.remaining <= 0:
                self.active = False
                self.command = 0
        self.bits = 0
        self.nbits = 0

    def _output_bit(self) -> int:
        reg = (self.command >> 4) & 7
        if reg == RTC_CMD_CONTROL:
            byte = self.control
        elif reg == RTC_CMD_DATETIME:
            byte = self.regs[7 - self.remaining]
        elif reg == RTC_CMD_TIME:
            byte = self.regs[7 - self.remaining]
        else:
            byte = 0
        return (byte >> self.nbits) & 1

    def _latch_clock(self) -> None:
        t = self.clock()
        hour = t.tm_hour
        if self.control & RTC_CONTROL_24H:
            h = _bcd(hour)                        # 24h: plain BCD (games validate <= 0x23)
        else:
            h = _bcd(hour % 12) | (0x80 if hour >= 12 else 0)   # 12h: bit 7 = PM
        self.regs = [_bcd(t.tm_year % 100), _bcd(t.tm_mon), _bcd(t.tm_mday),
                     (t.tm_wday + 1) % 7, h, _bcd(t.tm_min), _bcd(t.tm_sec)]
