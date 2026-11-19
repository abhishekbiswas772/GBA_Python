"""Save folder: saves/<gb|gbc|gba|nes>/<game>/<game>.sav, created on demand, old saves copied."""
from pathlib import Path

import pytest

from src.core.adaptors import saves
from src.core.gameboy.cartridge import Cartridge as GBCart
from src.core.nes.cartridge import Cartridge as NESCart


@pytest.fixture
def save_root(tmp_path):
    root = saves.set_save_dir(tmp_path / "saves")
    yield root
    saves.set_save_dir(None)                 # back to "next to the ROM" for other tests


def _gb_rom(path: Path, cgb: bool) -> Path:
    r = bytearray(0x8000)
    r[0x143] = 0x80 if cgb else 0x00
    r[0x147] = 0x03                          # MBC1 + RAM + battery
    r[0x149] = 0x02                          # 8 KB RAM
    path.write_bytes(r)
    return path


def _nes_rom(path: Path) -> Path:
    path.write_bytes(b"NES\x1a" + bytes([1, 1, 0x02, 0]) + bytes(8) + bytes(0x4000 + 0x2000))
    return path


def test_console_folders_created(save_root):
    assert sorted(p.name for p in save_root.iterdir()) == ["gb", "gba", "gbc", "nes", "ps1"]


def test_default_is_next_to_rom(tmp_path):
    saves.set_save_dir(None)
    assert saves.sav_path_for(tmp_path / "game.nes", "nes") == tmp_path / "game.sav"


def test_gb_and_gbc_get_their_own_game_folders(save_root, tmp_path):
    for name, cgb, core in (("Mono.gb", False, "gb"), ("Colour.gbc", True, "gbc")):
        cart = GBCart.from_file(_gb_rom(tmp_path / name, cgb))
        stem = Path(name).stem
        assert cart.sav_path == save_root / core / stem / f"{stem}.sav"
        assert not cart.sav_path.parent.exists()          # folder only when it saves
        cart.ram[0] = 7
        cart.ram_dirty = True
        assert cart.save_ram()
        assert cart.sav_path.read_bytes()[0] == 7


def test_old_save_is_copied_not_moved(save_root, tmp_path):
    rom = _nes_rom(tmp_path / "Quest.nes")
    old = tmp_path / "Quest.sav"
    old.write_bytes(b"\x42" * 0x2000)
    cart = NESCart.from_file(rom)
    assert cart.sav_path == save_root / "nes" / "Quest" / "Quest.sav"
    assert cart.prg_ram[0] == 0x42                         # progress carried over
    cart.prg_ram[0] = 0x99
    cart.ram_dirty = True
    cart.save_ram()
    assert old.read_bytes()[0] == 0x42                     # original untouched
    assert cart.sav_path.read_bytes()[0] == 0x99


def test_unwritable_save_dir_falls_back(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("not a folder")
    assert saves.set_save_dir(blocker / "saves") is None
    assert saves.sav_path_for(tmp_path / "g.gba", "gba") == tmp_path / "g.sav"
