"""One running game, as seen by the UI.

All emulation happens in an :class:`~src.bridge.runner.EmulatorRunner`
worker thread. It owns the Machine and the SDL audio, paces itself on the
sound card and frameskips when it falls behind. This class is the async
side: one asyncio task on the Flet page polls for the newest finished frame,
wakes when the worker signals a new frame (no busy polling), encodes it and
pushes it to the screen, polls gamepads, and routes input to
the worker. The UI thread never runs the emulator, so keys, clicks and
redraws stay responsive whatever the game is doing.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path

import flet as ft

from src import bridge
from src.bridge import savestate
from src.bridge.adaptors import GamepadAdaptor, InputRouter
from src.bridge.runner import EmulatorRunner, Stats
from src.ui.data.library import Game
from src.ui.data.moments import new_path
from src.ui.data.settings import Settings, data_dir
from src.ui.emulator import frame as fr
from src.ui.theme import GB_PALETTES

PAD_POLL = 1 / 60         # longest wait between display-loop ticks (gamepad polling)
STATS_EVERY = 0.5         # seconds between footer refreshes


def thumb_path(rom_path: str) -> Path:
    d = data_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha1(rom_path.encode()).hexdigest()[:16] + ".png")


class _RunnerSink:
    """Router sink: every press / release goes to the worker's input queue."""

    def __init__(self, session: "EmulatorSession"):
        self.session = session

    def press(self, button: str) -> None:
        self.session.runner.press(button)

    def release(self, button: str) -> None:
        self.session.runner.release(button)


_GAMEPADS: GamepadAdaptor | None = None        # one SDL controller handle per process


class EmulatorSession:
    def __init__(self, page: ft.Page, game: Game, settings: Settings):
        self.page = page
        self.game = game
        self.settings = settings
        self.error: str | None = None
        self.slot = 1
        self.stats = Stats()
        self.on_status = None          # callable() when stats / error change
        self.keys = None               # KeyboardAdaptor, created by the player view
        self._running = False
        self._rgb: bytes | None = None
        self._prev: bytes | None = None
        self._t_start = time.time()
        web = page is not None and bool(getattr(page, "web", False))
        self.runner = EmulatorRunner(lambda: bridge.load(game.path), audio=not web,
                                     palette=self._palette(), name=f"emu:{game.filename}",
                                     replay=settings.replay_buffer)
        # save-state slots live in the game's save folder: saves/<core>/<game>/states/
        self.slots = savestate.SlotStore(game.path, game.system.lower())
        self._slot_snaps: dict[int, tuple] = {}    # slot -> (in-memory snapshot, file mtime)
        self.input = InputRouter(_RunnerSink(self))     # keyboard + touch + gamepads
        self.gamepads = self._gamepads()
        self.apply_volume()

        self.image = ft.Image(
            src=fr.blank_png(*self._native()), fit=ft.BoxFit.CONTAIN, gapless_playback=True,
            filter_quality=self._filter(), expand=True,
        )

    def _native(self) -> tuple[int, int]:
        from src.ui.data.library import NATIVE_SIZE
        return NATIVE_SIZE.get(self.game.system, (160, 144))

    def _gamepads(self) -> GamepadAdaptor | None:
        """Shared gamepad adaptor re-targeted at this session (desktop only)."""
        global _GAMEPADS
        if self.page is not None and getattr(self.page, "web", False):
            return None
        if _GAMEPADS is None:
            _GAMEPADS = GamepadAdaptor(self.input)
        else:
            _GAMEPADS.router = self.input
        return _GAMEPADS if _GAMEPADS.available else None

    # ------------------------------------------------------------------ state
    @property
    def machine(self) -> bridge.Machine | None:
        return self.runner.machine

    @property
    def paused(self) -> bool:
        return self.runner.paused

    @property
    def speed(self) -> int:
        return self.runner.speed

    @speed.setter
    def speed(self, n: int) -> None:
        self.runner.set_speed(n)

    @property
    def fast_forward(self) -> bool:
        return self.runner.fast_forward

    @fast_forward.setter
    def fast_forward(self, on: bool) -> None:
        self.runner.set_fast_forward(on)

    @property
    def fps(self) -> float:
        """Frames drawn per second (what you see)."""
        return self.stats.drawn_fps

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.runner.start()
        self.page.run_task(self._display_loop)

    def close(self) -> None:
        self._running = False
        self.input.release_all()
        if self.recording:                     # closing mid-recording keeps what was recorded
            self.stop_recording()
        self.runner.close()                    # worker saves battery RAM + closes audio
        m = self.machine
        if self._rgb and m:
            try:
                thumb_path(self.game.path).write_bytes(fr.encode_png(self._rgb, m.width, m.height))
            except OSError:
                pass
        h = self.settings.history.setdefault(self.game.path, {})
        h["last"] = time.time()
        h["seconds"] = float(h.get("seconds", 0)) + (time.time() - self._t_start)
        self.settings.save()

    def reset(self) -> None:
        self.input.release_all()
        self.runner.reset()
        self._prev = None

    # ------------------------------------------------------------------ settings
    def _filter(self) -> ft.FilterQuality:
        return ft.FilterQuality.MEDIUM if self.settings.shader in ("smooth", "bilinear") else ft.FilterQuality.NONE

    def apply_video(self) -> None:
        self.image.filter_quality = self._filter()
        self.runner.set_palette(self._palette())

    def apply_volume(self) -> None:
        self.runner.set_volume(self.settings.volume / 100)

    # ------------------------------------------------------------------ input
    def press(self, button: str, source: str = "ui") -> None:
        try:
            self.input.press(source, button)
        except ValueError:
            pass                               # unknown button: ignore

    def release(self, button: str, source: str = "ui") -> None:
        try:
            self.input.release(source, button)
        except ValueError:
            pass

    def toggle_pause(self) -> None:
        paused = not self.paused
        if paused:
            self.input.release_all()
        self.runner.set_paused(paused)

    # ------------------------------------------------------------------ frames
    def _palette(self) -> tuple:
        return GB_PALETTES.get(self.settings.gb_palette, GB_PALETTES["pea"]).rgb()

    def _present(self, frame) -> bytes:
        """Post-process one RGB frame (LCD colours, ghosting) and encode it."""
        rgb = frame.rgb
        if self.game.system == "GBC" and self.settings.lcd_colors:
            rgb = fr.lcd_correct(rgb)
        if self.settings.frame_blend:
            out = fr.blend(self._prev, rgb)
            self._prev = rgb
            rgb = out
        self._rgb = rgb
        return fr.encode_png(rgb, frame.width, frame.height)

    async def _display_loop(self) -> None:
        r = self.runner
        loop = asyncio.get_running_loop()
        new_frame = asyncio.Event()

        def wake(_frame) -> None:                   # worker thread -> this loop, thread-safe
            try:
                loop.call_soon_threadsafe(new_frame.set)
            except RuntimeError:
                pass                                # loop already closed
        r.on_frame = wake
        await asyncio.to_thread(r.ready.wait, 30)
        last_seq = 0
        next_stats = time.perf_counter() + STATS_EVERY
        while self._running:
            # wait for the worker's next frame; the timeout keeps gamepads polled
            # (and stats fresh) even while paused or frameskipping
            try:
                await asyncio.wait_for(new_frame.wait(), PAD_POLL)
            except asyncio.TimeoutError:
                pass
            new_frame.clear()
            if not self._running:
                break
            if self.gamepads is not None:
                try:
                    self.gamepads.poll()
                except Exception:
                    pass
            f = r.latest
            if f is not None and f.seq != last_seq:
                last_seq = f.seq
                self.image.src = self._present(f)
                try:
                    self.image.update()
                except Exception:
                    pass                            # view not mounted (navigating away)
            if r.error and r.error != self.error:   # load failure or emulator crash
                self.error = r.error
                self._notify()
            now = time.perf_counter()
            if now >= next_stats:
                next_stats = now + STATS_EVERY
                if r.stats != self.stats:
                    self.stats = r.stats
                    self._notify()
        r.on_frame = None

    def _notify(self) -> None:
        if self.on_status:
            try:
                self.on_status()
            except Exception:
                pass

    # ------------------------------------------------------------------ save states
    def _thumbnail(self) -> bytes | None:
        m = self.machine
        if not self._rgb or not m:
            return None
        return fr.encode_png(self._rgb, m.width, m.height)

    async def save_state(self, slot: int) -> dict:
        """Save the current moment to `slot` (0 = the resume point). Runs on the emulator thread."""
        thumb = self._thumbnail()
        store, cache = self.slots, self._slot_snaps

        def work(m):
            snap = savestate.capture(m, level=1)
            meta = store.save(m, slot, thumbnail=thumb)
            cache[slot] = (snap, store.path(slot).stat().st_mtime)
            return meta
        return await asyncio.wrap_future(self.runner.submit(work))

    async def load_state(self, slot: int) -> None:
        """Load `slot`. A slot saved in this session is restored in place (instant)."""
        store, cache = self.slots, self._slot_snaps

        def work(m):
            hit = cache.get(slot)
            p = store.path(slot)
            if hit is not None and p.is_file() and p.stat().st_mtime == hit[1]:
                savestate.restore(m, hit[0])
            else:
                store.load(m, slot)
        self.input.release_all()
        await asyncio.wrap_future(self.runner.submit(work))
        self._prev = None

    def slot_info(self) -> list[dict]:
        return self.slots.list()

    # ------------------------------------------------------------------ rewind
    def set_rewinding(self, on: bool) -> None:
        if on:
            self.input.release_all()
        self.runner.set_rewinding(on)

    @property
    def rewinding(self) -> bool:
        return self.runner.rewinding

    @property
    def rewind_seconds(self) -> float:
        rw = self.runner.rewind
        return rw.seconds if rw is not None else 0.0

    # ------------------------------------------------------------------ recording
    @property
    def recording(self) -> bool:
        return self.runner.recorder.recording

    def _gif_opts(self) -> dict:
        w, _ = self._native()
        transform = fr.lcd_correct if (self.game.system == "GBC" and self.settings.lcd_colors) else None
        return {"scale": 2 if w <= 160 else 1, "transform": transform}

    def start_recording(self) -> None:
        self.runner.recorder.start(self.runner.frames)

    def stop_recording(self, on_done=None) -> bool:
        """Stop and encode in the background; on_done(path | None, error)."""
        path = new_path(self.game.title, self.game.system, ".gif")
        return self.runner.recorder.stop(path, on_done=on_done, **self._gif_opts())

    def clip(self, seconds: float = 30, on_done=None) -> bool:
        """Save the last `seconds` of play as a GIF (background)."""
        path = new_path(self.game.title, self.game.system, ".gif")
        return self.runner.recorder.clip(path, seconds=seconds, on_done=on_done, **self._gif_opts())

    def set_replay_buffer(self, on: bool) -> None:
        self.runner.recorder.replay_on = bool(on)

    # ------------------------------------------------------------------ moments
    def screenshot(self) -> Path | None:
        if not self._rgb or not self.machine:
            return None
        path = new_path(self.game.title, self.game.system)
        k = 4
        w, h = self.machine.width, self.machine.height
        path.write_bytes(fr.encode_png(fr.scale_nearest(self._rgb, w, h, k), w * k, h * k, level=6))
        return path
