"""InputRouter: merges several input sources into one press/release stream.

Every adaptor (keyboard, gamepad, touch, ...) reports button changes under its
own source name. The router keeps who holds what and forwards to the sink
(a Machine, an EmulatorSession, anything with press/release) only on the
first press and the last release of a button, so e.g. holding A on the
keyboard and on a gamepad and releasing one of them keeps A held.
"""
from __future__ import annotations

from src.bridge.base import normalize_button


class InputRouter:
    def __init__(self, sink):
        self.sink = sink
        self._holders: dict[str, set[str]] = {}

    def press(self, source: str, button: str) -> None:
        name = normalize_button(button)
        holders = self._holders.setdefault(name, set())
        first = not holders
        holders.add(source)
        if first:
            self._send("press", name)

    def release(self, source: str, button: str) -> None:
        name = normalize_button(button)
        holders = self._holders.get(name)
        if not holders or source not in holders:
            return
        holders.discard(source)
        if not holders:
            self._send("release", name)

    def set_held(self, source: str, buttons) -> None:
        """Make `source` hold exactly `buttons` (polled devices report full state)."""
        want = {normalize_button(b) for b in buttons}
        for name in [n for n, h in self._holders.items() if source in h and n not in want]:
            self.release(source, name)
        for name in want:
            if source not in self._holders.get(name, ()):
                self.press(source, name)

    def release_all(self, source: str | None = None) -> None:
        """Release everything `source` holds (or every source when None)."""
        for name, holders in list(self._holders.items()):
            if holders and (source is None or source in holders):
                if source is None:
                    holders.clear()
                    self._send("release", name)
                else:
                    self.release(source, name)

    def held(self, source: str | None = None) -> set[str]:
        return {n for n, h in self._holders.items() if h and (source is None or source in h)}

    def _send(self, action: str, name: str) -> None:
        if self.sink is None:
            return
        try:
            getattr(self.sink, action)(name)
        except ValueError:
            pass                 # the core has no such button (L/R on a Game Boy)
