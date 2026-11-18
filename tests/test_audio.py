"""Slow smoke: capture Pokemon Red title-screen audio to a WAV (no mixer used)."""
import shutil
import wave
from array import array
from pathlib import Path

import pytest

from src.core.gameboy.gameboy import GameBoy
from tests.helpers import find_rom

POKEMON_ROM = find_rom("Pokemon - Red Version (USA, Europe) (SGB Enhanced).gb")
POKEMON_WAV = Path(__file__).parent / "roms" / "other" / "pokemon-audio-actual.wav"


def test_pokemon_red_title_audio(tmp_path):
    if POKEMON_ROM is None:
        pytest.skip("commercial ROM not present in rom/gb/ (Pokemon - Red Version)")
    rom = tmp_path / "pokemon.gb"             # private copy: never touch the real .sav
    shutil.copyfile(POKEMON_ROM, rom)
    gb = GameBoy(rom)
    gb.apu.sampling = True
    for _ in range(1500):                     # boot -> title screen (see test_mbc)
        gb.run_frame()
    gb.apu.take_samples()                     # discard pre-title audio
    captured = array('h')
    for _ in range(300):                      # 5 s of title-screen music
        gb.run_frame()
        captured.extend(gb.apu.take_samples())

    with wave.open(str(POKEMON_WAV), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(captured.tobytes())

    frames = len(captured) // 2
    duration = frames / 44100
    left = captured[0::2]
    right = captured[1::2]

    def rms(xs):
        return (sum(v * v for v in xs) / max(len(xs), 1)) ** 0.5

    peak = max(abs(v) for v in captured)
    sat = sum(1 for v in captured if v >= 32767 or v <= -32768)
    print(f"\naudio: {duration:.2f}s, {frames} frames, peak {peak}, "
          f"rms L {rms(left):.0f} / R {rms(right):.0f}, "
          f"sat {sat}, NR51 0x{gb.apu.nr51:02X}")

    assert 4.5 < duration < 5.5               # ~5 s captured
    assert sat == 0                           # never clips
    assert peak > 1000                        # real signal, non-silent
    assert rms(left) > 100 and rms(right) > 100
    assert len(set(captured)) > 1000          # not constant / not a single tone
