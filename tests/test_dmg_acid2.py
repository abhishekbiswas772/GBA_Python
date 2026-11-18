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


CGB_ACID_ROM = ROM_DIR / "cgb-acid2.gbc"
CGB_ACID_REF = ROM_DIR / "cgb-acid2-reference.png"
CGB_ACID_ACTUAL = ROM_DIR / "cgb-acid2-actual.png"
CGB_GAME_PNG = ROM_DIR / "cgb-game-smoke-actual.png"


@pytest.mark.slow
def test_cgb_acid2_pixel_perfect():
    ref = pygame.image.tobytes(pygame.image.load(str(CGB_ACID_REF)), "RGB")
    gb = GameBoy(CGB_ACID_ROM)
    assert gb.cgb
    prev = None
    for _ in range(120):                    # until the LCD is on and the frame is stable
        gb.run_frame()
        cur = bytes(gb.ppu.rgb)
        if cur == prev and gb.ppu.lcdc & 0x80:
            break
        prev = cur
    pygame.image.save(pygame.image.frombytes(cur, (160, 144), "RGB"), str(CGB_ACID_ACTUAL))
    # compare at RGB555 precision: independent of the 5->8 bit expansion formula
    bad = sum(1 for a, b in zip(cur, ref) if a >> 3 != b >> 3)
    assert bad == 0, f"{bad} mismatching channel values; see {CGB_ACID_ACTUAL} vs {CGB_ACID_REF}"


@pytest.mark.slow
def test_cgb_game_smoke(tmp_path):
    import shutil
    roms = sorted((Path(__file__).parent.parent / "src" / "rom").glob("*.gbc"))
    if not roms:
        pytest.skip("no .gbc ROM in src/rom/")
    rom = tmp_path / roms[0].name               # private copy: never touch the real .sav
    shutil.copy(roms[0], rom)
    gb = GameBoy(rom)
    assert gb.cgb
    for _ in range(300):
        gb.run_frame()
    pygame.image.save(pygame.image.frombytes(bytes(gb.ppu.rgb), (160, 144), "RGB"),
                      str(CGB_GAME_PNG))
    assert len(set(gb.ppu.framebuffer)) > 1, "screen is a single colour after 300 frames"
