"""Player onboarding: five quick setup steps shown on first launch (Profile, Games folder,
Controls, Look, Extras). Every choice is a real setting and can be changed later."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from src.ui import components as c
from src.ui.theme import SHADERS, SYSTEM_NAMES, THEME_ORDER, THEMES, family

if TYPE_CHECKING:
    from src.ui.app import App

STEPS = [
    ("Profile", "Name and avatar"),
    ("Games folder", "ROMs and BIOS"),
    ("Controls", "Keyboard or gamepad"),
    ("Look", "Theme and screen"),
    ("Extras", "Clips, rumble, printer"),
]
AVATAR_COLORS = [("blue", "Blue", "#8EA2FF"), ("green", "Green", "#9FD356"), ("orange", "Orange", "#F2A65A"),
                 ("pink", "Pink", "#F07BB0"), ("teal", "Teal", "#5FD1C1")]
AVATAR_INK = "#0F1115"
SYSTEM_ORDER = ["GB", "GBC", "GBA", "NES", "PS1"]

# Which step is open and the input choice. Lives only while the app runs.
_state = {"step": 1, "input": "kb"}


def avatar_color(key: str) -> str:
    return next((v for k, _, v in AVATAR_COLORS if k == key), AVATAR_COLORS[0][2])


def initials(name: str) -> str:
    parts = [p for p in name.replace("@", " ").split() if p]
    return "".join(p[0] for p in parts[:2]).upper() or "P1"


def avatar(name: str, color_key: str, size: float = 88) -> ft.Container:
    return ft.Container(
        c.text(initials(name), size=size * 0.36, weight=700, color=AVATAR_INK),
        width=size, height=size, border_radius=size / 4, bgcolor=avatar_color(color_key),
        alignment=ft.Alignment.CENTER,
    )


def field(t, label: str, value: str, on_change, *, hint: str | None = None, mono: bool = False,
          on_submit=None, helper: str | None = None, expand=None) -> ft.Column:
    tf = ft.TextField(
        value=value, hint_text=hint, on_change=lambda e: on_change(e.control.value),
        on_submit=(lambda e: on_submit(e.control.value)) if on_submit else None,
        on_blur=(lambda e: on_submit(e.control.value)) if on_submit else None,
        height=48, filled=True, fill_color=t.card, bgcolor=t.card, border_color=t.line2,
        focused_border_color=t.accent, border_radius=10, cursor_color=t.accent,
        content_padding=c.pad(14, 12),
        text_style=ft.TextStyle(size=14, color=t.text, font_family=family("mono" if mono else "sans")),
        hint_style=ft.TextStyle(size=14, color=t.muted, font_family=family()),
    )
    items: list[ft.Control] = [c.text(label, weight=600, color=t.text), tf]
    if helper:
        items.append(c.text(helper, size=12, color=t.muted))
    return ft.Column(items, spacing=8, tight=True, expand=expand)


def build(app: "App") -> ft.Control:
    t, s, wide = app.t, app.settings, app.wide
    step = max(1, min(len(STEPS), _state["step"]))

    def go_step(n: int) -> None:
        _state["step"] = max(1, min(len(STEPS), n))
        s.save()
        app.render()

    def finish(e=None) -> None:
        s.onboarded = True
        s.save()
        _state["step"] = 1
        app.rescan()
        app.go("library")

    # ---------------------------------------------------------------- step list
    def step_button(i: int, label: str, hint: str) -> ft.Control:
        n = i + 1
        done, cur = n < step, n == step
        if done:
            badge = ft.Container(c.icon(ft.Icons.CHECK, t.on, 16), bgcolor=t.accent)
        else:
            badge = ft.Container(c.mono(str(n), size=12, weight=500, color=t.text if cur else t.muted),
                                 border=c.border(t.accent, 2) if cur else c.border(t.line2))
        badge.width = badge.height = 30
        badge.border_radius = 15
        badge.alignment = ft.Alignment.CENTER
        body = ft.Row([badge, ft.Column([c.text(label, weight=600, color=t.text),
                                         c.text(hint, size=12, color=t.muted)], spacing=2, tight=True)],
                      spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER)
        return c.button(body, lambda e: go_step(n), t, variant="ghost", height=56, h_pad=12,
                        bg=t.card2 if cur else None, align=ft.Alignment.CENTER_LEFT)

    logo = ft.Row([
        ft.Container(c.icon(ft.Icons.SPORTS_ESPORTS_OUTLINED, t.on, 20), width=36, height=36, border_radius=9,
                     bgcolor=t.accent, alignment=ft.Alignment.CENTER),
        c.text(_app_name(), size=20, weight=700, kind="display", color=t.text),
    ], spacing=10)
    intro = ft.Column([
        c.heading("Let's set up your handheld.", t, 28),
        c.text("Five quick steps. You can change everything later in Settings.", color=t.muted),
    ], spacing=6, tight=True)
    legal = c.text(f"{_app_name()} doesn't include any games. Use ROMs and BIOS files you legally own.",
                   size=12, color=t.muted)

    # ---------------------------------------------------------------- content
    builders = [_profile, _games, _controls, _look, _extras]
    content = builders[step - 1](app)

    progress = ft.Column([
        c.mono(f"Step {step} of {len(STEPS)}", size=12, color=t.accent),
        ft.Container(ft.Row([ft.Container(bgcolor=t.accent, expand=step),
                             ft.Container(expand=len(STEPS) - step)], spacing=0),
                     height=6, border_radius=3, bgcolor=t.card2, clip_behavior=ft.ClipBehavior.HARD_EDGE),
    ], spacing=10)

    last = step == len(STEPS)
    footer = ft.Row([
        c.button(c.text("Back", color=t.sub), (lambda e: go_step(step - 1)) if step > 1 else None, t,
                 variant="ghost", height=48, disabled=step == 1),
        c.button(c.text("Skip setup", color=t.muted), finish, t, variant="ghost", height=48),
        ft.Container(expand=True),
        c.button("Start playing", finish, t, variant="primary", height=48, h_pad=24) if last else
        c.button([c.text("Continue", weight=600, color=t.on), c.icon(ft.Icons.CHEVRON_RIGHT, t.on, 18)],
                 lambda e: go_step(step + 1), t, variant="primary", height=48, h_pad=24),
    ], spacing=12)

    main_body = ft.Column([progress, *content, ft.Container(footer, padding=ft.Padding.only(top=12))],
                          spacing=28, scroll=ft.ScrollMode.AUTO, expand=True)

    if wide:
        aside = ft.Container(
            ft.Column([logo, intro,
                       ft.Column([step_button(i, l, h) for i, (l, h) in enumerate(STEPS)], spacing=4),
                       ft.Container(expand=True), legal], spacing=36, expand=True),
            width=300, bgcolor=t.panel, padding=c.pad(28, 36),
            border=ft.Border.only(right=ft.BorderSide(1, t.line)),
        )
        main = ft.Container(main_body, padding=c.pad(64, 48), width=min(1000, app.width - 300))
        body = ft.Row([aside, main], spacing=0, expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH)
    else:
        head = ft.Container(ft.Column([logo, intro], spacing=16), bgcolor=t.panel, padding=c.pad(16, 20),
                            border=ft.Border.only(bottom=ft.BorderSide(1, t.line)))
        body = ft.Column([head, ft.Container(main_body, padding=c.pad(16, 24), expand=True)],
                         spacing=0, expand=True)
    return ft.Container(body, bgcolor=t.bg, expand=True)


def _section(t, title: str, sub: str) -> ft.Column:
    return ft.Column([c.heading(title, t, 26), c.text(sub, color=t.muted)], spacing=6, tight=True)


def _two_cols(app: "App", items: list[ft.Control]) -> ft.Control:
    cols = 2 if app.wide and app.width >= 1100 else 1
    return c.grid(items, cols, gap=18)


# -------------------------------------------------------------------- 1. profile
def _profile(app: "App") -> list[ft.Control]:
    t, s = app.t, app.settings
    preview = avatar(s.player_name, s.avatar_color)

    def set_name(v: str) -> None:
        s.player_name = v.strip()
        preview.content.value = initials(s.player_name)
        preview.update()

    def set_tag(v: str) -> None:
        v = v.strip()
        s.player_tag = v if not v or v.startswith("@") else "@" + v

    def pick_color(k: str) -> None:
        s.avatar_color = k
        app.render()

    swatches = []
    for key, name, value in AVATAR_COLORS:
        on = s.avatar_color == key
        swatches.append(ft.Container(
            width=36, height=36, border_radius=18, bgcolor=value, tooltip=name,
            border=c.border(t.text, 2) if on else None, on_click=lambda e, k=key: pick_color(k), ink=True,
        ))
    systems = [(k, SYSTEM_NAMES[k].replace(" (experimental)", "")) for k in reversed(SYSTEM_ORDER)]
    return [
        _section(t, "Create your player profile", "Shown on save states, Moments and when you link up with friends."),
        ft.Row([preview, ft.Column([c.text("Avatar color", weight=600, color=t.text),
                                    ft.Row(swatches, spacing=10)], spacing=10, tight=True)],
               spacing=20, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        _two_cols(app, [
            field(t, "Display name", s.player_name, set_name, hint="Your name"),
            field(t, "Player tag", s.player_tag, set_tag, hint="@player", mono=True,
                  helper="Friends use this to invite you to link play."),
            ft.Column([c.text("Favorite console", weight=600, color=t.text),
                       c.dropdown(s.favorite_system, systems,
                                  lambda v: setattr(s, "favorite_system", v), t)], spacing=8, tight=True),
        ]),
    ]


# -------------------------------------------------------------------- 2. games folder
def _games(app: "App") -> list[ft.Control]:
    t, s = app.t, app.settings
    current = s.rom_dirs[0] if s.rom_dirs else ""

    def use_folder(path: str) -> None:
        path = path.strip()
        if not path or (s.rom_dirs and s.rom_dirs[0] == path):
            return
        if s.rom_dirs:
            s.rom_dirs[0] = path
        else:
            s.rom_dirs.append(path)
        s.rom_dirs = list(dict.fromkeys(s.rom_dirs))
        s.save()
        app.rescan()
        app.render()

    async def browse(e) -> None:
        if app.is_web:
            app.toast("Folders can be picked in the desktop app.")
            return
        path = await ft.FilePicker().get_directory_path(dialog_title="Where are your games?")
        if path:
            use_folder(path)

    async def pick_bios(e) -> None:
        if app.is_web or app.is_mobile:
            app.toast("Picking a BIOS file needs the desktop app.")
            return
        files = await ft.FilePicker().pick_files(dialog_title="Choose a PS1 BIOS image", allow_multiple=False)
        if files and files[0].path:
            s.ps1_bios = files[0].path
            s.save()
            app.render()

    counts = {k: sum(1 for g in app.games if g.system == k) for k in SYSTEM_ORDER}
    stat_cards = [c.card(ft.Column([
        c.text(str(counts[k]), size=28, weight=600, color=t.system(k)),
        c.text(SYSTEM_NAMES[k].replace(" (experimental)", ""), color=t.muted, no_wrap=True),
    ], spacing=4, tight=True), t, padding=18, radius=12) for k in SYSTEM_ORDER]
    avail = app.width - (300 + 128 if app.wide else 32)

    have_bios = bool(s.ps1_bios) and Path(s.ps1_bios).is_file()
    bios = c.card(ft.Row([
        c.dot(t.gb if have_bios else t.gbc, 10),
        ft.Column([c.text("PS1 BIOS (optional)", weight=600, color=t.text),
                   c.text(f"Using {Path(s.ps1_bios).name}." if have_bios else
                          "Not set. A built-in kernel runs PS-X programs; add a BIOS dumped from your console "
                          "to boot through it.", size=12, color=t.muted)], spacing=3, tight=True, expand=True),
        c.button("Change BIOS file" if have_bios else "Add BIOS file", pick_bios, t, height=40, h_pad=14, radius=8),
    ], spacing=14, vertical_alignment=ft.CrossAxisAlignment.CENTER), t, padding=c.pad(18, 16), radius=12)

    return [
        _section(t, "Where are your games?",
                 "Pick a folder. We'll find .gb, .gbc, .gba, .nes and PS-X .exe files inside it."),
        ft.Row([field(t, "ROM folder", current, lambda v: None, mono=True, on_submit=use_folder,
                      hint=str(Path.home() / "Games" / "ROMs"), expand=True),
                c.button("Browse…", browse, t, height=48)],
               spacing=10, vertical_alignment=ft.CrossAxisAlignment.END),
        c.auto_grid(stat_cards, avail, 150, gap=12, max_cols=5),
        bios,
        c.text("More folders and the save folder are in Settings → Paths & saves.", size=12, color=t.muted),
    ]


# -------------------------------------------------------------------- 3. controls
def _controls(app: "App") -> list[ft.Control]:
    t, s = app.t, app.settings
    kb = s.keyboard_for("GBA")
    k = lambda b: c.key_label(kb.get(b, [""])[0])  # noqa: E731
    kb_keys = (f"{k('Up')} {k('Down')} {k('Left')} {k('Right')}  ·  {k('A')}  {k('B')}  ·  "
               f"{k('L')}  {k('R')}  ·  {k('Start')}")

    def pick(v: str) -> None:
        _state["input"] = v
        app.ui.settings_device = v
        app.render()

    options = [
        ("kb", "Keyboard", f"Arrows to move, {k('A')} / {k('B')} for A / B", kb_keys),
        ("pad", "Gamepad", "Xbox, PlayStation and Switch Pro work out of the box", "D-pad · A B · LB RB · Menu"),
    ]
    cards = []
    for key, name, desc, keys in options:
        on = _state["input"] == key
        cards.append(ft.Container(ft.Column([
            c.heading(name, t, 18),
            c.text(desc, color=t.muted),
            c.mono(keys, size=12, color=t.sub),
        ], spacing=8, tight=True), padding=22, border_radius=14, bgcolor=t.soft if on else t.card,
            border=c.border(t.accent if on else t.line, 2), on_click=lambda e, v=key: pick(v), ink=True))

    from src.ui.views.settings import _pad_status
    status = _pad_status()
    connected = status.startswith("Connected")
    return [
        _section(t, "How do you want to play?",
                 "We picked a layout for you. Fine-tune it any time in Settings → Controls."),
        _two_cols(app, cards),
        ft.Container(ft.Row([c.dot(t.gb if connected else t.muted, 10),
                             c.text(status, color=t.sub, expand=True)], spacing=12),
                     padding=c.pad(16, 14), border_radius=12, bgcolor=t.soft),
    ]


# -------------------------------------------------------------------- 4. look
def _look(app: "App") -> list[ft.Control]:
    t, s = app.t, app.settings

    def set_(attr: str, v) -> None:
        setattr(s, attr, v)
        s.save()
        app.render()

    cards = []
    for key in THEME_ORDER:
        x = THEMES[key]
        on = key == s.theme
        swatch = ft.Container(
            ft.Container(width=None, height=14, bgcolor=x.accent, border_radius=4,
                         margin=ft.Margin.only(right=60)),
            height=80, bgcolor=x.bg, border=c.border(x.line), border_radius=8, padding=10,
            alignment=ft.Alignment.BOTTOM_LEFT,
        )
        cards.append(ft.Container(ft.Column([swatch, c.text(x.name, weight=600, color=t.text)], spacing=10),
                                  padding=10, border_radius=14, bgcolor=t.card,
                                  border=c.border(t.accent if on else t.line, 2),
                                  on_click=lambda e, k=key: set_("theme", k), ink=True))
    avail = app.width - (300 + 128 if app.wide else 32)
    return [
        _section(t, "Pick your look", "This page switches as you click."),
        c.grid(cards, c.columns_for(avail, 150, 14, 4), gap=14),
        ft.Container(c.setting_row("Screen shader",
                                   c.dropdown(s.shader, [(x.key, x.name) for x in SHADERS],
                                              lambda v: set_("shader", v), t, width=220), t), width=420),
    ]


# -------------------------------------------------------------------- 5. extras
def _extras(app: "App") -> list[ft.Control]:
    t, s = app.t, app.settings

    def flag(attr: str):
        def on(v: bool) -> None:
            setattr(s, attr, v)
            s.save()
            app.render()
        return on

    rows = [
        ("Replay buffer", "Keep the last 30 s so F9 can save it as a moment.", "replay_buffer"),
        ("Rumble & tilt", "Map rumble cartridges to your gamepad's motors and gyro.", "rumble"),
        ("Game Boy Printer", "Saves prints as PNG to Moments.", "printer"),
        ("Camera → webcam", "Use your webcam for Game Boy Camera games.", "webcam"),
    ]
    return [_section(t, "Last bits, then play", "All optional. Turn on what you like."),
            ft.Column([c.toggle_card(n, d, getattr(s, a), flag(a), t, padding=c.pad(18, 16)) for n, d, a in rows],
                      spacing=12)]


def _app_name() -> str:
    from src.ui.app import APP_NAME
    return APP_NAME
