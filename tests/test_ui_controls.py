"""Per-system keyboard layouts: defaults, migration from the single layout, conflicts."""
import json

import pytest

flet = pytest.importorskip("flet")

from src.ui.data import settings as st  # noqa: E402
from src.ui.views.settings import assign_key  # noqa: E402


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FLET_APP_STORAGE_DATA", str(tmp_path))
    return tmp_path


def test_defaults_are_per_system(data_dir):
    s = st.Settings.load()
    assert set(s.keyboards) == {"GB", "GBC", "GBA", "NES"}
    assert "L" not in s.keyboard_for("GB") and "R" not in s.keyboard_for("GBC")
    assert s.keyboard_for("GBA")["L"][0] == "Q"
    assert s.hotkeys["fast_forward"] != "Tab"


def test_systems_are_independent(data_dir):
    s = st.Settings.load()
    s.keyboards["GBC"]["A"][0] = "M"
    s.save()
    s2 = st.Settings.load()
    assert s2.keyboard_for("GBC")["A"][0] == "M"
    assert s2.keyboard_for("GB")["A"][0] == "K"
    assert s2.keyboard_for("GBA")["A"][0] == "K"


def test_legacy_single_layout_migrates(data_dir):
    legacy = {"keyboard": {b: list(k) for b, k in st.DEFAULT_KEYBOARD_GBA.items()},
              "hotkeys": {"fast_forward": "Tab"}}
    legacy["keyboard"]["A"] = ["L", ""]
    (data_dir / "settings.json").write_text(json.dumps(legacy), encoding="utf-8")
    s = st.Settings.load()
    for sysname in st.SYSTEMS:
        assert s.keyboards[sysname]["A"][0] == "L"
    assert s.keyboards["GB"]["L"] == ["", ""]          # no shoulder buttons on GB
    assert s.hotkeys["fast_forward"] == "F"            # Tab default migrated away


def test_corrupt_keyboards_fall_back(data_dir):
    (data_dir / "settings.json").write_text(json.dumps({"keyboards": {"GB": {"A": 5}, "GBA": "x"}}),
                                            encoding="utf-8")
    s = st.Settings.load()
    assert s.keyboard_for("GB")["A"] == ["K", "X"]
    assert s.keyboard_for("GBA")["L"][0] == "Q"


def test_assign_key_clears_conflicts():
    kbs = st.default_keyboards()
    hot = {k: v[1] for k, v in st.HOTKEYS.items()}
    # binding P (the pause hotkey) to A on GBC removes it from the hotkey
    assign_key(kbs, hot, "button", "A", 0, "P", "GBC")
    assert kbs["GBC"]["A"][0] == "P" and hot["pause"] == ""
    assert kbs["GB"]["A"][0] == "K"                    # other systems untouched
    # binding K (A on every layout) to a hotkey removes it from every layout
    assign_key(kbs, hot, "hotkey", "screenshot", 0, "K", "GBA")
    assert hot["screenshot"] == "K"
    assert all("K" not in kbs[s]["A"] for s in st.SYSTEMS)
    # within one layout a key drives one button
    assign_key(kbs, hot, "button", "B", 1, "Arrow Up", "GBA")
    assert kbs["GBA"]["B"][1] == "Arrow Up" and "Arrow Up" not in kbs["GBA"]["Up"]
    assert "Arrow Up" in kbs["GB"]["Up"]
