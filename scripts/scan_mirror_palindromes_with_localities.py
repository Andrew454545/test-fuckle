#!/usr/bin/env python3
"""Run the exhaustive mirror-palindrome scanner with place=locality included.

This is the revised eligibility rule requested after the first Africa audit. It reuses
all normalization, naming-key, Unicode, font, rendering, and pixel-symmetry logic from
the locked scanner, changing only the eligible OSM place types by adding `locality`.
"""
import scan_mirror_palindromes as scanner

scanner.PLACE_TYPES.add("locality")

if __name__ == "__main__":
    raise SystemExit(scanner.main())
