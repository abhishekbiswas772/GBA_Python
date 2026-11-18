from src.core.gameboy.common import (
    RIGHT, LEFT, UP, DOWN, A, B, SELECT, START,
    BUTTON_NAMES as _NAMES,
)


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
