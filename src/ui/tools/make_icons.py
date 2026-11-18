"""Generate the app icon, favicon and splash image from one 16x16 pixel-art sprite.

    python -m src.ui.tools.make_icons

Writes into src/ui/assets/:
    icon.png      1024x1024, dark rounded tile  (flet build: app icon on every platform)
    icon.ico      256x256 PNG-in-ICO            (Windows window/taskbar icon)
    favicon.png   256x256, dark rounded tile    (web tab icon)
    splash.png    512x512, transparent          (flet build: native splash screen)
"""
from __future__ import annotations

import struct
from pathlib import Path

from src.ui.emulator.frame import encode_png

ASSETS = Path(__file__).resolve().parents[1] / "assets"

# B body (accent), D dark ink, G screen, L screen highlight, . transparent
SPRITE = [
    "................",
    "...BBBBBBBBBB...",
    "..BBBBBBBBBBBB..",
    "..BBDDDDDDDDBB..",
    "..BBDGGGGGGDBB..",
    "..BBDGLLGGGDBB..",
    "..BBDGLGGGGDBB..",
    "..BBDGGGGGGDBB..",
    "..BBDDDDDDDDBB..",
    "..BBBBBBBBBBBB..",
    "..BBBDBBBBBBDB..",
    "..BBDDDBBBDBBB..",
    "..BBBDBBBBBBBB..",
    "..BBBBBDDBBBB...",
    "...BBBBBBBBB....",
    "................",
]

PALETTE = {
    "B": (0x8E, 0xA2, 0xFF, 255),     # Midnight accent
    "D": (0x0F, 0x11, 0x15, 255),     # Midnight bg / ink
    "G": (0x9B, 0xBC, 0x0F, 255),     # DMG screen green
    "L": (0xE0, 0xF8, 0xD0, 255),     # DMG lightest
    ".": (0, 0, 0, 0),
}
TILE = (0x0F, 0x11, 0x15)             # background tile colour
TILE_EDGE = (0x23, 0x27, 0x33)        # 1-step border, Midnight `line`


def render(size: int, art: int, tile: bool) -> bytes:
    """RGBA pixels: optional rounded dark tile with the sprite centred at `art` px."""
    scale = art // 16
    off = (size - scale * 16) // 2
    radius = size * 0.22
    edge = max(2, size // 128)
    px = bytearray(size * size * 4)

    if tile:
        for y in range(size):
            for x in range(size):
                # distance outside the rounded-rect core -> inside/edge/outside
                dx = max(radius - x, 0, x - (size - 1 - radius))
                dy = max(radius - y, 0, y - (size - 1 - radius))
                d = (dx * dx + dy * dy) ** 0.5
                if d > radius:
                    continue
                i = (y * size + x) * 4
                px[i:i + 4] = bytes((*(TILE_EDGE if d > radius - edge or x < edge or y < edge
                                        or x >= size - edge or y >= size - edge else TILE), 255))

    for sy, row in enumerate(SPRITE):
        for sx, ch in enumerate(row):
            r, g, b, a = PALETTE[ch]
            if not a:
                continue
            for y in range(off + sy * scale, off + (sy + 1) * scale):
                start = (y * size + off + sx * scale) * 4
                px[start:start + scale * 4] = bytes((r, g, b, a)) * scale
    return bytes(px)


def png(size: int, art: int, tile: bool) -> bytes:
    return encode_png(render(size, art, tile), size, size, channels=4, level=9)


def ico(png_bytes: bytes) -> bytes:
    """Single-image ICO holding a 256x256 PNG (supported since Windows Vista)."""
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png_bytes), 6 + 16)
    return header + entry + png_bytes


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    (ASSETS / "icon.png").write_bytes(png(1024, 640, tile=True))
    fav = png(256, 192, tile=True)
    (ASSETS / "favicon.png").write_bytes(fav)
    (ASSETS / "icon.ico").write_bytes(ico(fav))
    (ASSETS / "splash.png").write_bytes(png(512, 512, tile=False))
    for name in ("icon.png", "icon.ico", "favicon.png", "splash.png"):
        print(f"wrote {ASSETS / name}")


if __name__ == "__main__":
    main()
