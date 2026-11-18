"""Settings: Controls (key rebinding, hotkeys) plus Audio and Paths & saves sections."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import flet as ft

from src.ui import components as c
from src.ui.data import samples
from src.ui.data.settings import CONSOLE_BUTTONS, GAMEPAD_LAYOUT, GBA_ONLY, HOTKEYS, Settings, data_dir
from src.ui.views.player import norm_key

if TYPE_CHECKING:
    from src.ui.app import App

# Flet key names -> how we store them (keeps "W" not "w", "]" not "Bracket Right").
_STORE = {"BRACKET RIGHT": "]", "BRACKET LEFT": "["}


def _stored(key: str) -> str:
    k = key.strip()
    up = k.upper()
    if up in _STORE:
        return _STORE[up]
    return up if len(k) == 1 else k


def build(app: "App") -> ft.Control:
    t, ui, wide = app.t, app.ui, app.wide
    if ui.draft_keyboard is None:
        ui.draft_keyboard = {k: list(v) for k, v in app.settings.keyboard.items()}
        ui.draft_hotkeys = dict(app.settings.hotkeys)

    def pick_section(name: str) -> None:
        if name == "Video":
            app.go("appearance")
            return
        ui.settings_section = name
        ui.listening = None
        app.render()

    nav = [c.nav_item(t, s, (lambda e, n=s: pick_section(n)), selected=ui.settings_section == s)
           for s in samples.SETTINGS_SECTIONS]
    side_controls = [
        c.back_link(t, lambda e: app.go("library"), "Back to library"),
        ft.Container(c.heading("Settings", t, 24), padding=c.pad(10, 0)),
    ]
    if wide:
        side_controls.append(ft.Column(nav, spacing=4))
    else:
        side_controls.append(ft.Row([c.segmented([(s, s) for s in samples.SETTINGS_SECTIONS],
                                                 ui.settings_section, pick_section, t, item_pad=12)],
                                    scroll=ft.ScrollMode.AUTO))
    side = c.sidebar(t, side_controls, wide=wide, spacing=20)

    section = ui.settings_section
    if section == "Controls":
        content = _controls(app)
    elif section == "Audio":
        content = _audio(app)
    elif section == "Paths & saves":
        content = _paths(app)
    else:
        content = [
            c.heading(section, t, 28),
            c.text("Nothing to configure here yet.", color=t.muted),
        ]

    main = ft.Container(
        ft.Column(content, spacing=28, scroll=ft.ScrollMode.AUTO, expand=True),
        padding=ft.Padding.only(left=48, right=48, top=32, bottom=40) if wide else c.pad(16, 20),
        expand=True,
    )
    main_wrap = ft.Row([ft.Container(main, width=min(1040, app.width - c.SIDEBAR_W), expand=False)], expand=True,
                       vertical_alignment=ft.CrossAxisAlignment.STRETCH) if wide else main
    body = ft.Row([side, main_wrap], spacing=0, expand=True,
                  vertical_alignment=ft.CrossAxisAlignment.STRETCH) if wide else ft.Column([side, main], expand=True,
                                                                                         spacing=0)

    def on_key(e) -> None:
        if not ui.listening:
            return
        if norm_key(e.key) == "ESCAPE":
            ui.listening = None
            app.render()
            return
        key = _stored(e.key)
        kind, ident, col = ui.listening
        if kind == "button":
            row = ui.draft_keyboard[ident]
            # A key drives one console button: clear it from anywhere else first.
            for other in ui.draft_keyboard.values():
                for i, k in enumerate(other):
                    if k == key:
                        other[i] = ""
            row[col] = key
        else:
            for hid, k in ui.draft_hotkeys.items():
                if k == key:
                    ui.draft_hotkeys[hid] = ""
            ui.draft_hotkeys[ident] = key
        ui.listening = None
        app.render()

    return ft.KeyboardListener(ft.Container(body, bgcolor=t.bg, expand=True), autofocus=True, on_key_down=on_key)


# -------------------------------------------------------------------- controls
def _controls(app: "App") -> list[ft.Control]:
    t, ui = app.t, app.ui
    kb = ui.settings_device == "kb"

    def listen(target) -> None:
        ui.listening = None if ui.listening == target else target
        app.render()

    def save(e) -> None:
        app.settings.keyboard = {k: list(v) for k, v in ui.draft_keyboard.items()}
        app.settings.hotkeys = dict(ui.draft_hotkeys)
        app.settings.save()
        app.toast("Controls saved")
        app.go("library")

    def reset(e) -> None:
        d = Settings()
        ui.draft_keyboard = d.keyboard
        ui.draft_hotkeys = d.hotkeys
        ui.listening = None
        app.render()

    def cancel(e) -> None:
        app.go("library")

    profile = ft.Column([
        c.text("Profile", size=12, color=t.muted),
        c.dropdown("kb1", [("kb1", "Keyboard — Player 1"), ("xbox", "Xbox controller"), ("ds", "DualSense")],
                   lambda v: None, t, width=240),
    ], spacing=6, tight=True)
    head = c.bar(
        ft.Column([c.heading("Controls", t, 28),
                   c.text("Click a binding, then press a key. Esc cancels." if kb else
                          _pad_status(), color=t.muted)], spacing=6, tight=True),
        [profile], wide=app.wide, align=ft.CrossAxisAlignment.END)

    devices = c.segmented([("kb", "Keyboard"), ("pad", "Gamepad")], ui.settings_device,
                          lambda v: (setattr(ui, "settings_device", v), setattr(ui, "listening", None), app.render()),
                          t, bg=t.panel, item_pad=16)

    def chip(label: str, target, primary: bool) -> ft.Control:
        on = ui.listening == target
        if on:
            bg, fg, side = t.soft, t.accent, t.accent
        elif primary:
            bg, fg, side = t.card2, t.text, "#2E3340" if t.key != "daylight" else t.line2
        else:
            bg, fg, side = None, t.muted, "#2E3340" if t.key != "daylight" else t.line2
        return c.button(c.mono("Press a key…" if on else c.key_label(label), size=12, color=fg),
                        (lambda e: listen(target)) if kb else None, t, variant="ghost", height=36,
                        h_pad=12, radius=8, bg=bg, side=side, width=None)

    def cell(control: ft.Control, flex: int) -> ft.Container:
        return ft.Container(control, expand=flex, alignment=ft.Alignment.CENTER_LEFT)

    header_row = ft.Container(ft.Row([
        cell(c.eyebrow("Console button", t), 14), cell(c.eyebrow("Primary", t), 10),
        cell(c.eyebrow("Secondary", t), 10),
    ]), padding=c.pad(20, 12), border=ft.Border.only(bottom=ft.BorderSide(1, t.line)))
    rows: list[ft.Control] = [header_row]
    divider = "#1E222B" if t.key == "midnight" else t.line
    for i, btn in enumerate(CONSOLE_BUTTONS):
        keys = ui.draft_keyboard[btn] if kb else GAMEPAD_LAYOUT[btn]
        name = ft.Row([c.text(btn, color=t.text)] + (
            [c.pill("GBA", "#232A45" if t.key != "daylight" else t.soft,
                    "#B7C3FF" if t.key != "daylight" else t.accent, size=10, radius=4, padding=c.pad(6, 2))]
            if btn in GBA_ONLY else []), spacing=10)
        rows.append(ft.Container(ft.Row([
            cell(name, 14),
            cell(chip(keys[0], ("button", btn, 0), True), 10),
            cell(chip(keys[1], ("button", btn, 1), False), 10),
        ], vertical_alignment=ft.CrossAxisAlignment.CENTER), padding=c.pad(20, 6),
            border=ft.Border.only(bottom=ft.BorderSide(1, divider)) if i < len(CONSOLE_BUTTONS) - 1 else None))
    table = ft.Container(ft.Column(rows, spacing=0), bgcolor=t.card, border=c.border(t.line), border_radius=14)
    if not app.wide:
        table = ft.Row([ft.Container(table, width=560)], scroll=ft.ScrollMode.AUTO)

    hk_cards = []
    for hid, (label, _) in HOTKEYS.items():
        target = ("hotkey", hid, 0)
        on = ui.listening == target
        key = ui.draft_hotkeys.get(hid, "")
        hk_cards.append(c.button(ft.Row([
            c.text(label, color=t.sub, expand=True, no_wrap=True),
            c.pill("Press a key…" if on else c.key_label(key), t.soft if on else t.card2,
                   t.accent if on else t.text, size=12, weight=400, padding=c.pad(8, 4)),
        ], spacing=12), lambda e, tg=target: listen(tg), t, variant="ghost", height=52, h_pad=14,
            bg=t.card, side=t.accent if on else t.line, align=ft.Alignment.CENTER_LEFT))
    cols = c.columns_for(app.width - (c.SIDEBAR_W + 96 if app.wide else 32), 240, 12, 3)
    hotkeys = ft.Column([c.heading("Emulator hotkeys", t, 17), c.grid(hk_cards, cols, gap=12)], spacing=12)

    footer = ft.Row([
        c.button(c.text("Reset to defaults", color=t.sub), reset, t, variant="ghost", h_pad=16),
        ft.Container(expand=True),
        c.button("Cancel", cancel, t),
        c.button("Save changes", save, t, variant="primary", h_pad=20),
    ], spacing=10, wrap=True)
    return [head, devices, table, hotkeys, ft.Container(footer, padding=ft.Padding.only(top=8))]


# -------------------------------------------------------------------- audio
def _audio(app: "App") -> list[ft.Control]:
    t, s = app.t, app.settings

    def on_volume(e) -> None:
        s.volume = int(e.control.value)

    return [
        ft.Column([c.heading("Audio", t, 28),
                   c.text("Sound plays through SDL on desktop. Mobile and web builds run silent for now.",
                          color=t.muted)], spacing=6),
        c.card(c.setting_row("Volume", ft.Slider(value=s.volume, min=0, max=100, divisions=20, width=260,
                                                 label="{value}", active_color=t.accent, inactive_color=t.line2,
                                                 on_change=on_volume, on_change_end=lambda e: s.save()), t),
               t, padding=c.pad(16, 10), radius=12),
    ]


# -------------------------------------------------------------------- paths
def _paths(app: "App") -> list[ft.Control]:
    t, s = app.t, app.settings

    async def add_folder(e) -> None:
        if app.is_web:
            app.toast("Folders can be added in the desktop app.")
            return
        path = await ft.FilePicker().get_directory_path(dialog_title="Add a ROM folder")
        if path and path not in s.rom_dirs:
            s.rom_dirs.append(path)
            s.save()
            app.rescan()
            app.render()

    def remove(path: str) -> None:
        s.rom_dirs.remove(path)
        s.save()
        app.rescan()
        app.render()

    rows = []
    for d in s.rom_dirs:
        exists = Path(d).expanduser().is_dir()
        rows.append(c.card(ft.Row([
            c.icon(ft.Icons.FOLDER_OPEN, t.muted, 18),
            ft.Column([c.mono(d, size=13, color=t.text),
                       c.text("Scanned for .gb, .gbc and .gba files" if exists else "Folder not found",
                              size=12, color=t.muted)], spacing=2, tight=True, expand=True),
            c.icon_button(ft.Icons.CLOSE, lambda e, p=d: remove(p), t, "Remove folder"),
        ], spacing=12), t, padding=c.pad(14, 10), radius=12))
    if not rows:
        rows.append(c.text("No folders yet.", color=t.muted))
    return [
        ft.Column([c.heading("Paths & saves", t, 28),
                   c.text("Battery saves (.sav) are written next to each ROM.", color=t.muted)], spacing=6),
        c.eyebrow("ROM folders", t),
        ft.Column(rows, spacing=10),
        ft.Row([c.button("Add folder", add_folder, t, variant="primary", icon_name=ft.Icons.ADD),
                c.button("Open app data", lambda e: app.open_folder(data_dir()), t)], spacing=10, wrap=True),
        c.text(f"Settings, thumbnails and moments: {data_dir()}", size=12, color=t.muted, selectable=True),
    ]


def _pad_status() -> str:
    """Connected-controller line for the Gamepad tab."""
    try:
        from src.ui.emulator import session
        pads = session._GAMEPADS
        if pads is None:
            from src.bridge.adaptors import GamepadAdaptor, InputRouter
            pads = session._GAMEPADS = GamepadAdaptor(InputRouter(None))
        if not pads.available:
            return "Gamepads need the desktop app (SDL game-controller support)."
        pads._scan()
        names = pads.names
    except Exception as e:
        return f"Gamepad support unavailable: {e}"
    if not names:
        return "No controller connected. Plug one in; this is the standard layout."
    return "Connected: " + ", ".join(names) + ". Standard layout:"

