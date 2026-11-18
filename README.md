# Game Boy / Game Boy Color / Game Boy Advance Emulator

A multi-platform retro gaming emulator written in Python, supporting **Game Boy (GB), Game Boy Color (GBC), and Game Boy Advance (GBA)**.

The project includes a Python-based emulation core and a cross-platform graphical interface built with [Flet](https://flet.dev/). It supports CPU emulation, graphics, audio, memory banking, interrupts, cartridge controllers, battery-backed saves, and more.

The Flet frontend provides a modern gaming experience across Windows, macOS, Linux, Android, iOS, and web.

---

## Features

### Game Boy / Game Boy Color (`src/core/gameboy/`)

The Game Boy core supports the original Game Boy and Game Boy Color hardware.

- **CPU:** Game Boy CPU instruction set emulation.
- **Timer & Interrupts:** Hardware timers and interrupt handling.
- **PPU:** Pixel Processing Unit for graphics rendering.
- **Joypad:** Game Boy button input handling.
- **Cartridge Support:** MBC1, MBC2, MBC3 (including RTC), and MBC5.
- **Battery Saves:** Persistent cartridge RAM with automatic saving.
- **APU:** Audio Processing Unit and sound emulation.
- **DMG Mode:** Original Game Boy compatibility with the classic green palette.
- **CGB Mode:** Game Boy Color support, including:
  - Color palettes.
  - Background tile attributes.
  - VRAM and WRAM banking.
  - HDMA (H-Blank DMA).
  - Double-speed mode.

**Hardware mode detection:**

The emulator automatically detects the hardware mode using the ROM header at address `0x0143`.

| ROM Type | Emulation Mode |
|---|---|
| Original Game Boy ROM | DMG |
| CGB-enhanced ROM | CGB |
| CGB-only ROM | CGB |
| Any ROM with `--dmg` | DMG (forced) |

### Game Boy Advance (`src/core/gba/`)

The Game Boy Advance core provides a separate ARM-based emulation implementation.

- **CPU:** ARM7TDMI processor with ARM and THUMB instruction support.
- **BIOS:** Built-in HLE BIOS; no external BIOS file required.
- **DMA:** Direct Memory Access support.
- **Timers:** Hardware timer emulation.
- **PPU:** Graphics rendering with display modes 0–5:
  - Text backgrounds.
  - Affine backgrounds.
  - Sprites, including affine sprites.
  - Windows.
  - Alpha blending and brightness effects.
  - Mosaic (BG and OBJ).
- **Audio:**
  - Direct Sound.
  - PSG audio channels.
- **Cartridge Saves:**
  - SRAM.
  - Flash 64K / 128K.
  - EEPROM.
- **Save Detection:** Save type automatically detected from the ROM.
- **Real-time clock:** cartridge GPIO + Seiko S-3511 RTC (Pokemon Ruby / Sapphire / Emerald, Boktai, ...),
  detected from the ROM, using the computer's clock.
- **Self-modifying code:** the instruction prefetch is emulated (Classic NES Series).
- **Persistent Storage:** Cartridge saves stored as `<rom>.sav`.

GBA ROMs (`.gba`) are automatically routed to the GBA emulator core.

---

## Emulator UI (Flet)

The graphical interface is built using [Flet](https://flet.dev/) and runs all three systems (GB, GBC, GBA) through `src/bridge/`.

The application uses a single codebase targeting:

- Windows
- macOS
- Linux
- Android
- iOS
- Web browsers

Input from keyboard, gamepads and touch goes through the adaptors in `src/bridge/adaptors/`.
The default theme is **Daylight**.

### UI Features

- Game library management.
- ROM scanning and loading.
- Interactive emulator player.
- Multiple visual themes.
- Game Boy-inspired color palettes.
- Custom pixel-art icons and splash screens.
- Settings and save-path management.
- Screenshots and game moments.
- Responsive layouts for different screen sizes.
- Keyboard, gamepad (Xbox / PlayStation / Switch Pro / generic) and touch controls.

### UI Architecture

```text
src/ui/
│
├── main.py, __main__.py     # Application entry points
├── app.py                   # Page setup, routing, themes, toasts
├── state.py                 # Transient view state
│
├── theme/
│   ├── Design tokens
│   ├── Four themes
│   ├── Game Boy palettes
│   ├── Shaders
│   └── Bundled fonts
│
├── components/
│   ├── UI primitives
│   ├── Text and buttons
│   ├── Segmented controls
│   ├── Toggles
│   ├── Layout bars
│   ├── Panels
│   └── Grids
│
├── views/
│   ├── library.py
│   ├── player.py
│   ├── settings.py
│   ├── moments.py
│   ├── appearance.py
│   └── together.py
│
├── emulator/
│   ├── Session management
│   ├── Emulator core integration
│   ├── Frame loop
│   ├── Input handling
│   ├── Audio handling
│   └── Frame encoding
│
├── data/
│   ├── Settings persistence
│   ├── ROM library scanning
│   ├── Moments
│   └── Sample data
│
├── tools/
│   └── make_icons.py
│
└── assets/
    ├── icons
    └── fonts/
        ├── Silkscreen
        ├── Outfit
        └── JetBrains Mono
```

### UI Implementation Details

- Every view follows the `build(app) -> ft.Control` interface.
- Views are rebuilt when application state changes.
- The player's screen image is the only persistent UI control and is updated in place for every emulated frame.
- Settings, thumbnails, and screenshots are stored in Flet's application storage directory when packaged.
- During local development, application data is stored in `~/.gba_python`.

**Splash screen:**

Once Flutter has loaded, the application displays its own splash screen (`views/splash.py`) while settings are loaded and ROM directories are scanned.

The splash screen remains visible for at least `SPLASH_MIN_SECONDS`, configured in `app.py`.

**Layout compatibility:**

Flet 1.1's `aspect_ratio` implementation causes layout issues on the web. Therefore, component dimensions are calculated explicitly instead of relying on `aspect_ratio`.

---

## Requirements

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Flet (`flet[all]`) for the desktop app, pygame for the play window / audio / gamepads
- pytest for the tests

Install everything (creates `.venv`):

```bash
uv sync
```

(or `pip install -r requirements.txt`)

---

## Running

All launching goes through `main.py`:

```bash
uv run python main.py                       # desktop app
uv run python main.py app rom/gba/game.gba  # desktop app, straight into a game
uv run python main.py game.gba              # play in a lightweight pygame window
uv run python main.py run game.gba --frames 600   # headless, prints emulation speed
uv run python main.py info game.gbc         # cartridge / save info
uv run python main.py --help
```

The system (GB, GBC or GBA) is detected from the file. ROM paths may be given relative to the
`rom/` folder (`main.py gba/game.gba`). Useful options for `play` / `run`:

```bash
--mute --scale 4            # no audio, 4x window
--keys app                  # use the desktop app key layout in the play window
--dmg                       # GB: run a CGB-enhanced ROM in original Game Boy mode
--bios path/to/bios.bin     # GBA: real BIOS dump (default: built-in HLE BIOS)
--doctor log.txt --steps N  # GB: Gameboy Doctor trace (headless)
```

### ROM library

The desktop app lists ROMs from `rom/` next to `main.py` (created automatically, with `gb/`,
`gb_color/` and `gba/` subfolders), from `~/Games/ROMs`, and from folders added under
**Settings -> Paths & Saves**. `main.py app --roms DIR` adds a folder for one session.

### Controls

| Console | Desktop app (default) | Play window (`--keys classic`) | Gamepad |
|---|---|---|---|
| D-pad | WASD / arrows | Arrows | D-pad / left stick |
| A / B | K / J | X / Z | A / B |
| L / R (GBA) | Q / E | A / S | LB / RB or triggers |
| Start | Enter | Enter | Menu |
| Select | Backspace | Right Shift / Backspace | View |

Desktop keys can be rebound under **Settings -> Controls**. Touch devices get an on-screen pad.

### Web / mobile

```bash
flet run --web src/ui/main.py
```

Sound and gamepads need the desktop app (SDL through pygame).

---

## Building the Application

Flet supports building the application for multiple platforms.

### Supported Targets

| Platform | Build Target |
|---|---|
| Windows | `windows` |
| macOS | `macos` |
| Linux | `linux` |
| Android APK | `apk` |
| Android App Bundle | `aab` |
| iOS | `ipa` |
| Web | `web` |

### Build Commands

```bash
# Windows
flet build windows

# macOS
flet build macos

# Linux
flet build linux

# Android APK
flet build apk

# Android App Bundle
flet build aab

# iOS
flet build ipa

# Web
flet build web
```

**Build requirements:**

- Desktop builds must run on their respective operating systems.
- iOS builds require macOS and Xcode.
- Android builds require the Android SDK, which Flet can download.
- The build must be executed from the repository root so that `src/` and the required application resources are bundled.
- Use `src/ui/main.py` as the application entry module.

### Application Assets

Flet reads the following assets during the build process:

| Asset | Purpose |
|---|---|
| `assets/icon.png` | Application icon |
| `assets/splash.png` | Native splash screen |
| `assets/favicon.png` | Web favicon |
| `icon.ico` | Windows window icon |

All assets are generated from a single 16×16 pixel-art sprite.

To regenerate the assets:

```bash
python -m src.ui.tools.make_icons
```

Modify `SPRITE` or `PALETTE` in `src/ui/tools/make_icons.py` before regenerating the icons.

---

## Battery Saves and Persistence

The emulator supports persistent cartridge saves.

For Game Boy and Game Boy Color:

- Battery-backed cartridge RAM is saved next to the ROM.
- Save files use the `.sav` extension.
- Autosaving occurs approximately every 10 seconds.
- The emulator also saves data when exiting.

Example:

```text
game.gb
game.gb.sav
```

For Game Boy Advance:

- SRAM, Flash 64K/128K, and EEPROM are supported.
- Save types are automatically detected from the ROM.
- Save files are stored as `<rom>.sav`.

The UI also stores application settings, thumbnails, and screenshots in its application storage directory when packaged, or in `~/.gba_python` during local development.

---

## Testing

The project includes unit tests, hardware compatibility tests, and ROM-based integration tests.

### Run Unit Tests

```bash
.venv/Scripts/python -m pytest -q
```

### Run Extended Tests

```bash
.venv/Scripts/python -m pytest -q --slow
```

The extended test suite includes:

- Blargg tests (DMG and CGB).
- DMG Acid2.
- CGB Acid2.
- ROM smoke tests.

### Blargg ROM Test Runner

```bash
.venv/Scripts/python -m tests.run_tests
```

This runs Blargg ROM tests in both DMG and CGB modes and provides an execution speed report.

### Test ROMs

Test ROMs are stored in:

```text
tests/roms/
```

The test suite includes:

- Blargg compatibility ROMs.
- DMG Acid2.
- CGB Acid2.
- GBA test ROMs.

CGB Acid2 is developed by Matt Currie and distributed under the MIT License. See `cgb-acid2-LICENSE.txt`.

ROM-based tests that need commercial ROMs in `rom/` (`.gb` / `.gbc` / `.gba`) are skipped if they are absent;
they always run on a temporary copy so your real `.sav` files are never touched.

Tests run against temporary copies of ROMs to ensure the original battery-backed `.sav` files are not modified.

### GBA Tests

GBA tests are located in:

```text
tests/test_gba.py
tests/roms/gba/
```

The GBA test suite uses test ROMs from jsmolka's `gba-tests` project, distributed under the MIT License.

---

## Current Limitations and Future Development

Some features are currently available in the UI as previews but are not yet implemented in the emulator core.

### UI Features Not Yet Implemented

| Feature | Status |
|---|---|
| Save States | UI preview only |
| Rewind | UI preview only |
| Video Recording | UI preview only |
| Clip Recording | UI preview only |
| Cheats | UI preview only |
| Achievements | UI preview only |
| Link Play | UI preview only |

Clicking these options currently displays an informational notice.

Save states and rewind require emulator-core snapshot support, which is not currently available.

### Audio Limitations

Audio output uses SDL through Pygame. Consequently, sound is currently supported only in desktop builds.

### Performance

Everything is pure Python. Emulation speed depends on the scene:

| | Typical speed |
|---|---|
| Game Boy / Color | ~100-150 % (full speed) |
| GBA, light scenes / intros | ~100-185 % |
| GBA, busy scenes with blending + sound (e.g. Minish Cap menus) | ~50-60 % (25-35 fps) |

The app itself adds about 2 ms per frame; the time goes into the CPU and PPU emulation.

---

## Project Structure

```text
project-root/
├── main.py                   # launcher: desktop app / play window / headless / info
├── pyproject.toml
├── rom/                      # your ROMs (git-ignored; gb/, gb_color/, gba/)
├── src/
│   ├── core/
│   │   ├── gameboy/          # Game Boy / Game Boy Color core
│   │   └── gba/              # Game Boy Advance core (CPU, PPU, APU, DMA, timers, BIOS, RTC)
│   ├── bridge/               # one Machine API over both cores (used by the app)
│   │   └── adaptors/         # input: keyboard, console_controller (gamepads), mobile (touch)
│   └── ui/                   # Flet application
│       ├── app.py, main.py, state.py
│       ├── views/, components/, theme/, emulator/, data/, tools/, assets/
└── tests/                    # unit + ROM tests (tests/roms/ holds the open-source test ROMs)
```

---

## License and Third-Party Components

Third-party test ROMs and dependencies retain their respective licenses.

- CGB Acid2: MIT License (Matt Currie).
- GBA Tests: MIT License (jsmolka).
- Silkscreen, Outfit, and JetBrains Mono fonts are distributed under their respective font licenses.

Refer to the individual license files and third-party project documentation for details.

---

## Project Goals

The primary goal is to build a cross-platform retro gaming emulator with accurate hardware emulation and a modern, accessible user interface.

The project is designed around three major components:

1. **Emulation Core:** Accurate Game Boy, Game Boy Color, and Game Boy Advance hardware emulation.
2. **Cross-Platform UI:** A unified Flet application for desktop, mobile, and web.
3. **Extensibility:** A modular architecture that allows additional hardware features, debugging tools, and gaming experiences to be integrated over time.

