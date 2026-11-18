"""Every screen builds a control tree that passes Flet's own validation.

Flet only validates when a page is sent to the client, so a bad property
(e.g. ``opacity=None``) used to show up as a crash the moment you pressed Play.
Here each view is built on a stub page and validated directly.
"""
import dataclasses
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

ft = pytest.importorskip("flet")
from flet.utils.validation import validate  # noqa: E402

ROMS = Path(__file__).parent / "roms"
GAMES = {
    "GB": ROMS / "other" / "dmg-acid2.gb",
    "GBC": ROMS / "other" / "cgb-acid2.gbc",
    "GBA": ROMS / "gba" / "hello.gba",
    "NES": ROMS / "nes" / "scanline.nes",
}


def _walk(node, seen):
    if id(node) in seen:
        return
    seen.add(id(node))
    if isinstance(node, (list, tuple)):
        for x in node:
            _walk(x, seen)
        return
    if not dataclasses.is_dataclass(node) or isinstance(node, type):
        return
    if isinstance(node, ft.BaseControl):
        validate(node)
    for f in dataclasses.fields(node):
        if f.name.startswith("_") or f.name in ("parent", "page"):
            continue
        try:
            v = getattr(node, f.name)
        except Exception:
            continue
        if isinstance(v, (list, tuple)) or dataclasses.is_dataclass(v):
            _walk(v, seen)


def _page():
    return SimpleNamespace(
        controls=[], update=lambda *a, **k: None, window=None, platform=None, web=False,
        width=1440, height=900, run_task=lambda *a, **k: None, bgcolor=None, theme=None,
        dark_theme=None, theme_mode=None, fonts=None, title="", padding=0, spacing=0,
        overlay=[], services=[], on_resize=None, on_platform_brightness_change=None,
        on_keyboard_event=None, on_close=None, platform_brightness=None,
    )


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path / "data"))
    from src.ui import app as appmod
    from src.ui.data.settings import Settings
    monkeypatch.setattr(appmod.App, "_preview_maker", lambda self: None)
    roms = tmp_path / "roms"
    roms.mkdir()
    for system, src in GAMES.items():
        if src.exists():
            shutil.copy(src, roms / f"{system} test{src.suffix}")
    s = Settings()
    s.rom_dirs = [str(roms)]
    a = appmod.App(_page(), s)
    a.ready = True
    return a


@pytest.mark.parametrize("route", ["library", "settings", "appearance", "moments", "together"])
def test_screens_validate(app, route):
    from src.ui.views import VIEWS
    app.route = route
    _walk(VIEWS[route](app), set())


@pytest.mark.parametrize("system", list(GAMES))
def test_player_validates(app, system, monkeypatch):
    from src.ui.emulator import session as sess
    from src.ui.views import VIEWS
    game = next((g for g in app.games if g.system == system), None)
    if game is None:
        pytest.skip(f"no {system} test ROM")
    monkeypatch.setattr(sess.EmulatorSession, "start", lambda self: None)   # no worker thread
    app.session = sess.EmulatorSession(app.page, game, app.settings)
    app.route = "player"
    try:
        _walk(VIEWS["player"](app), set())
    finally:
        app.session = None
