"""PS1 core (Rust, experimental) against real-hardware references.

The EXEs in tests/roms/ps1 come from JaCzekanski's ps1-tests (MIT, see the
LICENSE there). Each has the output of a real console: ``psx.log`` (the TTY
text) or ``vram.png`` (the frame buffer). Skipped when the core isn't built
(``uv run python -m src.core.ps1.build``).
"""
from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from src.core import ps1

ROMS = Path(__file__).parent / "roms" / "ps1"
needs_core = pytest.mark.skipif(not ps1.AVAILABLE, reason="PS1 core not built")


def test_core_built_when_required():
    """CI builds the core and sets PS1_CORE_REQUIRED, so a broken build fails instead of skipping."""
    if os.environ.get("PS1_CORE_REQUIRED") and not ps1.AVAILABLE:
        pytest.fail(f"PS1 core missing: {ps1.IMPORT_ERROR}")

LOG_TESTS = ["cop", "code-in-io", "dpcr", "otc-test", "gp0-e1", "mask-bit", "test-all", "memory-transfer"]
VRAM_TESTS = ["rectangles", "triangle", "transparency", "quad", "4bit", "8bit", "frame-15bit-dma"]
# these EXEs are newer builds than their psx.log: they run more sub-tests, so the log
# is checked as "every console line appears, in order"
NEWER_THAN_LOG = {"otc-test", "test-all", "memory-transfer"}


def _run(name: str, frames: int = 1500):
    m = ps1.Ps1(exe=(ROMS / name / f"{name}.exe").read_bytes())
    tty = bytearray()
    for _ in range(frames):
        m.run_frame()
        tty += m.take_tty()
        if b"Done" in tty:
            break
    for _ in range(4):               # let the last draw land
        m.run_frame()
    tty += m.take_tty()
    return m, tty.decode("latin-1")


def _lines(text: str) -> list[str]:
    out = []
    for line in text.replace("\r", "").split("\n"):
        line = line.removeprefix("% ").rstrip()
        if line and not line.startswith(("ResetGraph", "InitGeom")):
            out.append(line)
    return out


def test_exe_detection(tmp_path):
    assert ps1.is_exe(ROMS / "cop" / "cop.exe")
    fake = tmp_path / "setup.exe"
    fake.write_bytes(b"MZ\x90\x00" + bytes(100))     # a Windows program is not a PS1 game
    assert not ps1.is_exe(fake)


@needs_core
@pytest.mark.parametrize("name", LOG_TESTS)
def test_tty_matches_hardware(name):
    _, tty = _run(name)
    want = _lines((ROMS / name / "psx.log").read_text(encoding="latin-1"))
    got = _lines(tty)
    assert not [line for line in got if line.startswith("fail")]
    if name in NEWER_THAN_LOG:
        it = iter(got)
        assert all(line in it for line in want), (want, got)
    else:
        assert got == want


@needs_core
@pytest.mark.parametrize("name", VRAM_TESTS)
def test_vram_matches_hardware(name):
    from PIL import Image
    m, _ = _run(name, frames=400)
    ref = Image.open(ROMS / name / "vram.png").convert("RGB")
    px = struct.unpack("<524288H", m.vram())
    w, h = 320, 240                  # compare the drawn screen area
    exp = ref.crop((0, 0, w, h)).tobytes()
    # Gouraud colours (triangle, ~0.4% of pixels) and MDEC colour output (frame-15bit-dma,
    # ~20% of pixels) still round one 5-bit step off the console: allow 1 step (8) per
    # channel, nothing more. 4bit / 8bit (MDEC mono) and the other GPU tests are exact.
    bad = 0
    for y in range(h):
        for x in range(w):
            p = px[y * 1024 + x]
            got = ((p & 31) << 3, ((p >> 5) & 31) << 3, ((p >> 10) & 31) << 3)
            k = (y * w + x) * 3
            if any(abs(got[i] - exp[k + i]) > 8 for i in range(3)):
                bad += 1
    assert bad == 0, f"{bad} of {w * h} pixels differ from the console"


@needs_core
def test_machine_contract():
    from src.bridge import load
    m = load(ROMS / "gp0-e1" / "gp0-e1.exe")
    assert m.system == "PS1" and (m.width, m.height) == (320, 240)
    assert 59 < m.fps < 60.5
    m.apu.sampling = True
    for _ in range(3):
        m.run_frame()
    assert len(m.frame_rgb()) == 320 * 240 * 3
    assert b"gpu/gp0-e1" in bytes(m.tty)
    samples = m.apu.take_samples()
    assert samples.typecode == "h" and 2 * 735 * 3 - 20 < len(samples) < 2 * 735 * 3 + 20
    m.press("A")
    m.release("A")
    m.press_ps1("Triangle")
    m.reset()
    m.run_frame()


def test_bridge_rejects_non_exe(tmp_path):
    from src.bridge import load
    fake = tmp_path / "game.exe"
    fake.write_bytes(b"MZ" + bytes(4000))
    with pytest.raises(ValueError):
        load(fake)


@needs_core
def test_runs_in_the_real_runner(tmp_path):
    """The worker thread with default settings (rewind + replay on) on a PS1 game."""
    import time

    from src.bridge import load
    from src.bridge.runner import EmulatorRunner
    r = EmulatorRunner(lambda: load(ROMS / "triangle" / "triangle.exe"), audio=False)
    r.start()
    try:
        assert r.ready.wait(10)
        time.sleep(1.5)
        assert r.error is None, r.error
        assert r.frames > 30
        assert r.rewind is not None and len(r.rewind) > 0, "rewind history builds up"
        assert r.submit(lambda m: m.supports_states).result(5) is True
        r.press("A")
        r.release("A")
        r.set_paused(True)
        assert r.submit(lambda m: m.system).result(5) == "PS1"
        r.set_paused(False)
        r.reset()
        time.sleep(0.5)
        assert r.error is None, r.error
    finally:
        r.close()


def _fingerprint(m) -> str:
    import hashlib
    ps1 = m.core.ps1
    return hashlib.sha1(ps1.vram() + bytes(ps1.save_state()) + bytes(m.tty)).hexdigest()


@needs_core
def test_save_states_replay_exactly(tmp_path):
    """Snapshot, run on, go back, run the same frames again: identical machine, in memory and on disk."""
    from src.bridge import load, savestate
    m = load(ROMS / "triangle" / "triangle.exe")
    for _ in range(20):
        m.run_frame()
    snap = savestate.capture(m)
    f = tmp_path / "s.state"
    savestate.save_file(m, f)
    assert m.core.exe not in snap.data and len(snap.data) < 1_000_000   # EXE kept out, compressed
    m.tty.clear()
    for _ in range(15):
        m.run_frame()
    want = _fingerprint(m)
    savestate.restore(m, snap)                       # in place (rewind, this session's slots)
    m.tty.clear()
    for _ in range(15):
        m.run_frame()
    assert _fingerprint(m) == want
    savestate.load_file(m, f)                        # from disk, restricted unpickler
    m.tty.clear()
    for _ in range(15):
        m.run_frame()
    assert _fingerprint(m) == want


@needs_core
def test_state_from_another_game_is_refused(tmp_path):
    from src.bridge import load, savestate
    a = load(ROMS / "triangle" / "triangle.exe")
    a.run_frame()
    f = tmp_path / "a.state"
    savestate.save_file(a, f)
    b = load(ROMS / "quad" / "quad.exe")
    with pytest.raises(ValueError, match="different game"):
        savestate.load_file(b, f)


@needs_core
def test_audio_follows_a_loaded_state(tmp_path):
    from src.bridge import load, savestate
    m = load(ROMS / "triangle" / "triangle.exe")
    m.apu.sampling = True
    m.run_frame()
    f = tmp_path / "s.state"
    savestate.save_file(m, f)
    savestate.load_file(m, f)
    m.apu.take_samples()
    m.run_frame()
    assert m.apu.sampling and len(m.apu.take_samples()) > 1000     # the new core is the one heard


@needs_core
def test_gte_matches_hardware_fuzz_log():
    """1,100 GTE commands (22 opcodes x 50 random register sets) against a real console:
    every one of the 64 registers must read back as on hardware."""
    import re
    from src.core.ps1._ps1 import gte_exec
    text = (ROMS / "gte-fuzz" / "gte_valid_0xc0ffee_50.log").read_text()
    cases = re.findall(
        r"Test \d+\n((?:> r\[\d+\] = 0x[0-9a-f]+\n)+)GTE (0x[0-9a-f]+) (\w+) "
        r"\(sf=(\d), lm=(\d), tx=(\d), vx=(\d), mx=(\d)\)\n((?:< r\[\d+\] = 0x[0-9a-f]+\n)+)", text)
    assert len(cases) == 1100
    bad = []
    for ins, op, name, sf, lm, tx, vx, mx, outs in cases:
        regs = [int(x, 16) for x in re.findall(r"= (0x[0-9a-f]+)", ins)]
        want = [int(x, 16) for x in re.findall(r"= (0x[0-9a-f]+)", outs)]
        word = (int(op, 16) | int(sf) << 19 | int(lm) << 10 | int(tx) << 13 | int(vx) << 15 | int(mx) << 17)
        if gte_exec(regs, word) != want:
            bad.append(name)
    assert not bad, f"{len(bad)} GTE commands differ: {sorted(set(bad))}"


@needs_core
def test_spu_voices_play_and_pan():
    """spu/stereo: four ADPCM samples, keyed one at a time hard left / hard right every
    2 s, then two at once on opposite sides. No console recording exists, so check the
    layout of the sound: who is heard on which side, and when."""
    import array
    m = ps1.Ps1(exe=(ROMS / "stereo" / "stereo.exe").read_bytes())
    m.sampling = True
    a = array.array("h")
    for _ in range(530):
        m.run_frame()
        a.frombytes(bytes(m.take_samples()))
    tty = m.take_tty().decode("latin-1")
    assert tty.count("[SPU] Upload 18192 bytes") == 4

    def rms(t0, t1, ch):
        part = a[int(t0 * 44100) * 2 + ch:int(t1 * 44100) * 2:2]
        return (sum(x * x for x in part) / len(part)) ** 0.5

    # (start, end, left heard, right heard); the samples last ~1.2 s of each 2 s slot
    for t0, t1, left, right in [(0.1, 0.9, True, False), (2.1, 2.9, False, True), (4.1, 4.9, True, False),
                                (6.1, 6.9, False, True), (8.1, 8.6, True, True)]:
        rl, rr = rms(t0, t1, 0), rms(t0, t1, 1)
        assert (rl > 1000) == left and (rr > 1000) == right, (t0, rl, rr)
        assert (rl == 0) != left and (rr == 0) != right, (t0, rl, rr)
    assert rms(1.6, 1.9, 0) == rms(1.6, 1.9, 1) == 0          # the one-shot sample ended


@needs_core
def test_spu_decode_matches_an_independent_decoder():
    """The voice output follows the ADPCM data: decode the uploaded sample with a plain
    psx-spx decoder, resample it at the voice pitch and correlate with what the SPU played."""
    import array
    m = ps1.Ps1(exe=(ROMS / "stereo" / "stereo.exe").read_bytes())
    m.sampling = True
    a = array.array("h")
    for _ in range(110):
        m.run_frame()
        a.frombytes(bytes(m.take_samples()))
    ram = bytes(m.spu_ram())
    pos, neg = [0, 60, 115, 98, 122], [0, 0, -52, -55, -60]
    src, o1, o2, addr = [], 0, 0, 0x1000
    while True:
        sh, f, flags = ram[addr] & 15, ram[addr] >> 4, ram[addr + 1]
        for i in range(28):
            n = (ram[addr + 2 + i // 2] >> ((i & 1) * 4)) & 15
            s = ((n << 12) - 0x10000 if n & 8 else n << 12) >> sh
            s = max(-32768, min(32767, s + ((o1 * pos[f]) >> 6) + ((o2 * neg[f]) >> 6)))
            src.append(s)
            o2, o1 = o1, s
        addr += 16
        if flags & 1:
            break
    left = a[0::2]
    start = next(i for i, x in enumerate(left) if x)
    step = 2205 / 4096                    # the test's pitch register
    ref = []
    for k in range(20000):
        t = k * step
        i = int(t)
        ref.append(src[i] + (src[i + 1] - src[i]) * (t - i))

    def corr(x, y):
        n = len(x)
        mx, my = sum(x) / n, sum(y) / n
        sxy = sum((p - mx) * (q - my) for p, q in zip(x, y))
        return sxy / (sum((p - mx) ** 2 for p in x) * sum((q - my) ** 2 for q in y)) ** 0.5

    best = max(corr(ref, left[start + d:start + d + len(ref)]) for d in range(-4, 6) if start + d >= 0)
    assert best > 0.99, best
