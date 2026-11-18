"""One module per screen; each exposes build(app) -> ft.Control."""
from src.ui.views import appearance, library, moments, player, settings, together

VIEWS = {
    "library": library.build,
    "player": player.build,
    "settings": settings.build,
    "moments": moments.build,
    "appearance": appearance.build,
    "together": together.build,
}
