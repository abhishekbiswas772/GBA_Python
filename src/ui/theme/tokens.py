"""Design tokens: the four app themes, console colors, GB palettes and screen shaders.

Values are taken one-to-one from the "Emulator Desktop UI" design canvas.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    desc: str
    bg: str          # page background
    panel: str       # sidebars, headers, footers
    card: str        # cards and inputs
    card2: str       # selected nav / segmented thumb
    line: str        # hairline borders
    line2: str       # control borders
    text: str
    sub: str         # secondary text (nav links)
    muted: str       # tertiary text (captions)
    accent: str
    on: str          # text on accent
    soft: str        # accent-tinted fill
    screen: str      # game screen placeholder
    gb: str          # console colors
    gbc: str
    gba: str
    rec: str         # recording red
    gold: str        # achievements
    shell: str       # handheld frame around exported video
    nes: str = "#FF7A6B"
    ps1: str = "#8E9BFF"

    def system(self, system: str) -> str:
        return {"GB": self.gb, "GBC": self.gbc, "GBA": self.gba, "NES": self.nes,
                "PS1": self.ps1}.get(system, self.accent)


THEMES: dict[str, Theme] = {
    "midnight": Theme(
        "midnight", "Midnight", "Calm dark, blue accent",
        bg="#0F1115", panel="#13161C", card="#171A21", card2="#232836", line="#232733",
        line2="#2A2F3A", text="#E8EAF0", sub="#C9CED8", muted="#9AA3B2", accent="#8EA2FF",
        on="#0F1115", soft="#1A1F2E", screen="#000000", gb="#9FD356", gbc="#F2A65A",
        gba="#8EA2FF", rec="#FF7A7A", gold="#F2C14E", shell="#2B2F7A",
    ),
    "daylight": Theme(
        "daylight", "Daylight", "Bright and clean",
        bg="#F4F5F8", panel="#FFFFFF", card="#FFFFFF", card2="#ECEEF3", line="#E2E5EB",
        line2="#D3D8E0", text="#14171F", sub="#363C49", muted="#5A6271", accent="#3D55D6",
        on="#FFFFFF", soft="#E8ECFF", screen="#0B0D10", gb="#3F7D1F", gbc="#B3561A",
        gba="#3D55D6", rec="#C93434", gold="#C08A12", shell="#C9CCE8", nes="#C8372D",
    ),
    "dmg": Theme(
        "dmg", "DMG Green", "The 1989 pea-soup look",
        bg="#0E1A0B", panel="#13230F", card="#182C13", card2="#24401C", line="#243A1C",
        line2="#335028", text="#E6F2CC", sub="#C8DBA4", muted="#A2B880", accent="#9BBC0F",
        on="#0F380F", soft="#22391B", screen="#0F380F", gb="#9BBC0F", gbc="#E8B04A",
        gba="#A9B8FF", rec="#FF8A7A", gold="#E8C64A", shell="#B9B8B0",
    ),
    "berry": Theme(
        "berry", "Berry", "Color-era purple and pink",
        bg="#16101B", panel="#1D1523", card="#241A2C", card2="#35263F", line="#2E2238",
        line2="#3D2E4A", text="#F3EAF7", sub="#D9CBE1", muted="#B3A1BF", accent="#F07BB0",
        on="#16101B", soft="#33243F", screen="#000000", gb="#A6D86A", gbc="#F2A65A",
        gba="#9FB0FF", rec="#FF7A7A", gold="#F2C14E", shell="#7A2E5C",
    ),
}

THEME_ORDER = ["midnight", "daylight", "dmg", "berry"]

# Theme picked by "Match theme to console".
CONSOLE_THEME = {"GB": "dmg", "GBC": "berry", "GBA": "midnight", "NES": "midnight", "PS1": "midnight"}

SYSTEM_NAMES = {"GB": "Game Boy", "GBC": "Game Boy Color", "GBA": "Game Boy Advance", "NES": "NES",
                "PS1": "PlayStation (experimental)"}
SYSTEM_RES = {"GB": "160×144 · DMG", "GBC": "160×144 · Color", "GBA": "240×160", "NES": "256×240 · NTSC",
              "PS1": "320×240 · Rust core"}


@dataclass(frozen=True)
class GBPalette:
    key: str
    name: str
    colors: tuple[str, str, str, str]   # lightest -> darkest

    def rgb(self) -> tuple[tuple[int, int, int], ...]:
        return tuple(tuple(int(c[i:i + 2], 16) for i in (1, 3, 5)) for c in self.colors)


GB_PALETTES: dict[str, GBPalette] = {
    "pea": GBPalette("pea", "Pea green", ("#E0F8D0", "#88C070", "#346856", "#081820")),
    "pocket": GBPalette("pocket", "Pocket grey", ("#E4E4D8", "#A8A89C", "#5C5C54", "#1C1C18")),
    "light": GBPalette("light", "Backlit teal", ("#D8F4F0", "#62C6B8", "#1F7A72", "#06302C")),
    "sunset": GBPalette("sunset", "Sunset", ("#FFE9C8", "#F2A65A", "#B2463A", "#2A0F1E")),
}


@dataclass(frozen=True)
class Shader:
    key: str
    name: str
    desc: str


SHADERS: list[Shader] = [
    Shader("pixel", "Pixel-perfect", "Raw, sharp pixels"),
    Shader("lcd", "LCD grid", "Handheld subpixel look"),
    Shader("crt", "CRT scanlines", "Retro TV glow"),
    Shader("smooth", "Smooth (xBRZ)", "Upscaled, rounded art"),
    Shader("bilinear", "Soft", "Bilinear filter"),
]
SHADER_BY_KEY = {s.key: s for s in SHADERS}

# Overlay colors used on top of the game screen (same in every theme);
# apply opacity with components.primitives.alpha().
OVERLAY_BG = "#0F1115"         # used at 0.88
OVERLAY_TEXT = "#F2F3F6"
OVERLAY_SUB = "#B8BFCC"
SCREEN_LABEL = "#8A93A3"
