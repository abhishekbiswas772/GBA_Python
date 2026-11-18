"""Tests for src/bridge: the unified Machine adapter over the GB and GBA cores.

ROM-dependent tests skip when the ROM file is missing so the suite stays
green on fresh checkouts.
"""
from __future__ import annotations

import random
from array import array
from pathlib import Path

import pytest

from src.bridge import (
    BUTTONS, GBAMachine, GameBoyMachine, Machine, bgr555_to_rgb888, detect,
    load, normalize_button,
)
from src.bridge.audio import SAMPLE_RATE, set_sampling, take_samples
from src.bridge.gb_machine import dmg_rgb

ROMS_GBA = Path(__file__).parent / "roms" / "gba"
COMMERCIAL = Path(__file__).parent.parent / "rom"

HELLO = ROMS_GBA / "hello.gba"
SHADES = ROMS_GBA / "shades.gba"
MINISH_CAP = COMMERCIAL / "gba" / "Legend of Zelda, The - The Minish Cap (USA).gba"
POKEMON_RED = COMMERCIAL / "gb" / "Pokemon - Red Version (USA, Europe) (SGB Enhanced).gb"
POKEMON_CRYSTAL = COMMERCIAL / "gb_color" / "Pokemon - Crystal Version (USA, Europe) (Rev 1).gbc"

needs = lambda p: pytest.mark.skipif(not p.exists(), reason=f"{p.name} not present")


# ---------------------------------------------------------------- detect / load
def test_detect_extensions(tmp_path):
    assert detect(HELLO) == "GBA"
    gb = tmp_path / "x.gb"
    gb.write_bytes(b"\x00" * 0x200)
    assert detect(gb) == "GB"                       # no CGB flag at 0x0143
    gb.write_bytes(b"\x00" * 0x143 + b"\x80" + b"\x00" * 0x200)
    assert detect(gb) == "GBC"                      # CGB enhanced
    assert detect(tmp_path / "x.gbc") == "GBC"
    assert detect(tmp_path / "x.agb") == "GBA"


def test_detect_rejects_unknown_extension(tmp_path):
    bad = tmp_path / "rom.bin"
    bad.write_bytes(b"\x00" * 16)
    with pytest.raises(ValueError):
        detect(bad)


def test_normalize_button():
    assert normalize_button("a") == "A"
    assert normalize_button(" START ") == "Start"
    assert normalize_button("L") == "L"
    assert set(BUTTONS) == {"A", "B", "Start", "Select",
                            "Up", "Down", "Left", "Right", "L", "R"}
    with pytest.raises(ValueError):
        normalize_button("X")


# ---------------------------------------------------------------- BGR555 conversion
def _bgr555(r: int, g: int, b: int) -> bytes:
    return bytes(((r | g << 5 | b << 10) & 0xFF, (r | g << 5 | b << 10) >> 8))


def _exp5(v: int) -> int:
    return (v << 3) | (v >> 2)


def test_bgr555_known_colours():
    assert bgr555_to_rgb888(_bgr555(31, 31, 31)) == b"\xff\xff\xff"   # 0x7FFF white
    assert bgr555_to_rgb888(_bgr555(31, 0, 0)) == b"\xff\x00\x00"
    assert bgr555_to_rgb888(_bgr555(0, 31, 0)) == b"\x00\xff\x00"
    assert bgr555_to_rgb888(_bgr555(0, 0, 31)) == b"\x00\x00\xff"
    assert bgr555_to_rgb888(_bgr555(0, 0, 0)) == b"\x00\x00\x00"
    assert bgr555_to_rgb888(_bgr555(1, 2, 3)) == bytes(
        (_exp5(1), _exp5(2), _exp5(3)))


def test_bgr555_matches_naive_loop():
    rnd = random.Random(1234)
    raw = bytes(rnd.randrange(256) for _ in range(2 * 5000))
    expected = bytearray()
    for i in range(0, len(raw), 2):
        v = raw[i] | raw[i + 1] << 8
        for shift in (0, 5, 10):
            expected.append(_exp5((v >> shift) & 0x1F))
    assert bgr555_to_rgb888(raw) == bytes(expected)


def test_bgr555_empty():
    assert bgr555_to_rgb888(b"") == b""


# ---------------------------------------------------------------- DMG palette
def test_dmg_rgb_palette():
    fb = bytes([0, 1, 2, 3])
    pal = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (9, 9, 9))
    assert dmg_rgb(fb, pal) == bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 9, 9, 9])


# ---------------------------------------------------------------- GBA
@needs(HELLO)
def test_gba_hello_frames():
    m = load(HELLO)
    assert isinstance(m, GBAMachine)
    assert (m.system, m.width, m.height) == ("GBA", 240, 160)
    m.run_frames(30)
    rgb = m.frame_rgb()
    assert len(rgb) == 240 * 160 * 3
    colours = {rgb[i:i + 3] for i in range(0, len(rgb), 3)}
    assert len(colours) > 1, "frame is one flat colour"


@needs(SHADES)
def test_gba_shades_multi_colour():
    m = load(SHADES)
    m.run_frames(30)
    rgb = m.frame_rgb()
    assert len(rgb) == 240 * 160 * 3
    assert len({rgb[i:i + 3] for i in range(0, len(rgb), 3)}) > 4


@needs(MINISH_CAP)
def test_gba_minish_cap_boots():
    m = load(MINISH_CAP)
    assert m.system == "GBA"
    assert "ZELDA" in m.title_info.upper()
    for _ in range(4):                       # boot/logo takes a couple of seconds
        m.run_frames(30)
        rgb = m.frame_rgb()
        assert len(rgb) == 240 * 160 * 3
        if len({rgb[i:i + 3] for i in range(0, len(rgb), 3)}) > 1:
            break
    else:
        pytest.fail("still a flat colour after 120 frames")


@needs(HELLO)
def test_gba_input_and_save():
    m = load(HELLO)
    m.press("A"); m.press("L"); m.release("L"); m.release("A")
    assert isinstance(m.save(), bool)
    m.run_frames(2)
    m.reset()
    m.run_frames(2)
    assert len(m.frame_rgb()) == 240 * 160 * 3


@needs(HELLO)
def test_gba_audio_contract():
    m = load(HELLO)
    set_sampling(m.apu, True)
    for _ in range(3):
        m.run_frame()
    data = take_samples(m.apu)
    assert isinstance(data, array) and data.typecode == "h"
    assert len(data) % 2 == 0                  # interleaved stereo
    assert len(data) > 0


# ---------------------------------------------------------------- GB / GBC
@needs(POKEMON_RED)
def test_gb_pokemon_red():
    m = load(POKEMON_RED)
    assert isinstance(m, GameBoyMachine)
    assert m.system == "GB"                    # .gb without CGB flag
    m.run_frames(10)
    rgb = m.frame_rgb()
    assert len(rgb) == 160 * 144 * 3
    assert len(m.dmg_frame) == 160 * 144
    # L / R are GBA-only: accepted but ignored on Game Boy
    m.press("L"); m.press("R"); m.release("L"); m.release("R")


@needs(POKEMON_CRYSTAL)
def test_gbc_pokemon_crystal():
    m = load(POKEMON_CRYSTAL)
    assert m.system == "GBC"
    m.run_frames(10)
    rgb = m.frame_rgb()
    assert len(rgb) == 160 * 144 * 3
    # frame_rgb ignores the DMG palette in CGB mode
    assert m.frame_rgb(((0, 0, 0),) * 4) == rgb


@needs(HELLO)
def test_machine_is_protocol():
    assert isinstance(load(HELLO), Machine)


def test_fps_constants():
    assert 59.7 < GBAMachine.fps < 59.8
    assert 59.7 < GameBoyMachine.fps < 59.8
