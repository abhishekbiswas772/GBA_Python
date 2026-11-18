"""Themes & screen: app theme, screen shader, GB palette, display options and a live preview."""
from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from src.ui import components as c
from src.ui.theme import GB_PALETTES, SHADER_BY_KEY, SHADERS, THEME_ORDER, THEMES

if TYPE_CHECKING:
    from src.ui.app import App


def build(app: "App") -> ft.Control:
    t, s, wide = app.t, app.settings, app.wide

    def set_(attr: str, value) -> None:
        setattr(s, attr, value)
        s.save()
        app.render()

    def flag(attr: str):
        return lambda v: set_(attr, v)

    header = c.page_header(
        t, back=lambda e: app.go("library"), title="Themes & screen", wide=wide,
        subtitle="Changes apply instantly. Click a theme to try it on this page.",
        trailing=[c.button("Controls", lambda e: _controls(app), t, h_pad=16)],
    )
    main_w = app.width - (360 if wide else 0) - (64 if wide else 32)

    # ---------------------------------------------------------------- themes
    theme_cards = []
    for key in THEME_ORDER:
        x = THEMES[key]
        on = key == s.theme
        swatch = ft.Container(ft.Row([
            ft.Container(width=None, expand=28, bgcolor=x.panel, border=ft.Border.only(right=ft.BorderSide(1, x.line))),
            ft.Container(ft.Column([
                ft.Container(height=8, width=None, bgcolor=x.text, border_radius=3),
                ft.Container(height=6, bgcolor=x.muted, border_radius=3, margin=ft.Margin.only(right=30)),
                ft.Container(expand=True),
                ft.Container(height=16, width=44, bgcolor=x.accent, border_radius=4),
            ], spacing=6, horizontal_alignment=ft.CrossAxisAlignment.START), expand=72, padding=10),
        ], spacing=0, vertical_alignment=ft.CrossAxisAlignment.STRETCH),
            height=96, bgcolor=x.bg, border=c.border(x.line), border_radius=8,
            clip_behavior=ft.ClipBehavior.HARD_EDGE)
        theme_cards.append(ft.Container(
            ft.Column([swatch, ft.Container(ft.Column([
                c.text(x.name, size=14, weight=600, color=t.text),
                c.text(x.desc, size=12, color=t.muted, no_wrap=True),
            ], spacing=2), padding=ft.Padding.only(left=4, right=4, top=10, bottom=2))], spacing=0),
            padding=8, border_radius=14, bgcolor=t.card, border=c.border(t.accent if on else t.line, 2),
            on_click=lambda e, k=key: set_("theme", k), ink=True,
            tooltip=f"Use the {x.name} theme",
        ))
    themes = ft.Column([
        c.heading("App theme", t, 18),
        c.grid(theme_cards, c.columns_for(main_w, 150, 14, 4), gap=14),
        c.grid([
            c.toggle_card("Match theme to console", "Green for GB, Berry for GBC, Midnight for GBA",
                          s.match_console, flag("match_console"), t),
            c.toggle_card("Follow system light/dark", "Daylight by day, your theme at night",
                          s.follow_system, flag("follow_system"), t),
        ], c.columns_for(main_w, 300, 12, 2)),
    ], spacing=14)

    # ---------------------------------------------------------------- shaders
    shader_cards = []
    for sh in SHADERS:
        on = sh.key == s.shader
        shader_cards.append(ft.Container(
            ft.Column([c.text(sh.name, size=13, weight=600, color=t.text, no_wrap=True),
                       c.text(sh.desc, size=12, color=t.muted)], spacing=4),
            padding=14, border_radius=12, bgcolor=t.soft if on else t.card,
            border=c.border(t.accent if on else t.line, 2), ink=True,
            on_click=lambda e, k=sh.key: set_("shader", k),
        ))
    shaders = ft.Column([c.heading("Screen shader", t, 18),
                         c.grid(shader_cards, c.columns_for(main_w, 130, 12, 5))], spacing=14)

    # ---------------------------------------------------------------- palettes
    pal_cards = []
    for key, p in GB_PALETTES.items():
        on = key == s.gb_palette
        pal_cards.append(ft.Container(
            ft.Column([
                ft.Container(ft.Row([ft.Container(bgcolor=col, expand=1) for col in p.colors], spacing=0),
                             height=32, border_radius=6, clip_behavior=ft.ClipBehavior.HARD_EDGE),
                c.text(p.name, size=13, weight=600, color=t.text),
            ], spacing=10),
            padding=12, border_radius=12, bgcolor=t.card, border=c.border(t.accent if on else t.line, 2),
            ink=True, on_click=lambda e, k=key: set_("gb_palette", k),
        ))
    palettes = ft.Column([
        ft.Column([c.heading("Original Game Boy palette", t, 18),
                   c.text("Colors for black-and-white GB games.", size=13, color=t.muted)], spacing=4),
        c.grid(pal_cards, c.columns_for(main_w, 150, 12, 4)),
    ], spacing=14)

    display = c.grid([
        c.toggle_card("Original LCD colors", "Color correction for GBC/GBA", s.lcd_colors, flag("lcd_colors"), t),
        c.toggle_card("Integer scaling", "Sharp pixels, no shimmer", s.integer_scale, flag("integer_scale"), t),
        c.toggle_card("Frame blending", "LCD ghosting some games rely on", s.frame_blend, flag("frame_blend"), t),
    ], c.columns_for(main_w, 240, 12, 3))

    main = ft.Container(
        ft.Column([themes, shaders, palettes, display], spacing=32,
                  scroll=ft.ScrollMode.AUTO if wide else None, expand=wide),
        padding=ft.Padding.only(left=32, right=32, top=28, bottom=40) if wide else c.pad(16, 20), expand=wide,
    )

    # ---------------------------------------------------------------- preview
    pal = GB_PALETTES[s.gb_palette]
    shader = SHADER_BY_KEY[s.shader]
    shadows = None
    if s.shader == "crt":
        shadows = [ft.BoxShadow(blur_radius=40, color=c.alpha("#000000", 0.55), blur_style=ft.BlurStyle.INNER)]
    lcd = ft.Container(
        ft.Column([
            ft.Row([ft.Container(height=22, expand=1, bgcolor=col, border_radius=3) for col in pal.colors], spacing=6,
                   width=180),
            c.mono(f"{shader.name} · {pal.name}", size=11, color=pal.colors[0]),
        ], spacing=14, horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER),
        height=((312 if wide else app.width - 48) - 44) * 9 / 10, bgcolor=pal.colors[3], border_radius=6, alignment=ft.Alignment.CENTER,
        border=c.border(c.alpha("#000000", 0.25)) if s.shader == "lcd" else None, shadow=shadows,
    )
    handheld = ft.Container(ft.Column([
        lcd,
        ft.Row([
            ft.Container(width=56, height=56, border_radius=28, bgcolor=t.line2),
            ft.Row([ft.Container(width=34, height=34, border_radius=17, bgcolor=t.accent) for _ in range(2)],
                   spacing=10, tight=True),
        ], alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
    ], spacing=14), padding=ft.Padding.only(left=22, right=22, top=20, bottom=26), bgcolor=t.card2,
        border_radius=ft.BorderRadius.only(top_left=18, top_right=18, bottom_left=18, bottom_right=44))

    def summary_row(k: str, v: str) -> ft.Row:
        return ft.Row([c.text(k, color=t.muted), c.text(v, weight=600, color=t.text)],
                      alignment=ft.MainAxisAlignment.SPACE_BETWEEN)

    summary = c.card(ft.Column([summary_row("Theme", t.name), summary_row("Shader", shader.name),
                                summary_row("GB palette", pal.name)], spacing=8), t, padding=c.pad(16, 14), radius=12)
    actions = ft.Row([
        c.button("Import theme…", lambda e: app.not_ready("Custom themes"), t, expand=1),
        c.button("Done", lambda e: app.go("library"), t, variant="primary", expand=1),
    ], spacing=10)
    side = c.side_panel(t, [c.heading("Live preview", t, 16), handheld, summary, c.spacer(), actions],
                        width=360, wide=wide, padding=c.pad(24, 28), spacing=16, fits=app.height >= 800)

    body = c.split(main, side, wide=wide)
    return ft.Container(ft.Column([header, ft.Container(body, expand=True)], spacing=0, expand=True),
                        bgcolor=t.bg, expand=True)


def _controls(app: "App") -> None:
    app.ui.settings_section = "Controls"
    app.go("settings")
