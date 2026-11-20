"""README / launch media from real games, played headlessly with scripted input.

    uv run python docs/make_game_media.py                # all four consoles
    uv run python docs/make_game_media.py gb nes         # just some of them
    uv run python docs/make_game_media.py --state-dir D  # also write a Pallet Town save state (for UI shots)

Needs your own cartridge dumps in rom/ (git-ignored, never committed):

    rom/gb/        Pokemon - Red Version (USA, Europe) (SGB Enhanced).gb
    rom/gb_color/  Legend of Zelda, The - Oracle of Ages (USA, Australia).gbc
    rom/gba/       ... Pokemon Fire Red (U) ... .gba
    rom/nes/       Contra (USA).nes

A console whose ROM is missing is skipped. Writes:

    docs/games.png       one frame per console, labelled (the README banner)
    docs/game-gb.gif     Pokemon Red: title screen, then walking around Pallet Town
    docs/game-gbc.gif    Oracle of Ages: the opening cutscene
    docs/game-gba.gif    Fire Red: the Gengar vs Nidorino intro and the title screen
    docs/game-nes.gif    Contra: stage 1, running and shooting

Every frame is drawn by this repo's cores. Battery saves go to a throwaway temp
folder, so nothing is written next to your ROMs or into saves/. The PS1 core is
left out: CD-ROM games don't run yet.
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from src.bridge import load  # noqa: E402
from src.core.adaptors.saves import set_save_dir  # noqa: E402

OUT = ROOT / "docs"
ROMS = ROOT / "rom"
BG = (15, 17, 21)
CARD = (23, 26, 33)
TEXT = (232, 234, 240)
MUTED = (154, 163, 178)
ACCENT = {"GB": (159, 211, 86), "GBC": (242, 166, 90), "GBA": (142, 162, 255), "NES": (255, 122, 107)}


# ------------------------------------------------------------------ input scripts
class Script:
    """Button events keyed by frame number: tap() = press for n frames, hold() = press over a range."""

    def __init__(self) -> None:
        self.events: dict[int, list[tuple[bool, str]]] = {}

    def _add(self, f: int, down: bool, button: str) -> None:
        self.events.setdefault(f, []).append((down, button))

    def tap(self, f: int, button: str, n: int = 6) -> "Script":
        self._add(f, True, button)
        self._add(f + n, False, button)
        return self

    def taps(self, start: int, stop: int, step: int, button: str, n: int = 6) -> "Script":
        for f in range(start, stop + 1, step):
            self.tap(f, button, n)
        return self

    def hold(self, a: int, b: int, button: str) -> "Script":
        self._add(a, True, button)
        self._add(b, False, button)
        return self


def red_script() -> Script:
    """Pokemon Red: title -> New Game -> Oak's speech (names RED / BLUE from the list)
    -> bedroom -> downstairs -> out of the front door -> a walk round Pallet Town."""
    s = Script()
    s.tap(1300, "Start")                       # title screen
    s.taps(1400, 2920, 40, "A")                # NEW GAME, then Oak's speech
    o = 2958                                   # "what is your name?" list
    s.tap(o + 10, "Down").tap(o + 40, "A")     # RED
    s.taps(o + 80, o + 680, 40, "A")
    s.tap(o + 725, "Down").tap(o + 760, "A")   # rival: BLUE
    s.taps(o + 800, o + 2160, 40, "A")         # ...to the bedroom
    o = 5258
    s.tap(o + 10, "A")                         # close the SNES text box
    s.hold(o + 60, o + 200, "Right").hold(o + 200, o + 330, "Up")
    o = 5858
    s.hold(o + 10, o + 70, "Right")            # onto the stairs
    o = 6258
    s.hold(o + 10, o + 100, "Down").hold(o + 100, o + 190, "Left").hold(o + 190, o + 500, "Down")
    o = 6958                                   # outside, in Pallet Town
    s.hold(o + 10, o + 74, "Right").hold(o + 74, o + 170, "Down")
    s.hold(o + 170, o + 300, "Left").hold(o + 300, o + 420, "Up")
    return s


def zelda_script() -> Script:
    return Script()                            # the opening cutscene plays by itself


def firered_script() -> Script:
    return Script()                            # intro + title screen play by themselves


def contra_script() -> Script:
    s = Script()
    s.tap(300, "Start").tap(500, "Start")      # skip the scroll-in, then 1 PLAYER
    s.hold(870, 1700, "Right")
    s.taps(880, 1700, 10, "B", 4)              # keep firing
    for f in (1000, 1150, 1300, 1450, 1600):
        s.tap(f, "A", 10)                      # and jump now and then
    return s


# key, console, game, rom folder, glob, script, total frames,
# GIF clips as (first, last, every), banner frame, GIF scale
GAMES = [
    dict(key="GB", console="Game Boy", game="Pokémon Red", folder="gb", glob="Pokemon - Red*.gb",
         script=red_script, total=7440, clips=[(1230, 1520, 3), (1560, 1660, 3), (6958, 7438, 3)],
         banner=7068, scale=3, gif="game-gb.gif", state_at=7438),
    dict(key="GBC", console="Game Boy Color", game="Zelda: Oracle of Ages", folder="gb_color",
         glob="Legend of Zelda*Oracle of Ages*.gbc", script=zelda_script, total=2230,
         clips=[(330, 1140, 4), (1190, 1480, 4), (1500, 2220, 4)], banner=1679, scale=2, gif="game-gbc.gif"),
    dict(key="GBA", console="Game Boy Advance", game="Pokémon FireRed", folder="gba",
         glob="*Fire Red*.gba", script=firered_script, total=2060,
         clips=[(720, 1380, 3), (1680, 2058, 3)], banner=1979, scale=2, gif="game-gba.gif"),
    dict(key="NES", console="NES", game="Contra", folder="nes", glob="Contra*.nes",
         script=contra_script, total=1700, clips=[(240, 330, 4), (830, 1300, 4)], banner=1099,
         scale=2, gif="game-nes.gif"),
]


def find_rom(g: dict) -> Path | None:
    return next(iter(sorted((ROMS / g["folder"]).glob(g["glob"]))), None)


def play(g: dict, rom: Path, state_dir: Path | None) -> tuple[list[tuple[Image.Image, int]], Image.Image]:
    """Run the game; return the GIF frames (image, duration ms) and the banner frame."""
    m = load(rom)
    m.apu.sampling = False
    events = g["script"]().events
    wanted: dict[int, int] = {}                # frame -> GIF duration in ms
    for a, b, every in g["clips"]:
        for f in range(a, b + 1, every):
            wanted[f] = round(every * 1000 / m.fps)
    banner = None
    frames = []
    t = time.perf_counter()
    for f in range(g["total"]):
        for down, button in events.get(f, ()):
            (m.press if down else m.release)(button)
        draw = f in wanted or f == g["banner"] or f + 1 == g.get("state_at")
        m.run_frame(render=draw)
        if draw:
            img = Image.frombytes("RGB", (m.width, m.height), m.frame_rgb())
            if f in wanted:
                frames.append((img, wanted[f]))
            if f == g["banner"]:
                banner = img
        if state_dir is not None and f + 1 == g.get("state_at"):
            write_state(m, rom, img, state_dir)
    print(f"  {g['key']}: {g['total']} frames in {time.perf_counter() - t:.0f} s")
    return frames, banner


def write_state(m, rom: Path, img: Image.Image, state_dir: Path) -> None:
    """A save-state slot in the app's layout, for UI screenshots: <dir>/gb/<game>/states/slot1.state."""
    import io

    from src.bridge.savestate import save_file
    png = io.BytesIO()
    img.save(png, "PNG")
    path = state_dir / "gb" / rom.stem / "states" / "slot1.state"
    save_file(m, path, thumbnail=png.getvalue(), title=rom.stem)
    print("  wrote", path)


def save_gif(g: dict, frames: list[tuple[Image.Image, int]]) -> None:
    s = g["scale"]
    # one shared palette (from a sample of frames) keeps frame-to-frame deltas small
    sample = [img for img, _ in frames[::max(1, len(frames) // 16)]]
    mosaic = Image.new("RGB", (sample[0].width, sample[0].height * len(sample)))
    for i, img in enumerate(sample):
        mosaic.paste(img, (0, i * img.height))
    pal = mosaic.quantize(g.get("colors", 128), method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    imgs, durs = [], []
    for img, d in frames:
        big = img.resize((img.width * s, img.height * s), Image.NEAREST)
        imgs.append(big.quantize(palette=pal, dither=Image.Dither.NONE))
        durs.append(d)
    durs[-1] = 1200
    path = OUT / g["gif"]
    imgs[0].save(path, save_all=True, append_images=imgs[1:], duration=durs, loop=0,
                 optimize=True, disposal=1)
    print(f"  docs/{g['gif']}: {len(imgs)} frames, {path.stat().st_size / 1e6:.2f} MB")


def font(size: int):
    for name in ("seguisb.ttf", "segoeui.ttf", "DejaVuSans-Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def banner_card(shots: list[tuple[dict, Image.Image]]) -> None:
    tile_h = 288
    pad, gap = 28, 20
    widths = [round(img.width * tile_h / img.height) for _, img in shots]
    W = pad * 2 + sum(widths) + gap * (len(shots) - 1)
    H = pad + tile_h + 66 + pad - 6
    out = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(out)
    big, small = font(20), font(15)
    x = pad
    for (g, img), w in zip(shots, widths):
        d.rounded_rectangle((x - 6, pad - 6, x + w + 6, pad + tile_h + 66), 14, fill=CARD)
        out.paste(img.resize((w, tile_h), Image.NEAREST), (x, pad))
        d.ellipse((x, pad + tile_h + 18, x + 10, pad + tile_h + 28), fill=ACCENT[g["key"]])
        d.text((x + 18, pad + tile_h + 12), g["console"], font=big, fill=TEXT)
        d.text((x + 18, pad + tile_h + 38), g["game"], font=small, fill=MUTED)
        x += w + gap
    out.save(OUT / "games.png", optimize=True)
    print("docs/games.png", out.size)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("only", nargs="*", help="gb, gbc, gba and/or nes (default: all)")
    ap.add_argument("--state-dir", type=Path, help="also write the Pokemon Red Pallet Town state here")
    args = ap.parse_args()
    only = {k.upper() for k in args.only}
    set_save_dir(tempfile.mkdtemp(prefix="retropy-media-"))   # never touch real saves
    shots = []
    for g in GAMES:
        rom = find_rom(g)
        if rom is None:
            print(f"skipped {g['key']}: no rom/{g['folder']}/{g['glob']}")
            continue
        if only and g["key"] not in only:
            continue
        print(f"{g['console']}: {rom.name}")
        frames, banner = play(g, rom, args.state_dir)
        save_gif(g, frames)
        shots.append((g, banner))
    if len(shots) == len(GAMES):
        banner_card(shots)
    elif shots:
        print("(docs/games.png needs all four consoles; not rewritten)")


if __name__ == "__main__":
    main()
