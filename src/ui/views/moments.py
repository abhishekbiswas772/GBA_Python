"""Moments: gallery of screenshots/clips with a trim, export and share panel."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from src.ui import components as c
from src.ui.data.moments import list_moments, moments_dir

if TYPE_CHECKING:
    from src.ui.app import App

FILTERS = [("all", "All"), ("CLIP", "Clips"), ("GIF", "GIFs"), ("SHOT", "Screenshots")]
SIZES = [("4x", "4× pixel-perfect (640×576)"), ("1080p", "1080p"), ("vertical", "Vertical 9:16 for Shorts / Reels"),
         ("native", "Native 160×144")]


def build(app: "App") -> ft.Control:
    t, ui, wide = app.t, app.ui, app.wide
    everything = list_moments()
    items = [m for m in everything if ui.moments_filter == "all" or m.kind == ui.moments_filter]
    sel = next((m for m in everything if m.path == ui.moments_sel), None) or (items[0] if items else None)

    def pick(path: str) -> None:
        ui.moments_sel = path
        app.render()

    header = c.page_header(
        t, back=lambda e: app.go("library"), title="Moments", wide=wide,
        subtitle=f"Clips, GIFs and screenshots. Press {app.settings.hotkeys['screenshot']} in-game for a screenshot.",
        trailing=[
            c.segmented(FILTERS, ui.moments_filter, lambda v: (setattr(ui, "moments_filter", v), app.render()), t),
            c.button("Open folder", lambda e: app.open_folder(moments_dir()), t, h_pad=16),
        ],
    )

    main_w = app.width - (400 if wide else 0) - (64 if wide else 32)
    if items:
        thumb_w = c.cell_width(main_w, 220, 18) - 16
        cards = []
        for m in items:
            on = sel is not None and m.path == sel.path
            thumb = ft.Stack([
                c.screen_placeholder(t, width=thumb_w, aspect=3 / 2, radius=8, image=m.path if m.kind == "SHOT" else None),
                ft.Container(c.mono(m.kind, size=10, weight=500, color="#FFFFFF"), left=8, top=8,
                             bgcolor=c.alpha("#000000", 0.72), border_radius=4, padding=c.pad(6, 2)),
                ft.Container(c.mono(m.length, size=11, color="#FFFFFF"), right=8, bottom=8,
                             bgcolor=c.alpha("#000000", 0.72), border_radius=4, padding=c.pad(6, 2)),
            ], width=thumb_w, height=thumb_w * 2 / 3)
            cards.append(ft.Container(
                ft.Column([thumb, ft.Container(ft.Column([
                    c.text(m.title, size=14, weight=600, color=t.text, no_wrap=True),
                    c.text(m.meta, size=12, color=t.muted, no_wrap=True),
                ], spacing=3), padding=ft.Padding.only(left=4, right=4, top=10, bottom=2))], spacing=0),
                padding=8, border_radius=12, bgcolor=t.card, border=c.border(t.accent if on else t.line, 2),
                on_click=lambda e, p=m.path: pick(p), ink=True,
            ))
        gallery: ft.Control = c.auto_grid(cards, main_w, 220, gap=18)
    else:
        gallery = c.empty_state(
            t, "No moments yet" if not everything else "Nothing in this filter",
            f"Take a screenshot with {app.settings.hotkeys['screenshot']} or the camera button while playing. "
            "Clip and GIF capture are coming with the recorder.", icon_name=ft.Icons.PHOTO_CAMERA_OUTLINED)

    main = ft.Container(ft.Column([gallery], scroll=ft.ScrollMode.AUTO if wide else None, expand=wide),
                        padding=c.pad(32 if wide else 16, 24), expand=wide)
    side = c.side_panel(t, _editor(app, sel), width=400, wide=wide, padding=24, fits=app.height >= 1000)
    body = c.split(main, side, wide=wide)
    return ft.Container(ft.Column([header, ft.Container(body, expand=True)], spacing=0, expand=True),
                        bgcolor=t.bg, expand=True)


def _editor(app: "App", sel) -> list[ft.Control]:
    t, ui = app.t, app.ui
    if sel is None:
        return [c.heading("Edit & share", t, 20),
                c.text("Select a moment to trim, export or share it.", size=13, color=t.muted)]

    def flip(attr: str):
        def handler(v: bool) -> None:
            setattr(ui, attr, v)
            app.render()
        return handler

    async def export(e) -> None:
        fmt = ui.moments_fmt
        if sel.kind != "SHOT" or fmt != "PNG":
            app.not_ready(f"{fmt} export" if sel.kind == "SHOT" else "Clip export")
            return
        if app.is_web:
            app.toast("Exporting needs the desktop or mobile app.")
            return
        src = Path(sel.path)
        dest = await ft.FilePicker().save_file(
            dialog_title="Export moment", file_name=src.name, allowed_extensions=["png"],
            file_type=ft.FilePickerFileType.CUSTOM, src_bytes=src.read_bytes() if app.is_mobile else None)
        if dest and not app.is_mobile:
            shutil.copyfile(src, dest)
            app.toast(f"Exported to {dest}")

    async def copy_image(e) -> None:
        if sel.kind != "SHOT":
            app.not_ready("Copying clips")
            return
        try:
            await ft.Clipboard().set_image(Path(sel.path).read_bytes())
            app.toast("Copied to clipboard")
        except Exception as err:
            app.toast(f"Couldn't copy: {err}")

    panel_w = 352 if app.wide else app.width - 48
    preview_w = panel_w - (68 if ui.moments_frame else 0)

    def preview() -> ft.Control:
        screen = c.screen_placeholder(t, f"Preview · {sel.length}", width=preview_w, aspect=3 / 2, radius=4,
                                      image=sel.path if sel.kind == "SHOT" else None)
        parts: list[ft.Control] = [screen]
        if ui.moments_inputs and sel.kind != "SHOT":
            chip = lambda k, on: c.pill(k, t.accent if on else t.card2, t.on if on else t.text, size=11,  # noqa: E731
                                        weight=400, radius=4, padding=c.pad(8, 3))
            parts.append(ft.Row([chip("←", False), chip("A", True), chip("B", False), chip("R", True)],
                                spacing=6, alignment=ft.MainAxisAlignment.CENTER))
        col = ft.Column(parts, spacing=8)
        if ui.moments_frame:
            return ft.Container(col, padding=ft.Padding.only(left=34, right=34, top=22, bottom=18),
                                bgcolor=t.shell,
                                border_radius=ft.BorderRadius.only(top_left=18, top_right=18, bottom_left=18,
                                                                   bottom_right=40))
        return col

    trim = ft.Column([
        ft.Row([c.mono("In 00:00.0", t), c.mono("Out —", t), c.mono("Still image" if sel.kind == "SHOT" else "—", t)],
               alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Container(ft.Stack([
            ft.Container(left=0, right=0, top=0, bottom=0, bgcolor=t.soft, border=c.border(t.accent, 2),
                         border_radius=6),
            ft.Container(width=8, left=-1, top=6, bottom=6, bgcolor=t.accent, border_radius=3),
            ft.Container(width=8, right=-1, top=6, bottom=6, bgcolor=t.accent, border_radius=3),
        ]), height=44, bgcolor=t.card, border=c.border(t.line), border_radius=8,
            tooltip="Trimming arrives with clip recording"),
    ], spacing=8)

    export_box = ft.Column([
        c.eyebrow("Export as", t, size=12),
        c.segmented([(f, f) for f in ("MP4", "GIF", "WebM", "PNG")], ui.moments_fmt,
                    lambda v: (setattr(ui, "moments_fmt", v), app.render()), t, mono_font=True, equal=True),
        c.setting_row("Size", c.dropdown(ui.moments_size, SIZES, lambda v: setattr(ui, "moments_size", v), t,
                                         width=230), t),
        c.setting_row("Show button presses", c.checkbox(ui.moments_inputs, flip("moments_inputs"), t), t),
        c.setting_row("Handheld frame around video", c.checkbox(ui.moments_frame, flip("moments_frame"), t), t),
        c.setting_row("Include game audio", c.checkbox(ui.moments_audio, flip("moments_audio"), t), t),
    ], spacing=10)

    def share_btn(label, handler, icon_name=None):
        return c.button(label, handler, t, icon_name=icon_name, expand=1, h_pad=8)

    share = ft.Column([
        c.eyebrow("Share", t, size=12),
        ft.Row([share_btn("Copy link", lambda e: app.not_ready("Share links"), ft.Icons.LINK),
                share_btn("Copy to clipboard", copy_image, ft.Icons.CONTENT_COPY)], spacing=8),
        ft.Row([share_btn("Discord", lambda e: app.not_ready("Discord sharing")),
                share_btn("X / Reddit", lambda e: app.not_ready("Social sharing"))], spacing=8),
        c.button(f"Export {ui.moments_fmt}", export, t, variant="primary", height=48, icon_name=ft.Icons.DOWNLOAD),
    ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)

    out = [
        ft.Column([c.heading(sel.title, t, 20), c.text(sel.meta, size=13, color=t.muted)], spacing=4),
        preview(), trim, export_box,
    ]
    out += [c.spacer(), share]
    return out
