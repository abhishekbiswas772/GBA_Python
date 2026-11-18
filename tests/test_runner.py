"""EmulatorRunner: worker-thread emulation, input latching, frameskip, shutdown."""
import time
from pathlib import Path

import pytest

from src import bridge
from src.bridge.runner import EmulatorRunner

ROMS = Path(__file__).parent / "roms"


class FakeApu:
    sampling = False

    def take_samples(self):
        return []


class FakeMachine:
    """Records what the runner does; drawing a frame costs `draw_cost` seconds."""
    system, width, height, fps = "GBA", 2, 1, 60.0

    def __init__(self, draw_cost=0.0, logic_cost=0.0):
        self.apu = FakeApu()
        self.draw_cost, self.logic_cost = draw_cost, logic_cost
        self.frames = 0
        self.drawn = 0
        self.log = []                 # (frame index, "press"/"release", button)
        self.saved = 0

    def run_frame(self, render=True):
        if self.logic_cost:
            time.sleep(self.logic_cost)
        if render:
            self.drawn += 1
            if self.draw_cost:
                time.sleep(self.draw_cost)
        self.frames += 1

    def press(self, b):
        self.log.append((self.frames, "press", b))

    def release(self, b):
        self.log.append((self.frames, "release", b))

    def frame_rgb(self, palette=None):
        return b"\x00" * 6

    def save(self):
        self.saved += 1
        return True

    def reset(self):
        pass


def _wait(cond, timeout=3.0):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


def test_quick_tap_is_held_for_a_frame():
    m = FakeMachine()
    r = EmulatorRunner(lambda: m, audio=False)
    r.set_paused(True)                          # queue the tap while no frame runs
    r.start()
    assert r.ready.wait(2)
    r.press("A")
    r.release("A")
    r.set_paused(False)
    assert _wait(lambda: any(e[1] == "release" for e in m.log))
    r.close()
    (f_press, _, b1), (f_release, _, b2) = [e for e in m.log if e[2] == "A"][:2]
    assert b1 == b2 == "A"
    assert f_release > f_press                  # the game saw A for at least one frame


def test_held_key_and_release_all():
    m = FakeMachine()
    r = EmulatorRunner(lambda: m, audio=False)
    r.start()
    r.press("Left")
    assert _wait(lambda: m.frames > 3)
    r.release_all()
    assert _wait(lambda: ("release", "Left") in [(e[1], e[2]) for e in m.log])
    r.close()


def test_frameskip_keeps_game_speed_when_drawing_is_slow():
    # drawing costs 2 frames' worth of time: without frameskip the game would run at ~50%
    m = FakeMachine(draw_cost=1 / 30)
    r = EmulatorRunner(lambda: m, audio=False)
    r.start()
    time.sleep(1.5)
    r.close()
    assert m.frames >= 60                       # ~90 frames of game time in 1.5 s (>= 2/3)
    assert m.drawn < m.frames                   # ...because some frames were not drawn
    assert m.drawn >= m.frames // 4             # never more than MAX_SKIP in a row


@pytest.mark.parametrize("speed,ff,expect", [(1, False, 60), (2, False, 120), (1, True, 240)])
def test_speed_multipliers(speed, ff, expect):
    m = FakeMachine()
    r = EmulatorRunner(lambda: m, audio=False)
    r.set_speed(speed)
    r.set_fast_forward(ff)
    r.start()
    assert r.ready.wait(2)
    f0 = m.frames
    time.sleep(1.0)
    n = m.frames - f0
    r.close()
    assert 0.75 * expect <= n <= 1.3 * expect, n


def test_close_saves_and_stops():
    m = FakeMachine()
    r = EmulatorRunner(lambda: m, audio=False)
    r.start()
    assert _wait(lambda: m.frames > 2)
    r.close()
    assert not r.alive
    assert m.saved == 1


def test_load_error_is_reported():
    def boom():
        raise ValueError("not a ROM")
    r = EmulatorRunner(boom, audio=False)
    r.start()
    assert r.ready.wait(2)
    assert "not a ROM" in r.error
    assert r.paused
    r.close()


def test_real_rom_publishes_frames():
    rom = ROMS / "gba" / "stripes.gba"
    if not rom.is_file():
        pytest.skip("missing stripes.gba")
    r = EmulatorRunner(lambda: bridge.load(rom), audio=False)
    r.start()
    assert r.ready.wait(10)
    assert _wait(lambda: r.latest is not None and r.latest.seq > 5, timeout=10)
    f = r.latest
    assert (f.width, f.height) == (240, 160) and len(f.rgb) == 240 * 160 * 3
    r.close()
    assert r.error is None


def test_cgb_joypad_reads_pressed_button():
    """Core-level check: on a Game Boy Color ROM, A pressed through the bridge is
    visible in FF00 (so a GBC keyboard problem is UI-side, not in the core)."""
    rom = ROMS / "other" / "cgb-acid2.gbc"
    if not rom.is_file():
        pytest.skip("missing cgb-acid2.gbc")
    m = bridge.load(rom)
    assert m.system == "GBC"
    mmu = m.core.mmu
    m.press("A")
    mmu.write(0xFF00, 0x10)                     # select the action buttons
    assert mmu.read(0xFF00) & 0x01 == 0         # A pressed (active low)
    m.release("A")
    assert mmu.read(0xFF00) & 0x01 == 1
