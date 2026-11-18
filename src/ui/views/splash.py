"""Splash screen shown while settings load and the ROM library is scanned."""
from __future__ import annotations

import asyncio
from pathlib import Path

import flet as ft

from src.ui import components as c
from src.ui.theme import Theme

SPLASH_IMAGE = Path(__file__).resolve().parents[1] / "assets" / "splash.png"


class RetroLoader:
    """Pixel loading bar: chunky square blocks inside a 2px frame that fill one by one,
    plus a blinking "LOADING..." label in the pixel font. Animated from an asyncio task."""

    BLOCKS = 12
    TICK = 0.09          # seconds per block

    def __init__(self, t: Theme):
        self.t = t
        self._done = False
        self._running = False
        self._cells = [ft.Container(width=14, height=14, bgcolor=t.card2) for _ in range(self.BLOCKS)]
        self.label = c.text("LOADING", size=12, kind="display", color=t.muted)
        self.status = c.text("", size=12, color=t.muted)
        bar = ft.Container(
            ft.Row(self._cells, spacing=4, tight=True),
            padding=4, border=ft.Border.all(2, t.accent), bgcolor=t.bg,   # square corners on purpose
        )
        self.control = ft.Column([bar, self.label, self.status], spacing=12, tight=True,
                                 horizontal_alignment=ft.CrossAxisAlignment.CENTER)

    def _paint(self, filled: int) -> None:
        t = self.t
        for i, cell in enumerate(self._cells):
            # the leading block is brighter, like a cursor
            cell.bgcolor = (t.text if i == filled - 1 and not self._done else t.accent) if i < filled else t.card2

    async def run(self) -> None:
        self._running = True
        step = 0
        while self._running and not self._done:
            filled = step % (self.BLOCKS + 1)
            self._paint(filled)
            self.label.value = "LOADING" + "." * (step // 3 % 4)
            try:
                self.control.update()
            except Exception:
                return              # splash already replaced
            step += 1
            await asyncio.sleep(self.TICK)

    async def finish(self, message: str) -> None:
        """Fill the bar to the end, then show the result."""
        self._done = True
        t = self.t
        try:
            for i in range(self.BLOCKS + 1):
                for j, cell in enumerate(self._cells):
                    cell.bgcolor = t.accent if j < i else t.card2
                self.control.update()
                await asyncio.sleep(0.03)
            self.label.value = "READY!"
            self.label.color = t.accent
            self.status.value = message
            self.control.update()
        except RuntimeError:
            return              # splash already replaced (control no longer on the page)

    def stop(self) -> None:
        self._running = False


def build(t: Theme, app_name: str, version: str, loader: RetroLoader) -> ft.Control:
    art = SPLASH_IMAGE.read_bytes() if SPLASH_IMAGE.exists() else None
    logo = ft.Image(src=art, width=128, height=128, filter_quality=ft.FilterQuality.NONE) if art else \
        ft.Container(c.icon(ft.Icons.SPORTS_ESPORTS_OUTLINED, t.on, 64), width=128, height=128,
                     bgcolor=t.accent, alignment=ft.Alignment.CENTER)
    return ft.Container(
        ft.Column([
            logo,
            ft.Container(height=8),
            c.text(app_name, size=38, weight=700, kind="display", color=t.text, align=ft.TextAlign.CENTER),
            c.mono("GB · GBC · GBA", t, size=13, spacing=2),
            ft.Container(height=28),
            loader.control,
            ft.Container(height=36),
            c.mono(f"v{version}", t, size=11),
        ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER, tight=True),
        bgcolor=t.bg, expand=True, alignment=ft.Alignment.CENTER,
    )
