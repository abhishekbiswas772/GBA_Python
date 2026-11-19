"""GBA Python launcher: desktop app or command line.

    python main.py                      desktop app (library: ./rom + your ROM folders)
    python main.py app [ROM]            desktop app, optionally straight into a game
    python main.py play ROM [options]   play in a lightweight pygame window
    python main.py ROM [options]        same as `play`
    python main.py run ROM --frames N   headless run, prints emulation speed
    python main.py info ROM             cartridge / save / system info

Game Boy (.gb), Game Boy Color (.gbc) and Game Boy Advance (.gba) are detected
automatically. Keyboard, gamepads (Xbox / PlayStation / Switch Pro / generic)
and touch work in the desktop app; keyboard + gamepads in the play window.
"""
import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ROM_DIR = ROOT / "rom"
ROM_SUBDIRS = ("gb", "gb_color", "gba", "nes", "ps1")
COMMANDS = ("app", "play", "run", "info")

KEYS_HELP = (
    "play-window keys (--keys classic, default):\n"
    "  Arrows D-pad   X / Z  A / B   A / S  L / R (GBA)\n"
    "  Enter  Start   Right Shift / Backspace  Select\n"
    "--keys app uses the desktop app layout: WASD/arrows, K/J = A/B, Q/E = L/R.\n"
    "Gamepads: D-pad or left stick, A/B, LB/RB (or triggers) = L/R, Menu = Start, View = Select."
)


# ------------------------------------------------------------------ helpers
class _CoreSink:
    """Router sink for the raw cores (GB joypad / GBA keypad take lower-case names)."""

    def __init__(self, target):
        self.target = target

    def press(self, button: str) -> None:
        self.target.press(button.lower())

    def release(self, button: str) -> None:
        self.target.release(button.lower())


def ensure_save_dir() -> Path | None:
    """Create the save folder (settings, else ./saves) and send every core's .sav there."""
    try:
        from src.ui.data.settings import Settings
        return Settings.load().apply_save_dir()
    except ImportError:                       # UI package missing: plain ./saves
        from src.core.adaptors.saves import set_save_dir
        return set_save_dir(ROOT / "saves")


def ensure_rom_dir() -> Path:
    """The ./rom library folder (created with gb / gb_color / gba / nes subfolders if missing)."""
    created = not ROM_DIR.is_dir()
    for sub in ROM_SUBDIRS:
        (ROM_DIR / sub).mkdir(parents=True, exist_ok=True)
    if created:
        print(f"[created ROM folder {ROM_DIR} - put your .gb / .gbc / .gba / .nes / PS-X .exe files in it]")
    return ROM_DIR


def _system(rom: str) -> str:
    from src.bridge import detect
    return detect(rom)


def _check_rom(rom: str) -> str:
    p = Path(rom)
    if not p.is_file() and not p.is_absolute() and (ROM_DIR / p).is_file():
        p = ROM_DIR / p                         # `main.py gba/game.gba` finds ./rom/gba/game.gba
    if not p.is_file():
        ensure_rom_dir()
        sys.exit(f"error: ROM not found: {rom} (ROM folder: {ROM_DIR})")
    return str(p)


def _input(display, target, keys: str):
    """Keyboard + gamepad adaptors feeding `target` through one router."""
    from src.bridge.adaptors import (GamepadAdaptor, InputRouter, KeyboardAdaptor,
                                     PRESET_APP, PRESET_CLASSIC)
    router = InputRouter(_CoreSink(target))
    keyboard = KeyboardAdaptor(router, PRESET_APP if keys == "app" else PRESET_CLASSIC)
    display.on_key = keyboard.handle_pygame
    pads = GamepadAdaptor(router)
    if pads.available:
        display.on_frame = pads.poll
        if pads.names:
            print(f"[gamepad: {', '.join(pads.names)}]")
    return router


def _speed(cycles: int, hz: int, secs: float, label: str) -> str:
    emu = cycles / hz
    return f"= {emu:.2f}s of {label} time -> {emu / max(secs, 1e-9) * 100:.0f}% of real speed"


# ------------------------------------------------------------------ commands
def cmd_app(args) -> None:
    try:
        from src.ui.app import run
    except ImportError as e:
        sys.exit(f"error: the desktop app needs Flet ({e}). Install with: uv sync")
    rom = _check_rom(args.rom) if args.rom else None
    dirs = [ensure_rom_dir()] + [d for d in args.roms if Path(d).is_dir()]
    run(rom=rom, rom_dirs=[str(d) for d in dirs])


def cmd_info(args) -> None:
    rom = _check_rom(args.rom)
    system = _system(rom)
    if system == "GBA":
        from src.core.gba.cartridge import Cartridge
    elif system == "NES":
        from src.core.nes.cartridge import Cartridge
    else:
        from src.core.gameboy.cartridge import Cartridge
    cart = Cartridge.from_file(rom)
    print(f"[{system}] {cart}")


def _play_gb(args, headless: bool) -> None:
    from src.core.gameboy.gameboy import GameBoy
    gb = GameBoy(args.rom, cgb=False if args.dmg else None)
    print(f"[{'CGB' if gb.cgb else 'DMG'} mode]")
    print(gb.cart)
    t0 = time.time()
    steps = None
    try:
        if not headless:
            from src.core.gameboy.display import Display
            title = Path(args.rom).name
            disp = Display(gb, scale=args.scale, title=title, mute=args.mute)
            _input(disp, gb.joypad, args.keys)
            try:
                disp.run()
            except KeyboardInterrupt:
                print("\n[Stopped by user]")
        elif args.frames is not None:
            for _ in range(args.frames):
                gb.run_frame()
        else:
            log = open(args.doctor, "w") if args.doctor else None
            try:
                steps = gb.run(max_steps=args.steps, doctor_log=log)
            except KeyboardInterrupt:
                print("\n[Stopped by user]")
            finally:
                if log:
                    log.close()
    finally:
        if gb.save():
            print(f"[saved battery RAM -> {gb.cart.sav_path.name}]")
    secs = time.time() - t0
    head = f"{steps:,} instructions, " if steps is not None else ""
    print(f"{head}{gb.total_cycles:,} T-cycles in {secs:.1f}s "
          + _speed(gb.total_cycles, 4_194_304, secs, "Game Boy"))


def _play_gba(args, headless: bool) -> None:
    from src.core.gba.common import CPU_HZ
    from src.core.gba.gba import GBA
    gba = GBA(args.rom, bios=args.bios)
    print(gba.cart)
    t0 = time.time()
    try:
        if not headless:
            from src.core.gba.display import Display
            disp = Display(gba, scale=args.scale, title=Path(args.rom).name, mute=args.mute)
            _input(disp, gba, args.keys)
            try:
                disp.run()
            except KeyboardInterrupt:
                print("\n[Stopped by user]")
        else:
            try:
                gba.run_frames(args.frames if args.frames is not None else 600)
            except KeyboardInterrupt:
                print("\n[Stopped by user]")
    finally:
        if gba.save():
            print(f"[saved backup memory -> {gba.cart.sav_path.name}]")
    secs = time.time() - t0
    print(f"{gba.ppu.frame:,} frames, {gba.cpu.now:,} cycles in {secs:.1f}s "
          + _speed(gba.cpu.now, CPU_HZ, secs, "GBA"))


def _play_nes(args, headless: bool) -> None:
    from src.core.nes.common import CPU_HZ
    from src.core.nes.nes import NES
    nes = NES(args.rom)
    print(nes.cart)
    t0 = time.time()
    try:
        if not headless:
            from src.core.nes.display import Display
            disp = Display(nes, scale=args.scale, title=Path(args.rom).name, mute=args.mute)
            _input(disp, nes, args.keys)
            try:
                disp.run()
            except KeyboardInterrupt:
                print("\n[Stopped by user]")
        else:
            try:
                nes.run_frames(args.frames if args.frames is not None else 600)
            except KeyboardInterrupt:
                print("\n[Stopped by user]")
    finally:
        if nes.save():
            print(f"[saved battery RAM -> {nes.cart.sav_path.name}]")
    secs = time.time() - t0
    print(f"{nes.ppu.frame:,} frames, {nes.cpu.cyc:,} cycles in {secs:.1f}s "
          + _speed(nes.cpu.cyc, CPU_HZ, secs, "NES"))


def cmd_play(args, headless: bool | None = None) -> None:
    args.rom = _check_rom(args.rom)
    ensure_save_dir()
    if headless is None:
        headless = bool(args.headless or args.frames is not None or args.doctor
                        or args.steps is not None)
    system = _system(args.rom)
    if system == "GBA":
        _play_gba(args, headless)
    elif system == "NES":
        _play_nes(args, headless)
    else:
        _play_gb(args, headless)


def cmd_run(args) -> None:
    if args.frames is None and args.steps is None and not args.doctor:
        args.frames = 600
    cmd_play(args, headless=True)


# ------------------------------------------------------------------ CLI
def _play_options(p: argparse.ArgumentParser) -> None:
    p.add_argument("rom", help=".gb / .gbc / .gba file")
    p.add_argument("--scale", default=3, type=int, help="window scale factor (default 3)")
    p.add_argument("--mute", action="store_true", help="disable audio output")
    p.add_argument("--keys", choices=("classic", "app"), default="classic",
                   help="keyboard layout of the play window")
    p.add_argument("--headless", action="store_true", help="run without a window")
    p.add_argument("--frames", type=int, help="headless: number of frames to run")
    p.add_argument("--bios", help="GBA: real BIOS image (default: built-in HLE BIOS)")
    p.add_argument("--dmg", action="store_true",
                   help="GB: force original Game Boy mode for CGB-enhanced ROMs")
    p.add_argument("--doctor", help="GB: write a Gameboy Doctor trace to this file")
    p.add_argument("--steps", type=int, help="GB headless: max instructions to run")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="main.py", description="Game Boy / Game Boy Color / Game Boy Advance emulator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="With no command the desktop app opens.\n\n" + KEYS_HELP)
    sub = ap.add_subparsers(dest="command")
    a = sub.add_parser("app", help="open the desktop app (default)")
    a.add_argument("rom", nargs="?", help="open this ROM straight away")
    a.add_argument("--roms", action="append", default=[], metavar="DIR",
                   help="extra ROM folder for this session (repeatable)")
    p = sub.add_parser("play", help="play a ROM in a pygame window",
                       formatter_class=argparse.RawDescriptionHelpFormatter, epilog=KEYS_HELP)
    _play_options(p)
    r = sub.add_parser("run", help="headless run, prints emulation speed")
    _play_options(r)
    i = sub.add_parser("info", help="show cartridge info")
    i.add_argument("rom")
    return ap


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] not in COMMANDS and not argv[0].startswith("-"):
        argv.insert(0, "play")                 # `main.py game.gba ...` = play
    args = build_parser().parse_args(argv)
    if args.command is None:
        args.command, args.rom, args.roms = "app", None, []
    {"app": cmd_app, "play": cmd_play, "run": cmd_run, "info": cmd_info}[args.command](args)


if __name__ == "__main__":
    main()
