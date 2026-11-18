"""EmulatorRunner: one long-lived worker that owns a Machine and runs it in real time.

The UI never touches the machine directly. It talks to the runner through
a thread-safe command queue and reads two things back: the newest finished
frame and a small stats snapshot. This keeps the async UI loop free for
drawing and input while the emulator runs flat out on its own thread:

    runner = EmulatorRunner(lambda: bridge.load(path), audio=True)
    runner.start()
    runner.press("A"); runner.release("A")      # from any thread
    frame = runner.latest                       # Frame(seq, rgb, width, height) or None
    runner.stats                                # Stats(fps, drawn_fps, speed, skipped)
    runner.close()                              # saves battery RAM, stops audio

Real-time pacing
----------------
* With audio, the sound card is the clock (like the pygame window): after
  each frame the worker waits until the SDL queue drains to ``AUDIO_TARGET``
  seconds, so emulation runs at exactly the device's sample rate and the
  sound never drifts.
* Without audio (muted device, no SDL), a perf_counter schedule at the
  machine's refresh rate is used instead.

Frameskip
---------
Pure-Python cores can be slower than the console at drawing (the GBA PPU
alone can take more than a whole 16.7 ms frame in busy scenes). When the
worker falls behind (audio queue under ``AUDIO_LOW`` / schedule late) it
runs the next frames with ``render=False``. Game logic and audio stay at
100% speed, and only the number of drawn frames drops. At most ``MAX_SKIP``
frames in a row are skipped, so the picture keeps moving.

Rewind, save states, recording
------------------------------
Every ``INTERVAL`` emulated frames a snapshot goes into ``runner.rewind``
(:mod:`src.bridge.rewind`).  ``set_rewinding(True)`` walks back through them,
silent and about 3x faster than real time.  Every drawn frame is offered to
``runner.recorder`` (:mod:`src.bridge.recorder`), together with each frame's
sound, for MP4 / GIF recording and the "last 30 s" clip buffer.  ``submit(fn)`` runs ``fn(machine)`` on the worker
between two frames and returns a Future; save states use it so nothing ever
touches a core mid-frame.

Input
-----
Presses and releases are queued and applied at the start of the next frame,
in the worker thread, never in the middle of a CPU instruction. A press is
latched for at least one emulated frame, so a quick tap whose key-down and
key-up both arrive between two frames still reaches the game.
"""
from __future__ import annotations

import queue
import threading
import time
from array import array
from concurrent.futures import Future
from dataclasses import dataclass

from src.bridge.base import Machine, normalize_button
from src.bridge.recorder import Recorder
from src.bridge.rewind import RewindBuffer

AUDIO_TARGET = 0.095        # seconds of audio kept queued (latency / jitter cushion)
AUDIO_LOW = 0.060           # below this the worker is behind: skip drawing (measured:
                            # 35 ms gave ~7 underruns/s in heavy GBA scenes, 55-70 ms ~0)
MAX_SKIP = 3                # never skip more than this many frames in a row
FF_MULTIPLIER = 4           # frames per tick while the fast-forward hotkey is held
REWIND_STEPS_PER_SECOND = 30   # snapshots walked back per second while rewinding


@dataclass(frozen=True)
class Frame:
    seq: int                # increases with every drawn frame
    rgb: bytes              # packed RGB888, width * height * 3
    width: int
    height: int


@dataclass(frozen=True)
class Stats:
    fps: float = 0.0        # emulated frames per second (game speed)
    drawn_fps: float = 0.0  # frames actually drawn per second
    speed: float = 0.0      # emulated speed in % of the real console
    skipped: int = 0        # frames skipped in the last second


class _Volume:
    """Scales int16 samples (only allocates when the volume isn't 100%)."""

    def __init__(self) -> None:
        self.level = 1.0

    def apply(self, data: array) -> array:
        v = self.level
        if v >= 0.999:
            return data
        if v <= 0.001:
            return array("h", bytes(len(data) * 2))
        return array("h", [int(s * v) for s in data])


class EmulatorRunner:
    def __init__(self, factory, *, audio: bool = True, palette=None, name: str = "emulator",
                 rewind: bool = True, replay: bool = True):
        """``factory()`` builds the Machine. It is called in the worker
        thread, so a slow ROM load never blocks the UI."""
        self._factory = factory
        self._want_audio = audio
        self._palette = palette
        self._q: queue.SimpleQueue = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._main, name=name, daemon=True)
        self._volume = _Volume()
        self.machine: Machine | None = None
        self.audio = None
        self.error: str | None = None
        self.ready = threading.Event()          # set once the machine is loaded (or failed)
        self.latest: Frame | None = None
        self.stats = Stats()
        self.paused = False
        self.speed = 1                          # 1, 2, 4, 8 x
        self.fast_forward = False
        self.on_frame = None                    # optional callable(Frame), called in the worker
        self.frames = 0                         # emulated frames since start
        self.rewind: RewindBuffer | None = None
        self._want_rewind = rewind
        self.rewinding = False
        self.recorder = Recorder(replay=replay)
        self._stop = False
        self._seq = 0

    # ------------------------------------------------------------ control (any thread)
    def start(self) -> None:
        self._thread.start()

    def press(self, button: str) -> None:
        self._q.put(("press", normalize_button(button)))

    def release(self, button: str) -> None:
        self._q.put(("release", normalize_button(button)))

    def release_all(self) -> None:
        self._q.put(("release_all",))

    def set_paused(self, on: bool) -> None:
        self.paused = bool(on)
        self._q.put(("pause", self.paused))

    def set_speed(self, n: int) -> None:
        self.speed = max(1, int(n))

    def set_fast_forward(self, on: bool) -> None:
        self.fast_forward = bool(on)

    def set_volume(self, level: float) -> None:
        self._volume.level = max(0.0, min(1.0, float(level)))

    def set_palette(self, palette) -> None:
        self._q.put(("palette", palette))

    def reset(self) -> None:
        self._q.put(("reset",))

    def set_rewinding(self, on: bool) -> None:
        """Hold to rewind: walk back through recent snapshots until released."""
        self._q.put(("rewind", bool(on)))

    def submit(self, fn) -> Future:
        """Run ``fn(machine)`` on the emulator thread between two frames (works
        while paused). The Future resolves to its result or raises its error."""
        fut: Future = Future()
        if not self._thread.is_alive() and self.ready.is_set():
            fut.set_exception(RuntimeError("the emulator is not running"))
            return fut
        self._q.put(("call", fn, fut))
        return fut

    def close(self, timeout: float = 3.0) -> None:
        """Stop the worker; it saves battery RAM and closes audio on the way out."""
        self._stop = True
        self._q.put(("quit",))
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    # ------------------------------------------------------------ worker
    def _main(self) -> None:
        try:
            self.machine = self._factory()
        except Exception as e:                       # bad ROM, unreadable file, ...
            self.error = f"Couldn't load this ROM: {e}"
            self.paused = True
            self.ready.set()
            return
        if self._want_audio:
            self.audio = _open_audio(self.machine.apu)
        if self._want_rewind:
            self.rewind = RewindBuffer(self.machine.fps)
        self.recorder.fps = self.machine.fps
        self._publish()                              # first (blank) frame right away
        self.ready.set()
        try:
            self._loop()
        except Exception as e:                       # emulator crash: report, keep the save
            self.error = f"Emulation stopped: {type(e).__name__}: {e}"
            self.paused = True
        finally:
            self._shutdown()

    def _loop(self) -> None:
        m = self.machine
        period = 1.0 / m.fps
        next_t = time.perf_counter()
        held: set[str] = set()                       # what the core currently has pressed
        skipped_row = 0
        # one-second stats window
        win_t, win_frames, win_drawn, win_skipped = time.perf_counter(), 0, 0, 0

        while not self._stop:
            deferred = self._drain(held)
            if self._stop:
                break
            if self.rewinding and not self.error:
                self._rewind_step()
                next_t = time.perf_counter()
                continue
            if self.paused or self.error:
                time.sleep(0.02)
                next_t = time.perf_counter()
                win_t, win_frames, win_drawn, win_skipped = next_t, 0, 0, 0
                self.stats = Stats()
                continue

            n = self.speed * (FF_MULTIPLIER if self.fast_forward else 1)
            audio = self.audio
            realtime = n == 1
            if realtime and audio is not None and audio.started:
                behind = audio.queued() < audio.low
            else:
                behind = time.perf_counter() > next_t + period
            draw = not behind or skipped_row >= MAX_SKIP

            for i in range(n):
                m.run_frame(render=draw and i == n - 1)
            self.frames += n
            if self.rewind is not None and self.rewind.enabled:
                try:
                    self.rewind.tick(m, n)
                except Exception as e:                # a core that can't snapshot: no rewind,
                    self.rewind.enabled = False       # but the game keeps running
                    print(f"[runner] rewind disabled: {type(e).__name__}: {e}")
            # quick taps: a key pressed and released before this frame ran is
            # released only now, after the game has seen it for one frame
            for name in deferred:
                if name in held:
                    held.discard(name)
                    m.release(name)

            self._route_audio(m, audio, realtime)
            if draw:
                skipped_row = 0
                self._publish()
                win_drawn += 1
            else:
                skipped_row += 1
                win_skipped += 1
            win_frames += n

            now = time.perf_counter()
            if now - win_t >= 1.0:
                span = now - win_t
                fps = win_frames / span
                self.stats = Stats(fps=fps, drawn_fps=win_drawn / span,
                                   speed=fps / m.fps * 100, skipped=win_skipped)
                win_t, win_frames, win_drawn, win_skipped = now, 0, 0, 0

            # pacing
            if realtime and audio is not None and audio.started:
                audio.wait()                          # sound-card clock
                next_t = time.perf_counter()
            else:
                next_t += period                      # n frames per tick already = speed
                delay = next_t - time.perf_counter()
                if delay > 0:
                    time.sleep(delay)
                elif delay < -0.25:                   # hopelessly behind: don't spiral
                    next_t = time.perf_counter()

    def _drain(self, held: set[str]) -> list[str]:
        """Apply queued commands; returns releases deferred to after this frame."""
        m = self.machine
        pressed_now: set[str] = set()
        deferred: list[str] = []
        while True:
            try:
                cmd = self._q.get_nowait()
            except queue.Empty:
                return deferred
            op = cmd[0]
            if op == "press":
                name = cmd[1]
                if name not in held:
                    held.add(name)
                    pressed_now.add(name)
                    _safe(m.press, name)
                if name in deferred:
                    deferred.remove(name)
            elif op == "release":
                name = cmd[1]
                if name in pressed_now:
                    if name not in deferred:
                        deferred.append(name)         # tap: hold it for one frame first
                elif name in held:
                    held.discard(name)
                    _safe(m.release, name)
            elif op == "release_all":
                for name in list(held):
                    _safe(m.release, name)
                held.clear()
                deferred.clear()
            elif op == "pause":
                if cmd[1]:
                    for name in list(held):
                        _safe(m.release, name)
                    held.clear()
                    deferred.clear()
                if self.audio is not None:
                    self.audio.halt()
            elif op == "palette":
                self._palette = cmd[1]
                self._publish()
            elif op == "reset":
                m.reset()                             # saves battery RAM first
                held.clear()
                deferred.clear()
                self._core_changed()
            elif op == "rewind":
                on = cmd[1] and self.rewind is not None and len(self.rewind) > 0
                if on != self.rewinding:
                    self.rewinding = on
                    for name in list(held):           # no stuck buttons across time
                        _safe(m.release, name)
                    held.clear()
                    deferred.clear()
                    if self.audio is not None:
                        self.audio.halt()
            elif op == "call":
                fn, fut = cmd[1], cmd[2]
                if fut.set_running_or_notify_cancel():
                    core = m.core
                    try:
                        fut.set_result(fn(m))
                    except BaseException as e:
                        fut.set_exception(e)
                    if m.core is not core:            # a state file replaced the core
                        self._core_changed()
                    self._publish()
            elif op == "quit":
                self._stop = True
                return deferred

    def _route_audio(self, m, audio, realtime: bool) -> None:
        """This frame's sound goes to the recorder (always, in game time) and to the
        speakers (only at 1x; fast-forward is silent)."""
        apu, rec = m.apu, self.recorder
        wants = rec.wants_audio
        if audio is None and wants and not apu.sampling:
            apu.sampling = True                       # no sound card, but recordings want sound
        if not apu.sampling:
            return
        data = apu.take_samples()
        if wants:
            try:
                rec.feed_audio(data, self.frames)
            except Exception:
                pass
        if audio is not None and realtime:
            audio.pump(self._volume, data)

    def _core_changed(self) -> None:
        """The machine got a new core object (reset / state loaded from disk)."""
        self.recorder.fps = self.machine.fps
        if self.audio is not None:
            self.audio.retarget(self.machine.apu)
        if self.rewind is not None:
            self.rewind.clear()

    def _rewind_step(self) -> None:
        t0 = time.perf_counter()
        m = self.machine
        if self.rewind is None or not self.rewind.step_back(m):
            self.rewinding = False
            return
        m.apu.take_samples()                          # rewinding is silent
        self._publish(feed=False)
        delay = 1.0 / REWIND_STEPS_PER_SECOND - (time.perf_counter() - t0)
        if delay > 0:
            time.sleep(delay)

    def _publish(self, feed: bool = True) -> None:
        m = self.machine
        if m.system == "GB":
            rgb = m.frame_rgb(self._palette) if self._palette is not None else m.frame_rgb()
        else:
            rgb = m.frame_rgb()
        self._seq += 1
        frame = Frame(self._seq, rgb, m.width, m.height)
        self.latest = frame                           # atomic reference swap
        if feed:
            try:
                self.recorder.feed(rgb, m.width, m.height, self.frames)
            except Exception:
                pass
        cb = self.on_frame
        if cb is not None:
            try:
                cb(frame)
            except Exception:
                pass

    def _shutdown(self) -> None:
        while True:                                   # never leave a submit() hanging
            try:
                cmd = self._q.get_nowait()
            except queue.Empty:
                break
            if cmd[0] == "call" and cmd[2].set_running_or_notify_cancel():
                cmd[2].set_exception(RuntimeError("the emulator stopped"))
        if self.audio is not None:
            try:
                self.audio.close()
            except Exception:
                pass
            self.audio = None
        if self.machine is not None:
            try:
                self.machine.save()
            except Exception as e:
                print(f"[runner] battery save failed: {e}")


def _safe(fn, name: str) -> None:
    try:
        fn(name)
    except ValueError:
        pass                     # e.g. L/R on a Game Boy


# ---------------------------------------------------------------- audio
def _open_audio(apu):
    """SDL push-queue output (desktop). None when there is no SDL / device."""
    try:
        from src.core.gameboy.display import AudioOut
    except Exception:
        return None

    class RunnerAudio(AudioOut):
        def __init__(self, apu):
            super().__init__(apu, target=AUDIO_TARGET)
            self.low = int(AUDIO_LOW * self.RATE) * self.BYTES_PER_FRAME

        def pump(self, volume: _Volume | None = None, data=None) -> None:
            if data is None:
                data = self.apu.take_samples()
            if len(data) < 2:
                return
            q = self.queued()
            if self.started and q == 0:
                self.underruns += 1
            if q > self.max_bytes:
                self.dropped += 1
                return
            if volume is not None:
                data = volume.apply(data)
            raw = data.tobytes()
            self.sdl.SDL_QueueAudio(self.dev, raw, len(raw))
            self.played += 1
            if not self.started and q + len(raw) >= self.target:
                self.sdl.SDL_PauseAudioDevice(self.dev, 0)
                self.started = True

        def halt(self) -> None:
            """Pause: silence now, re-prime on resume (no stale burst)."""
            self.sdl.SDL_PauseAudioDevice(self.dev, 1)
            self.sdl.SDL_ClearQueuedAudio(self.dev)
            self.started = False
            self.apu.take_samples()

        def retarget(self, apu) -> None:
            self.apu.sampling = False
            self.halt()
            self.apu = apu
            apu.sampling = True

    try:
        return RunnerAudio(apu)
    except Exception as e:
        print(f"[runner] audio disabled: {e}")
        return None
