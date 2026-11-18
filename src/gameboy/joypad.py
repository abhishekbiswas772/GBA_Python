_NAMES = {"right": 0, "left": 1, "up": 2, "down": 3,
          "a": 4, "b": 5, "select": 6, "start": 7}

RIGHT, LEFT, UP, DOWN = 0, 1, 2, 3
A, B, SELECT, START = 4, 5, 6, 7


def _index(button: str | int) -> int:
    if isinstance(button, str):
        try:
            return _NAMES[button.lower()]
        except KeyError:
            raise ValueError(f"unknown button {button!r}") from None
    if not 0 <= int(button) < 8:
        raise ValueError(f"bad button id {button!r}")
    return int(button)


class Joypad:
    """Register 0xFF00 (P1), active-low.

    Bit 5 selects the button keys (A/B/Select/Start) and bit 4 the
    direction keys (Right/Left/Up/Down); a clear select bit selects its
    group. Bits 7-6 read as 1. Pressing a key in a selected group (or
    selecting a group with a key held) drops a line 1->0 and requests the
    joypad interrupt (IF bit 4).
    """

    def __init__(self, mmu):
        self.mmu = mmu
        self.select = 0x30
        self.dirs = 0            # 1 = pressed, bit0=Right ... bit3=Down
        self.buttons = 0         # 1 = pressed, bit0=A ... bit3=Start

    def _nibble(self) -> int:
        nib = 0x0F
        if not self.select & 0x10:
            nib &= ~self.dirs & 0x0F
        if not self.select & 0x20:
            nib &= ~self.buttons & 0x0F
        return nib

    def _edge(self, old: int) -> None:
        if old & ~self._nibble() & 0x0F:
            self.mmu.request_interrupt(0x10)

    def press(self, button: str | int) -> None:
        i = _index(button)
        old = self._nibble()
        if i < 4:
            self.dirs |= 1 << i
        else:
            self.buttons |= 1 << (i - 4)
        self._edge(old)

    def release(self, button: str | int) -> None:
        i = _index(button)
        old = self._nibble()
        if i < 4:
            self.dirs &= ~(1 << i) & 0x0F
        else:
            self.buttons &= ~(1 << (i - 4)) & 0x0F
        self._edge(old)

    def read(self) -> int:
        return 0xC0 | self.select | self._nibble()

    def write(self, value: int) -> None:
        old = self._nibble()
        self.select = value & 0x30
        self._edge(old)
