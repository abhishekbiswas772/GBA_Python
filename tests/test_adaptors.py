"""Input adaptors (src/bridge/adaptors): router, keyboard, gamepad mapping, touch."""
import pytest

from src.bridge.adaptors import (
    DPad, InputRouter, KeyboardAdaptor, PRESET_APP, PRESET_CLASSIC, TouchAdaptor,
    TouchButton, TouchLayout, gba_layout, held_from_state, normalize_key,
)
from src.bridge.adaptors.console_controller import (
    AXIS_LT, AXIS_LX, AXIS_LY, AXIS_MAX, BTN_A, BTN_DOWN, BTN_RB, BTN_START,
)


class Sink:
    def __init__(self):
        self.log = []

    def press(self, b):
        self.log.append(("press", b))

    def release(self, b):
        self.log.append(("release", b))


def test_router_merges_sources():
    s = Sink()
    r = InputRouter(s)
    r.press("keyboard", "a")
    r.press("gamepad", "A")
    r.release("keyboard", "A")
    assert s.log == [("press", "A")]          # still held by the gamepad
    r.release("gamepad", "A")
    assert s.log == [("press", "A"), ("release", "A")]
    r.release("gamepad", "A")                 # releasing again is a no-op
    assert len(s.log) == 2


def test_router_set_held_and_release_all():
    s = Sink()
    r = InputRouter(s)
    r.set_held("pad", {"Up", "A"})
    r.set_held("pad", {"A", "B"})
    assert r.held("pad") == {"A", "B"}
    assert ("release", "Up") in s.log and ("press", "B") in s.log
    r.press("keys", "Start")
    r.release_all()
    assert r.held() == set()


def test_router_ignores_buttons_the_core_lacks():
    class GB:
        def press(self, b):
            if b in ("L", "R"):
                raise ValueError(b)
    r = InputRouter(GB())
    r.press("keys", "L")                      # Game Boy has no L: swallowed
    with pytest.raises(ValueError):
        r.press("keys", "Turbo")              # not a console button at all


@pytest.mark.parametrize("raw,norm", [
    ("Arrow Up", "ARROW UP"), ("up", "ARROW UP"), ("return", "ENTER"), ("Enter", "ENTER"),
    ("right shift", "SHIFT RIGHT"), ("k", "K"), ("space", " "), (" ", " "),
])
def test_normalize_key_flet_and_pygame(raw, norm):
    assert normalize_key(raw) == norm


def test_keyboard_presets_and_remap():
    s = Sink()
    kb = KeyboardAdaptor(InputRouter(s), PRESET_APP)
    assert kb.key_down("K") and kb.key_down("Arrow Left")
    assert not kb.key_down("F5")              # unmapped: not consumed
    assert kb.key_up("k")
    assert s.log == [("press", "A"), ("press", "Left"), ("release", "A")]
    kb.set_mapping(PRESET_CLASSIC)            # remap releases what was held
    assert ("release", "Left") in s.log
    assert kb.button_for("x") == "A" and kb.button_for("right shift") == "Select"


def test_keyboard_rerender_keeps_held_keys():
    s = Sink()
    kb = KeyboardAdaptor(InputRouter(s), PRESET_APP)
    kb.key_down("K")
    kb.set_mapping(PRESET_APP)                # same mapping (UI re-render): nothing released
    assert s.log == [("press", "A")]


def test_gamepad_state_mapping():
    held = held_from_state({BTN_A: True, BTN_START: True, BTN_RB: True}, {})
    assert held == {"A", "Start", "R"}
    held = held_from_state({BTN_DOWN: True}, {AXIS_LX: -AXIS_MAX, AXIS_LY: 1000, AXIS_LT: AXIS_MAX})
    assert held == {"Down", "Left", "L"}      # stick left, small Y ignored, trigger = L
    assert held_from_state({}, {AXIS_LX: AXIS_MAX // 4}) == set()   # inside the deadzone


def test_gamepad_adaptor_without_controllers():
    from src.bridge.adaptors import GamepadAdaptor
    s = Sink()
    pads = GamepadAdaptor(InputRouter(s))
    assert pads.poll() == set() or pads.connected   # no pad attached in CI: nothing held
    pads.close()


def test_touch_dpad_sectors_and_slide():
    pad = DPad(100, 100, 50)
    assert pad.buttons_at(150, 100) == ("Right",)
    assert pad.buttons_at(100, 60) == ("Up",)
    assert set(pad.buttons_at(135, 65)) == {"Up", "Right"}          # diagonal
    assert pad.buttons_at(102, 101) == ()                            # deadzone
    s = Sink()
    t = TouchAdaptor(InputRouter(s), TouchLayout(dpad=pad, buttons=[TouchButton("A", 300, 100, 30)]))
    t.down(1, 150, 100)
    t.move(1, 100, 150)                       # thumb rolls from Right to Down
    t.down(2, 300, 100)                       # second finger on A
    assert t.held() == {"Down", "A"}
    t.up(1)
    assert t.held() == {"A"}
    t.cancel()
    assert s.log[-1] == ("release", "A")


def test_gba_layout_has_every_button():
    names = {b.name for b in gba_layout(520, 260).buttons}
    assert names == {"A", "B", "Start", "Select", "L", "R"}
    assert {b.name for b in gba_layout(520, 260, shoulders=False).buttons} == {"A", "B", "Start", "Select"}
