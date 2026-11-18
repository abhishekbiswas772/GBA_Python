"""Bundled fonts (OFL): Silkscreen (pixel) for headings and the logo, Outfit for UI text,
JetBrains Mono for numbers and key names.

Each weight is registered as its own family so text renders with the real face
instead of synthesized bold, on every platform and offline.
"""
from __future__ import annotations

SANS = "Outfit"
SANS_MEDIUM = "Outfit Medium"
SANS_SEMI = "Outfit SemiBold"
DISPLAY = "Silkscreen"
DISPLAY_BOLD = "Silkscreen Bold"
MONO = "JetBrains Mono"
MONO_MEDIUM = "JetBrains Mono Medium"

# family -> path relative to the assets dir
FONTS: dict[str, str] = {
    SANS: "fonts/Outfit-Regular.ttf",
    SANS_MEDIUM: "fonts/Outfit-Medium.ttf",
    SANS_SEMI: "fonts/Outfit-SemiBold.ttf",
    DISPLAY: "fonts/Silkscreen-Regular.ttf",
    DISPLAY_BOLD: "fonts/Silkscreen-Bold.ttf",
    MONO: "fonts/JetBrainsMono-Regular.ttf",
    MONO_MEDIUM: "fonts/JetBrainsMono-Medium.ttf",
}


def family(kind: str = "sans", weight: int = 400) -> str:
    """Pick the bundled face closest to a CSS font-weight."""
    if kind == "mono":
        return MONO_MEDIUM if weight >= 500 else MONO
    if kind == "display":
        return DISPLAY_BOLD if weight >= 700 else DISPLAY
    if weight >= 600:
        return SANS_SEMI
    return SANS_MEDIUM if weight >= 500 else SANS


def display_size(size: float) -> float:
    """Pixel fonts are wide and look crisp on a 4px grid: scale the design's
    heading sizes down a little and snap them (28 -> 24, 18 -> 16, 16 -> 12)."""
    return max(12, round(size * 0.85 / 4) * 4)
