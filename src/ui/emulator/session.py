"""One running game: owns the emulator Machine (via src.bridge), the frame
loop, input and audio.

The frame loop is an asyncio task on the Flet page; each batch of frames is
emulated in a worker thread so the UI stays responsive.
"""
from __future__ import annotations

import asyncio
import hashlib
import time
from array import array
from pathlib import Path

import flet as ft

from src import bridge
from src.bridge.adaptors import GamepadAdaptor, InputRouter
from src.ui.data.library import Game
from src.ui.data.moments import new_path
from src.ui.data.settings import Settings, data_dir
from src.ui.emulator import frame as fr
from src.ui.theme import GB_PALETTES


def _make_audio(apu):
    """SDL audio via the core's AudioOut (desktop only: needs pygame/SDL2)."""
    try:
        from src.core.gameboy.display import AudioOut
    except Exception:
        return None

    class ScaledAudio(AudioOut):
        volume = 1.0

        def pump(self) -> None:
            data = self.apu.take_samples()
            if len(data) < 2:
                return
            q = self.queued()
            if self.started and q == 0:
                self.underruns += 1
            if q > self.max_bytes:
                self.dropped += 1
                return
            if self.volume < 0.999:
                v = self.volume
                data = array("h", [int(s * v) for s in data])
            raw = data.tobytes()
            self.sdl.SDL_QueueAudio(self.dev, raw, len(raw))
            self.played += 1
            if not self.started and q + len(raw) >= self.target:
                self.sdl.SDL_PauseAudioDevice(self.dev, 0)
                self.started = True

    try:
        return ScaledAudio(apu)
    except Exception as e:          # no audio device, no SDL, ...
        print(f"[ui] audio disabled: {e}")
        return None


def thumb_path(rom_path: str) -> Path:
    d = data_dir() / "thumbs"
    d.mkdir(parents=True, exist_ok=True)
    return d / (hashlib.sha1(rom_path.encode()).hexdigest()[:16] + ".png")


class _MachineSink:
    """Router sink: forwards to whichever machine the session currently runs."""

    def __init__(self, session: "EmulatorSession"):
        self.session = session

    def press(self, button: str) -> None:
        if self.session.machine:
            self.session.machine.press(button)

    def release(self, button: str) -> None:
        if self.session.machine:
            self.session.machine.release(button)


_GAMEPADS: GamepadAdaptor | None = None        # one SDL controller handle per process


class EmulatorSession:
    def __init__(self, page: ft.Page, game: Game, settings: Settings):
        self.page = page
        self.game = game
        self.settings = settings
        self.machine: bridge.Machine | None = None
        self.audio = None
        self.error: str | None = None
        self.paused = False
        self.speed = 1
        self.fast_forward = False      # hotkey held
        self.fps = 0.0
        self.slot = 1
        self.on_status = None          # callable() when fps/paused changes
        self._running = False
        self._rgb: bytes | None = None
        self._prev: bytes | None = None
        self._t_start = time.time()
        self.input = InputRouter(_MachineSink(self))     # keyboard + touch + gamepads
        self.gamepads = self._gamepads()

        self.image = ft.Image(
            src=fr.blank_png(), fit=ft.BoxFit.CONTAIN, gapless_playback=True,
            filter_quality=self._filter(), expand=True,
        )
        self._boot()

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

    # ------------------------------------------------------------------ lifecycle
    def _boot(self) -> None:
        try:
            self.machine = bridge.load(self.game.path)
        except Exception as e:
            self.machine = None
            self.error = f"Couldn't load this ROM: {e}"
            return
        self._attach_audio()

    def _attach_audio(self) -> None:
        if self.audio is None:
            self.audio = _make_audio(self.machine.apu)
        else:
            self.audio.apu.sampling = False
            self.audio.apu = self.machine.apu
            self.machine.apu.sampling = True
        self.apply_volume()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self.page.run_task(self._loop)

    def close(self) -> None:
        self._running = False
        self.input.release_all()
        if self.machine:
            try:
                self.machine.save()
            except Exception as e:
                print(f"[ui] save RAM failed: {e}")
        if self.audio:
            self.audio.close()
            self.audio = None
        if self._rgb and self.machine:
            try:
                thumb_path(self.game.path).write_bytes(
                    fr.encode_png(self._rgb, self.machine.width, self.machine.height))
            except OSError:
                pass
        h = self.settings.history.setdefault(self.game.path, {})
        h["last"] = time.time()
        h["seconds"] = float(h.get("seconds", 0)) + (time.time() - self._t_start)
        self.settings.save()

    def reset(self) -> None:
        if self.machine:
            self.machine.reset()               # saves battery RAM first
            self._attach_audio()
        self._prev = None

    # ------------------------------------------------------------------ settings
    def _filter(self) -> ft.FilterQuality:
        return ft.FilterQuality.MEDIUM if self.settings.shader in ("smooth", "bilinear") else ft.FilterQuality.NONE

    def apply_video(self) -> None:
        self.image.filter_quality = self._filter()

    def apply_volume(self) -> None:
        if self.audio:
            self.audio.volume = max(0.0, min(1.0, self.settings.volume / 100))

    # ------------------------------------------------------------------ input
    def press(self, button: str, source: str = "ui") -> None:
        try:
            self.input.press(source, button)   # router -> bridge maps "A"/"L"/... to the core
        except ValueError:
            pass                               # unknown button: ignore

    def release(self, button: str, source: str = "ui") -> None:
        try:
            self.input.release(source, button)
        except ValueError:
            pass

    def toggle_pause(self) -> None:
        self.paused = not self.paused
        if self.paused:
            self.input.release_all()
        if self.audio and self.paused:
            self.audio.sdl.SDL_ClearQueuedAudio(self.audio.dev)
            self.audio.started = False
            self.audio.sdl.SDL_PauseAudioDevice(self.audio.dev, 1)

    # ------------------------------------------------------------------ frames
    def _palette(self) -> tuple:
        return GB_PALETTES.get(self.settings.gb_palette, GB_PALETTES["pea"]).rgb()

    def _step(self, n: int) -> bytes:
        m = self.machine
        for _ in range(n):
            m.run_frame()
        if self.audio:
            if n == 1 and self.settings.volume > 0:
                self.audio.pump()
            else:
                m.apu.take_samples()           # drop audio while fast-forwarding
        if m.system == "GB":
            rgb = m.frame_rgb(self._palette())
        else:
            rgb = m.frame_rgb()
            if m.system == "GBC" and self.settings.lcd_colors:
                rgb = fr.lcd_correct(rgb)
        if self.settings.frame_blend:
            out = fr.blend(self._prev, rgb)
            self._prev = rgb
            rgb = out
        self._rgb = rgb
        return fr.encode_png(rgb, m.width, m.height)

    async def _loop(self) -> None:
        period = 1 / (self.machine.fps if self.machine else 60.0)
        next_t = time.perf_counter()
        counted, t_count = 0, time.perf_counter()
        while self._running:
            if self.paused or self.machine is None:
                await asyncio.sleep(0.05)
                next_t = time.perf_counter()
                continue
            n = self.speed * (4 if self.fast_forward else 1)
            if self.gamepads is not None:
                self.gamepads.poll()
            try:
                png = await asyncio.to_thread(self._step, n)
            except Exception as e:                  # emulator crash: stop and report
                self.error = f"Emulation stopped: {e}"
                self.paused = True
                self._notify()
                continue
            if not self._running:
                break
            self.image.src = png
            try:
                self.image.update()
            except Exception:
                pass                                # view not mounted (navigating away)
            counted += n
            now = time.perf_counter()
            if now - t_count >= 1.0:
                self.fps = counted / (now - t_count)
                counted, t_count = 0, now
                self._notify()
            if self.audio and self.audio.started and n == 1:
                await asyncio.to_thread(self.audio.wait)
                next_t = time.perf_counter()
            else:
                next_t += period
                delay = next_t - time.perf_counter()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    next_t = time.perf_counter()
                    await asyncio.sleep(0)

    def _notify(self) -> None:
        if self.on_status:
            try:
                self.on_status()
            except Exception:
                pass

    # ------------------------------------------------------------------ moments
    def screenshot(self) -> Path | None:
        if not self._rgb or not self.machine:
            return None
        path = new_path(self.game.title, self.game.system)
        k = 4
        w, h = self.machine.width, self.machine.height
        path.write_bytes(fr.encode_png(fr.scale_nearest(self._rgb, w, h, k), w * k, h * k, level=6))
        return path
