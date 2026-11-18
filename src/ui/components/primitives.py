"""Small building blocks shared by every screen. All take the active Theme `t`."""
from __future__ import annotations

from contextlib import contextmanager
from typing import Callable, Iterable, Sequence

import flet as ft

from src.ui.theme import Theme, family
from src.ui.theme.fonts import display_size

Handler = Callable | None

# While building the player screen, controls are made non-focusable. Flutter's
# focus system otherwise turns game keys into UI actions: arrows / Tab move
# focus onto a button, Enter / Space then *click* it (Start = Enter would hit
# Reset or Pause), and Left / Right nudge a focused slider.
_NO_FOCUS = False


@contextmanager
def unfocusable():
    """Build buttons / checkboxes inside this block as plain tap targets that
    can never take keyboard focus (they still work with mouse and touch)."""
    global _NO_FOCUS
    old, _NO_FOCUS = _NO_FOCUS, True
    try:
        yield
    finally:
        _NO_FOCUS = old


def tap(content: ft.Control, on_click: Handler, *, tooltip: str | None = None, expand=None,
        col=None) -> ft.GestureDetector:
    """A clickable area that never takes keyboard focus."""
    return ft.GestureDetector(
        content=content, on_tap=on_click,
        mouse_cursor=ft.MouseCursor.CLICK if on_click else None,
        tooltip=tooltip, expand=expand, col=col if col is not None else 12,
    )


def alpha(color: str, opacity: float) -> str:
    return ft.Colors.with_opacity(opacity, color)


def pad(h: float = 0, v: float = 0) -> ft.Padding:
    return ft.Padding.symmetric(horizontal=h, vertical=v)


def border(color: str, width: float = 1) -> ft.Border:
    return ft.Border.all(width, color)


# ---------------------------------------------------------------- text
def text(value: str, *, size: float = 14, color: str | None = None, weight: int = 400,
         kind: str = "sans", spacing: float | None = None, max_lines: int | None = None,
         align: ft.TextAlign | None = None, expand: bool | int | None = None,
         no_wrap: bool = False, selectable: bool = False) -> ft.Text:
    if kind == "display":            # pixel font: snapped size, no tracking
        size, spacing = display_size(size), None
    return ft.Text(
        value, size=size, color=color, font_family=family(kind, weight),
        style=ft.TextStyle(letter_spacing=spacing) if spacing is not None else None,
        max_lines=max_lines, overflow=ft.TextOverflow.ELLIPSIS if (max_lines or no_wrap) else ft.TextOverflow.CLIP,
        text_align=align or ft.TextAlign.START, expand=expand, no_wrap=no_wrap, selectable=selectable or None,
    )


def heading(value: str, t: Theme, size: float = 28, weight: int = 600, color: str | None = None) -> ft.Text:
    return text(value, size=size, weight=weight, kind="display", color=color or t.text)


def eyebrow(value: str, t: Theme, size: float = 11, color: str | None = None) -> ft.Text:
    return text(value.upper(), size=size, weight=600, color=color or t.muted, spacing=0.08 * size)


def mono(value: str, t: Theme | None = None, *, size: float = 12, color: str | None = None,
         weight: int = 400, spacing: float | None = None) -> ft.Text:
    return text(value, size=size, kind="mono", weight=weight, spacing=spacing,
                color=color or (t.muted if t else None))


def icon(name, color: str, size: float = 16) -> ft.Icon:
    return ft.Icon(name, size=size, color=color)


# ---------------------------------------------------------------- surfaces
def card(content: ft.Control, t: Theme, *, padding=22, radius: float = 16, bg: str | None = None,
         line: str | None = None, expand=None, width=None, height=None, on_click: Handler = None,
         col=None) -> ft.Container:
    return ft.Container(
        content=content, padding=padding, border_radius=radius, bgcolor=bg or t.card,
        border=border(line or t.line), expand=expand, width=width, height=height,
        on_click=on_click, ink=on_click is not None, col=col if col is not None else 12,
    )


def pill(value: str, bg: str, fg: str, *, size: float = 11, mono_font: bool = True,
         weight: int = 500, radius: float = 6, padding=pad(8, 3)) -> ft.Container:
    return ft.Container(
        text(value, size=size, color=fg, weight=weight, kind="mono" if mono_font else "sans"),
        bgcolor=bg, border_radius=radius, padding=padding,
    )


def system_badge(system: str, t: Theme) -> ft.Container:
    return pill(system, t.system(system), t.on)


def kbd(key: str, t: Theme) -> ft.Container:
    return pill(key_label(key), t.card2, t.text, size=12, weight=400, padding=pad(8, 4))


def divider_v(t: Theme, height: float = 24) -> ft.Container:
    return ft.Container(width=1, height=height, bgcolor=t.line2, margin=ft.Margin.symmetric(horizontal=4))


def dot(color: str, size: float = 8, radius: float | None = None) -> ft.Container:
    return ft.Container(width=size, height=size, bgcolor=color,
                        border_radius=size / 2 if radius is None else radius)


def screen_placeholder(t: Theme, label: str = "", *, width: float, aspect: float = 3 / 2, radius: float = 10,
                       image: bytes | str | None = None) -> ft.Container:
    """Game-screen box of an explicit size. (Flet 1.1's aspect_ratio breaks layout on web,
    so sizes are always computed: height = width / aspect.)"""
    if image is not None:
        inner = ft.Image(src=image, fit=ft.BoxFit.CONTAIN, filter_quality=ft.FilterQuality.NONE,
                         border_radius=radius)
    else:
        inner = mono(label, size=12, color="#8A93A3") if label else None
    return ft.Container(inner, bgcolor=t.screen, border_radius=radius, width=width, height=width / aspect,
                        alignment=ft.Alignment.CENTER, clip_behavior=ft.ClipBehavior.HARD_EDGE)


# ---------------------------------------------------------------- buttons
def button(content: str | ft.Control | Sequence[ft.Control], on_click: Handler, t: Theme, *,
           variant: str = "secondary", height: float = 44, h_pad: float = 18, radius: float = 10,
           icon_name=None, width=None, expand=None, tooltip: str | None = None,
           align: ft.Alignment | None = None, bg: str | None = None, fg: str | None = None,
           side: str | None = None, weight: int | None = None, size: float = 14,
           disabled: bool = False, col=None) -> ft.TextButton:
    """variant: primary (accent), inverse (text on bg), secondary (outlined), ghost (no chrome)."""
    if variant == "primary":
        bg, fg, side = bg or t.accent, fg or t.on, side
        weight = weight or 600
    elif variant == "inverse":
        bg, fg = bg or t.text, fg or t.bg
        weight = weight or 600
    elif variant == "secondary":
        bg, fg, side = bg or "#00000000", fg or t.text, side or t.line2
    else:  # ghost
        bg, fg = bg or "#00000000", fg or t.text
    weight = weight or 400

    if isinstance(content, str):
        parts: list[ft.Control] = []
        if icon_name is not None:
            parts.append(icon(icon_name, fg, 16))
        parts.append(text(content, size=size, weight=weight, color=fg, no_wrap=True))
        body: ft.Control = ft.Row(parts, spacing=8, tight=True,
                                  alignment=ft.MainAxisAlignment.CENTER)
    elif isinstance(content, (list, tuple)):
        body = ft.Row(list(content), spacing=8, tight=True)
    else:
        body = content

    if _NO_FOCUS:
        box = ft.Container(
            body, bgcolor=bg, height=height, width=width, padding=pad(h_pad, 0),
            border_radius=radius, border=border(side) if side else None,
            alignment=align or ft.Alignment.CENTER, expand=expand, opacity=0.4 if disabled else None,
        )
        return tap(box, None if disabled else on_click, tooltip=tooltip, expand=expand, col=col)

    style = ft.ButtonStyle(
        bgcolor=bg, color=fg, elevation=0, padding=pad(h_pad, 0),
        shape=ft.RoundedRectangleBorder(radius=radius),
        side=ft.BorderSide(1, side) if side else None,
        overlay_color=alpha(fg, 0.08), alignment=align,
        mouse_cursor=ft.MouseCursor.CLICK,
    )
    return ft.TextButton(content=body, on_click=on_click, style=style, height=height, width=width,
                     expand=expand, tooltip=tooltip, disabled=disabled,
                     col=col if col is not None else 12)


def icon_button(name, on_click: Handler, t: Theme, label: str, *, color: str | None = None,
                size: float = 16, width: float = 44, height: float = 40, bg: str | None = None) -> ft.TextButton:
    return button(icon(name, color or t.text, size), on_click, t, variant="ghost", height=height,
                  width=width, h_pad=0, radius=8, tooltip=label, bg=bg)


def back_link(t: Theme, on_click: Handler, label: str = "Library") -> ft.TextButton:
    return button([icon(ft.Icons.CHEVRON_LEFT, t.sub, 18), text(label, color=t.sub)], on_click, t,
                  variant="ghost", height=40, h_pad=10, radius=8)


def segmented(items: Iterable[tuple[str, str]], value: str, on_pick: Callable[[str], None], t: Theme, *,
              mono_font: bool = False, equal: bool = False, bg: str | None = None,
              item_pad: float = 14) -> ft.Container:
    """Pill-in-a-tray tab control used for filters, tabs and speed pickers."""
    buttons = []
    for key, label in items:
        on = key == value
        buttons.append(button(
            text(label, size=13, weight=600 if on else 400, color=t.text if on else t.muted,
                 kind="mono" if mono_font else "sans", no_wrap=True),
            (lambda e, k=key: on_pick(k)), t, variant="ghost", height=36, radius=7,
            h_pad=item_pad, bg=t.card2 if on else None, expand=1 if equal else None,
        ))
    return ft.Container(
        ft.Row(buttons, spacing=4, tight=not equal),
        padding=4, bgcolor=bg or t.bg, border=border(t.line), border_radius=10,
    )


def checkbox(value: bool, on_change: Callable[[bool], None] | None, t: Theme, label: str = "") -> ft.Control:
    if _NO_FOCUS:
        mark = ft.Icon(ft.Icons.CHECK_BOX if value else ft.Icons.CHECK_BOX_OUTLINE_BLANK,
                       color=t.accent if value else t.muted, size=22, semantics_label=label or None)
        return tap(ft.Container(mark, padding=4),
                   (lambda e: on_change(not value)) if on_change else None)
    return ft.Checkbox(
        value=value, semantics_label=label or None,
        on_change=(lambda e: on_change(bool(e.control.value))) if on_change else None,
        fill_color={ft.ControlState.SELECTED: t.accent, ft.ControlState.DEFAULT: "#00000000"},
        check_color=t.on, border_side=ft.BorderSide(1.5, t.muted),
    )


def toggle_card(title: str, desc: str, value: bool, on_change: Callable[[bool], None] | None, t: Theme, *,
                boxed: bool = True, title_size: float = 14, col=None, padding=None) -> ft.Container:
    body = ft.Row([
        ft.Column([text(title, weight=600, size=title_size, color=t.text),
                   text(desc, size=12, color=t.muted)], spacing=3, expand=True, tight=True),
        checkbox(value, on_change, t, title),
    ], spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER)
    return ft.Container(
        body, padding=padding or pad(16, 14) if boxed else 0, border_radius=12 if boxed else 0,
        bgcolor=t.card if boxed else None, border=border(t.line) if boxed else None,
        on_click=(lambda e: on_change(not value)) if on_change else None,
        col=col if col is not None else 12,
    )


def setting_row(label: str, control: ft.Control, t: Theme) -> ft.Row:
    return ft.Row([text(label, color=t.sub, expand=True), control], spacing=12,
                  vertical_alignment=ft.CrossAxisAlignment.CENTER)


def dropdown(value: str, options: Sequence[tuple[str, str]], on_change: Callable[[str], None] | None,
             t: Theme, *, width: float | None = None, label: str | None = None) -> ft.Dropdown:
    return ft.Dropdown(
        value=value, options=[ft.DropdownOption(key=k, text=v) for k, v in options],
        on_select=(lambda e: on_change(e.control.value)) if on_change else None,
        width=width, dense=True, filled=True, fill_color=t.card, bgcolor=t.card,
        border_color=t.line2, border_radius=8, color=t.text, label=label,
        text_style=ft.TextStyle(size=14, color=t.text, font_family=family()),
        content_padding=pad(10, 0),
    )


# ---------------------------------------------------------------- key names
_KEY_LABELS = {
    "Arrow Up": "↑", "Arrow Down": "↓", "Arrow Left": "←", "Arrow Right": "→",
    "Bracket Left": "[", "Bracket Right": "]", "": "—",
}


def key_label(key: str) -> str:
    return _KEY_LABELS.get(key, key)
