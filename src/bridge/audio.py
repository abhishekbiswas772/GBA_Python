"""Audio contract shared by the Game Boy and GBA cores.

Both cores already produce samples in the same shape, so the bridge mostly
just documents and verifies the contract instead of re-mixing anything:

* ``apu.take_samples()`` returns an ``array('h')`` of **interleaved stereo
  int16** samples (L, R, L, R, ...) at :data:`SAMPLE_RATE` Hz, and empties
  the APU's buffer,
* ``apu.sampling`` is a settable flag: ``True`` while a consumer wants
  samples, ``False`` to let the APU skip sample generation entirely (used
  while fast-forwarding or when audio is disabled),
* ``apu`` may buffer across frames; call ``take_samples()`` once per emulated
  frame for gapless playback.

The UI streams these bytes straight into SDL via
``src.core.gameboy.display.AudioOut``, which works with either core's APU.

:func:`take_samples` normalises any alternative representation (list of
ints, raw little-endian bytes) to ``array('h')`` so future core changes stay
compatible with the UI's audio path.
"""
from __future__ import annotations

from array import array

# Both cores clock their sample generators against this rate
# (src.core.gameboy.common.SAMPLE_RATE; the GBA APU imports the same constant).
SAMPLE_RATE = 44_100


def normalize(data) -> array:
    """Return ``data`` as an ``array('h')`` of interleaved stereo int16."""
    if isinstance(data, array):
        return data if data.typecode == "h" else array("h", data)
    if isinstance(data, (bytes, bytearray)):
        out = array("h")
        out.frombytes(bytes(data))
        return out
    return array("h", data)


def take_samples(apu) -> array:
    """Drain an APU's pending samples as int16 stereo interleaved."""
    return normalize(apu.take_samples())


def drain(apu) -> int:
    """Drop pending samples (fast-forward / muted); returns the sample count."""
    return len(take_samples(apu))


def set_sampling(apu, on: bool) -> None:
    """Enable or disable sample generation on an APU."""
    apu.sampling = bool(on)
