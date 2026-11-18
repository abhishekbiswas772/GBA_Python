import os
from pathlib import Path

_ENV = Path(__file__).resolve().parents[1] / ".env"
if _ENV.is_file():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _value = _line.split("=", 1)
        os.environ.setdefault(_key.strip(), _value.strip().strip('"').strip("'"))

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")

import pytest


def pytest_addoption(parser):
    parser.addoption("--slow", action="store_true", default=False,
                     help="run slow tests (Blargg ROM integration)")


@pytest.fixture(autouse=True)
def _isolated_save_folder(tmp_path, monkeypatch):
    """Tests never write to the real ./saves: the save folder points at a temp dir,
    and the cores go back to "next to the ROM" after every test."""
    from src.core import saves
    from src.ui.data import settings
    monkeypatch.setattr(settings.Settings, "save_folder", lambda self: tmp_path / "saves")
    yield
    saves.set_save_dir(None)


def pytest_collection_modifyitems(config, items):
    if config.getoption("--slow"):
        return
    skip = pytest.mark.skip(reason="need --slow option to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip)
