"""Library previews: headless screenshots that never touch battery saves."""
import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("flet")

from src.ui.emulator import previews  # noqa: E402

ROMS = Path(__file__).parent / "roms"
PNG = b"\x89PNG\r\n\x1a\n"


@pytest.mark.parametrize("rom", [ROMS / "gba" / "stripes.gba", ROMS / "other" / "cgb-acid2.gbc",
                                 ROMS / "other" / "dmg-acid2.gb"], ids=lambda p: p.name)
def test_preview_is_png(rom, monkeypatch):
    if not rom.is_file():
        pytest.skip(f"missing {rom.name}")
    monkeypatch.setattr(previews, "CHECKPOINTS", (30, 60))      # keep the test quick
    png = previews.make_preview(str(rom))
    assert png and png.startswith(PNG)


def test_preview_never_writes_the_save(tmp_path, monkeypatch):
    src = ROMS / "gba" / "flash64.gba"
    if not src.is_file():
        pytest.skip("missing flash64.gba")
    rom = tmp_path / "game.gba"
    shutil.copy(src, rom)
    sav = tmp_path / "game.sav"
    sav.write_bytes(b"\xAB" * 65536)
    before = sav.stat().st_mtime_ns
    monkeypatch.setattr(previews, "CHECKPOINTS", (60, 120))
    previews.make_preview(str(rom))
    assert sav.read_bytes() == b"\xAB" * 65536
    assert sav.stat().st_mtime_ns == before
    assert not list(tmp_path.glob("*.tmp"))


def test_maker_writes_thumbnail_and_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    monkeypatch.setattr(previews, "CHECKPOINTS", (30,))
    from src.ui.data.library import Game
    rom = ROMS / "other" / "dmg-acid2.gb"
    if not rom.is_file():
        pytest.skip("missing dmg-acid2.gb")
    done = []
    maker = previews.PreviewMaker(on_ready=done.append)
    g = Game(str(rom), "acid", "GB", rom.stat().st_size, 0, 0)
    maker.queue_games([g])
    end = time.time() + 20
    while not done and time.time() < end:
        time.sleep(0.05)
    assert done == [str(rom)]
    assert previews.thumb_path(str(rom)).read_bytes().startswith(PNG)
