"""Save states, rewind and recording (src/bridge/savestate, rewind, recorder)."""
import hashlib
import io
import pickle
import time
from pathlib import Path

import pytest

from src.bridge import EmulatorRunner, load, savestate
from src.bridge.recorder import Recorder, encode_gif
from src.bridge.rewind import RewindBuffer

ROMS = Path(__file__).parent / "roms"
GAMES = [
    ROMS / "other" / "dmg-acid2.gb",
    ROMS / "other" / "cgb-acid2.gbc",
    ROMS / "cpu_instrs" / "02-interrupts.gb",
    ROMS / "gba" / "stripes.gba",
    ROMS / "gba" / "arm.gba",
    ROMS / "nes" / "scanline.nes",
    ROMS / "nes" / "cpu_timing_test.nes",
]


def _machine(path):
    if not path.exists():
        pytest.skip(f"missing {path.name}")
    m = load(path)
    m.core.cart.save_ram = lambda *a, **k: False       # never touch a real .sav
    m.apu.sampling = True
    return m


def _play(m, n=60) -> str:
    """Run n frames; hash of every frame and all audio."""
    h = hashlib.sha1()
    m.apu.take_samples()
    for _ in range(n):
        m.run_frame()
        h.update(m.frame_rgb())
        h.update(m.apu.take_samples().tobytes())
    return h.hexdigest()


@pytest.mark.parametrize("rom", GAMES, ids=lambda p: p.name)
def test_restore_in_place_replays_identically(rom):
    m = _machine(rom)
    m.run_frames(60)
    snap = savestate.capture(m)
    ref = _play(m)
    core = m.core
    savestate.restore(m, snap)
    assert m.core is core                       # same objects: compiled code stays valid
    assert _play(m) == ref
    savestate.restore(m, snap)                  # a snapshot can be used again
    assert _play(m) == ref


@pytest.mark.parametrize("rom", GAMES, ids=lambda p: p.name)
def test_state_file_round_trip(rom, tmp_path):
    m = _machine(rom)
    m.run_frames(60)
    f = tmp_path / "s.state"
    meta = savestate.save_file(m, f, thumbnail=b"png", title="t")
    assert meta["system"] == m.system and f.with_suffix(".png").read_bytes() == b"png"
    ref = _play(m)
    fresh = _machine(rom)                       # a new session of the same game
    savestate.load_file(fresh, f)
    assert _play(fresh) == ref
    assert savestate.read_meta(f)["system"] == m.system


def test_state_keeps_rom_out_of_the_file(tmp_path):
    m = _machine(ROMS / "gba" / "stripes.gba")
    f = tmp_path / "s.state"
    savestate.save_file(m, f)
    assert f.stat().st_size < len(m.core.bus.rom) // 4


def test_wrong_game_is_rejected(tmp_path):
    a = _machine(ROMS / "nes" / "scanline.nes")
    b = _machine(ROMS / "nes" / "cpu_timing_test.nes")
    f = tmp_path / "a.state"
    savestate.save_file(a, f)
    with pytest.raises(ValueError, match="different game"):
        savestate.load_file(b, f)
    gb = _machine(ROMS / "other" / "dmg-acid2.gb")
    with pytest.raises(ValueError, match="for NES"):
        savestate.load_file(gb, f)


def test_malicious_state_file_cannot_run_code(tmp_path):
    m = _machine(ROMS / "nes" / "scanline.nes")
    f = tmp_path / "evil.state"
    savestate.save_file(m, f)
    head = f.read_bytes()
    n = int.from_bytes(head[len(savestate.MAGIC):len(savestate.MAGIC) + 4], "little")

    class Evil:
        def __reduce__(self):
            return (print, ("pwned",))
    import zlib
    f.write_bytes(head[:len(savestate.MAGIC) + 4 + n] + zlib.compress(pickle.dumps(Evil())))
    with pytest.raises(pickle.UnpicklingError, match="not allowed"):
        savestate.load_file(m, f)


def test_slot_store_lives_in_the_game_folder(tmp_path):
    from src.core.adaptors import saves
    root = saves.set_save_dir(tmp_path / "saves")
    try:
        m = _machine(ROMS / "nes" / "scanline.nes")
        store = savestate.SlotStore(ROMS / "nes" / "scanline.nes", "nes")
        assert store.folder == root / "nes" / "scanline" / "states"
        assert store.latest() is None
        store.save(m, 2, thumbnail=b"x")
        entries = {e["slot"]: e for e in store.list()}
        assert entries[2]["time"] and entries[2]["thumb"] is not None and not entries[1]["time"]
        assert store.latest()["slot"] == 2
        store.load(m, 2)
        with pytest.raises(FileNotFoundError):
            store.load(m, 3)
        store.delete(2)
        assert store.latest() is None
    finally:
        saves.set_save_dir(None)


def test_snapshot_survives_core_replacement():
    """A snapshot taken before reset / a disk load still restores (full load)."""
    m = _machine(ROMS / "gba" / "stripes.gba")
    m.run_frames(30)
    snap = savestate.capture(m)
    ref = _play(m, 30)
    m.reset()                                   # GBA reset builds a new core
    m.core.cart.save_ram = lambda *a, **k: False
    m.apu.sampling = True                       # (the runner re-enables sound after a reset)
    savestate.restore(m, snap)
    assert _play(m, 30) == ref


# ------------------------------------------------------------------ rewind
def test_rewind_goes_back_in_time():
    m = _machine(ROMS / "nes" / "scanline.nes")
    rw = RewindBuffer(m.fps, interval=6, seconds=5)
    frames = []
    for i in range(120):
        m.run_frame()
        rw.tick(m)
        if i == 59:
            frames.append(m.frame_rgb())
    assert len(rw) == 20 and rw.seconds == pytest.approx(2.0)
    for _ in range(10):                         # 10 steps * 6 frames = back to frame 60
        assert rw.step_back(m)
    assert m.frame_rgb() == frames[0]


def test_rewind_is_bounded():
    m = _machine(ROMS / "other" / "dmg-acid2.gb")
    rw = RewindBuffer(m.fps, interval=1, seconds=0.1)       # ~6 snapshots max
    for _ in range(30):
        m.run_frame()
        rw.tick(m)
    assert len(rw) == rw.capacity <= 6


# ------------------------------------------------------------------ recorder
def test_recording_and_clip_make_gifs(tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image
    rec = Recorder(replay=True, replay_seconds=2)
    rec.start(0)
    done = []
    w, h = 256, 240
    for n in range(1, 181):                     # 3 s of game time, every frame different
        rgb = bytes([n % 256, (n * 7) % 256, 0]) * (w * h)
        rec.feed(rgb, w, h, n)
    assert rec.recorded_seconds == pytest.approx(3.0, abs=0.1)
    assert rec.stop(tmp_path / "rec.gif", scale=2, on_done=lambda p, e: done.append((p, e)))
    assert rec.clip(tmp_path / "clip.gif", seconds=1, on_done=lambda p, e: done.append((p, e)))
    t0 = time.time()
    while len(done) < 2 and time.time() - t0 < 30:
        time.sleep(0.05)
    assert [e for _, e in done] == [None, None]
    with Image.open(tmp_path / "rec.gif") as g:
        assert g.size == (512, 480) and g.n_frames >= 80          # ~30 fps x 3 s
    with Image.open(tmp_path / "clip.gif") as g:
        assert 25 <= g.n_frames <= 35                            # 1 s
    assert not rec.recording


def test_empty_recording_is_not_saved(tmp_path):
    rec = Recorder(replay=False)
    rec.start()
    assert rec.stop(tmp_path / "x.gif") is False
    assert rec.clip(tmp_path / "y.gif") is False
    with pytest.raises(ValueError):
        encode_gif([], (2, 2), tmp_path / "z.gif")


# ------------------------------------------------------------------ runner integration
def test_runner_submit_save_load_and_rewind(tmp_path):
    rom = ROMS / "nes" / "scanline.nes"
    if not rom.exists():
        pytest.skip("missing ROM")
    r = EmulatorRunner(lambda: load(rom), audio=False)
    r.start()
    try:
        assert r.ready.wait(10)
        time.sleep(1.0)                         # ~60 frames: rewind history builds up
        assert r.rewind is not None and len(r.rewind) > 0
        f = tmp_path / "s.state"
        r.submit(lambda m: savestate.save_file(m, f)).result(5)
        r.set_paused(True)
        assert r.submit(lambda m: m.system).result(5) == "NES"   # works while paused
        r.submit(lambda m: savestate.load_file(m, f)).result(5)
        r.set_paused(False)
        time.sleep(1.0)                         # loading replaced the core: history restarts
        frames_before = r.frames
        r.set_rewinding(True)
        time.sleep(0.4)
        assert r.rewinding
        r.set_rewinding(False)
        time.sleep(0.3)
        assert not r.rewinding and r.frames >= frames_before
        with pytest.raises(ZeroDivisionError):
            r.submit(lambda m: 1 / 0).result(5)
    finally:
        r.close()
    assert r.submit(lambda m: 1).exception(5) is not None      # stopped: fails, never hangs
