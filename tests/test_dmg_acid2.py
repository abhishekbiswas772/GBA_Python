import os
os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

from pathlib import Path

import pygame
import pytest

from src.gameboy.display import GRAY_PALETTE, framebuffer_surface
from src.gameboy.gameboy import GameBoy

ROM_DIR = Path(__file__).parent / "roms" / "other"
ACID_ROM = ROM_DIR / "dmg-acid2.gb"
ACID_REF = ROM_DIR / "dmg-acid2-reference.png"
ACID_ACTUAL = ROM_DIR / "dmg-acid2-actual.png"
POKEMON_ROM = (Path(__file__).parent.parent / "src" / "rom" /
               "Pokemon - Red Version (USA, Europe) (SGB Enhanced).gb")
POKEMON_PNG = ROM_DIR / "pokemon-smoke-actual.png"


def load_reference_indices(path: Path) -> bytes:
    surf = pygame.image.load(str(path))
    assert surf.get_size() == (160, 144), f"reference is {surf.get_size()}, expected (160, 144)"
    data = pygame.image.tobytes(surf, "RGB")
    shades = {data[i:i + 3] for i in range(0, len(data), 3)}
    assert len(shades) == 4, f"reference has {len(shades)} shades, expected 4"
    order = sorted(shades, key=lambda c: -(c[0] + c[1] + c[2]))   # brightest = color 0
    mapping = {c: i for i, c in enumerate(order)}
    return bytes(mapping[data[i:i + 3]] for i in range(0, len(data), 3))


@pytest.mark.slow
def test_dmg_acid2_pixel_perfect():
    ref = load_reference_indices(ACID_REF)
    gb = GameBoy(ACID_ROM)
    for _ in range(10):
        gb.run_frame()
    fb = bytes(gb.ppu.framebuffer)
    pygame.image.save(framebuffer_surface(gb.ppu.framebuffer, palette=GRAY_PALETTE),
                      str(ACID_ACTUAL))
    bad = sum(1 for a, b in zip(fb, ref) if a != b)
    assert bad == 0, f"{bad}/23040 mismatching pixels; see {ACID_ACTUAL} vs {ACID_REF}"


@pytest.mark.slow
def test_pokemon_smoke():
    if not POKEMON_ROM.exists():
        pytest.skip("commercial ROM not present")
    gb = GameBoy(POKEMON_ROM)
    for _ in range(200_000):
        gb.steps()
    pygame.image.save(framebuffer_surface(gb.ppu.framebuffer, palette=GRAY_PALETTE),
                      str(POKEMON_PNG))
