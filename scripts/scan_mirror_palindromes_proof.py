#!/usr/bin/env python3
"""Proof-grade wrapper for the exhaustive mirror-palindrome scanner.

Changes relative to the legacy scanner:
- includes place=locality
- accepts bare locked name keys plus language/script variants only
- excludes metadata/history-like name:* suffixes such as name:source,
  name:etymology, name:historic, and name:old

The underlying normalization, grapheme, casing, rendering, and pixel-mirror
logic remains unchanged.
"""
from __future__ import annotations

import re
import scan_mirror_palindromes as scanner

scanner.PLACE_TYPES.add("locality")

# In OSM, language variants overwhelmingly use ISO-639 primary subtags (2-3
# letters), optionally followed by BCP47-ish script/region/variant subtags.
# Restricting the primary subtag to 2-3 letters excludes metadata suffixes such
# as source/etymology/historic/prefix/signed while retaining normal language
# and script variants (e.g. name:en, name:sr-Latn, name:zh-Hant, name:pt_BR).
LANG_SUFFIX_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*$")
BLOCKED_PRIMARY_SUBTAGS = {"old"}


def _language_suffix_for_key(key: str):
    for base in scanner.NAME_BASES:
        prefix = base + ":"
        if key.startswith(prefix):
            suffix = key[len(prefix):]
            if not LANG_SUFFIX_RE.fullmatch(suffix):
                return None
            primary = re.split(r"[-_]", suffix, maxsplit=1)[0].lower()
            if primary in BLOCKED_PRIMARY_SUBTAGS:
                return None
            return suffix.replace("_", "-")
    return None


def eligible_name_key(key: str) -> bool:
    if key in scanner.NAME_BASES:
        return True
    return _language_suffix_for_key(key) is not None


def language_from_key(key: str) -> str:
    return _language_suffix_for_key(key) or "und"


scanner.eligible_name_key = eligible_name_key
scanner.language_from_key = language_from_key

if __name__ == "__main__":
    raise SystemExit(scanner.main())
