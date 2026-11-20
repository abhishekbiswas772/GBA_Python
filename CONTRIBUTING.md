# Contributing to RetroPy

Thanks for wanting to help. This project emulates the Game Boy, Game Boy Color, Game Boy Advance and NES (GB, GBC and NES in pure Python; the GBA and the experimental PS1 in Rust, managed from Python). Every kind of contribution is welcome: bug reports, test-ROM results, speed-ups, new mappers, UI polish and docs.

## Ground rules

- **No copyrighted ROMs or BIOS files.** Never commit, attach or link commercial games or a Nintendo BIOS. That covers issues and PRs too. The `rom/` folder is git-ignored for exactly this reason.
- **Pure Python cores** (GB, GBC, NES). They use only the standard library. No C extensions, no NumPy in the hot paths. Speed comes from technique; see "Performance" below. The GBA and PS1 cores are Rust crates in `src/core/<core>/native/`; build them with `uv run python -m src.core.<core>.build`.
- **Licence.** By contributing you agree that your work is released under the project's [MIT License](LICENSE).
- **Be kind.** Assume good intent, keep reviews about the code, and help newcomers.

## Setup

```bash
git clone https://github.com/abhishekbiswas772/RetroPy.git
cd RetroPy
uv sync                      # Python 3.13+, installs Flet, pygame, pytest and the `retropy` command
uv run python main.py        # the desktop app
uv run python -m src.core.ps1.build   # optional: the PS1 core (needs Rust, https://rustup.rs)
```

The same CLI runs any of these ways: `uv run python main.py`, `uv run retropy`,
`uv run python -m src`, or `retropy` after `uv tool install .` / `pip install .`.
With no arguments it opens the desktop app; `retropy play <rom>`, `retropy run <rom>`
and `retropy info <rom>` work as before.

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

## Branch protection

`main` is protected:

- changes land through a pull request (no approval count is required, but review comments must be resolved);
- the CI checks **Checks (Linux)**, **Unit tests (Linux)**, **Unit tests (Windows)** and **Test ROMs (Linux)** must pass, on a branch that is up to date with `main`;
- history stays linear: rebase or squash-merge, no merge commits;
- force pushes and deleting `main` are blocked.

So work on a branch in your fork, open a PR against `main`, and rebase onto `main` if it moved.
Maintainers can bypass the rules for urgent fixes; everyone else, including bots, goes through a PR.

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
