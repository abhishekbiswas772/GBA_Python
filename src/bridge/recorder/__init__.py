"""Video capture: recordings and an instant-replay buffer ("clip the last 30 s").

The runner offers every drawn frame (RGB888) and every chunk of game audio
(int16 stereo, 44.1 kHz).  Both are kept zlib-compressed in memory, tagged
with the *emulated* frame number, so the output follows game time even when
frames were skipped while drawing, or the game ran fast-forward.  Encoding
happens on a background thread when a recording stops, never on the
emulator thread.

    rec = Recorder()                          # one per session
    rec.fps = machine.fps                     # emulated frames per second
    rec.feed(frame_rgb, w, h, emu_frame)      # every drawn frame   (emulator thread)
    rec.feed_audio(samples, emu_frame)        # every frame's sound (emulator thread)
    rec.start(); ...; rec.stop("x.mp4")       # Record button
    rec.clip("y.mp4", seconds=30)             # "Clip last 30 s"

The file extension picks the format:

* ``.mp4``: H.264 video + AAC sound, via PyAV (FFmpeg is bundled in its wheels).
  Pixel art is scaled up with nearest-neighbour to ~720 lines, so it stays sharp.
* ``.gif``: silent animated GIF, via Pillow.
"""
from __future__ import annotations

import math
import threading
import zlib
from collections import deque
from fractions import Fraction
from pathlib import Path

CAPTURE_FPS = 30             # frames kept per emulated second (every other 60 Hz frame)
GIF_FPS = CAPTURE_FPS
REPLAY_SECONDS = 30
MAX_RECORD_SECONDS = 10 * 60
SAMPLE_RATE = 44_100         # every core outputs int16 stereo at this rate
VIDEO_LINES = 720            # MP4: scale up to about this height
FORMATS = ("mp4", "gif")


class _Frames:
    """Compressed video frames and audio chunks with their emulated frame numbers."""

    def __init__(self, max_frames: int):
        self.items: deque = deque(maxlen=max_frames)
        self.audio: deque = deque()
        self.size = (0, 0)

    def add(self, rgb: bytes, w: int, h: int, n: int) -> None:
        if self.size != (w, h):
            self.items.clear()                 # resolution changed: start over
            self.audio.clear()
            self.size = (w, h)
        self.items.append((n, zlib.compress(rgb, 1)))

    def add_audio(self, raw: bytes, n: int) -> None:
        self.audio.append((n, zlib.compress(raw, 1)))

    def trim_audio(self, oldest_frame: int) -> None:
        a = self.audio
        while a and a[0][0] < oldest_frame:
            a.popleft()


def _audio_after(chunks, first_frame: int) -> list:
    """Audio chunks produced after video frame `first_frame` was drawn."""
    return [c for c in chunks if c[0] > first_frame]


# ---------------------------------------------------------------- encoders
def encode_gif(items: list, size: tuple[int, int], path, scale: int = 1, transform=None,
               fps: float = 60.0) -> Path:
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
        durations.append(max(20, round((nxt - n) * 1000 / fps / 10) * 10))
    if not frames:
        raise ValueError("nothing recorded")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    frames[0].save(tmp, format="GIF", save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, optimize=False, disposal=1)
    tmp.replace(path)
    return path


def video_scale(w: int, h: int) -> int:
    """Integer upscale to about VIDEO_LINES (GB 5x, GBA 4x, NES 3x)."""
    return max(2, VIDEO_LINES // h)


def encode_mp4(items: list, size: tuple[int, int], path, audio: list = (), *, fps: float = 60.0,
               scale: int | None = None, transform=None, poster=None) -> Path:
    """Write `items` [(emu_frame, zlib RGB)] and `audio` [(emu_frame, zlib int16 stereo)]
    as an MP4 (H.264 + AAC) at a constant CAPTURE_FPS. Returns the path.

    Frames skipped while drawing are held, so the picture stays in step with the
    sound. `poster`, if given, receives a PNG of a frame from the middle."""
    import av
    from PIL import Image
    if not items:
        raise ValueError("nothing recorded")
    w, h = size
    k = scale or video_scale(w, h)
    W, H = (w * k) & ~1, (h * k) & ~1
    n0 = items[0][0]
    total_samples = sum(len(zlib.decompress(d)) // 4 for _, d in audio) if audio else 0
    video_s = (items[-1][0] - n0) / fps + 1 / CAPTURE_FPS
    duration = max(video_s, total_samples / SAMPLE_RATE)
    n_out = max(1, math.ceil(duration * CAPTURE_FPS - 1e-6))

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.stem + ".tmp" + path.suffix)
    out = av.open(str(tmp), "w", format="mp4")
    try:
        vs = out.add_stream("libx264", rate=CAPTURE_FPS)
        vs.width, vs.height, vs.pix_fmt = W, H, "yuv420p"
        vs.options = {"crf": "18", "preset": "veryfast", "tune": "animation", "movflags": "+faststart"}
        aus = None
        if audio:
            aus = out.add_stream("aac", rate=SAMPLE_RATE)
            aus.layout = "stereo"
            aus.bit_rate = 160_000

        # video: constant frame rate, holding the latest captured frame
        idx, shown, image, mid = 0, -1, None, n_out // 2
        for j in range(n_out):
            target = n0 + j / CAPTURE_FPS * fps + 1e-6
            while idx + 1 < len(items) and items[idx + 1][0] <= target:
                idx += 1
            if idx != shown:
                rgb = zlib.decompress(items[idx][1])
                if transform is not None:
                    rgb = transform(rgb)
                image = Image.frombytes("RGB", (w, h), rgb).resize((W, H), Image.NEAREST)
                shown = idx
            if j == mid and poster is not None:
                _write_poster(image, poster)
            frame = av.VideoFrame.from_image(image)
            frame.pts = j
            for pkt in vs.encode(frame):
                out.mux(pkt)
        for pkt in vs.encode(None):
            out.mux(pkt)

        if aus is not None:
            pts = 0
            for _, data in audio:
                raw = zlib.decompress(data)
                n = len(raw) // 4
                if not n:
                    continue
                af = av.AudioFrame(format="s16", layout="stereo", samples=n)
                plane = af.planes[0]
                plane.update(raw[:n * 4] + bytes(plane.buffer_size - n * 4))
                af.sample_rate, af.time_base, af.pts = SAMPLE_RATE, Fraction(1, SAMPLE_RATE), pts
                pts += n
                for pkt in aus.encode(af):
                    out.mux(pkt)
            for pkt in aus.encode(None):
                out.mux(pkt)
    except BaseException:
        out.close()
        tmp.unlink(missing_ok=True)
        raise
    out.close()
    tmp.replace(path)
    return path


def _write_poster(image, poster) -> None:
    try:
        p = Path(poster)
        p.parent.mkdir(parents=True, exist_ok=True)
        image.save(p, format="PNG")
    except OSError:
        pass


def encode(items: list, size, path, audio: list = (), *, fps: float = 60.0, scale: int | None = None,
           transform=None, poster=None) -> Path:
    """Pick the encoder from the file extension (.mp4 or .gif)."""
    if Path(path).suffix.lower() == ".gif":
        return encode_gif(items, size, path, scale or (2 if size[0] <= 160 else 1), transform, fps)
    return encode_mp4(items, size, path, audio, fps=fps, scale=scale, transform=transform, poster=poster)


# ---------------------------------------------------------------- recorder
class Recorder:
    def __init__(self, *, replay: bool = True, replay_seconds: int = REPLAY_SECONDS):
        self.replay_on = replay
        self.replay_seconds = replay_seconds
        self.fps = 60.0                         # emulated frames per second (runner sets it)
        self._replay = _Frames(int(replay_seconds * CAPTURE_FPS))
        self._rec: _Frames | None = None
        self._rec_start = 0
        self._last_n = -10
        self._lock = threading.Lock()
        self.encoding = 0                       # background encodes in progress

    @property
    def recording(self) -> bool:
        return self._rec is not None

    @property
    def wants_audio(self) -> bool:
        return self.replay_on or self._rec is not None

    @property
    def recorded_seconds(self) -> float:
        rec = self._rec
        if rec is None or not rec.items:
            return 0.0
        return (rec.items[-1][0] - self._rec_start) / self.fps

    # ------------------------------------------------------------ emulator thread
    def feed(self, rgb: bytes, w: int, h: int, emu_frame: int) -> None:
        """Offer a drawn frame. Kept at most CAPTURE_FPS times per emulated second."""
        if emu_frame - self._last_n < self.fps / CAPTURE_FPS - 0.01:
            return
        if not (self.replay_on or self._rec is not None):
            return
        self._last_n = emu_frame
        with self._lock:
            if self.replay_on:
                self._replay.add(rgb, w, h, emu_frame)
            if self._rec is not None:
                self._rec.add(rgb, w, h, emu_frame)

    def feed_audio(self, samples, emu_frame: int) -> None:
        """Offer the sound made up to `emu_frame` (int16 stereo array or bytes)."""
        if not len(samples):
            return
        raw = samples if isinstance(samples, (bytes, bytearray)) else samples.tobytes()
        with self._lock:
            if self.replay_on:
                r = self._replay
                r.add_audio(raw, emu_frame)
                if r.items:
                    r.trim_audio(r.items[0][0])
                else:
                    r.trim_audio(emu_frame - int(self.replay_seconds * self.fps))
            if self._rec is not None:
                self._rec.add_audio(raw, emu_frame)

    # ------------------------------------------------------------ UI thread
    def start(self, emu_frame: int = 0) -> None:
        with self._lock:
            self._rec = _Frames(MAX_RECORD_SECONDS * CAPTURE_FPS)
            self._rec_start = emu_frame
            self._last_n = -10

    def stop(self, path, *, scale: int | None = None, transform=None, poster=None, on_done=None) -> bool:
        """End the recording and encode it in the background. on_done(path | None, error)."""
        with self._lock:
            rec, self._rec = self._rec, None
        if rec is None or not rec.items:
            return False
        items = list(rec.items)
        audio = _audio_after(rec.audio, items[0][0])
        self._encode(items, rec.size, path, audio, scale, transform, poster, on_done)
        return True

    def cancel(self) -> None:
        with self._lock:
            self._rec = None

    def clip(self, path, *, seconds: float = REPLAY_SECONDS, scale: int | None = None, transform=None,
             poster=None, on_done=None) -> bool:
        """Save the last `seconds` of play (background). False if nothing buffered."""
        with self._lock:
            items = list(self._replay.items)
            chunks = list(self._replay.audio)
            size = self._replay.size
        if not items:
            return False
        first = items[-1][0] - int(seconds * self.fps)
        items = [it for it in items if it[0] >= first]
        self._encode(items, size, path, _audio_after(chunks, items[0][0]), scale, transform, poster, on_done)
        return True

    def _encode(self, items, size, path, audio, scale, transform, poster, on_done) -> None:
        self.encoding += 1
        fps = self.fps

        def work():
            try:
                out = encode(items, size, path, audio, fps=fps, scale=scale, transform=transform,
                             poster=poster)
                err = None
            except Exception as e:                   # PyAV / Pillow missing, disk full, ...
                out, err = None, e
            finally:
                self.encoding -= 1
            if on_done is not None:
                try:
                    on_done(out, err)
                except Exception:
                    pass

        threading.Thread(target=work, name="video-encoder", daemon=True).start()
