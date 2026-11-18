"""Achievements & link play. Both are previews: the core has neither yet."""
from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from src.ui import components as c
from src.ui.data import samples
from src.ui.theme import family

if TYPE_CHECKING:
    from src.ui.app import App


def build(app: "App") -> ft.Control:
    t, ui, s, wide = app.t, app.ui, app.settings, app.wide

    def set_ui(attr: str, value) -> None:
        setattr(ui, attr, value)
        app.render()

    def set_setting(attr: str):
        def handler(v: bool) -> None:
            setattr(s, attr, v)
            s.save()
            app.render()
        return handler

    user = ft.Row([
        ft.Container(c.text("AB", size=12, weight=600, color=t.text), width=32, height=32, border_radius=16,
                     bgcolor=t.card2, alignment=ft.Alignment.CENTER),
        c.text(f"{samples.USERNAME} · 1,240 pts", color=t.sub),
    ], spacing=10, tight=True)
    header = c.page_header(t, back=lambda e: app.go("library"), title="Achievements & link play",
                           wide=wide, trailing=[user])

    # ---------------------------------------------------------------- achievements
    recent = sorted((g for g in app.games if g.last_played), key=lambda g: -g.last_played)
    game_title = recent[0].title if recent else "Star Courier"
    ach = samples.ACHIEVEMENTS
    won = sum(1 for a in ach if a["done"])
    shown = [a for a in ach if ui.together_tab == "all" or (bool(a["done"]) == (ui.together_tab == "won"))]

    rows = []
    for a in shown:
        done = bool(a["done"])
        rows.append(ft.Container(ft.Row([
            ft.Container(c.icon(ft.Icons.EMOJI_EVENTS_OUTLINED, "#2A1F00" if done else t.muted, 20),
                         width=44, height=44, border_radius=10, bgcolor=t.gold if done else t.card2,
                         alignment=ft.Alignment.CENTER),
            ft.Column([c.text(a["name"], weight=600, color=t.text), c.text(a["desc"], size=12, color=t.muted)],
                      spacing=3, tight=True, expand=True),
            ft.Column([c.mono(f"{a['pts']} pts", size=12, weight=500, color=t.text),
                       c.mono(a["done"] or "Locked", t)], spacing=3, tight=True,
                      horizontal_alignment=ft.CrossAxisAlignment.END),
        ], spacing=14), padding=c.pad(14, 12), border_radius=12, bgcolor=t.card, border=c.border(t.line),
            opacity=1 if done else 0.72))

    main_w = app.width - (380 if wide else 0) - (64 if wide else 32)
    top = c.bar(
        ft.Column([c.heading(game_title, t, 20),
                   c.text(f"{won} of {len(ach)} unlocked · {'Hardcore' if ui.hardcore else 'Casual'} · preview data",
                          size=13, color=t.muted)], spacing=4, tight=True),
        [ft.Container(c.toggle_card("Hardcore mode", "No save states, rewind or cheats · 2× points", ui.hardcore,
                                    lambda v: set_ui("hardcore", v), t, title_size=13, padding=c.pad(14, 10)),
                      width=340 if main_w > 600 else None)], wide=main_w > 600)
    bar = ft.ProgressBar(value=won / len(ach), color=t.gold, bgcolor=t.card2, bar_height=10, border_radius=5)
    tabs = ft.Row([c.segmented([("all", "All"), ("won", "Unlocked"), ("locked", "Locked")], ui.together_tab,
                               lambda v: set_ui("together_tab", v), t, bg=t.panel)])
    main = ft.Container(
        ft.Column([top, bar, tabs, c.grid(rows, c.columns_for(main_w, 300, 12, 2))], spacing=22,
                  scroll=ft.ScrollMode.AUTO if wide else None, expand=wide),
        padding=ft.Padding.only(left=32, right=32, top=28, bottom=40) if wide else c.pad(16, 20), expand=wide,
    )

    # ---------------------------------------------------------------- link play
    if ui.link_mode == "host":
        room = c.card(ft.Column([
            c.text("Share this code with your friend", size=12, color=t.muted),
            c.mono(samples.ROOM_CODE, size=28, weight=500, color=t.text, spacing=3.9),
            ft.Row([
                c.button("Copy invite", lambda e: app.not_ready("Link play"), t, variant="primary", height=40,
                         radius=8, expand=1),
                c.button("New code", lambda e: app.not_ready("Link play"), t, height=40, radius=8, expand=1),
            ], spacing=8),
        ], spacing=10), t, padding=16, radius=12)
    else:
        room = c.card(ft.Column([
            c.text("Room code", size=12, color=t.muted),
            ft.TextField(hint_text="ABC-123", height=44, border_radius=8, border_color=t.line2, bgcolor=t.bg,
                         filled=True, fill_color=t.bg, content_padding=c.pad(12, 0), cursor_color=t.accent,
                         text_style=ft.TextStyle(size=18, color=t.text, letter_spacing=1.8, font_family=family("mono")),
                         hint_style=ft.TextStyle(size=18, color=t.muted, letter_spacing=1.8,
                                                 font_family=family("mono"))),
            c.button("Join", lambda e: app.not_ready("Link play"), t, variant="primary", radius=8),
        ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH), t, padding=16, radius=12)

    def player(tag: str, name: str, right: str, host: bool) -> ft.Container:
        return c.card(ft.Row([
            ft.Container(c.text(tag, size=12, weight=600, color=t.on if host else t.text), width=32, height=32,
                         border_radius=16, bgcolor=t.accent if host else t.card2, alignment=ft.Alignment.CENTER),
            c.text(name, weight=600, color=t.text, expand=True),
            c.mono(right, t),
        ], spacing=10), t, padding=c.pad(12, 10), radius=10)

    side = c.side_panel(t, [
        ft.Column([c.heading("Link play", t, 20),
                   c.text("Virtual link cable over the internet or your local network. Preview: not connected "
                          "to the core yet.", size=13, color=t.muted)], spacing=4),
        c.segmented([("host", "Host"), ("join", "Join")], ui.link_mode, lambda v: set_ui("link_mode", v), t,
                    equal=True),
        room,
        ft.Column([c.eyebrow("Players", t, size=12),
                   player("P1", "You (host)", game_title, True),
                   player("P2", samples.FRIEND, "38 ms", False)], spacing=8),
        ft.Column([
            c.eyebrow("Virtual accessories", t, size=12),
            c.toggle_card("Game Boy Printer", "Saves prints as PNG to Moments", s.printer, set_setting("printer"), t,
                          title_size=13, padding=c.pad(14, 12)),
            c.toggle_card("Camera → webcam", "Use your webcam for camera games", s.webcam, set_setting("webcam"), t,
                          title_size=13, padding=c.pad(14, 12)),
            c.toggle_card("Rumble & tilt", "Map to gamepad rumble and gyro", s.rumble, set_setting("rumble"), t,
                          title_size=13, padding=c.pad(14, 12)),
        ], spacing=8),
    ], width=380, wide=wide, padding=c.pad(24, 28))

    body = c.split(main, side, wide=wide)
    return ft.Container(ft.Column([header, ft.Container(body, expand=True)], spacing=0, expand=True),
                        bgcolor=t.bg, expand=True)
