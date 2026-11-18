# Contributing to GBA Python

Thanks for wanting to help. This project emulates the Game Boy, Game Boy Color, Game Boy Advance and NES in pure Python. Every kind of contribution is welcome: bug reports, test-ROM results, speed-ups, new mappers, UI polish and docs.

## Ground rules

- **No copyrighted ROMs or BIOS files.** Never commit, attach or link commercial games or a Nintendo BIOS. That covers issues and PRs too. The `rom/` folder is git-ignored for exactly this reason.
- **Pure Python cores.** `src/core/` uses only the standard library. No C extensions, no NumPy in the hot paths. Speed comes from technique; see "Performance" below.
- **Licence.** By contributing you agree that your work is released under the project's [MIT License](LICENSE).
- **Be kind.** Assume good intent, keep reviews about the code, and help newcomers.

## Setup

```bash
git clone https://github.com/abhishekbiswas772/GBA_Python.git
cd GBA_Python
uv sync                      # Python 3.13+, installs Flet, pygame and pytest
uv run python main.py        # the desktop app
```

## Before you open a pull request

```bash
uv run python -m pytest -q                 # must pass
uv run python -m pytest -q --slow          # run it if you touched a core
uvx ruff check --select E9,F63,F7,F82 main.py src tests
```

CI runs the same steps on Linux. A PR that changes timing, rendering or audio should say which test ROMs it fixes or affects.

## Where things live

| Path | What |
|---|---|
| `src/core/gameboy/`, `src/core/gba/`, `src/core/nes/` | the emulator cores (no UI imports) |
| `src/bridge/` | the `Machine` API every core is wrapped in, `EmulatorRunner`, input adaptors |
| `src/ui/` | the Flet app |
| `tests/` | pytest suite; open test ROMs in `tests/roms/` |
| `docs/make_media.py` | regenerates the README images and benchmark |

## Common contributions

### Fixing accuracy

1. Find a failing open test ROM: blargg, Acid2, jsmolka, or the [nes-test-roms](https://github.com/christopherpow/nes-test-roms) collection.
2. Add it under `tests/roms/<system>/` with its licence or readme if it has one.
3. Add a test. blargg-style NES ROMs can reuse `_blargg()` in `tests/test_nes.py`.
4. Fix the core, then run the whole suite. Timing fixes often break a neighbouring test.

### Adding an NES mapper

Mappers live in `src/core/nes/cartridge.py`. Subclass `Mapper`, then:

- switch banks with `set_prg8/16/32` and `set_chr1/4/8`;
- set mirroring with `mirror()`;
- raise IRQs with `set_irq()`;
- if the mapper needs a per-scanline clock, override `scanline()` and return `True` from `wants_scanlines`;
- register the class in `MAPPERS` under its iNES number;
- add a test.

### Adding a system to the app

A new core needs a `Machine` subclass in `src/bridge/` (see `nes_machine.py`), registered in `src/bridge/__init__.py`. Then add the system to:

- `ROM_EXTS`, `PLAYABLE` and `NATIVE_SIZE` in `src/ui/data/library.py`;
- `SYSTEMS` and the default keys in `src/ui/data/settings.py`;
- the colours and names in `src/ui/theme/tokens.py`.

## Performance

The cores run inside the CPython interpreter, so a few habits matter:

- Keep hot state in **local variables**. The NES and GBA CPUs generate their interpreter source and `exec` it for exactly this reason.
- Use **catch-up timing**: run the CPU to the next event, then sync the PPU/APU. Avoid per-cycle callbacks.
- Cache rendered rows and tiles. Use `bytes.translate` and slice assignment instead of per-pixel loops.
- `run_frame(render=False)` must stay cheap and must still behave exactly the same as a drawn frame.
- Measure before and after with `uv run python docs/make_media.py` or `main.py run <rom> --frames 600`, and put the numbers in your PR.

## Commits and pull requests

- Use one topic per PR, with a short imperative title such as "nes: fix MMC3 IRQ reload at 0".
- Explain *why*, and link the test ROM or the hardware doc you followed (NESdev wiki, Pan Docs, GBATEK).
- Don't reformat files you aren't changing.

## Reporting bugs

Please include:

- your OS and Python version;
- the system, and the cartridge type from `main.py info <rom>`;
- what you expected and what happened;
- a screenshot or the console output.

Don't attach the ROM. If it's a homebrew or test ROM, a link to it is great.
