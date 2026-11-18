# Game Boy / Game Boy Color emulator in Python

CPU, timer/interrupts, PPU, joypad, MBC1/2/3(+RTC)/5 with battery saves, APU (sound),
and Game Boy Color mode: colour palettes, BG tile attributes, VRAM/WRAM banking,
HDMA, double speed.

The mode comes from the ROM header (0x0143): CGB-enhanced/only ROMs run in CGB mode,
original Game Boy ROMs run in DMG mode (green palette). `--dmg` forces DMG mode.

## Run

```
.venv/Scripts/python main.py path/to/game.gb            # window + sound
.venv/Scripts/python main.py game.gb --mute --scale 4
.venv/Scripts/python main.py game.gbc --dmg              # CGB-enhanced ROM in DMG mode
.venv/Scripts/python main.py rom.gb --headless           # serial output only (test ROMs)
.venv/Scripts/python main.py rom.gb --doctor log.txt     # Gameboy Doctor trace
```

Keys: arrows = D-pad, X = A, Z = B, Enter = Start, Right Shift / Backspace = Select.
Battery RAM is saved next to the ROM as `<rom>.sav` (autosave ~10 s and on exit).

## Tests

```
.venv/Scripts/python -m pytest -q            # fast unit tests
.venv/Scripts/python -m pytest -q --slow     # + Blargg (DMG+CGB), dmg-acid2, cgb-acid2, ROM smoke tests
.venv/Scripts/python -m tests.run_tests      # Blargg ROM runner (DMG and CGB modes) with speed report
```

Test ROMs live in `tests/roms/` (cgb-acid2 by Matt Currie, MIT, see `cgb-acid2-LICENSE.txt`). ROM-based tests that need `src/rom/` (`*.gb` / `*.gbc`) skip if it is absent;
they run on a temp copy of the ROM so the real `.sav` is never touched.
