"""Rewind: a ring buffer of in-memory save states.

While a game runs, the runner calls :meth:`RewindBuffer.tick` once per
emulated frame, and every ``interval`` frames a snapshot is pushed (a few
KB each, zlib-compressed).  Holding rewind calls :meth:`step_back` once per
display tick: it restores the newest snapshot and drops it, so the game
runs backwards ``interval`` frames per tick.

Memory is bounded both by count (``seconds`` of history) and by bytes
(``max_bytes``); the oldest snapshots go first.
"""
from __future__ import annotations

from collections import deque

from src.bridge import savestate

INTERVAL = 6                 # frames between snapshots (10 per second at 60 Hz)
SECONDS = 30                 # history kept
MAX_BYTES = 96 * 1024 * 1024


class RewindBuffer:
    def __init__(self, fps: float = 60.0, *, interval: int = INTERVAL, seconds: float = SECONDS,
                 max_bytes: int = MAX_BYTES):
        self.interval = max(1, int(interval))
        self.capacity = max(2, int(seconds * fps / self.interval))
        self.max_bytes = max_bytes
        self._snaps: deque = deque()
        self._bytes = 0
        self._count = 0              # frames since the last snapshot
        self.enabled = True

    def __len__(self) -> int:
        return len(self._snaps)

    @property
    def seconds(self) -> float:
        """How far back you can currently go (at 60 Hz)."""
        return len(self._snaps) * self.interval / 60.0

    @property
    def bytes(self) -> int:
        return self._bytes

    def clear(self) -> None:
        self._snaps.clear()
        self._bytes = 0
        self._count = 0

    def tick(self, machine, frames: int = 1) -> None:
        """Count `frames` emulated frames; snapshot when an interval is reached."""
        if not self.enabled:
            return
        self._count += frames
        if self._count < self.interval:
            return
        self._count = 0
        snap = savestate.capture(machine)
        self._snaps.append(snap)
        self._bytes += snap.size
        while self._snaps and (len(self._snaps) > self.capacity or self._bytes > self.max_bytes):
            self._bytes -= self._snaps.popleft().size

    def step_back(self, machine) -> bool:
        """Go back one snapshot. Returns False when the history is used up."""
        if not self._snaps:
            return False
        snap = self._snaps.pop()
        self._bytes -= snap.size
        savestate.restore(machine, snap)
        self._count = 0
        if not self._snaps:              # keep the oldest point so holding stays put there
            self._snaps.append(snap)
            self._bytes += snap.size
        return True
