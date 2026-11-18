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
    if isinstance(node, ft.GestureDetector):          # checked by the Flutter client, not validate()
        assert any(getattr(node, f.name) for f in dataclasses.fields(node) if f.name.startswith("on_")), \
            "GestureDetector should have at least one event handler"
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
        dialogs=[], show_dialog=None, pop_dialog=None,
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


def test_player_with_states_and_recording_validates(app, tmp_path, monkeypatch):
    """Saves panel with a filled slot + resume point, recording in progress, and a GIF
    in Moments: all must pass Flet's validation."""
    from src.bridge import load
    from src.core.adaptors import saves
    from src.ui.data.moments import new_path
    from src.ui.emulator import frame as fr
    from src.ui.emulator import session as sess
    from src.ui.views import VIEWS
    game = next((g for g in app.games if g.system == "NES"), None)
    if game is None:
        pytest.skip("no NES test ROM")
    saves.set_save_dir(tmp_path / "saves")
    m = load(game.path)
    png = fr.encode_png(m.frame_rgb(), m.width, m.height)
    monkeypatch.setattr(sess.EmulatorSession, "start", lambda self: None)
    s = sess.EmulatorSession(app.page, game, app.settings)
    for slot in (0, 1):
        s.slots.save(m, slot, thumbnail=png)
    s.runner.machine = m                       # as if the worker had loaded it
    s.runner.recorder.start(0)
    app.session, app.route = s, "player"
    try:
        _walk(VIEWS["player"](app), set())
    finally:
        app.session = None
    rec = s.runner.recorder
    rec.feed(m.frame_rgb(), m.width, m.height, 2)
    done = []
    rec.stop(new_path(game.title, "NES", ".gif"), on_done=lambda p, e: done.append(e))
    import time
    t0 = time.time()
    while not done and time.time() - t0 < 20:
        time.sleep(0.05)
    assert done == [None]
    app.route = "moments"
    _walk(VIEWS["moments"](app), set())


def test_leaving_a_game_pauses_and_asks_to_save(app, monkeypatch):
    from src.bridge import load
    from src.ui.emulator import session as sess
    game = next((g for g in app.games if g.system == "NES"), None)
    if game is None:
        pytest.skip("no NES test ROM")
    page = app.page
    page.show_dialog = page.dialogs.append
    page.pop_dialog = lambda: page.dialogs.pop() if page.dialogs else None
    monkeypatch.setattr(sess.EmulatorSession, "start", lambda self: None)
    s = sess.EmulatorSession(page, game, app.settings)
    s.runner.machine = load(game.path)
    app.session, app.route = s, "player"
    app.go("library")
    assert app.route == "player" and app.session is s          # not left yet
    assert s.paused                                           # game paused behind the dialog
    (dialog,) = page.dialogs
    _walk(dialog, set())
    labels = [a.content.value for a in dialog.actions]
    assert labels == ["Cancel", "Leave without saving", "Save and leave"]
    dialog.actions[0].on_click(None)                         # Cancel: back to the game, running
    assert not page.dialogs and not s.paused and app.session is s
    app.go("library")
    page.dialogs[-1].actions[1].on_click(None)               # Leave without saving
    assert app.route == "library" and app.session is None
