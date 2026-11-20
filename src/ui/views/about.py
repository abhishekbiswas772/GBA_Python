"""About us: who makes the app, what it believes in, what's inside and where it's heading."""
from __future__ import annotations

from typing import TYPE_CHECKING

import flet as ft

from src.ui import components as c

if TYPE_CHECKING:
    from src.ui.app import App

CREATOR = "Abhishek Biswas"
REPO = "https://github.com/abhishekbiswas772/RetroPy"

VALUES = [
    ("01", "Accuracy first", "Games should run the way they did on real hardware — timing, sound and quirks included."),
    ("02", "Your games stay yours", "Works fully offline. No accounts required, no ads, no tracking."),
    ("03", "Made for sharing", "Great moments deserve to be seen. Clipping one is a single key press."),
    ("04", "Play fair", "Bring games you own. We never ship or link to ROMs or BIOS files."),
]
FEATURES = [
    ("One library for five consoles", "Game Boy, Color, Advance, NES and PlayStation side by side, "
                                      "sorted by when you last played."),
    ("Save states and rewind", "Save slots per game, plus hold R to undo a mistake."),
    ("Moments", "Record, clip the last 30 seconds and export as MP4 or GIF."),
    ("Themes and shaders", "Midnight, Daylight, DMG Green and Berry; LCD, CRT and pixel-perfect screens."),
    ("Written in Python", "Every core is readable Python (the PlayStation runs on a small Rust core)."),
    ("Open source", "MIT licensed. Read the code, file an issue or send a pull request."),
]
ROADMAP = [
    ("Now", ["GB, GBC, GBA and NES support", "Moments: record, clip and share", "Themes and screen shaders"]),
    ("Next", ["Online link play", "Achievements in supported games", "Controller presets for more pads"]),
    ("Later", ["Built-in debugger for homebrew devs", "Cloud save sync", "Your idea here: open an issue"]),
]


def build(app: "App") -> ft.Control:
    t, wide = app.t, app.wide
    name = _app_name()
    gutter = 32 if wide else 16
    avail = min(1160, app.width) - gutter * 2

    def h2(value: str, size: float = 36) -> ft.Text:
        return c.heading(value, t, size)

    header = c.page_header(
        t, back=lambda e: app.go("library"), title=f"About {name}", wide=wide,
        trailing=[c.button("Run setup again", lambda e: app.go("onboarding"), t, variant="primary", h_pad=18)],
    )

    intro = ft.Column([
        c.mono("About us", size=13, color=t.accent),
        c.heading("Made by one player who missed the Game Boy.", t, 60 if wide else 36),
        c.text(f"{name} is a desktop emulator for Game Boy, Game Boy Color, Game Boy Advance and NES games, "
               "with an experimental PlayStation core — built to feel like the handheld you remember, "
               "with the tools you'd want today.", size=20 if wide else 16, color=t.sub),
    ], spacing=22)

    initials = "".join(p[0] for p in CREATOR.split()[:2]).upper()
    creator_card = ft.Column([
        ft.Container(c.text(initials, size=56, weight=700, color=t.on), width=160, height=160, border_radius=32,
                     bgcolor=t.accent, alignment=ft.Alignment.CENTER),
        ft.Column([c.text(CREATOR, size=22, weight=600, color=t.text),
                   c.text("Creator & solo developer", size=14, color=t.muted)], spacing=2, tight=True),
    ], spacing=16, tight=True)
    story = ft.Column([
        h2("Why I built this", 30),
        c.text("I grew up with a handheld in my school bag. Years later, I wanted to replay those games on my "
               "laptop — but every emulator I tried felt like a settings panel first and a game second.",
               size=16, color=t.sub),
        c.text(f"So I started {name} as a side project: a clean library, instant save states, and a one-key "
               "way to clip the moment you finally beat that boss. It grew from there.", size=16, color=t.sub),
        c.text("It's still built by one person, in the evenings, with a lot of help from players who send bug "
               "reports and ideas. If you're one of them — thank you.", size=16, color=t.sub),
        c.text(f"— {CREATOR.split()[0]}", size=18, weight=600, color=t.text),
    ], spacing=18, expand=wide)
    creator = ft.Container(
        ft.Row([creator_card, story], spacing=48, vertical_alignment=ft.CrossAxisAlignment.START) if wide
        else ft.Column([creator_card, story], spacing=28),
        padding=40 if wide else 24, border_radius=24, bgcolor=t.card, border=c.border(t.line),
    )

    value_cards = [c.card(ft.Column([
        c.mono(n, size=13, color=t.accent),
        c.text(title, size=20, weight=600, color=t.text),
        c.text(body, size=15, color=t.muted),
    ], spacing=10, tight=True), t, padding=26, radius=18) for n, title, body in VALUES]
    values = ft.Column([h2("What we believe"), c.auto_grid(value_cards, avail, 240, gap=18)], spacing=28)

    feature_rows = [ft.Container(ft.Row([
        ft.Container(c.dot(t.accent, 10, 3), padding=ft.Padding.only(top=7)),
        ft.Column([c.text(title, weight=600, color=t.text, size=16),
                   c.text(body, size=15, color=t.muted)], spacing=4, tight=True, expand=True),
    ], spacing=16, vertical_alignment=ft.CrossAxisAlignment.START), padding=c.pad(0, 20),
        border=ft.Border.only(top=ft.BorderSide(1, t.line))) for title, body in FEATURES]
    features = ft.Column([h2("What's inside"),
                          c.grid(feature_rows, c.columns_for(avail, 320, 40, 2), gap=0)], spacing=28)

    road_cards = []
    for i, (when, items) in enumerate(ROADMAP):
        tag = c.pill(when, t.accent if i == 0 else t.card2, t.on if i == 0 else t.sub, size=12,
                     padding=c.pad(10, 4))
        road_cards.append(c.card(ft.Column([
            ft.Row([tag]),
            ft.Column([ft.Row([c.text("•", color=t.sub), c.text(x, size=15, color=t.sub, expand=True)],
                              spacing=8, vertical_alignment=ft.CrossAxisAlignment.START) for x in items],
                      spacing=8),
        ], spacing=14, tight=True), t, padding=26, radius=18))
    roadmap = ft.Column([h2("Where it's heading"), c.grid(road_cards, 3 if wide else 1, gap=18)], spacing=28)

    def link(label: str, url: str, primary: bool = False) -> ft.Control:
        async def open_url(e) -> None:
            try:
                await ft.UrlLauncher().launch_url(url)
            except Exception:
                app.toast(url)

        return c.button(label, open_url, t,
                        variant="primary" if primary else "secondary", height=48, h_pad=20)

    contact = ft.Container(
        (ft.Row if wide else ft.Column)([
            ft.Column([h2("Say hi, report a bug, ask for a feature", 32),
                       c.text("I read every message. Bug reports with a save state attached get fixed fastest.",
                              size=16, color=t.sub)], spacing=10, expand=wide),
            ft.Row([link("Report an issue", f"{REPO}/issues", True), link("GitHub", REPO),
                    link("Releases", f"{REPO}/releases")], spacing=10, wrap=True),
        ], spacing=32),
        padding=40 if wide else 24, border_radius=24, bgcolor=t.soft,
    )

    footer = ft.Container(ft.Column([
        ft.Row([c.text(f"© 2026 {name} · {CREATOR}", size=13, color=t.muted),
                c.text("MIT License", size=13, color=t.muted)], spacing=32, wrap=True),
        c.text(f"{name} does not include or distribute games or BIOS files. Game Boy, NES and PlayStation are "
               f"trademarks of their owners; {name} is not affiliated with or endorsed by them.",
               size=13, color=t.muted),
    ], spacing=12), padding=ft.Padding.only(top=28), border=ft.Border.only(top=ft.BorderSide(1, t.line)))

    body = ft.Container(ft.Column([intro, creator, values, features, roadmap, contact, footer], spacing=96 if wide else 56),
                        width=min(1160, app.width), padding=ft.Padding.only(left=gutter, right=gutter,
                                                                            top=88 if wide else 32, bottom=80))
    page = ft.Column([ft.Row([body], alignment=ft.MainAxisAlignment.CENTER)], scroll=ft.ScrollMode.AUTO, expand=True)
    return ft.Container(ft.Column([header, page], spacing=0, expand=True), bgcolor=t.bg, expand=True)


def _app_name() -> str:
    from src.ui.app import APP_NAME
    return APP_NAME
