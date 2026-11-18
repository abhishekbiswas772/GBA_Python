import argparse
from src.gameboy.gameboy import GameBoy
import time


def main() -> None:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="keyboard mapping (window mode):\n"
               "  Arrow keys   D-pad (Right/Left/Up/Down)\n"
               "  X / Z        A / B\n"
               "  Enter        Start\n"
               "  Right Shift  Select (Backspace also works)")
    ap.add_argument("rom")
    ap.add_argument("--doctor", help="Write Gameboy Doctor log to this file")
    ap.add_argument("--steps", default=None, type=int, help="max instruction to run")
    ap.add_argument("--headless", action="store_true", help="run without a window")
    ap.add_argument("--scale", default=3, type=int, help="window scale factor")
    args = ap.parse_args()

    gb = GameBoy(args.rom)
    print(gb.cart)
    t0 = time.time()

    try:
        window = not (args.headless or args.doctor or args.steps)
        if window:
            from src.gameboy.display import Display, joypad_key_handler
            title = args.rom.replace("\\", "/").rsplit("/", 1)[-1]
            disp = Display(gb, scale=args.scale, title=title)
            disp.on_key = joypad_key_handler(gb.joypad)
            try:
                disp.run()
            except KeyboardInterrupt:
                print("\n[Stopped by user]")
            steps = None
        else:
            log = open(args.doctor, "w") if args.doctor else None
            try:
                steps = gb.run(max_steps=args.steps, doctor_log=log)
            except KeyboardInterrupt:
                steps = None
                print("\n[Stopped by user]")
            finally:
                if log:
                    log.close()
    finally:
        if gb.save():
            print(f"[saved battery RAM -> {gb.cart.sav_path.name}]")

    secs = time.time() - t0
    if steps:
        emulated_secs = gb.total_cycles / 4_194_304  
        print(f"\n\n{steps:,} instructions, {gb.total_cycles:,} T-cycles in {secs:.1f}s")
        speed = emulated_secs / max(secs, 1e-9) * 100
        print(f"= {emulated_secs:.2f}s of Game Boy time -> {speed:.0f}% of real speed")
    elif window:
        emulated_secs = gb.total_cycles / 4_194_304
        speed = emulated_secs / max(secs, 1e-9) * 100
        print(f"{gb.total_cycles:,} T-cycles in {secs:.1f}s "
              f"= {emulated_secs:.2f}s of Game Boy time -> {speed:.0f}% of real speed")



    
if __name__ == "__main__":
    main()
