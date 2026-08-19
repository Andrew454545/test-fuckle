#!/usr/bin/env python3
"""Run the GeoNames all-feature scan with a visually meaningful mirror tolerance.

The original 0.1% pixel threshold is too strict for nominally vertically
symmetric uppercase Noto Sans glyphs because of real font-outline/kerning
asymmetries. The completed global candidate run showed a clean gap:
TAMAHAMAT 3.24%, MAYAVAYAM 3.29%, OWOMOMOWO 3.59%, then 15.69% for the
next-best candidate. A 5% threshold therefore admits the intended visually
mirrorable words while remaining well separated from the next candidate.
"""
from __future__ import annotations

import scan_mirror_palindromes as base

base.MIRROR_THRESHOLD = 0.05

import scan_geonames_mirror_palindromes as scan

if __name__ == "__main__":
    raise SystemExit(scan.main())
