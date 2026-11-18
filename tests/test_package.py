"""Packaging settings the release workflow ships with."""
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUNDLE_ID = "com.abhishekbiswas.gameboy_python"


def test_bundle_id_and_flet_entry():
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    flet = cfg["tool"]["flet"]
    assert flet["org"] == "com.abhishekbiswas"
    assert flet["bundle_id"] == BUNDLE_ID
    assert flet["app"]["module"] == "main.py"
    assert (ROOT / "main.py").is_file()
    for name in ("icon.png", "splash.png", "favicon.png"):
        assert (ROOT / "assets" / name).is_file(), name
    assert cfg["tool"]["flet"]["linux"]["bundle_id"] == BUNDLE_ID
    for platform in ("windows", "macos", "linux"):
        deps = cfg["tool"]["flet"][platform]["dependencies"]
        assert any(item.startswith("pygame") for item in deps)


def test_open_source_test_roms_are_present():
    assert len(list((ROOT / "tests" / "roms" / "cpu_instrs").glob("*.gb"))) == 11
    other = ROOT / "tests" / "roms" / "other"
    for name in ("dmg-acid2.gb", "cgb-acid2.gbc", "instr_timing.gb", "cpu_instrs.gb"):
        assert (other / name).is_file(), name
    for name in ("arm.gba", "thumb.gba", "memory.gba", "bios.gba", "nes.gba"):
        assert (ROOT / "tests" / "roms" / "gba" / name).is_file(), name
