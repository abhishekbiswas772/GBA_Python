"""main.py launcher: argument routing and the ./rom folder."""
import pytest

import main


@pytest.fixture
def calls(monkeypatch):
    seen = []
    for name in ("cmd_app", "cmd_play", "cmd_run", "cmd_info"):
        monkeypatch.setattr(main, name, lambda args, _n=name: seen.append((_n, args)))
    # main() dispatches through a dict built at call time from module globals
    return seen


def test_no_arguments_opens_desktop_app(calls):
    main.main([])
    assert calls[0][0] == "cmd_app" and calls[0][1].rom is None


def test_bare_rom_means_play(calls):
    main.main(["game.gba", "--mute", "--scale", "4"])
    name, args = calls[0]
    assert name == "cmd_play" and args.rom == "game.gba" and args.mute and args.scale == 4


@pytest.mark.parametrize("argv,cmd", [
    (["app", "x.gba"], "cmd_app"), (["play", "x.gb"], "cmd_play"),
    (["run", "x.gb", "--frames", "5"], "cmd_run"), (["info", "x.gbc"], "cmd_info"),
])
def test_subcommands(calls, argv, cmd):
    main.main(argv)
    assert calls[0][0] == cmd


def test_rom_folder_created_with_subfolders(tmp_path, monkeypatch):
    root = tmp_path / "rom"
    monkeypatch.setattr(main, "ROM_DIR", root)
    assert main.ensure_rom_dir() == root
    assert sorted(p.name for p in root.iterdir()) == ["gb", "gb_color", "gba", "nes"]
    main.ensure_rom_dir()                     # idempotent


def test_rom_paths_resolve_inside_rom_folder(tmp_path, monkeypatch):
    root = tmp_path / "rom"
    (root / "gba").mkdir(parents=True)
    (root / "gba" / "game.gba").write_bytes(b"\x00" * 0x200)
    monkeypatch.setattr(main, "ROM_DIR", root)
    assert main._check_rom("gba/game.gba") == str(root / "gba" / "game.gba")
    with pytest.raises(SystemExit):
        main._check_rom("missing.gba")


def test_info_and_headless_run(tmp_path, capsys, monkeypatch):
    rom = tmp_path / "t.gba"
    rom.write_bytes(b"\x00" * 0x400)
    main.main(["info", str(rom)])
    assert "[GBA]" in capsys.readouterr().out
    main.main(["run", str(rom), "--frames", "3"])
    assert "3 frames" in capsys.readouterr().out
