import time
import pygame
from src.gameboy.ppu import WIDTH, HEIGHT

DMG_PALETTE = ((0xE0, 0xF8, 0xD0), (0x88, 0xC0, 0x70), (0x34, 0x68, 0x56), (0x08, 0x18, 0x20))
GRAY_PALETTE = ((0xFF, 0xFF, 0xFF), (0xAA, 0xAA, 0xAA), (0x55, 0x55, 0x55), (0x00, 0x00, 0x00))


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


class Display:
    def __init__(self, gb, scale: int = 3, title: str = "Game Boy", palette=DMG_PALETTE):
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
        self.frames = 0

    def run(self, max_frames=None) -> None:
        t0 = time.perf_counter()
        c0 = self.gb.total_cycles
        while self.running and (max_frames is None or self.frames < max_frames):
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    self.running = False
                elif event.type in (pygame.KEYDOWN, pygame.KEYUP) and self.on_key:
                    self.on_key(event)
            if not self.running:
                break
            self.gb.run_frame()
            surf = framebuffer_surface(self.gb.ppu.framebuffer, self.tables, self.palette)
            pygame.transform.scale(surf.convert(), self.screen.get_size(), self.screen)
            pygame.display.flip()
            self.frames += 1
            if self.frames % 30 == 0:
                wall = time.perf_counter() - t0
                if wall > 0:
                    speed = (self.gb.total_cycles - c0) / wall / 4_194_304 * 100
                    pygame.display.set_caption(f"{self.title} - {speed:.0f}%")
            self.clock.tick(60)
        pygame.display.quit()
