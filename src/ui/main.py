"""Entry point for `flet run src/ui/main.py` and `python -m src.ui`.

Packaged builds use the repository `main.py` instead. With no arguments that
opens this same app.

Puts the repository root on sys.path so `src.core.gameboy` (the emulator core) and
`src.ui` import the same way they do from `python -m src.ui`.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.ui.app import main, run  # noqa: E402

if __name__ == "__main__":
    run()
