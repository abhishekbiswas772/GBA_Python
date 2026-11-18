"""Turn emulator framebuffers into PNG bytes for ft.Image.

Pure Python (zlib + struct) so it works on desktop, mobile and web without
Pillow or pygame. A 160x144 frame encodes in about a millisecond.
"""
from __future__ import annotations

import struct
import zlib
from functools import lru_cache

from src.core.gameboy.common import HEIGHT, WIDTH


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_png(pixels: bytes, w: int, h: int, channels: int = 3, level: int = 1) -> bytes:
    stride = w * channels
    raw = b"".join(b"\x00" + pixels[y * stride:(y + 1) * stride] for y in range(h))
    color_type = 2 if channels == 3 else 6
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(raw, level))
            + _chunk(b"IEND", b""))


@lru_cache(maxsize=8)
def _tables(palette: tuple) -> tuple[bytes, bytes, bytes]:
    return tuple(bytes(palette[i & 3][ch] for i in range(256)) for ch in range(3))


def _interleave(r: bytes, g: bytes, b: bytes) -> bytes:
    out = bytearray(len(r) * 3)
    out[0::3], out[1::3], out[2::3] = r, g, b
    return bytes(out)


def dmg_rgb(framebuffer, palette: tuple) -> bytes:
    """DMG framebuffer of shade indices 0-3 -> RGB888 using a 4-color palette."""
    src = bytes(framebuffer)
    t = _tables(palette)
    return _interleave(src.translate(t[0]), src.translate(t[1]), src.translate(t[2]))


# A mild per-channel curve that approximates the darker, less saturated GBC LCD.
_LCD_CURVE = bytes(min(255, int(((i / 255) ** 1.25) * 235 + 12)) for i in range(256))


def lcd_correct(rgb: bytes) -> bytes:
    return rgb.translate(_LCD_CURVE)


def blend(prev: bytes | None, cur: bytes) -> bytes:
    """Average two frames (LCD ghosting) with big-int arithmetic: no per-pixel loop."""
    if not prev or len(prev) != len(cur):
        return cur
    n = len(cur)
    mask = _mask(n)
    a = int.from_bytes(prev, "big") & mask
    b = int.from_bytes(cur, "big") & mask
    return ((a >> 1) + (b >> 1)).to_bytes(n, "big")


@lru_cache(maxsize=2)
def _mask(n: int) -> int:
    return int.from_bytes(b"\xfe" * n, "big")


def scale_nearest(rgb: bytes, w: int, h: int, k: int) -> bytes:
    rows = []
    for y in range(h):
        row = rgb[y * w * 3:(y + 1) * w * 3]
        wide = b"".join(row[x * 3:x * 3 + 3] * k for x in range(w))
        rows.append(wide * k)
    return b"".join(rows)


@lru_cache(maxsize=8)
def shader_overlay(shader: str, w: int = WIDTH, h: int = HEIGHT) -> bytes | None:
    """Transparent RGBA PNG drawn over the screen for LCD grid / CRT scanlines."""
    k = 4
    W, H = w * k, h * k
    clear = b"\x00\x00\x00\x00"
    if shader == "lcd":                  # thin dark grid between every LCD cell
        dark = b"\x00\x00\x00" + bytes([70])
        cell_row = (clear * (k - 1) + dark) * w
        rows = [cell_row] * (k - 1) + [dark * W]
    elif shader == "crt":                # darker scanline every native row
        dark = b"\x00\x00\x00" + bytes([120])
        rows = [clear * W] * (k - 2) + [dark * W] * 2
    else:
        return None
    px = b"".join(rows) * h
    return encode_png(px, W, H, channels=4, level=9)


def blank_png(w: int = WIDTH, h: int = HEIGHT, color: tuple = (0, 0, 0)) -> bytes:
    return encode_png(bytes(color) * (w * h), w, h)
