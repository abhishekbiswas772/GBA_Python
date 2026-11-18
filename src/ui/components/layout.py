"""Page-level layout: header bars, sidebars, side panels and responsive grids.

The design uses CSS flex-wrap; here each screen gets an explicit `wide` flag
(window >= WIDE_BREAKPOINT) and stacks panes vertically on phones/narrow windows.
"""
from __future__ import annotations

import math
from typing import Sequence

import flet as ft

from src.ui.components.primitives import Handler, back_link, border, heading, icon, pad, text
from src.ui.theme import Theme

WIDE_BREAKPOINT = 960
SIDEBAR_W = 240


def bar(lead: ft.Control, trailing: Sequence[ft.Control], *, wide: bool, spacing: float = 16,
        align=ft.CrossAxisAlignment.CENTER) -> ft.Control:
    """`lead` grows, `trailing` sits at the end. Narrow: trailing wraps onto its own line.
    (Flutter's Wrap can't hold expanded children, so never combine wrap with expand.)"""
    if wide:
        return ft.Row([ft.Container(lead, expand=True), *trailing], spacing=spacing,
                      vertical_alignment=align)
    if not trailing:
        return lead
    return ft.Column([lead, ft.Row(list(trailing), spacing=10, wrap=True, run_spacing=10)], spacing=12)


def page_header(t: Theme, *, back: Handler, title: str, subtitle: str | None = None,
                trailing: Sequence[ft.Control] = (), title_size: float = 24, wide: bool = True,
                back_label: str = "Library") -> ft.Container:
    titles = ft.Column([heading(title, t, title_size)] + (
        [text(subtitle, size=13, color=t.muted)] if subtitle else []), spacing=2, tight=True)
    lead = ft.Row([back_link(t, back, back_label), ft.Container(titles, expand=True)], spacing=16)
    return ft.Container(
        bar(lead, trailing, wide=wide),
        padding=pad(32 if wide else 16, 20 if wide else 14), bgcolor=t.panel,
        border=ft.Border.only(bottom=ft.BorderSide(1, t.line)),
    )


def scroll_col(controls: list[ft.Control], *, spacing: float = 24, padding=None,
               expand=True) -> ft.Container:
    return ft.Container(
        ft.Column(controls, spacing=spacing, scroll=ft.ScrollMode.AUTO, expand=True),
        padding=padding, expand=expand,
    )


def spacer() -> ft.Container:
    """CSS `margin-top: auto`: pushes later items to the bottom of a pinned column.
    Dropped automatically when the column has to scroll instead."""
    return ft.Container(expand=True, data="spacer")


def _pinned_column(controls: list[ft.Control], spacing: float, wide: bool, fits: bool) -> ft.Column:
    if wide and fits:
        return ft.Column(controls, spacing=spacing, expand=True)
    kept = [x for x in controls if getattr(x, "data", None) != "spacer"]
    return ft.Column(kept, spacing=spacing, scroll=ft.ScrollMode.AUTO if wide else None, expand=wide)


def side_panel(t: Theme, controls: list[ft.Control], *, width: float, wide: bool, padding=None,
               spacing: float = 20, left: bool = False, fits: bool = False) -> ft.Container:
    """Right-hand (or left) panel; becomes a full-width section below the main pane when narrow.
    `fits`: the window is tall enough to pin content without scrolling (keeps spacers)."""
    side = ft.BorderSide(1, t.line)
    return ft.Container(
        _pinned_column(controls, spacing, wide, fits),
        width=width if wide else None, bgcolor=t.panel,
        padding=padding if padding is not None else pad(24, 28),
        border=(ft.Border.only(right=side) if left else ft.Border.only(left=side)) if wide
        else ft.Border.only(top=side),
    )


def split(main: ft.Control, side: ft.Control, *, wide: bool, side_first: bool = False) -> ft.Control:
    """Main pane + side panel. Wide: a row with independent scrolling. Narrow: one scroll column."""
    if wide:
        row = [side, main] if side_first else [main, side]
        return ft.Row(row, spacing=0, expand=True, vertical_alignment=ft.CrossAxisAlignment.STRETCH)
    stacked = [side, main] if side_first else [main, side]
    return ft.Column(stacked, spacing=0, scroll=ft.ScrollMode.AUTO, expand=True)


def columns_for(avail: float, min_w: float, gap: float, max_cols: int = 12) -> int:
    return max(1, min(max_cols, int((avail + gap) // (min_w + gap))))


def grid(children: Sequence[ft.Control], cols: int, *, gap: float = 12) -> ft.Column:
    """Equal-width columns, like CSS grid repeat(cols, 1fr)."""
    rows = []
    for i in range(0, len(children), cols):
        chunk = list(children[i:i + cols])
        cells = [ft.Container(c, expand=1) for c in chunk]
        cells += [ft.Container(expand=1) for _ in range(cols - len(chunk))]
        rows.append(ft.Row(cells, spacing=gap, vertical_alignment=ft.CrossAxisAlignment.START))
    return ft.Column(rows, spacing=gap)


def cell_width(avail: float, min_w: float, gap: float = 20, max_cols: int = 12) -> float:
    """Width of one auto_grid cell, for children that need an explicit size."""
    cols = columns_for(avail, min_w, gap, max_cols)
    return (avail - gap * (cols - 1)) / cols


def auto_grid(children: Sequence[ft.Control], avail: float, min_w: float, *, gap: float = 20,
              max_cols: int = 12) -> ft.Column:
    """CSS repeat(auto-fill, minmax(min_w, 1fr))."""
    return grid(children, columns_for(avail, min_w, gap, max_cols), gap=gap)


def nav_item(t: Theme, label: str, on_click: Handler, *, icon_name=None, lead: ft.Control | None = None,
             trailing: ft.Control | None = None, selected: bool = False) -> ft.TextButton:
    from src.ui.components.primitives import button
    color = t.text if selected else t.sub
    left = [c for c in (lead, icon(icon_name, color, 16) if icon_name is not None else None) if c]
    left.append(text(label, color=color, weight=600 if selected else 400, no_wrap=True, expand=True))
    parts: list[ft.Control] = [ft.Row(left, spacing=10, expand=True,
                                      vertical_alignment=ft.CrossAxisAlignment.CENTER)]
    if trailing is not None:
        parts.append(trailing)
    return button(ft.Row(parts, spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER), on_click, t,
                  variant="ghost", height=40, h_pad=10,
                  radius=8, bg=t.card2 if selected else None, align=ft.Alignment.CENTER_LEFT)


def sidebar(t: Theme, controls: list[ft.Control], *, wide: bool, spacing: float = 26,
            fits: bool = False) -> ft.Container:
    return ft.Container(
        _pinned_column(controls, spacing, wide, fits),
        width=SIDEBAR_W if wide else None, bgcolor=t.panel, padding=pad(12, 24),
        border=ft.Border.only(right=ft.BorderSide(1, t.line)) if wide
        else ft.Border.only(bottom=ft.BorderSide(1, t.line)),
    )


def empty_state(t: Theme, title: str, body: str, actions: Sequence[ft.Control] = (), icon_name=None) -> ft.Container:
    items: list[ft.Control] = []
    if icon_name is not None:
        items.append(ft.Container(icon(icon_name, t.accent, 22), width=48, height=48, border_radius=12,
                                  bgcolor=t.soft, alignment=ft.Alignment.CENTER))
    items += [heading(title, t, 18), text(body, size=13, color=t.muted, align=ft.TextAlign.CENTER)]
    if actions:
        items.append(ft.Row(list(actions), spacing=10, wrap=True, alignment=ft.MainAxisAlignment.CENTER))
    return ft.Container(
        ft.Column(items, spacing=10, horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
        padding=32, border_radius=16, bgcolor=t.card, border=border(t.line), alignment=ft.Alignment.CENTER,
    )


def fit_scale(avail_w: float, avail_h: float, w: int, h: int, integer: bool) -> float:
    s = min(avail_w / w, avail_h / h)
    if integer and s >= 1:
        s = math.floor(s)
    return max(s, 0.5)
