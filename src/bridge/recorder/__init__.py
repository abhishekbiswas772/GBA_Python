"""Video capture: GIF recording and an instant-replay buffer ("clip the last 30 s").

Both take the frames the runner draws (RGB888) and keep them zlib-compressed
in memory, with the *emulated* frame number of each one.  Durations in the GIF
then follow game time even when frames were skipped while drawing.  Encoding
happens on a background thread when the recording stops, never on the
emulator thread.

    rec = Recorder()                       # one per session
    rec.feed(frame_rgb, w, h, emu_frame)   # every drawn frame (emulator thread)
    rec.start(); ...; rec.stop(path)       # Record button
    rec.clip(path, seconds=30)             # "Clip last 30 s"

GIF has no sound; clips are silent.  Encoding needs Pillow (a dependency).
"""
from __future__ import annotations

import threading
import zlib
from collections import deque
from pathlib import Path

GIF_FPS = 30                 # keep every other 60 Hz frame
REPLAY_SECONDS = 30
MAX_RECORD_SECONDS = 10 * 60
_EMU_FPS = 60.0


class _Frames:
    """Compressed frames with their emulated frame numbers."""

    def __init__(self, max_frames: int):
        self.items: deque = deque(maxlen=max_frames)
        self.size = (0, 0)

    def add(self, rgb: bytes, w: int, h: int, n: int) -> None:
        if self.size != (w, h):
            self.items.clear()                 # resolution changed (GB <-> GBC never does, but be safe)
            self.size = (w, h)
        self.items.append((n, zlib.compress(rgb, 1)))

    def since(self, first_frame: int) -> list:
        return [it for it in self.items if it[0] >= first_frame]


def encode_gif(items: list, size: tuple[int, int], path, scale: int = 1, transform=None) -> Path:
    """Write `items` [(emu_frame, zlib RGB)] as an animated GIF. Returns the path."""
    from PIL import Image
    w, h = size
    frames, durations = [], []
    for i, (n, data) in enumerate(items):
        rgb = zlib.decompress(data)
        if transform is not None:
            rgb = transform(rgb)
        im = Image.frombytes("RGB", (w, h), rgb)
        if scale > 1:
            im = im.resize((w * scale, h * scale), Image.NEAREST)
        frames.append(im.quantize(256, method=Image.Quantize.FASTOCTREE, dither=Image.Dither.NONE))
        nxt = items[i + 1][0] if i + 1 < len(items) else n + 2
        durations.append(max(20, round((nxt - n) * 1000 / _EMU_FPS / 10) * 10))
    if not frames:
        raise ValueError("nothing recorded")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    frames[0].save(tmp, format="GIF", save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, optimize=False, disposal=1)
    tmp.replace(path)
    return path


class Recorder:
    def __init__(self, *, replay: bool = True, replay_seconds: int = REPLAY_SECONDS):
        self.replay_on = replay
        self._replay = _Frames(int(replay_seconds * GIF_FPS))
        self._rec: _Frames | None = None
        self._rec_start = 0
        self._last_n = -10
        self._lock = threading.Lock()
        self.encoding = 0                      # background encodes in progress

    @property
    def recording(self) -> bool:
        return self._rec is not None

    @property
    def recorded_seconds(self) -> float:
        rec = self._rec
        if rec is None or not rec.items:
            return 0.0
        return (rec.items[-1][0] - self._rec_start) / _EMU_FPS

    # ------------------------------------------------------------ emulator thread
    def feed(self, rgb: bytes, w: int, h: int, emu_frame: int) -> None:
        """Offer a drawn frame. Kept at most GIF_FPS times per emulated second."""
        if emu_frame - self._last_n < _EMU_FPS / GIF_FPS:
            return
        if not (self.replay_on or self._rec is not None):
            return
        self._last_n = emu_frame
        with self._lock:
            if self.replay_on:
                self._replay.add(rgb, w, h, emu_frame)
            if self._rec is not None:
                self._rec.add(rgb, w, h, emu_frame)

    # ------------------------------------------------------------ UI thread
    def start(self, emu_frame: int = 0) -> None:
        with self._lock:
            self._rec = _Frames(MAX_RECORD_SECONDS * GIF_FPS)
            self._rec_start = emu_frame
            self._last_n = -10

    def stop(self, path, *, scale: int = 1, transform=None, on_done=None) -> bool:
        """End the recording and encode it in the background. on_done(path | None, error)."""
        with self._lock:
            rec, self._rec = self._rec, None
        if rec is None or not rec.items:
            return False
        self._encode(list(rec.items), rec.size, path, scale, transform, on_done)
        return True

    def cancel(self) -> None:
        with self._lock:
            self._rec = None

    def clip(self, path, *, seconds: float = REPLAY_SECONDS, scale: int = 1, transform=None,
             on_done=None) -> bool:
        """Save the last `seconds` of play as a GIF (background). False if nothing buffered."""
        with self._lock:
            items = list(self._replay.items)
            size = self._replay.size
        if not items:
            return False
        first = items[-1][0] - int(seconds * _EMU_FPS)
        items = [it for it in items if it[0] >= first]
        self._encode(items, size, path, scale, transform, on_done)
        return True

    def _encode(self, items, size, path, scale, transform, on_done) -> None:
        self.encoding += 1

        def work():
            try:
                out = encode_gif(items, size, path, scale, transform)
                err = None
            except Exception as e:                   # Pillow missing, disk full, ...
                out, err = None, e
            finally:
                self.encoding -= 1
            if on_done is not None:
                try:
                    on_done(out, err)
                except Exception:
                    pass

        threading.Thread(target=work, name="gif-encoder", daemon=True).start()
