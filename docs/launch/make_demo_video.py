"""Stitch the launch demo video from the README media.

    uv run python docs/make_game_media.py          # first: docs/game-*.gif (needs your ROMs)
    uv run python docs/launch/make_demo_video.py   # -> docs/launch/retropy-demo.mp4

1280x720, 30 fps, H.264 (yuv420p, faststart), silent. Sequence: title card, one
gameplay clip per console (from docs/game-*.gif), the app screenshots
(docs/ui-*.png), closing card. Uses PyAV (already a dependency) and Pillow.
"""
from __future__ import annotations

import sys
from fractions import Fraction
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageFont, ImageSequence

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs"
FONTS = ROOT / "src" / "ui" / "assets" / "fonts"
OUT = DOCS / "launch" / "retropy-demo.mp4"
W, H, FPS = 1280, 720, 30
REPO = "github.com/abhishekbiswas772/RetroPy"

BG = (15, 17, 21)
CARD = (23, 26, 33)
TEXT = (232, 234, 240)
MUTED = (154, 163, 178)
BLUE = (61, 85, 214)
ACCENT = {"GB": (159, 211, 86), "GBC": (242, 166, 90), "GBA": (142, 162, 255), "NES": (255, 122, 107)}

# console, game, gif, windows of the GIF timeline to use (seconds)
CLIPS = [
    ("GB", "Game Boy", "Pokémon Red", "game-gb.gif", [(0.6, 2.6), (7.6, 12.1)]),
    ("GBC", "Game Boy Color", "The Legend of Zelda: Oracle of Ages", "game-gbc.gif", [(18.4, 23.6)]),
    ("GBA", "Game Boy Advance", "Pokémon FireRed", "game-gba.gif", [(1.0, 5.0), (11.6, 13.8)]),
    ("NES", "NES", "Contra", "game-nes.gif", [(2.2, 8.2)]),
]
SHOTS = [
    ("ui-library.png", "Your library", "Covers from your last frame, continue where you left off"),
    ("ui-player.png", "Save states and rewind", "4 slots + resume point on every core, hold R to rewind 30 s"),
    ("ui-recording.png", "Record and clip", "MP4 with sound or GIF, or save the last 30 s after the fact"),
    ("ui-settings.png", "Per-console controls", "Keyboard, gamepads and touch, every hotkey rebindable"),
]


def font(name: str, size: int):
    try:
        return ImageFont.truetype(str(FONTS / name), size)
    except OSError:
        return ImageFont.load_default()


def centered(d: ImageDraw.ImageDraw, y: int, text: str, f, fill) -> None:
    w = d.textlength(text, font=f)
    d.text(((W - w) / 2, y), text, font=f, fill=fill)


def title_card() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    icon = Image.open(ROOT / "assets" / "icon.png").convert("RGBA").resize((120, 120), Image.LANCZOS)
    img.paste(icon, ((W - 120) // 2, 150), icon)
    centered(d, 300, "RETROPY", font("Silkscreen-Bold.ttf", 76), TEXT)
    centered(d, 410, "Game Boy · Game Boy Color · Game Boy Advance · NES",
             font("Outfit-SemiBold.ttf", 34), TEXT)
    centered(d, 462, "emulated from scratch in pure Python", font("Outfit-Regular.ttf", 30), MUTED)
    centered(d, 540, "+ an experimental PlayStation core in Rust", font("Outfit-Regular.ttf", 24), MUTED)
    return img


def end_card() -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    centered(d, 170, "RETROPY", font("Silkscreen-Bold.ttf", 64), TEXT)
    centered(d, 270, "Save states · rewind · MP4 recording · gamepads · 4 themes",
             font("Outfit-Regular.ttf", 28), MUTED)
    centered(d, 316, "Windows installer and .deb built by CI · MIT licensed",
             font("Outfit-Regular.ttf", 28), MUTED)
    f = font("JetBrainsMono-Medium.ttf", 34)
    tw = d.textlength(REPO, font=f)
    x0, y0 = (W - tw) / 2 - 28, 410
    d.rounded_rectangle((x0, y0, x0 + tw + 56, y0 + 74), 16, fill=BLUE)
    d.text((x0 + 28, y0 + 16), REPO, font=f, fill=(255, 255, 255))
    centered(d, 530, "Try it, break it, star it.", font("Outfit-SemiBold.ttf", 30), TEXT)
    centered(d, 640, "No games are included. Footage: the author's own cartridge dumps.",
             font("Outfit-Regular.ttf", 18), MUTED)
    return img


def label_bar(img: Image.Image, key: str, console: str, game: str) -> None:
    d = ImageDraw.Draw(img)
    d.rectangle((0, H - 92, W, H), fill=CARD)
    d.ellipse((40, H - 58, 58, H - 40), fill=ACCENT[key])
    d.text((74, H - 72), console, font=font("Outfit-SemiBold.ttf", 32), fill=TEXT)
    cw = d.textlength(console, font=font("Outfit-SemiBold.ttf", 32))
    d.text((74 + cw + 18, H - 66), game, font=font("Outfit-Regular.ttf", 26), fill=MUTED)
    tag = "pure Python core · real-time capture of the emulator's frames"
    tf = font("Outfit-Regular.ttf", 18)
    d.text((W - 40 - d.textlength(tag, font=tf), H - 58), tag, font=tf, fill=MUTED)


def gif_frames(path: Path, windows) -> list[Image.Image]:
    """Resample a GIF to FPS, keeping only the given (start, end) second windows."""
    src, t = [], 0.0
    with Image.open(path) as im:
        for fr in ImageSequence.Iterator(im):
            dur = fr.info.get("duration", 50) / 1000
            src.append((t, t + dur, fr.convert("RGB")))
            t += dur
    out = []
    for a, b in windows:
        n = int((b - a) * FPS)
        i = 0
        for k in range(n):
            ts = a + k / FPS
            while i < len(src) - 1 and src[i][1] <= ts:
                i += 1
            out.append(src[i][2])
    return out


def clip_frames(key, console, game, gif, windows) -> list[Image.Image]:
    frames = []
    area_h = H - 92 - 48
    for g in gif_frames(DOCS / gif, windows):
        s = max(1, area_h // g.height)                   # integer scale: sharp pixels
        big = g.resize((g.width * s, g.height * s), Image.NEAREST)
        if big.height > area_h or big.width > W - 80:
            r = min(area_h / big.height, (W - 80) / big.width)
            big = g.resize((round(g.width * s * r), round(g.height * s * r)), Image.NEAREST)
        img = Image.new("RGB", (W, H), BG)
        img.paste(big, ((W - big.width) // 2, 24 + (area_h - big.height) // 2))
        label_bar(img, key, console, game)
        frames.append(img)
    return frames


def shot_card(name: str, title: str, sub: str) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((48, 28), title, font=font("Outfit-SemiBold.ttf", 38), fill=TEXT)
    d.text((48, 80), sub, font=font("Outfit-Regular.ttf", 24), fill=MUTED)
    shot = Image.open(DOCS / name).convert("RGB")
    box_w, box_h = W - 96, H - 150
    r = min(box_w / shot.width, box_h / shot.height)
    shot = shot.resize((round(shot.width * r), round(shot.height * r)), Image.LANCZOS)
    x, y = (W - shot.width) // 2, 128 + (box_h - shot.height) // 2
    d.rounded_rectangle((x - 3, y - 3, x + shot.width + 3, y + shot.height + 3), 10, fill=(60, 66, 80))
    img.paste(shot, (x, y))
    return img


def hold(img: Image.Image, seconds: float) -> list[Image.Image]:
    return [img] * int(seconds * FPS)


def crossfade(a: list[Image.Image], b: list[Image.Image], n: int = 9) -> list[Image.Image]:
    if not a:
        return b
    mid = [Image.blend(a[-1], b[0], (i + 1) / (n + 1)) for i in range(n)]
    return a + mid + b


def main() -> None:
    missing = [p for p in [c[3] for c in CLIPS] + [s[0] for s in SHOTS] if not (DOCS / p).is_file()]
    if missing:
        sys.exit(f"missing docs/{', docs/'.join(missing)}: run docs/make_game_media.py and take the UI shots first")
    seq: list[Image.Image] = hold(title_card(), 2.6)
    for c in CLIPS:
        seq = crossfade(seq, clip_frames(*c))
    for s in SHOTS:
        seq = crossfade(seq, hold(shot_card(*s), 3.0))
    seq = crossfade(seq, hold(end_card(), 4.0))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with av.open(str(OUT), "w", options={"movflags": "+faststart"}) as box:
        st = box.add_stream("libx264", rate=FPS)
        st.width, st.height, st.pix_fmt = W, H, "yuv420p"
        st.time_base = Fraction(1, FPS)
        st.options = {"crf": "20", "preset": "slow", "tune": "animation"}
        for i, img in enumerate(seq):
            fr = av.VideoFrame.from_image(img)
            fr.pts = i
            for pkt in st.encode(fr):
                box.mux(pkt)
        for pkt in st.encode():
            box.mux(pkt)
    print(f"{OUT.relative_to(ROOT)}: {len(seq) / FPS:.1f} s, {OUT.stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
