"""Library: console filters, continue-playing hero, recent moments and the game grid."""
from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from src.ui import components as c
from src.ui.data import library as lib
from src.ui.data.moments import list_moments, new_count
from src.ui.emulator.session import thumb_path
from src.ui.theme import SYSTEM_NAMES, SYSTEM_RES, THEME_ORDER, THEMES, family

if TYPE_CHECKING:
    from src.ui.app import App

_search_ref: ft.Ref[ft.TextField] = ft.Ref()

FILTERS = [("all", "All games"), ("GB", "Game Boy"), ("GBC", "Game Boy Color"), ("GBA", "Game Boy Advance"),
           ("NES", "NES"), ("PS1", "PlayStation")]
HEADINGS = {**dict(FILTERS), "fav": "Favorites"}
SORTS = [("recent", "Last played"), ("title", "Title A–Z"), ("system", "System")]


async def focus_search(app: "App") -> None:
    if _search_ref.current:
        await _search_ref.current.focus()


def build(app: "App") -> ft.Control:
    t, ui, wide = app.t, app.ui, app.wide

    def set_filter(f: str) -> None:
        ui.lib_filter = f
        app.render()

    def cycle_theme(e) -> None:
        keys = THEME_ORDER
        app.settings.theme = keys[(keys.index(app.settings.theme) + 1) % len(keys)]
        app.settings.save()
        app.render()

    async def add_roms(e) -> None:
        if app.is_web:
            app.toast("Adding ROMs needs the desktop or mobile app.")
            return
        files = await ft.FilePicker().pick_files(
            dialog_title="Add ROMs", allow_multiple=True, file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["gb", "gbc", "cgb", "gba", "agb", "nes", "exe", "psx"])
        paths = [f.path for f in files or [] if f.path]
        if not paths:
            return
        for p in paths:
            if p not in app.settings.extra_roms:
                app.settings.extra_roms.append(p)
        app.settings.save()
        app.rescan()
        app.toast(f"Added {len(paths)} ROM{'s' if len(paths) != 1 else ''}")
        app.render()

    def toggle_fav(g: lib.Game) -> None:
        favs = app.settings.favorites
        favs.remove(g.path) if g.path in favs else favs.append(g.path)
        app.settings.save()
        app.render()

    # ---------------------------------------------------------------- sidebar
    def count(f: str) -> str:
        if f == "all":
            return str(len(app.games))
        return str(sum(1 for g in app.games if g.system == f))

    dot_color = {"all": t.text, "GB": t.gb, "GBC": t.gbc, "GBA": t.gba, "NES": t.nes, "PS1": t.ps1}
    nav = [c.eyebrow("Library", t)] + [
        c.nav_item(t, label, (lambda e, k=key: set_filter(k)), lead=c.dot(dot_color[key], 8, 2),
                   trailing=c.mono(count(key), t), selected=ui.lib_filter == key)
        for key, label in FILTERS
    ]
    fresh = new_count()
    more = [
        c.eyebrow("More", t),
        c.nav_item(t, "Moments", lambda e: app.go("moments"), icon_name=ft.Icons.VIDEOCAM_OUTLINED,
                   trailing=c.pill(f"{fresh} new", t.soft, t.accent, size=11, weight=400, radius=4,
                                   padding=c.pad(6, 2)) if fresh else None),
        c.nav_item(t, "Achievements & link play", lambda e: app.go("together"),
                   icon_name=ft.Icons.EMOJI_EVENTS_OUTLINED),
        c.nav_item(t, "Favorites", lambda e: set_filter("fav"), icon_name=ft.Icons.STAR_OUTLINE,
                   selected=ui.lib_filter == "fav",
                   trailing=c.mono(str(len(app.settings.favorites)), t) if app.settings.favorites else None),
        c.nav_item(t, "Save states", lambda e: app.open_folder(app.settings.apply_save_dir() or app.settings.save_folder()),
                   icon_name=ft.Icons.SAVE_OUTLINED),
    ]
    bottom = [
        c.nav_item(t, "Themes & screen", lambda e: app.go("appearance"), icon_name=ft.Icons.PALETTE_OUTLINED),
        c.nav_item(t, "Settings", lambda e: app.go("settings"), icon_name=ft.Icons.SETTINGS_OUTLINED),
        ft.Container(c.mono(f"v{app_version()} · GB/GBC core", t, size=11), padding=10),
    ]
    logo = ft.Row([
        ft.Container(c.icon(ft.Icons.SPORTS_ESPORTS_OUTLINED, t.on, 18), width=32, height=32,
                     border_radius=8, bgcolor=t.accent, alignment=ft.Alignment.CENTER),
        c.text(app_name(), size=18, weight=700, kind="display", color=t.text, spacing=-0.18),
    ], spacing=10)

    if wide:
        side = c.sidebar(t, [
            ft.Container(logo, padding=c.pad(8, 0)),
            ft.Column(nav, spacing=4),
            ft.Column(more, spacing=4),
            c.spacer(),
            ft.Column(bottom, spacing=4),
        ], wide=True, fits=app.height >= 760)
    else:
        chips = ft.Row([c.segmented(FILTERS, ui.lib_filter, set_filter, t, item_pad=12)],
                       scroll=ft.ScrollMode.AUTO)
        links = ft.Row([
            c.icon_button(ft.Icons.VIDEOCAM_OUTLINED, lambda e: app.go("moments"), t, "Moments"),
            c.icon_button(ft.Icons.EMOJI_EVENTS_OUTLINED, lambda e: app.go("together"), t, "Achievements & link play"),
            c.icon_button(ft.Icons.PALETTE_OUTLINED, lambda e: app.go("appearance"), t, "Themes & screen"),
            c.icon_button(ft.Icons.SETTINGS_OUTLINED, lambda e: app.go("settings"), t, "Settings"),
        ], spacing=0, tight=True)
        side = c.sidebar(t, [ft.Row([ft.Container(logo, expand=True), links]), chips], wide=False, spacing=14)

    # ---------------------------------------------------------------- header
    search_field = ft.TextField(
        ref=_search_ref, value=ui.lib_search, hint_text="Search games",
        border=ft.InputBorder.NONE, dense=True, expand=True, content_padding=0,
        text_style=ft.TextStyle(size=14, color=t.text, font_family=family()),
        hint_style=ft.TextStyle(size=14, color=t.muted, font_family=family()),
        cursor_color=t.accent,
    )
    search = ft.Container(
        ft.Row([
            c.icon(ft.Icons.SEARCH, t.muted, 16),
            search_field,
            *([ft.Container(c.mono("Ctrl K", t, size=11), border=c.border(t.line2), border_radius=4,
                            padding=c.pad(6, 2))] if not app.is_mobile else []),
        ], spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        height=44, width=280 if wide else None, expand=None if wide else True,
        padding=c.pad(14, 0), bgcolor=t.card,
        border=c.border(t.line2), border_radius=10,
    )
    nxt = THEMES[THEME_ORDER[(THEME_ORDER.index(app.settings.theme) + 1) % len(THEME_ORDER)]]
    theme_btn = c.button(
        [ft.Row([c.dot(t.accent, 10, 3), c.dot(t.card2, 10, 3)], spacing=3, tight=True),
         c.text(THEMES[app.settings.theme].name, color=t.text, no_wrap=True)],
        cycle_theme, t, h_pad=14, tooltip=f"Theme: {THEMES[app.settings.theme].name}. Switch to {nxt.name}",
    )
    add_btn = c.button("Add ROMs", add_roms, t, variant="primary", icon_name=ft.Icons.ADD)

    games = _visible_games(app)
    folder = app.settings.rom_dirs[0] if app.settings.rom_dirs else "~"
    folder = folder.replace(str(_home()), "~")
    count_text = c.text(f"{len(games)} ROM{'s' if len(games) != 1 else ''} · {folder}", size=13, color=t.muted)
    title_block = ft.Column([c.heading(HEADINGS[ui.lib_filter], t, 28), count_text], spacing=4, tight=True)
    if wide:
        header = c.bar(title_block, [search, theme_btn, add_btn], wide=True, spacing=12)
    else:
        header = ft.Column([title_block, ft.Row([search]), ft.Row([theme_btn, add_btn], spacing=10, wrap=True)],
                           spacing=12)

    # ---------------------------------------------------------------- hero row
    main_w = app.width - (c.SIDEBAR_W if wide else 0) - (80 if wide else 32)
    hero = _continue_card(app, add_roms, main_w)
    moments = _moments_card(app)
    if main_w >= 900:
        top = ft.Row([ft.Container(hero, expand=3), ft.Container(moments, expand=1)], spacing=20,
                     vertical_alignment=ft.CrossAxisAlignment.START)
    else:
        top = ft.Column([hero, moments], spacing=20)

    # ---------------------------------------------------------------- grid
    def grid_content() -> ft.Control:
        favs = set(app.settings.favorites)
        shown = _visible_games(app)
        cell = c.cell_width(main_w, 168, 20)
        COVERS.clear()
        cards = [_game_card(app, g, g.path in favs, toggle_fav, cell) for g in shown]
        if cards:
            return c.auto_grid(cards, main_w, 168, gap=20)
        if app.games:
            return c.empty_state(t, "No matches", "Try another search or console filter.")
        return ft.Container()

    def on_search(value: str) -> None:
        ui.lib_search = value
        n = len(_visible_games(app))
        grid_box.content = grid_content()
        count_text.value = f"{n} ROM{'s' if n != 1 else ''} · {folder}"
        grid_box.update()
        count_text.update()

    search_field.on_change = lambda e: on_search(e.control.value)
    sort = c.dropdown(ui.lib_sort, SORTS, lambda v: (setattr(ui, "lib_sort", v), app.render()), t, width=170)
    grid_head = ft.Row([c.heading(HEADINGS[ui.lib_filter], t, 18), ft.Container(expand=True),
                        c.text("Sort", size=13, color=t.muted), sort], spacing=8,
                       vertical_alignment=ft.CrossAxisAlignment.CENTER)
    grid_box = ft.Container(grid_content())

    main = ft.Container(
        ft.Column([header, top, ft.Column([grid_head, grid_box], spacing=16)], spacing=32,
                  scroll=ft.ScrollMode.AUTO, expand=True),
        padding=ft.Padding.only(left=40, right=40, top=28, bottom=48) if wide else c.pad(16, 20),
        expand=True,
    )
    body = ft.Row([side, main], spacing=0, expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH) \
        if wide else ft.Column([side, main], spacing=0, expand=True)
    return ft.Container(body, bgcolor=t.bg, expand=True)


# -------------------------------------------------------------------- pieces
def _visible_games(app: "App") -> list[lib.Game]:
    ui = app.ui
    games = app.games
    if ui.lib_filter == "fav":
        favs = set(app.settings.favorites)
        games = [g for g in games if g.path in favs]
    elif ui.lib_filter != "all":
        games = [g for g in games if g.system == ui.lib_filter]
    q = ui.lib_search.strip().lower()
    if q:
        games = [g for g in games if q in g.title.lower() or q in g.filename.lower()]
    return lib.sort_games(games, ui.lib_sort)


def _continue_card(app: "App", add_roms, main_w: float) -> ft.Control:
    t = app.t
    recent = sorted((g for g in app.games if g.last_played), key=lambda g: -g.last_played)
    if not app.games:
        return c.empty_state(
            t, "No ROMs yet", "Add .gb, .gbc or .gba files, or drop them into "
            f"{app.settings.rom_dirs[0] if app.settings.rom_dirs else 'your ROM folder'}.",
            [c.button("Add ROMs", add_roms, t, variant="primary", icon_name=ft.Icons.ADD)],
            icon_name=ft.Icons.SPORTS_ESPORTS_OUTLINED,
        )
    g = recent[0] if recent else lib.sort_games(app.games, "title")[0]
    thumb = thumb_path(g.path)
    shot = thumb.read_bytes() if thumb.exists() else None
    w, h = lib.NATIVE_SIZE.get(g.system, (160, 144))
    screen = c.screen_placeholder(t, f"Last frame · {w}×{h}" if recent else f"{w}×{h}",
                                  aspect=w / h, image=shot, width=300 if app.width >= 760 else min(main_w - 44, 480))
    meta = [lib.played(g.seconds) if g.seconds else "Not played yet", lib.ago(g.last_played), g.filename]
    info = ft.Column([
        c.eyebrow("Continue playing" if recent else "Start playing", t),
        ft.Row([c.heading(g.title, t, 26), c.system_badge(g.system, t)], spacing=12, wrap=True),
        ft.Row([c.text(m, size=13, color=t.muted) for m in meta], spacing=18, wrap=True, run_spacing=4),
        ft.Row([
            c.button([c.icon(ft.Icons.PLAY_ARROW, t.bg, 16), c.text("Resume" if recent else "Play", weight=600, color=t.bg)],
                     lambda e: app.play(g), t, variant="inverse", h_pad=20),
            _load_state_button(app, g),
        ], spacing=10, wrap=True),
    ], spacing=12, tight=True)
    content = ft.Row([screen, ft.Container(info, expand=True)], spacing=24,
                     vertical_alignment=ft.CrossAxisAlignment.CENTER) \
        if app.width >= 760 else ft.Column([screen, info], spacing=18)
    return c.card(content, t)


def _moments_card(app: "App") -> ft.Control:
    t = app.t
    items = list_moments()[:3]
    rows: list[ft.Control] = [ft.Row([
        c.heading("Recent moments", t, 16), ft.Container(expand=True),
        c.button(c.text("See all", size=13, color=t.accent), lambda e: app.go("moments"), t,
                 variant="ghost", height=32, h_pad=6),
    ])]
    if not items:
        rows.append(c.text("No moments yet. Press F12 in a game to take a screenshot.", size=13, color=t.muted))
    for m in items:
        thumb = ft.Stack([
            c.screen_placeholder(t, width=84, aspect=3 / 2, radius=6, image=m.path if m.kind == "SHOT" else None),
            ft.Container(c.mono(m.length, size=10, color="#FFFFFF"), right=4, bottom=4,
                         bgcolor=c.alpha("#000000", 0.7), border_radius=3, padding=c.pad(4, 1)),
        ], width=84, height=56)
        rows.append(c.button(ft.Row([thumb, ft.Column([
            c.text(m.title, size=13, weight=600, color=t.text, no_wrap=True),
            c.text(m.meta, size=12, color=t.muted, no_wrap=True),
        ], spacing=2, tight=True, expand=True)], spacing=12), lambda e, p=m.path: _open_moment(app, p),
            t, variant="ghost", height=64, h_pad=0, align=ft.Alignment.CENTER_LEFT))
    return c.card(ft.Column(rows, spacing=12), t)


def _open_moment(app: "App", path: str) -> None:
    app.ui.moments_sel = path
    app.go("moments")


# rom path -> the cover box of its card on screen, so a preview made in the
# background can drop into place without re-rendering the whole library
COVERS: dict[str, tuple[ft.Container, float]] = {}


def _cover_image(g: lib.Game) -> bytes | None:
    p = thumb_path(g.path)
    try:
        return p.read_bytes() if p.exists() else None
    except OSError:
        return None


def _fill_cover(box: ft.Container, png: bytes, size: float) -> None:
    box.content = ft.Image(src=png, fit=ft.BoxFit.CONTAIN, filter_quality=ft.FilterQuality.NONE,
                           width=size, height=size, gapless_playback=True)
    box.bgcolor = "#0B0D10"


def show_preview(app: "App", path: str) -> None:
    """A preview was just made for `path`: put it on its card if it's visible."""
    slot = COVERS.get(path)
    if slot is None or app.route != "library":
        return
    box, size = slot
    g = app.game_by_path(path)
    png = _cover_image(g) if g else None
    if png is None:
        return
    _fill_cover(box, png, size)
    try:
        box.update()
    except Exception:
        pass                       # card no longer mounted


def _game_card(app: "App", g: lib.Game, fav: bool, toggle_fav, size: float) -> ft.Control:
    t = app.t
    box = ft.Container(
        c.text(g.system, size=34, weight=700, kind="display", color=t.system(g.system), spacing=-0.68),
        alignment=ft.Alignment.CENTER, bgcolor=t.card, border=c.border(t.line2), border_radius=12,
        clip_behavior=ft.ClipBehavior.HARD_EDGE, left=0, right=0, top=0, bottom=0,
    )
    shot = _cover_image(g) if g.playable else None
    if shot is not None:
        _fill_cover(box, shot, size)
    COVERS[g.path] = (box, size)
    cover = ft.Stack([
        box,
        ft.Container(c.system_badge(g.system, t), left=8, top=8) if shot is not None else
        ft.Container(c.mono(SYSTEM_RES[g.system], t, size=10), left=10, bottom=10),
        ft.Container(
            ft.IconButton(ft.Icons.STAR if fav else ft.Icons.STAR_OUTLINE, icon_size=16,
                          icon_color=t.gold if fav else t.muted, on_click=lambda e: toggle_fav(g),
                          tooltip="Remove from favorites" if fav else "Add to favorites"),
            right=2, top=2,
        ),
    ], width=size, height=size)
    meta = lib.ago(g.last_played) if g.playable else "Needs the GBA core"
    return ft.Container(
        ft.Column([
            cover,
            ft.Column([c.text(g.title, size=14, weight=600, color=t.text, max_lines=1),
                       c.text(meta, size=12, color=t.muted, no_wrap=True)], spacing=3),
        ], spacing=10),
        on_click=lambda e: app.play(g), border_radius=12, ink=True,
        tooltip=f"{g.title} · {SYSTEM_NAMES[g.system]} · {lib.human_size(g.size)}",
    )


def _home():
    from pathlib import Path
    return Path.home()


def app_name() -> str:
    from src.ui.app import APP_NAME
    return APP_NAME


def app_version() -> str:
    from src.ui.app import VERSION
    return VERSION


def _load_state_button(app: "App", g) -> ft.Control:
    """Open the game at its newest save state (the resume point or a numbered slot)."""
    from src.bridge.savestate import SlotStore
    t = app.t
    latest = SlotStore(g.path, g.system.lower()).latest() if g.playable else None
    if latest is None:
        return c.button("No save state yet", None, t, disabled=True)
    label = "Load resume point" if latest["slot"] == 0 else f"Load slot {latest['slot']}"
    return c.button(f"{label} · {lib.ago(latest['time'], '')}",
                    lambda e: app.play(g, load_slot=latest["slot"]), t)
