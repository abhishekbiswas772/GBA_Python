"""Moments: gallery of screenshots and recordings / clips (MP4 with sound, or GIF)."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from src.ui import components as c
from src.ui.data.moments import list_moments, moments_dir, poster_for

if TYPE_CHECKING:
    from src.ui.app import App

FILTERS = [("all", "All"), ("video", "Recordings & clips"), ("SHOT", "Screenshots")]
VIDEO = ("CLIP", "GIF")


def _matches(m, f: str) -> bool:
    if f == "all":
        return True
    if f in ("video", "GIF", "CLIP"):
        return m.kind in VIDEO
    return m.kind == f


def build(app: "App") -> ft.Control:
    t, ui, wide = app.t, app.ui, app.wide
    everything = list_moments()
    items = [m for m in everything if _matches(m, ui.moments_filter)]
    sel = next((m for m in everything if m.path == ui.moments_sel), None) or (items[0] if items else None)

    def pick(path: str) -> None:
        ui.moments_sel = path
        app.render()

    header = c.page_header(
        t, back=lambda e: app.go("library"), title="Moments", wide=wide,
        subtitle=(f"Screenshots ({app.settings.hotkeys['screenshot']}), recordings "
                  f"({app.settings.hotkeys.get('record', '')}) and clips of the last 30 s "
                  f"({app.settings.hotkeys.get('clip', '')})."),
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
                c.screen_placeholder(t, width=thumb_w, aspect=3 / 2, radius=8, image=m.image),
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
            f"While playing: {app.settings.hotkeys['screenshot']} takes a screenshot, Record makes a video with sound, "
            "and Clip last 30s saves what just happened.", icon_name=ft.Icons.PHOTO_CAMERA_OUTLINED)

    main = ft.Container(ft.Column([gallery], scroll=ft.ScrollMode.AUTO if wide else None, expand=wide),
                        padding=c.pad(32 if wide else 16, 24), expand=wide)
    side = c.side_panel(t, _editor(app, sel), width=400, wide=wide, padding=24, fits=app.height >= 1000)
    body = c.split(main, side, wide=wide)
    return ft.Container(ft.Column([header, ft.Container(body, expand=True)], spacing=0, expand=True),
                        bgcolor=t.bg, expand=True)


def _editor(app: "App", sel) -> list[ft.Control]:
    t = app.t
    if sel is None:
        return [c.heading("Export & share", t, 20),
                c.text("Select a moment to export or copy it.", size=13, color=t.muted)]
    src = Path(sel.path)
    ext = src.suffix.lower().lstrip(".")

    async def export(e) -> None:
        if app.is_web:
            app.toast("Exporting needs the desktop or mobile app.")
            return
        dest = await ft.FilePicker().save_file(
            dialog_title="Export moment", file_name=src.name, allowed_extensions=[ext],
            file_type=ft.FilePickerFileType.CUSTOM, src_bytes=src.read_bytes() if app.is_mobile else None)
        if dest and not app.is_mobile:
            shutil.copyfile(src, dest)
            app.toast(f"Exported to {dest}")

    async def copy_image(e) -> None:
        try:
            await ft.Clipboard().set_image(src.read_bytes())
            app.toast("Copied to clipboard")
        except Exception as err:
            app.toast(f"Couldn't copy: {err}")

    def delete(e) -> None:
        try:
            src.unlink()
            poster_for(src).unlink(missing_ok=True)
        except OSError as err:
            app.toast(f"Couldn't delete: {err}")
            return
        app.ui.moments_sel = None
        app.render()

    panel_w = 352 if app.wide else app.width - 48
    preview = c.screen_placeholder(t, f"Preview · {sel.length}", width=panel_w, aspect=3 / 2, radius=4,
                                   image=sel.image)
    try:
        size_kb = src.stat().st_size / 1024
    except OSError:
        size_kb = 0
    facts = ft.Row([c.mono(ext.upper(), t), c.mono(f"{size_kb:,.0f} KB", t),
                    c.mono({"GIF": "animated, no sound", "CLIP": "H.264 + AAC sound"}.get(sel.kind, "still image"),
                           t)],
                   alignment=ft.MainAxisAlignment.SPACE_BETWEEN)
    share = ft.Column([
        c.eyebrow("Share", t, size=12),
        ft.Row([c.button("Play", lambda e: app.open_folder(src), t, icon_name=ft.Icons.PLAY_ARROW, expand=1,
                         h_pad=8) if sel.kind == "CLIP" else
                c.button("Copy to clipboard", copy_image, t, icon_name=ft.Icons.CONTENT_COPY, expand=1, h_pad=8),
                c.button("Show in folder", lambda e: app.open_folder(src.parent), t,
                         icon_name=ft.Icons.FOLDER_OPEN, expand=1, h_pad=8)], spacing=8),
        c.button("Delete", delete, t, variant="ghost", icon_name=ft.Icons.DELETE_OUTLINE),
        c.button(f"Export {ext.upper()}", export, t, variant="primary", height=48, icon_name=ft.Icons.DOWNLOAD),
    ], spacing=10, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
    return [
        ft.Column([c.heading(sel.title, t, 20), c.text(sel.meta, size=13, color=t.muted)], spacing=4),
        preview, facts, c.spacer(), share,
    ]
