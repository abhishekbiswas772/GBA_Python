# Game Boy (DMG) emulator in Python

CPU, timer/interrupts, PPU, joypad, MBC1/2/3(+RTC)/5 with battery saves, and APU (sound).

## Run

```
.venv/Scripts/python main.py path/to/game.gb            # window + sound
.venv/Scripts/python main.py game.gb --mute --scale 4
.venv/Scripts/python main.py rom.gb --headless           # serial output only (test ROMs)
.venv/Scripts/python main.py rom.gb --doctor log.txt     # Gameboy Doctor trace
```

Keys: arrows = D-pad, X = A, Z = B, Enter = Start, Right Shift / Backspace = Select.
Battery RAM is saved next to the ROM as `<rom>.sav` (autosave ~10 s and on exit).

## Tests

```
.venv/Scripts/python -m pytest -q            # fast unit tests
.venv/Scripts/python -m pytest -q --slow     # + Blargg, dmg-acid2, Pokemon smoke/audio ROM tests
.venv/Scripts/python -m tests.run_tests      # Blargg ROM runner with speed report
```

Test ROMs live in `tests/roms/`. ROM-based tests that need `src/rom/` skip if it is absent;
they run on a temp copy of the ROM so the real `.sav` is never touched.
