"""One module per screen; each exposes build(app) -> ft.Control."""
from src.ui.views import about, appearance, library, moments, onboarding, player, settings, together

VIEWS = {
    "onboarding": onboarding.build,
    "about": about.build,
    "library": library.build,
    "player": player.build,
    "settings": settings.build,
    "moments": moments.build,
    "appearance": appearance.build,
    "together": together.build,
}
