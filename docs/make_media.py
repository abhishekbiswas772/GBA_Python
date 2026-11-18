"""Regenerate the README media from the emulator cores (open test ROMs only).

    uv run python docs/make_media.py

Writes docs/systems.png (one frame per core), docs/nes.gif (the NES core
working through its hardware tests) and docs/benchmark.json (frames per
second on this machine, used for the README speed table).
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from src.bridge import load  # noqa: E402

ROMS = ROOT / "tests" / "roms"
OUT = ROOT / "docs"
BG = (15, 17, 21)
CARD = (23, 26, 33)
TEXT = (232, 234, 240)
MUTED = (154, 163, 178)
ACCENT = {"GB": (159, 211, 86), "GBC": (242, 166, 90), "GBA": (142, 162, 255), "NES": (255, 122, 107)}


def frame(path: Path, frames: int, press: str | None = None) -> Image.Image:
    m = load(path)
    m.core.cart.save_ram = lambda *a, **k: False
    for i in range(frames):
        if press and i == frames // 2:
            m.press(press)
        m.run_frame(render=i >= frames - 2)
    return Image.frombytes("RGB", (m.width, m.height), m.frame_rgb())


def font(size: int):
    for name in ("seguisb.ttf", "segoeui.ttf", "DejaVuSans-Bold.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def systems_card() -> None:
    shots = [
        ("GB", "Game Boy", "DMG Acid2", frame(ROMS / "other" / "dmg-acid2.gb", 60)),
        ("GBC", "Game Boy Color", "CGB Acid2", frame(ROMS / "other" / "cgb-acid2.gbc", 60)),
        ("GBA", "Game Boy Advance", "Hello world test ROM", frame(ROMS / "gba" / "hello.gba", 60)),
        ("NES", "NES", "blargg CPU timing: passed", frame(ROMS / "nes" / "cpu_timing_test.nes", 1100)),
    ]
    tile_h = 240
    pad, gap = 28, 20
    widths = [round(img.width * tile_h / img.height) for *_, img in shots]
    W = pad * 2 + sum(widths) + gap * (len(shots) - 1)
    H = pad + tile_h + 66 + pad - 6
    out = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(out)
    big, small = font(20), font(14)
    x = pad
    for (key, name, rom, img), w in zip(shots, widths):
        d.rounded_rectangle((x - 6, pad - 6, x + w + 6, pad + tile_h + 66), 14, fill=CARD)
        out.paste(img.resize((w, tile_h), Image.NEAREST), (x, pad))
        d.ellipse((x, pad + tile_h + 18, x + 10, pad + tile_h + 28), fill=ACCENT[key])
        d.text((x + 18, pad + tile_h + 12), name, font=big, fill=TEXT)
        d.text((x + 18, pad + tile_h + 38), rom, font=small, fill=MUTED)
        x += w + gap
    out.save(OUT / "systems.png", optimize=True)
    print("docs/systems.png", out.size)


def nes_gif() -> None:
    """Real frames from the NES core: CPU timing test running, then PPU tests."""
    frames: list[Image.Image] = []
    durations: list[int] = []

    def record(path: Path, total: int, every: int, hold_last: int = 1400) -> None:
        m = load(path)
        m.core.cart.save_ram = lambda *a, **k: False
        for i in range(total):
            draw = i % every == 0 or i == total - 1
            m.run_frame(render=draw)
            if draw:
                frames.append(Image.frombytes("RGB", (256, 240), m.frame_rgb()))
                durations.append(int(every * 1000 / 60))
        durations[-1] = hold_last

    record(ROMS / "nes" / "cpu_timing_test.nes", 1080, 60)
    record(ROMS / "nes" / "01.basics.nes", 90, 30, 1100)
    record(ROMS / "nes" / "full_palette.nes", 40, 39, 1400)
    big = [f.resize((512, 480), Image.NEAREST).quantize(64, dither=Image.Dither.NONE) for f in frames]
    big[0].save(OUT / "nes.gif", save_all=True, append_images=big[1:], duration=durations,
                loop=0, optimize=True, disposal=1)
    print("docs/nes.gif", len(big), "frames")


def benchmark() -> None:
    cases = {
        "GB": ROMS / "other" / "dmg-acid2.gb",
        "GBC": ROMS / "other" / "cgb-acid2.gbc",
        "GBA": ROMS / "gba" / "hello.gba",
        "NES": ROMS / "nes" / "cpu_timing_test.nes",
    }
    res = {}
    for key, path in cases.items():
        m = load(path)
        m.core.cart.save_ram = lambda *a, **k: False
        m.run_frames(60)
        for render in (True, False):
            t = time.perf_counter()
            for _ in range(180):
                m.run_frame(render=render)
            fps = 180 / (time.perf_counter() - t)
            res.setdefault(key, {})["draw" if render else "skip"] = round(fps, 1)
        res[key]["realtime_pct"] = round(res[key]["draw"] / m.fps * 100)
    (OUT / "benchmark.json").write_text(json.dumps(res, indent=2) + "\n")
    print(json.dumps(res, indent=2))


if __name__ == "__main__":
    systems_card()
    nes_gif()
    benchmark()
