"""In-game player: toolbar, game screen with overlays, session panel and status bar."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import flet as ft

from src.bridge.adaptors import KeyboardAdaptor, normalize_key
from src.ui import components as c
from src.ui.data import library as lib
from src.ui.data import samples
from src.ui.emulator import frame as fr
from src.ui.theme import SHADER_BY_KEY
from src.ui.theme.tokens import OVERLAY_BG, OVERLAY_SUB, OVERLAY_TEXT, SCREEN_LABEL

if TYPE_CHECKING:
    from src.ui.app import App

NATIVE = {"GB": (160, 144), "GBC": (160, 144), "GBA": (240, 160), "NES": (256, 240)}
VISIBLE_SLOTS = (1, 2, 3, 4)          # slot 0 is the resume point saved when you leave


def norm_key(key: str) -> str:
    """Key names as the settings screen stores them (same rules as the keyboard adaptor)."""
    return normalize_key(key)


def build(app: "App") -> ft.Control:
    s = app.session
    if s is None:                        # navigated here without a game
        app.route = "library"
        from src.ui.views.library import build as library
        return library(app)
    with c.unfocusable():                # nothing on this screen may steal game keys
        listener = _build(app, s)

    async def grab_focus() -> None:      # after the page update has mounted the listener
        await asyncio.sleep(0.05)
        try:
            await listener.focus()
        except Exception:
            pass
    app.page.run_task(grab_focus)
    return listener


def _build(app: "App", s) -> ft.KeyboardListener:
    t, ui, wide, settings = app.t, app.ui, app.wide, app.settings
    g = s.game

    # ---------------------------------------------------------------- actions
    def toggle_pause(e=None) -> None:
        s.toggle_pause()
        app.render()

    def reset(e=None) -> None:
        s.reset()
        app.toast("Game reset")

    def set_speed(v: str) -> None:
        s.speed = int(v)
        app.render()

    def set_slot(n: int) -> None:
        s.slot = n
        app.render()

    def screenshot(e=None) -> None:
        path = s.screenshot()
        if path is None:
            app.toast("Nothing to capture yet")
            return
        ui.toast = str(path)
        app.render()

        async def hide(p=str(path)):
            await asyncio.sleep(5)
            if ui.toast == p and app.route == "player":
                ui.toast = None
                app.render()
        app.page.run_task(hide)

    def toggle_fullscreen(e=None) -> None:
        if app.is_mobile or app.is_web or not app.page.window:
            app.toast("Fullscreen is controlled by your device here.")
            return
        app.page.window.full_screen = not app.page.window.full_screen
        app.page.update()

    # ---------------------------------------------------------------- save states
    def quick_save(e=None) -> None:
        async def go() -> None:
            try:
                await s.save_state(s.slot)
                app.toast(f"Saved to slot {s.slot}")
            except Exception as err:
                app.toast(f"Couldn't save: {err}")
            app.render()
        app.page.run_task(go)

    def quick_load(e=None, slot: int | None = None) -> None:
        n = s.slot if slot is None else slot

        async def go() -> None:
            try:
                await s.load_state(n)
                app.toast("Loaded the resume point" if n == 0 else f"Loaded slot {n}")
            except FileNotFoundError:
                app.toast(f"Slot {n} is empty" if n else "No resume point yet")
            except Exception as err:
                app.toast(f"Couldn't load: {err}")
            app.render()
        app.page.run_task(go)

    # ---------------------------------------------------------------- rewind
    def rewind_burst(e=None) -> None:
        """Toolbar button: rewind for a moment (hold the hotkey for longer)."""
        if not s.rewind_seconds:
            app.toast("Nothing to rewind yet")
            return
        s.set_rewinding(True)

        async def stop() -> None:
            await asyncio.sleep(1.5)
            s.set_rewinding(False)
        app.page.run_task(stop)

    # ---------------------------------------------------------------- recording
    def gif_done(path, err) -> None:              # encoder thread -> UI loop
        async def show() -> None:
            if err is not None:
                app.toast(f"Couldn't save the GIF: {err}")
                return
            ui.toast = str(path)
            if app.route == "player":
                app.render()
            else:
                app.toast("GIF saved to Moments")
        try:
            app.page.run_task(show)
        except Exception:
            pass

    def toggle_record(e=None) -> None:
        if s.recording:
            s.stop_recording(on_done=gif_done)
            app.toast("Recording saved; encoding the GIF…")
        else:
            s.start_recording()
            app.toast("Recording… press again to stop")
            app.page.run_task(record_timer)
        app.render()

    async def record_timer() -> None:
        while s.recording and app.session is s:
            secs = int(s.runner.recorder.recorded_seconds)
            rec_label.value = f"Stop {secs // 60}:{secs % 60:02d}"
            try:
                rec_label.update()
            except Exception:
                pass
            await asyncio.sleep(0.5)

    def clip(e=None) -> None:
        if not settings.replay_buffer:
            app.toast("Turn on the replay buffer to clip the last 30 s")
            return
        if s.clip(30, on_done=gif_done):
            app.toast("Saving the last 30 s as a GIF…")
        else:
            app.toast("Nothing to clip yet")

    def open_moment(e=None) -> None:
        ui.moments_sel = ui.toast
        ui.toast = None
        app.go("moments")

    # ---------------------------------------------------------------- keyboard
    layout = settings.keyboard_for(g.system)     # GB, GBC and GBA each have their own keys
    keys = getattr(s, "keys", None)              # one adaptor per session: re-renders keep held keys
    if keys is None:
        keys = s.keys = KeyboardAdaptor(s.input, layout)
    else:
        keys.set_mapping(layout)
    key_to_hot = {norm_key(v): k for k, v in settings.hotkeys.items() if v}
    hot_actions = {
        "pause": toggle_pause, "screenshot": screenshot, "fullscreen": toggle_fullscreen,
        "rewind": lambda: s.set_rewinding(True),
        "quick_save": quick_save,
        "quick_load": quick_load,
        "record": toggle_record,
        "clip": clip,
        "next_slot": lambda: set_slot(s.slot % 4 + 1),
        "prev_slot": lambda: set_slot((s.slot - 2) % 4 + 1),
    }

    def on_key_down(e) -> None:
        k = norm_key(e.key)
        if keys.key_down(e.key):
            return
        act = key_to_hot.get(k)
        if act == "fast_forward":
            s.fast_forward = True
        elif act == "rewind":
            if not s.rewinding:
                s.set_rewinding(True)
        elif act in hot_actions:
            hot_actions[act]()

    def on_key_up(e) -> None:
        k = norm_key(e.key)
        if keys.key_up(e.key):
            return
        act = key_to_hot.get(k)
        if act == "fast_forward":
            s.fast_forward = False
        elif act == "rewind":
            s.set_rewinding(False)

    # ---------------------------------------------------------------- header
    def tool(icon_name, label, handler):
        return c.icon_button(icon_name, handler, t, label, size=18)

    rec_color = t.rec
    rec_label = c.text("Stop 0:00" if s.recording else "Record", size=13, weight=600, kind="mono",
                       color=rec_color if s.recording else t.text)
    if s.recording:
        app.page.run_task(record_timer)
    toolbar = ft.Container(ft.Row([
        tool(ft.Icons.PLAY_ARROW if s.paused else ft.Icons.PAUSE, "Resume" if s.paused else "Pause", toggle_pause),
        tool(ft.Icons.FAST_REWIND, f"Rewind (hold {settings.hotkeys['rewind']})", rewind_burst),
        tool(ft.Icons.FAST_FORWARD, f"Fast forward (hold {settings.hotkeys['fast_forward']})",
             lambda e: set_speed("1" if s.speed > 1 else "2")),
        tool(ft.Icons.RESTART_ALT, "Reset", reset),
        c.divider_v(t),
        tool(ft.Icons.PHOTO_CAMERA_OUTLINED, f"Screenshot ({settings.hotkeys['screenshot']})", screenshot),
        c.button([c.dot(rec_color, 10), rec_label],
                 toggle_record, t, variant="ghost", height=40, h_pad=12, radius=8,
                 bg=c.alpha(rec_color, 0.14) if s.recording else None),
        c.button([c.icon(ft.Icons.HISTORY, t.text, 16), c.text("Clip last 30s", size=13, weight=600, color=t.text),
                  c.mono(settings.hotkeys.get("clip", ""), t, size=11)],
                 clip, t, variant="ghost", height=40, h_pad=12, radius=8),
        c.divider_v(t),
        tool(ft.Icons.FULLSCREEN, f"Fullscreen ({settings.hotkeys['fullscreen']})", toggle_fullscreen),
    ], spacing=2, tight=True, wrap=not wide), padding=4, bgcolor=t.bg, border=c.border(t.line), border_radius=10)

    title = ft.Row([c.text(g.title, size=16, weight=600, kind="display", color=t.text, no_wrap=True),
                    c.system_badge(g.system, t)], spacing=10, tight=True)
    header = ft.Container(
        c.bar(ft.Row([c.back_link(t, lambda e: app.go("library")), title], spacing=14),
              [toolbar] if wide else [ft.Row([toolbar], scroll=ft.ScrollMode.AUTO)], wide=wide, spacing=14),
        padding=c.pad(20 if wide else 12, 12), bgcolor=t.panel,
        border=ft.Border.only(bottom=ft.BorderSide(1, t.line)),
    )

    # ---------------------------------------------------------------- screen
    nw, nh = NATIVE[g.system]
    side_w = 320
    if wide:
        avail_w = app.width - side_w - 64
        avail_h = app.height - 64 - 41 - 64
    else:
        avail_w = app.width - 24
        avail_h = app.height * (0.42 if app.touch_controls else 0.6)
    scale = c.fit_scale(min(avail_w, 960), avail_h, nw, nh, settings.integer_scale)
    sw, sh = nw * scale, nh * scale

    layers: list[ft.Control] = [ft.Container(s.image, bgcolor=t.screen, left=0, top=0, right=0, bottom=0)]
    overlay = fr.shader_overlay(settings.shader, nw, nh) if g.playable else None
    if overlay:
        layers.append(ft.Image(src=overlay, fit=ft.BoxFit.FILL, left=0, top=0, right=0, bottom=0,
                               filter_quality=ft.FilterQuality.MEDIUM))
    if s.error or not g.playable:
        layers.append(ft.Container(
            ft.Column([
                c.mono("Game output", size=13, color=SCREEN_LABEL),
                c.mono(f"{nw}×{nh} · {SHADER_BY_KEY[settings.shader].name}", size=11, color=SCREEN_LABEL),
                ft.Container(height=8),
                c.text(s.error or "", size=14, color=OVERLAY_TEXT, align=ft.TextAlign.CENTER),
            ], spacing=6, horizontal_alignment=ft.CrossAxisAlignment.CENTER, tight=True),
            left=0, top=0, right=0, bottom=0, alignment=ft.Alignment.CENTER, padding=24,
        ))
    if ui.toast:
        toast = ft.Container(
            ft.Row([
                ft.Column([c.text("Moment saved", size=13, weight=600, color=OVERLAY_TEXT),
                           c.text(f"Screenshot · {g.title}", size=12, color=OVERLAY_SUB)], spacing=2, tight=True),
                c.button(c.text("Open", size=13, color=OVERLAY_TEXT), open_moment, t, height=36, h_pad=14,
                         radius=8, side=c.alpha("#FFFFFF", 0.2)),
                c.button(c.text("Share", size=13, weight=600, color="#0F1115"), open_moment, t, height=36,
                         h_pad=14, radius=8, bg=OVERLAY_TEXT, variant="ghost"),
            ], spacing=14, tight=True),
            padding=ft.Padding.only(left=16, right=10, top=10, bottom=10),
            bgcolor=c.alpha(OVERLAY_BG, 0.92), border=c.border(c.alpha("#FFFFFF", 0.12)), border_radius=12,
        )
        layers.append(ft.Container(ft.Row([toast], alignment=ft.MainAxisAlignment.CENTER),
                                   bottom=20, left=0, right=0))
    if s.paused and not s.error:
        layers.append(ft.Container(
            ft.Column([
                c.heading("Paused", t, 32, color=OVERLAY_TEXT),
                ft.Row([
                    c.button(c.text("Resume", weight=600, color="#0F1115"), toggle_pause, t, variant="ghost",
                             bg=OVERLAY_TEXT, h_pad=20),
                    c.button(c.text("Screen & theme", color=OVERLAY_TEXT), lambda e: _leave(app, "appearance"), t,
                             side=c.alpha("#FFFFFF", 0.25), h_pad=20),
                    c.button(c.text("Controls", color=OVERLAY_TEXT), lambda e: _leave(app, "settings"), t,
                             side=c.alpha("#FFFFFF", 0.25), h_pad=20),
                ], spacing=10, wrap=True, alignment=ft.MainAxisAlignment.CENTER),
            ], spacing=16, horizontal_alignment=ft.CrossAxisAlignment.CENTER, alignment=ft.MainAxisAlignment.CENTER),
            left=0, top=0, right=0, bottom=0, bgcolor=c.alpha("#0B0D10", 0.74), border_radius=6,
        ))
    screen = ft.Container(
        ft.Stack(layers, width=sw, height=sh), width=sw, height=sh, border_radius=6,
        clip_behavior=ft.ClipBehavior.HARD_EDGE, bgcolor=t.screen,
        shadow=[ft.BoxShadow(spread_radius=1, color=t.line),
                ft.BoxShadow(blur_radius=60, offset=ft.Offset(0, 24), color=c.alpha("#000000", 0.45))],
    )
    stage_items: list[ft.Control] = [screen]
    if app.touch_controls and g.playable:
        stage_items.append(_touch_pad(app))
    stage = ft.Container(
        ft.Column(stage_items, spacing=20, horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                  alignment=ft.MainAxisAlignment.CENTER, tight=not wide),
        alignment=ft.Alignment.CENTER, padding=32 if wide else 12, expand=wide,
    )

    # ---------------------------------------------------------------- side panel
    tabs = c.segmented([("saves", "Saves"), ("cheats", "Cheats"), ("link", "Link")], ui.player_tab,
                       lambda v: (setattr(ui, "player_tab", v), app.render()), t, equal=True)
    panel: list[ft.Control] = [tabs]
    if ui.player_tab == "saves":
        panel.append(_saves(app, set_slot, set_speed, quick_save, quick_load))
    elif ui.player_tab == "cheats":
        panel.append(_cheats(app))
    else:
        panel.append(_link(app))

    vol_text = c.mono(f"{settings.volume}%", t, size=13, color=t.text)

    def nudge_volume(delta: int) -> None:
        settings.volume = max(0, min(100, settings.volume + delta))
        s.apply_volume()
        settings.save()
        vol_text.value = f"{settings.volume}%"
        try:
            vol_text.update()
        except Exception:
            pass

    # +/- stepper instead of a Slider: a focused slider eats Left / Right (the d-pad)
    volume = ft.Row([
        c.icon_button(ft.Icons.REMOVE, lambda e: nudge_volume(-10), t, "Volume down", size=16, width=36, height=32),
        ft.Container(vol_text, width=48, alignment=ft.Alignment.CENTER),
        c.icon_button(ft.Icons.ADD, lambda e: nudge_volume(10), t, "Volume up", size=16, width=36, height=32),
    ], spacing=2, tight=True)

    def on_replay(v: bool) -> None:
        settings.replay_buffer = v
        settings.save()
        s.set_replay_buffer(v)
        app.render()

    panel.append(ft.Container(ft.Column([
        c.setting_row("Volume", volume, t),
        c.setting_row("Replay buffer (last 30 s)", c.checkbox(settings.replay_buffer, on_replay, t,
                                                              "Replay buffer"), t),
    ], spacing=12), padding=ft.Padding.only(top=16), border=ft.Border.only(top=ft.BorderSide(1, t.line)),
        margin=ft.Margin.only(top=0)))
    panel.insert(-1, c.spacer())
    side = c.side_panel(t, panel, width=side_w, wide=wide, padding=20, spacing=22, fits=app.height >= 860)

    # ---------------------------------------------------------------- footer
    status_dot = c.dot("#F2A65A" if s.paused else t.gb)
    status_text = c.mono("Paused" if s.paused else "Running", t)
    def perf() -> str:
        st = s.stats
        if s.paused or not st.fps:
            return "— FPS"
        # drawn frames vs. game speed: frameskip keeps the game (and sound) at
        # full speed when drawing every frame is too slow
        return f"{st.drawn_fps:.0f} FPS · game {st.speed:.0f}%"

    fps_text = c.mono(perf(), t)
    shown_error = s.error

    def on_status() -> None:
        nonlocal shown_error
        if s.error != shown_error:          # load failure / crash: show the overlay
            shown_error = s.error
            app.render()
            return
        fps_text.value = perf()
        try:
            fps_text.update()
        except Exception:
            pass
    s.on_status = on_status

    footer_items = [
        ft.Row([status_dot, status_text], spacing=8, tight=True), fps_text,
        c.mono(f"{s.speed * 100}% speed", t), c.mono(f"Slot {s.slot}", t),
    ]
    if settings.replay_buffer:
        footer_items.append(c.mono("Replay buffer 30 s", t))
    if s.recording:
        footer_items.append(c.mono("● REC", t, color=t.rec))
    footer = ft.Container(
        ft.Row([ft.Row(footer_items, spacing=22, wrap=True, run_spacing=4, expand=True),
                c.mono(f"{g.filename} · {lib.human_size(g.size)}", t)], spacing=22),
        padding=c.pad(20, 10), bgcolor=t.panel, border=ft.Border.only(top=ft.BorderSide(1, t.line)),
    )

    body = c.split(stage, side, wide=wide)
    root = ft.Column([header, ft.Container(body, expand=True), footer], spacing=0, expand=True)
    return ft.KeyboardListener(ft.Container(root, bgcolor=t.bg, expand=True), autofocus=True,
                               on_key_down=on_key_down, on_key_up=on_key_up)


def _leave(app: "App", route: str) -> None:
    """Pause-menu shortcut to another screen (app.go asks to save a state first)."""
    app.go(route)


# -------------------------------------------------------------------- panel tabs
def _saves(app: "App", set_slot, set_speed, quick_save, quick_load) -> ft.Control:
    t, s, hk = app.t, app.session, app.settings.hotkeys
    info = {e["slot"]: e for e in s.slot_info()}
    rows: list[ft.Control] = [ft.Row([c.heading("Save states", t, 15), ft.Container(expand=True),
                                      c.text(f"{hk.get('quick_save', '')} save · {hk.get('quick_load', '')} load",
                                             size=12, color=t.muted)])]

    def thumb(e) -> ft.Control:
        if e and e["thumb"] is not None:
            try:
                return ft.Container(ft.Image(src=e["thumb"].read_bytes(), fit=ft.BoxFit.COVER,
                                             filter_quality=ft.FilterQuality.NONE),
                                    width=72, height=48, border_radius=4, clip_behavior=ft.ClipBehavior.HARD_EDGE)
            except OSError:
                pass
        return ft.Container(width=72, height=48, bgcolor=t.screen, border_radius=4)

    def when(e) -> str:
        return lib.ago(e["time"], "") if e and e["time"] else "Empty"

    for n in VISIBLE_SLOTS:
        e = info.get(n)
        on = s.slot == n
        rows.append(c.button(ft.Row([
            thumb(e),
            ft.Column([c.text(f"Slot {n}", size=13, weight=600, color=t.text),
                       c.text(when(e), size=12, color=t.muted)], spacing=3, tight=True, expand=True),
            c.mono("●" if on else "", t, size=11, color=t.accent),
        ], spacing=12), (lambda ev, k=n: set_slot(k)), t, variant="ghost", height=64, h_pad=8,
            bg=t.soft if on else t.card, side=t.accent if on else t.line, align=ft.Alignment.CENTER_LEFT))
    rows.append(ft.Row([
        c.button("Save", quick_save, t, variant="primary", height=38, radius=8, expand=1, icon_name=ft.Icons.SAVE),
        c.button("Load", lambda ev: quick_load(), t, height=38, radius=8, expand=1,
                 disabled=not (info.get(s.slot) and info[s.slot]["time"])),
    ], spacing=8))
    resume = info.get(0)
    if resume and resume["time"]:
        rows.append(c.button(ft.Row([c.icon(ft.Icons.HISTORY, t.text, 16),
                                     c.text(f"Resume point · {when(resume)}", size=13, color=t.text)], spacing=8),
                             lambda ev: quick_load(slot=0), t, variant="ghost", height=38, radius=8))
    rw = s.rewind_seconds
    rows.append(c.text(f"Rewind: hold {hk.get('rewind', '')} · {rw:.0f} s available" if rw else
                       f"Rewind: hold {hk.get('rewind', '')}", size=12, color=t.muted))
    rows.append(ft.Container(c.heading("Speed", t, 15), margin=ft.Margin.only(top=12)))
    rows.append(c.segmented([(str(v), f"{v}×") for v in (1, 2, 4, 8)], str(s.speed), set_speed, t,
                            mono_font=True, equal=True))
    return ft.Column(rows, spacing=10)


def _cheats(app: "App") -> ft.Control:
    t, ui = app.t, app.ui

    def toggle(cid: str, v: bool) -> None:
        ui.cheats[cid] = v
        app.render()

    rows: list[ft.Control] = [
        ft.Row([c.heading("Cheats", t, 15), ft.Container(expand=True),
                c.button(c.text("+ Add code", size=13, color=t.accent), lambda e: app.not_ready("Cheats"), t,
                         variant="ghost", height=32, h_pad=6)]),
        c.text("GameShark, Action Replay and Code Breaker codes. Achievements pause while cheats are on. "
               "Preview: codes are not applied by the core yet.", size=12, color=t.muted),
    ]
    for ch in samples.CHEATS:
        rows.append(ft.Container(ft.Row([
            ft.Column([c.text(ch["name"], size=13, weight=600, color=t.text),
                       c.mono(ch["code"], t, size=11)], spacing=3, tight=True, expand=True),
            c.checkbox(ui.cheats.get(ch["id"], False), lambda v, k=ch["id"]: toggle(k, v), t, ch["name"]),
        ], spacing=12), padding=c.pad(12, 10), border_radius=10, bgcolor=t.card, border=c.border(t.line)))
    return ft.Column(rows, spacing=10)


def _link(app: "App") -> ft.Control:
    t = app.t
    return ft.Column([
        c.heading("Link cable · online", t, 15),
        c.text("Trade, battle or race with a friend as if you were linked by cable. Preview: link play "
               "isn't in the core yet.", size=12, color=t.muted),
        c.card(ft.Column([
            c.text("Room code", size=12, color=t.muted),
            c.mono(samples.ROOM_CODE, size=22, weight=500, color=t.text, spacing=2.6),
            ft.Container(height=6),
            c.button("Copy invite link", lambda e: app.not_ready("Link play"), t, variant="primary", height=40,
                     radius=8, expand=True),
        ], spacing=6, horizontal_alignment=ft.CrossAxisAlignment.STRETCH), t, padding=14, radius=12),
        c.card(ft.Row([
            ft.Container(c.text("P2", size=12, weight=600, color=t.text), width=32, height=32, border_radius=16,
                         bgcolor=t.card2, alignment=ft.Alignment.CENTER),
            ft.Column([c.text(samples.FRIEND, size=13, weight=600, color=t.text),
                       c.text("Connected · 38 ms", size=12, color=t.muted)], spacing=2, tight=True, expand=True),
            c.dot(t.gb),
        ], spacing=10), t, padding=c.pad(12, 10), radius=10),
        c.button(c.text("Open link play & achievements", size=13, color=t.accent),
                 lambda e: app.go("together"), t, variant="ghost", height=32, h_pad=0,
                 align=ft.Alignment.CENTER_LEFT),
    ], spacing=12)


# -------------------------------------------------------------------- touch controls
def _touch_pad(app: "App") -> ft.Control:
    t, s = app.t, app.session

    def hold(content: ft.Control, button: str) -> ft.GestureDetector:
        def down(e):
            s.press(button, "touch")

        def up(e):
            s.release(button, "touch")
        return ft.GestureDetector(content, on_tap_down=down, on_tap_up=up, on_tap_cancel=up,
                                  on_long_press_end=up, on_pan_end=up, on_pan_cancel=up)

    def key(label: str, button: str, size: float = 52, round_: bool = False, fill: str | None = None) -> ft.Control:
        box = ft.Container(
            c.text(label, size=14 if len(label) == 1 else 11, weight=600,
                   color=t.on if fill == t.accent else t.text),
            width=size, height=size if not label.isalpha() or len(label) == 1 else 30,
            border_radius=size / 2 if round_ else 10, bgcolor=fill or t.card2,
            alignment=ft.Alignment.CENTER,
        )
        return hold(box, button)

    blank = ft.Container(width=52, height=52)
    dpad = ft.Column([
        ft.Row([blank, key("▲", "Up"), blank], spacing=0, tight=True),
        ft.Row([key("◀", "Left"), ft.Container(width=52, height=52, bgcolor=t.card2), key("▶", "Right")],
               spacing=0, tight=True),
        ft.Row([blank, key("▼", "Down"), blank], spacing=0, tight=True),
    ], spacing=0, tight=True)
    ab = ft.Row([
        ft.Container(key("B", "B", 60, True, t.accent), margin=ft.Margin.only(top=36)),
        key("A", "A", 60, True, t.accent),
    ], spacing=14, tight=True, vertical_alignment=ft.CrossAxisAlignment.START)
    start_select = ft.Row([key("SELECT", "Select", 72), key("START", "Start", 72)], spacing=12, tight=True)
    rows = []
    if s.game.system == "GBA":                 # shoulder buttons only exist on GBA
        rows.append(ft.Row([key("L", "L", 60, True), key("R", "R", 60, True)],
                           alignment=ft.MainAxisAlignment.SPACE_BETWEEN, spacing=0, tight=True))
    rows += [
        ft.Row([dpad, ab], alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
               vertical_alignment=ft.CrossAxisAlignment.CENTER),
        ft.Row([start_select], alignment=ft.MainAxisAlignment.CENTER),
    ]
    return ft.Container(ft.Column(rows, spacing=16), width=min(app.width - 24, 520), padding=c.pad(8, 0))
