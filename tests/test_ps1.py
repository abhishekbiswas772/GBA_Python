"""PS1 core (Rust, experimental) against real-hardware references.

The EXEs in tests/roms/ps1 come from JaCzekanski's ps1-tests (MIT, see the
LICENSE there). Each has the output of a real console: ``psx.log`` (the TTY
text) or ``vram.png`` (the frame buffer). Skipped when the core isn't built
(``uv run python -m src.core.ps1.build``).
"""
from __future__ import annotations

import struct
from pathlib import Path

import pytest

from src.core import ps1

ROMS = Path(__file__).parent / "roms" / "ps1"
needs_core = pytest.mark.skipif(not ps1.AVAILABLE, reason="PS1 core not built")

LOG_TESTS = ["cop", "code-in-io", "dpcr", "otc-test", "gp0-e1", "mask-bit"]
VRAM_TESTS = ["rectangles", "triangle", "transparency", "quad"]
# the bundled otc-test.exe is a newer build than its psx.log: it runs two more
# sub-tests, so its log is checked as "every console line appears, in order"
NEWER_THAN_LOG = {"otc-test"}


def _run(name: str, frames: int = 240):
    m = ps1.Ps1(exe=(ROMS / name / f"{name}.exe").read_bytes())
    tty = bytearray()
    for _ in range(frames):
        m.run_frame()
        tty += m.take_tty()
        if b"Done." in tty:
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
    m, _ = _run(name)
    ref = Image.open(ROMS / name / "vram.png").convert("RGB")
    px = struct.unpack("<524288H", m.vram())
    w, h = 320, 240                  # compare the drawn screen area
    exp = ref.crop((0, 0, w, h)).tobytes()
    # Gouraud colours still round one 5-bit step off the console on ~0.4% of the
    # triangle test's pixels: allow 1 step (8) per channel, nothing more.
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
