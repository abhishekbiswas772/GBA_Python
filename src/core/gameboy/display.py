import time
import pygame
from src.core.gameboy.common import WIDTH, HEIGHT, SAMPLE_RATE, DMG_PALETTE, GRAY_PALETTE


def rgb_tables(palette=DMG_PALETTE):
    r = bytes(palette[i & 3][0] for i in range(256))
    g = bytes(palette[i & 3][1] for i in range(256))
    b = bytes(palette[i & 3][2] for i in range(256))
    return r, g, b


def framebuffer_rgb(fb, tables=None, palette=DMG_PALETTE) -> bytes:
    if tables is None:
        tables = rgb_tables(palette)
    n = WIDTH * HEIGHT
    src = bytes(fb)
    planar = src.translate(tables[0]) + src.translate(tables[1]) + src.translate(tables[2])
    out = bytearray(n * 3)
    out[0::3] = planar[:n]
    out[1::3] = planar[n:2 * n]
    out[2::3] = planar[2 * n:]
    return bytes(out)


def framebuffer_surface(fb, tables=None, palette=DMG_PALETTE):
    return pygame.image.frombytes(framebuffer_rgb(fb, tables, palette), (WIDTH, HEIGHT), "RGB")


def cgb_surface(ppu):
    """CGB mode: the PPU already produced RGB888 (RGB555 expanded, no colour correction)."""
    return pygame.image.frombytes(bytes(ppu.rgb), (WIDTH, HEIGHT), "RGB")


KEYMAP = {
    pygame.K_RIGHT: "right",
    pygame.K_LEFT: "left",
    pygame.K_UP: "up",
    pygame.K_DOWN: "down",
    pygame.K_x: "a",
    pygame.K_z: "b",
    pygame.K_RETURN: "start",
    pygame.K_RSHIFT: "select",
    pygame.K_BACKSPACE: "select",
}


def joypad_key_handler(joypad):
    def on_key(event):
        button = KEYMAP.get(event.key)
        if button is None:
            return
        if event.type == pygame.KEYDOWN:
            if not getattr(event, "repeat", False):
                joypad.press(button)
        else:
            joypad.release(button)
    return on_key


def _load_sdl2():
    """ctypes handle to the SDL2 library pygame ships with (falls back to a system SDL2)."""
    import ctypes
    import ctypes.util
    import glob
    import os
    base = os.path.dirname(pygame.__file__)
    cands = [os.path.join(base, "SDL2.dll")]
    cands += sorted(glob.glob(os.path.join(base, ".dylibs", "libSDL2*.dylib")))
    cands += sorted(glob.glob(os.path.join(os.path.dirname(base), "pygame.libs", "libSDL2*.so*")))
    found = ctypes.util.find_library("SDL2")
    if found:
        cands.append(found)
    for c in cands:
        if os.path.exists(c) or c == found:
            try:
                return ctypes.CDLL(c)
            except OSError:
                pass
    raise pygame.error("SDL2 library not found")


class AudioOut:
    """Streams APU samples to the default audio device (44100 Hz, S16 stereo)
    through SDL's push queue (SDL_QueueAudio): every frame's samples are appended
    to one continuous device queue, so playback is gapless and needs no Python
    callback on the audio thread (no GIL stalls). The queue fill level is the
    pacing clock: Display.run waits for it to drain to `target` before emulating
    the next frame, so emulation is locked to the sound card's rate. SDL converts
    if the device wants a different format/rate (allowed_changes=0: no pitch
    shift on 48 kHz-only devices)."""

    RATE = SAMPLE_RATE
    BYTES_PER_FRAME = 4                       # S16 stereo

    def __init__(self, apu, target: float = 0.095, max_latency: float = 0.25):
        import ctypes
        c = ctypes

        class Spec(c.Structure):
            _fields_ = [("freq", c.c_int), ("format", c.c_uint16), ("channels", c.c_uint8),
                        ("silence", c.c_uint8), ("samples", c.c_uint16), ("padding", c.c_uint16),
                        ("size", c.c_uint32), ("callback", c.c_void_p), ("userdata", c.c_void_p)]

        try:
            sdl = _load_sdl2()
        except OSError as e:
            raise pygame.error(str(e))
        sdl.SDL_InitSubSystem.argtypes = [c.c_uint32]
        sdl.SDL_QuitSubSystem.argtypes = [c.c_uint32]
        sdl.SDL_GetError.restype = c.c_char_p
        sdl.SDL_OpenAudioDevice.restype = c.c_uint32
        sdl.SDL_OpenAudioDevice.argtypes = [c.c_char_p, c.c_int, c.POINTER(Spec),
                                            c.POINTER(Spec), c.c_int]
        sdl.SDL_QueueAudio.argtypes = [c.c_uint32, c.c_void_p, c.c_uint32]
        sdl.SDL_GetQueuedAudioSize.restype = c.c_uint32
        sdl.SDL_GetQueuedAudioSize.argtypes = [c.c_uint32]
        sdl.SDL_ClearQueuedAudio.argtypes = [c.c_uint32]
        sdl.SDL_PauseAudioDevice.argtypes = [c.c_uint32, c.c_int]
        sdl.SDL_CloseAudioDevice.argtypes = [c.c_uint32]
        self.sdl = sdl
        if sdl.SDL_InitSubSystem(0x10) != 0:              # SDL_INIT_AUDIO
            raise pygame.error(sdl.SDL_GetError().decode(errors="replace"))
        want = Spec(self.RATE, 0x8010, 2, 0, 512, 0, 0, None, None)   # AUDIO_S16LSB
        have = Spec()
        self.dev = sdl.SDL_OpenAudioDevice(None, 0, c.byref(want), c.byref(have), 0)
        if not self.dev:
            err = sdl.SDL_GetError().decode(errors="replace")
            sdl.SDL_QuitSubSystem(0x10)
            raise pygame.error(err)
        self.apu = apu
        self.target = int(target * self.RATE) * self.BYTES_PER_FRAME
        self.low = int(0.060 * self.RATE) * self.BYTES_PER_FRAME    # below: behind real time
        self.max_bytes = int(max_latency * self.RATE) * self.BYTES_PER_FRAME
        self.started = False
        self.played = 0                       # frames of samples queued
        self.underruns = 0                    # queue ran dry before the next frame
        self.dropped = 0                      # frames dropped (queue over max_latency)
        apu.sampling = True

    def queued(self) -> int:
        """Bytes still waiting in the device queue."""
        return self.sdl.SDL_GetQueuedAudioSize(self.dev)

    def pump(self) -> None:
        data = self.apu.take_samples()
        if len(data) < 2:
            return
        q = self.queued()
        if self.started and q == 0:
            self.underruns += 1
        if q > self.max_bytes:
            self.dropped += 1                 # far ahead (e.g. after a stall): skip
            return
        raw = data.tobytes()
        self.sdl.SDL_QueueAudio(self.dev, raw, len(raw))
        self.played += 1
        if not self.started and q + len(raw) >= self.target:
            self.sdl.SDL_PauseAudioDevice(self.dev, 0)       # primed: start playback
            self.started = True

    def wait(self, timeout: float = 0.1) -> None:
        """Block until the queue has drained to the target fill (audio-clock pacing)."""
        if not self.started:
            return
        deadline = time.perf_counter() + timeout
        while self.queued() > self.target and time.perf_counter() < deadline:
            time.sleep(0.001)

    def close(self) -> None:
        self.apu.sampling = False
        self.apu.take_samples()
        if self.dev:
            self.sdl.SDL_PauseAudioDevice(self.dev, 1)
            self.sdl.SDL_ClearQueuedAudio(self.dev)
            self.sdl.SDL_CloseAudioDevice(self.dev)
            self.dev = 0
            self.sdl.SDL_QuitSubSystem(0x10)


class Display:
    def __init__(self, gb, scale: int = 3, title: str = "Game Boy", palette=DMG_PALETTE,
                 mute: bool = False):
        pygame.display.init()
        pygame.font.init()
        self.gb = gb
        self.scale = scale
        self.palette = palette
        self.tables = rgb_tables(palette)
        self.screen = pygame.display.set_mode((WIDTH * scale, HEIGHT * scale))
        pygame.display.set_caption(title)
        self.clock = pygame.time.Clock()
        self.title = title
        self.running = True
        self.on_key = None
        self.on_frame = None          # optional callable() before each frame (gamepad polling)
        self.frames = 0
        self._skipped = 0
        self.audio = None
        if not mute:
            try:
                self.audio = AudioOut(gb.apu)
            except pygame.error as e:
                print(f"[audio disabled: {e}]")

    def run(self, max_frames=None) -> None:
        t0 = time.perf_counter()
        c0 = self.gb.total_cycles
        try:
            while self.running and (max_frames is None or self.frames < max_frames):
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        self.running = False
                    elif event.type in (pygame.KEYDOWN, pygame.KEYUP) and self.on_key:
                        self.on_key(event)
                if not self.running:
                    break
                if self.on_frame:
                    self.on_frame()
                draw = self._skipped >= 3 or not (self.audio and self.audio.started
                                                  and self.audio.queued() < self.audio.low)
                self.gb.ppu.skip_render = not draw        # frameskip when behind real time
                try:
                    self.gb.run_frame()
                finally:
                    self.gb.ppu.skip_render = False
                if self.audio:
                    self.audio.pump()
                if draw:
                    self._skipped = 0
                    if self.gb.cgb:
                        surf = cgb_surface(self.gb.ppu)
                    else:
                        surf = framebuffer_surface(self.gb.ppu.framebuffer, self.tables, self.palette)
                    pygame.transform.scale(surf.convert(), self.screen.get_size(), self.screen)
                    pygame.display.flip()
                else:
                    self._skipped += 1
                self.frames += 1
                if self.frames % 30 == 0:
                    wall = time.perf_counter() - t0
                    if wall > 0:
                        speed = (self.gb.total_cycles - c0) / wall / 4_194_304 * 100
                        pygame.display.set_caption(f"{self.title} - {speed:.0f}%")
                if self.audio and self.audio.started:
                    self.audio.wait()         # paced by the sound card clock
                else:
                    self.clock.tick(60)
        finally:
            if self.audio:
                self.audio.close()
                self.audio = None
            pygame.display.quit()
