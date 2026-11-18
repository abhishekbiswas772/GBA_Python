"""pygame window for the NES core (256x240 RGB888 framebuffer, scaled)."""
import time

import pygame

from src.core.nes.common import CPU_HZ, HEIGHT, WIDTH

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


def key_handler(nes):
    def on_key(event):
        button = KEYMAP.get(event.key)
        if button is None:
            return
        if event.type == pygame.KEYDOWN:
            if not getattr(event, "repeat", False):
                nes.press(button)
        else:
            nes.release(button)
    return on_key


class Display:
    def __init__(self, nes, scale: int = 3, title: str = "NES", mute: bool = False):
        pygame.display.init()
        self.nes = nes
        self.screen = pygame.display.set_mode((WIDTH * scale, HEIGHT * scale))
        pygame.display.set_caption(title)
        self.clock = pygame.time.Clock()
        self.title = title
        self.running = True
        self.on_key = key_handler(nes)
        self.on_frame = None          # optional callable() before each frame (gamepad polling)
        self.frames = 0
        self._skipped = 0
        self.audio = None
        if not mute:
            try:
                from src.core.gameboy.display import AudioOut
                self.audio = AudioOut(nes.apu)
            except pygame.error as e:
                print(f"[audio disabled: {e}]")

    def run(self, max_frames=None) -> None:
        t0 = time.perf_counter()
        c0 = self.nes.cpu.cyc
        ppu = self.nes.ppu
        try:
            while self.running and (max_frames is None or self.frames < max_frames):
                for event in pygame.event.get():
                    if event.type == pygame.QUIT:
                        self.running = False
                    elif event.type in (pygame.KEYDOWN, pygame.KEYUP):
                        self.on_key(event)
                if not self.running:
                    break
                if self.on_frame:
                    self.on_frame()
                # frameskip when the sound queue runs low (game + audio stay at 100%)
                draw = self._skipped >= 3 or not (self.audio and self.audio.started
                                                  and self.audio.queued() < self.audio.low)
                ppu.skip_render = not draw
                try:
                    self.nes.run_frame()
                finally:
                    ppu.skip_render = False
                if self.audio:
                    self.audio.pump()
                if draw:
                    self._skipped = 0
                    surf = pygame.image.frombuffer(bytes(ppu.rgb), (WIDTH, HEIGHT), "RGB")
                    pygame.transform.scale(surf, self.screen.get_size(), self.screen)
                    pygame.display.flip()
                else:
                    self._skipped += 1
                self.frames += 1
                if self.frames % 30 == 0:
                    wall = time.perf_counter() - t0
                    if wall > 0:
                        speed = (self.nes.cpu.cyc - c0) / wall / CPU_HZ * 100
                        pygame.display.set_caption(f"{self.title} - {speed:.0f}%")
                if self.audio and self.audio.started:
                    self.audio.wait()
                else:
                    self.clock.tick(60)
        finally:
            if self.audio:
                self.audio.close()
                self.audio = None
            pygame.display.quit()
