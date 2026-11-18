"""Library previews: a screenshot of every game, made in the background.

For a game that has never been played, a background thread boots the ROM
headless (no audio, no window), runs it past the boot logos and saves one
frame as the card's preview. Playing a game later replaces the preview with
the last frame you saw (EmulatorSession.close writes the same file).

Preview runs never write battery saves: the cartridge's save_ram is
disabled on the preview machine, so your .sav files are only ever read.
Generation pauses while a game is being played so it never slows emulation.
"""
from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path

from src import bridge
from src.ui.emulator import frame as fr
from src.ui.emulator.session import thumb_path

# frames at which a candidate screenshot is drawn (~4 s to ~15 s of game time);
# the first one that isn't a near-blank screen wins, else the last one
CHECKPOINTS = (240, 420, 600, 900)
MAX_SECONDS = 20.0          # wall-clock cap per ROM (slow GBA intros)
MIN_COLOURS = 4             # a frame with fewer distinct colours counts as blank


def _interesting(rgb: bytes) -> bool:
    px = {rgb[i:i + 3] for i in range(0, len(rgb) - 2, 3 * 37)}   # sampled pixels
    return len(px) >= MIN_COLOURS


def make_preview(path: str, palette=None) -> bytes | None:
    """Boot `path` headless and return a PNG screenshot (None if it can't run)."""
    m = bridge.load(path)
    m.core.cart.save_ram = lambda *a, **k: False      # never touch the .sav file
    m.apu.sampling = False
    t0 = time.perf_counter()
    done = 0
    rgb = None
    for target in CHECKPOINTS:
        while done < target - 1:
            m.run_frame(render=False)
            done += 1
            if time.perf_counter() - t0 > MAX_SECONDS:
                break
        m.run_frame(render=True)
        done += 1
        if m.system == "GB" and palette is not None:
            rgb = m.frame_rgb(palette)
        else:
            rgb = m.frame_rgb()
        if _interesting(rgb) or time.perf_counter() - t0 > MAX_SECONDS:
            break
    if rgb is None:
        return None
    if m.system == "GBC":
        rgb = fr.lcd_correct(rgb)
    return fr.encode_png(rgb, m.width, m.height)


class PreviewMaker:
    """One background thread working through games that have no preview yet."""

    def __init__(self, on_ready=None, palette=None):
        self.on_ready = on_ready          # callable(path), called in the worker thread
        self.palette = palette
        self.busy = threading.Event()     # set while a game is playing: wait
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._queued: set[str] = set()
        self._failed: set[str] = set()
        self._thread: threading.Thread | None = None

    def queue_games(self, games) -> None:
        for g in games:
            if not g.playable or g.path in self._queued or g.path in self._failed:
                continue
            if thumb_path(g.path).exists():
                continue
            self._queued.add(g.path)
            self._q.put(g.path)
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="previews", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                path = self._q.get(timeout=5)
            except queue.Empty:
                return                    # idle: the thread ends, restarted on demand
            while self.busy.is_set():     # a game is running: don't compete with it
                time.sleep(0.5)
            out = thumb_path(path)
            try:
                if out.exists():          # played meanwhile: keep the real last frame
                    continue
                png = make_preview(path, self.palette)
                if png is None or out.exists():
                    continue
                tmp = out.with_suffix(".tmp")
                tmp.write_bytes(png)
                os.replace(tmp, out)
            except Exception as e:        # broken / unsupported ROM: show the placeholder
                self._failed.add(path)
                print(f"[previews] {Path(path).name}: {e}")
                continue
            finally:
                self._queued.discard(path)
            cb = self.on_ready
            if cb is not None:
                try:
                    cb(path)
                except Exception:
                    pass
