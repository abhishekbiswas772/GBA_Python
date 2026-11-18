"""App shell: page setup, routing between screens, theme resolution."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

import flet as ft

from src.ui.data import library as lib
from src.ui.data.settings import Settings
from src.ui.emulator.session import EmulatorSession
from src.ui.state import UIState
from src.ui.theme import CONSOLE_THEME, FONTS, THEMES, Theme, family
from src.ui.components.layout import WIDE_BREAKPOINT

APP_NAME = "GBA Python"
VERSION = "0.1.0"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

# set by run() from the command line: session-only ROM folders / a ROM to open at start
LAUNCH: dict = {"rom": None, "dirs": []}

ROUTES = ("library", "player", "settings", "moments", "appearance", "together")
MOBILE = {ft.PagePlatform.ANDROID, ft.PagePlatform.IOS, ft.PagePlatform.ANDROID_TV}


class App:
    def __init__(self, page: ft.Page, settings: Settings | None = None):
        self.page = page
        self.settings = settings or Settings.load()
        self.ui = UIState()
        self.route = "library"
        self.session: EmulatorSession | None = None
        self.games: list[lib.Game] = []
        self._wide: bool | None = None
        self.ready = False          # renders wait until the splash has finished
        self.previews = self._preview_maker()
        self.rescan()

        page.on_resize = self._on_resize
        page.on_platform_brightness_change = lambda e: self.render()
        page.on_keyboard_event = self._on_page_key
        page.on_close = lambda e: self.close_session()

    # ------------------------------------------------------------------ environment
    @property
    def width(self) -> float:
        return self.page.width or 1280

    @property
    def height(self) -> float:
        return self.page.height or 800

    @property
    def wide(self) -> bool:
        return self.width >= WIDE_BREAKPOINT

    @property
    def is_mobile(self) -> bool:
        return self.page.platform in MOBILE

    @property
    def is_web(self) -> bool:
        return bool(self.page.web)

    @property
    def touch_controls(self) -> bool:
        return self.is_mobile or self.width < 700

    @property
    def t(self) -> Theme:
        s = self.settings
        key = s.theme
        if s.match_console and self.route == "player" and self.session:
            key = CONSOLE_THEME.get(self.session.game.system, key)
        elif s.follow_system and self.page.platform_brightness == ft.Brightness.LIGHT:
            key = "daylight"
        return THEMES.get(key, THEMES["daylight"])

    # ------------------------------------------------------------------ data
    def rescan(self) -> None:
        rom = LAUNCH.get("rom")
        self.games = lib.scan(self.settings, LAUNCH.get("dirs", ()), [rom] if rom else ())
        if self.previews is not None:
            self.previews.queue_games(self.games)      # screenshots for never-played games

    def _preview_maker(self):
        """Background library previews (desktop only: web / mobile are too slow)."""
        if self.is_web or self.is_mobile:
            return None
        from src.ui.emulator.previews import PreviewMaker
        from src.ui.theme import GB_PALETTES
        pal = GB_PALETTES.get(self.settings.gb_palette, GB_PALETTES["pea"]).rgb()

        def ready(path: str) -> None:                   # preview thread -> UI loop
            async def put() -> None:
                from src.ui.views.library import show_preview
                show_preview(self, path)
            try:
                self.page.run_task(put)
            except Exception:
                pass
        return PreviewMaker(on_ready=ready, palette=pal)

    def game_by_path(self, path: str) -> lib.Game | None:
        return next((g for g in self.games if g.path == path), None)

    # ------------------------------------------------------------------ routing
    def go(self, route: str) -> None:
        assert route in ROUTES, route
        playing = self.session.game.system if self.session else None
        if route != "player":
            self.close_session()
        if route == "settings" and self.ui.draft_keyboards is None:
            self.ui.draft_keyboards = {sysname: {b: list(k) for b, k in m.items()}
                                       for sysname, m in self.settings.keyboards.items()}
            self.ui.draft_hotkeys = dict(self.settings.hotkeys)
            if playing:                           # Controls opened from a game: show its system
                self.ui.settings_system = playing
        if route != "settings":
            self.ui.draft_keyboards = self.ui.draft_hotkeys = None
            self.ui.listening = None
        self.route = route
        self.render()

    def play(self, game: lib.Game) -> None:
        self.close_session()
        if self.previews is not None:
            self.previews.busy.set()                    # previews wait while you play
        self.session = EmulatorSession(self.page, game, self.settings)
        self.ui.toast = None
        self.ui.recording = False
        self.route = "player"
        self.render()
        self.session.start()

    def close_session(self) -> None:
        if self.session:
            self.session.close()
            self.session = None
            if self.previews is not None:
                self.previews.busy.clear()
            self.rescan()           # refresh play history
            if self.page.window and self.page.window.full_screen:
                self.page.window.full_screen = False

    def render(self) -> None:
        if not self.ready:          # e.g. a resize event while the splash is showing
            return
        from src.ui.views import VIEWS
        t = self.t
        self._wide = self.wide
        self._apply_theme(t)
        root = VIEWS[self.route](self)
        self.page.controls.clear()
        self.page.controls.append(ft.SafeArea(root, expand=True))
        self.page.update()

    def _apply_theme(self, t: Theme) -> None:
        page = self.page
        page.bgcolor = t.bg
        mode = ft.ThemeMode.LIGHT if t.key == "daylight" else ft.ThemeMode.DARK
        scheme = ft.ColorScheme(
            primary=t.accent, on_primary=t.on, secondary=t.accent, surface=t.card, on_surface=t.text,
            on_surface_variant=t.muted, outline=t.line2, outline_variant=t.line,
        )
        theme = ft.Theme(font_family=family(), color_scheme=scheme)
        page.theme = theme
        page.dark_theme = theme
        page.theme_mode = mode

    def _on_resize(self, e) -> None:
        # Re-layout: grids, integer scaling and the wide/narrow switch depend on size.
        self.render()

    def _on_page_key(self, e: ft.KeyboardEvent) -> None:
        if self.route == "library" and e.ctrl and e.key.upper() == "K":
            from src.ui.views.library import focus_search
            self.page.run_task(focus_search, self)

    # ------------------------------------------------------------------ helpers
    def toast(self, message: str) -> None:
        t = self.t
        self.page.show_dialog(ft.SnackBar(
            ft.Text(message, color=t.bg, font_family=family()), bgcolor=t.text,
            behavior=ft.SnackBarBehavior.FLOATING, width=min(480, self.width - 32),
        ))

    def not_ready(self, feature: str) -> None:
        self.toast(f"{feature} isn't supported by the emulator core yet.")

    def open_folder(self, path: Path) -> None:
        if self.is_mobile or self.is_web:
            self.toast(f"Saved in {path}")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as err:
            self.toast(f"Couldn't open folder: {err}")


SPLASH_MIN_SECONDS = 1.2


async def main(page: ft.Page) -> None:
    from src.ui.views import splash

    page.title = APP_NAME
    page.padding = 0
    page.spacing = 0
    page.fonts = FONTS
    if page.platform not in MOBILE and not page.web and page.window:
        page.window.width = 1440
        page.window.height = 900
        page.window.min_width = 380
        page.window.min_height = 600
        page.window.icon = str(ASSETS_DIR / ("icon.ico" if sys.platform.startswith("win") else "icon.png"))

    # Splash while settings load and ROM folders are scanned (can take a moment on big libraries).
    t0 = time.perf_counter()
    settings = Settings.load()
    t = THEMES.get(settings.theme, THEMES["daylight"])
    loader = splash.RetroLoader(t)
    page.bgcolor = t.bg
    page.theme = page.dark_theme = ft.Theme(font_family=family())
    page.theme_mode = ft.ThemeMode.LIGHT if t.key == "daylight" else ft.ThemeMode.DARK
    page.controls.append(ft.SafeArea(splash.build(t, APP_NAME, VERSION, loader), expand=True))
    page.update()
    page.run_task(loader.run)

    app = await asyncio.to_thread(App, page, settings)
    await asyncio.sleep(max(0.0, SPLASH_MIN_SECONDS - (time.perf_counter() - t0)))
    await loader.finish(f"{len(app.games)} ROM{'s' if len(app.games) != 1 else ''} found")
    await asyncio.sleep(0.45)
    loader.stop()
    app.ready = True
    app.render()
    rom = LAUNCH.get("rom")
    if rom:
        game = app.game_by_path(str(Path(rom).resolve()))
        if game is not None and game.playable:
            app.play(game)


def run(rom: str | None = None, rom_dirs=()) -> None:
    """Start the desktop app. `rom` opens straight into that game; `rom_dirs` are
    extra library folders for this session only."""
    LAUNCH["rom"] = str(Path(rom).resolve()) if rom else None
    LAUNCH["dirs"] = [str(Path(d).expanduser()) for d in rom_dirs]
    ft.run(main, assets_dir=str(ASSETS_DIR))
