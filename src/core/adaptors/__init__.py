"""Adaptors shared by every core.

* :mod:`src.core.adaptors.saves`: where battery saves (.sav) live
  (``saves/<gb|gbc|gba|nes>/<game>/<game>.sav``).
* :mod:`src.core.adaptors.snapshot`: the hook cores use to be save-stated
  (which attributes are transient and how to rebuild them).
"""
