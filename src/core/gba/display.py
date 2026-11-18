"""pygame window for the GBA core (240x160 BGR555 framebuffer, scaled)."""
import time

import pygame

from src.core.gba.common import WIDTH, HEIGHT, CPU_HZ

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
    pygame.K_a: "l",
    pygame.K_s: "r",
}

# GBA colour: bits 0-4 red, 5-9 green, 10-14 blue
RGB555_MASKS = (0x001F, 0x03E0, 0x7C00, 0)


def framebuffer_surface(fb: bytes) -> pygame.Surface:
    surf = pygame.Surface((WIDTH, HEIGHT), 0, 16, RGB555_MASKS)
    surf.get_buffer().write(bytes(fb))
    return surf


def key_handler(gba):
    def on_key(event):
        button = KEYMAP.get(event.key)
        if button is None:
            return
        if event.type == pygame.KEYDOWN:
            if not getattr(event, "repeat", False):
                gba.press(button)
        else:
            gba.release(button)
    return on_key


class Display:
    def __init__(self, gba, scale: int = 3, title: str = "Game Boy Advance", mute: bool = False):
        pygame.display.init()
        self.gba = gba
        self.scale = scale
        self.screen = pygame.display.set_mode((WIDTH * scale, HEIGHT * scale))
        pygame.display.set_caption(title)
        self.surface = pygame.Surface((WIDTH, HEIGHT), 0, 16, RGB555_MASKS)
        self.clock = pygame.time.Clock()
        self.title = title
        self.running = True
        self.on_key = key_handler(gba)
        self.on_frame = None          # optional callable() before each frame (gamepad polling)
        self.frames = 0
        self.audio = None
        if not mute and gba.apu is not None:
            try:
                from src.core.gameboy.display import AudioOut
                self.audio = AudioOut(gba.apu)
            except pygame.error as e:
                print(f"[audio disabled: {e}]")

    def run(self, max_frames=None) -> None:
        t0 = time.perf_counter()
        c0 = self.gba.cpu.now
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
                self.gba.run_frame()
                if self.audio:
                    self.audio.pump()
                self.surface.get_buffer().write(bytes(self.gba.ppu.fb))   # proxy freed at once
                pygame.transform.scale(self.surface.convert(), self.screen.get_size(), self.screen)
                pygame.display.flip()
                self.frames += 1
                if self.frames % 30 == 0:
                    wall = time.perf_counter() - t0
                    if wall > 0:
                        speed = (self.gba.cpu.now - c0) / wall / CPU_HZ * 100
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
