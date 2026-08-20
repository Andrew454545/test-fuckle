#!/usr/bin/env python3
"""Proof-safety helpers shared by supplemental palindrome scans.

The locked proof definition remains NFC + the repository's locked separator
removal + Unicode grapheme clusters + Noto Sans Regular rendering.  This module
adds two safety properties without changing the historical evidence artifacts:

1. ICU locale-aware case forms are generated *before* textual-palindrome
   rejection, including Turkish/Azeri/Lithuanian fallback locales when a source
   has no usable language tag.
2. A second permissive punctuation pass is emitted only as quarantine evidence.
   It can reveal false negatives, but can never become a proof result without
   manual validation under the locked normalization.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_mirror_palindromes as base

LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*$")
# These are the practically important locale-specific casing fallbacks when a
# gazetteer gives us no usable language tag.  Turkish/Azeri are proof-critical
# for dotted/dotless I; Lithuanian has contextual dot handling.
FALLBACK_CASE_LOCALES = ("und", "tr", "az", "lt")


def normalized_language(lang: str | None) -> str:
    lang = (lang or "").strip().replace("_", "-")
    return lang if LANG_RE.fullmatch(lang) else "und"


def locale_candidates(lang: str | None) -> list[str]:
    lang = normalized_language(lang)
    if lang != "und":
        # Also evaluate root/und as a safety guard against bad source language
        # tagging while preferring the source locale first.
        return list(dict.fromkeys((lang, "und")))
    return list(FALLBACK_CASE_LOCALES)


def _icu_case_forms_raw(text: str, lang: str) -> list[tuple[str, str, str]]:
    if base.icu is None:
        raise RuntimeError("PyICU required for locale-aware candidate safety")
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for loc_tag in locale_candidates(lang):
        loc = base.icu.Locale("" if loc_tag == "und" else loc_tag)
        u = base.icu.UnicodeString(unicodedata.normalize("NFC", text))
        vals = [
            ("recorded", str(u)),
            ("uppercase", str(base.icu.UnicodeString(str(u)).toUpper(loc))),
            ("lowercase", str(base.icu.UnicodeString(str(u)).toLower(loc))),
        ]
        bi = base.icu.BreakIterator.createWordInstance(loc)
        vals.append(("titlecase", str(base.icu.UnicodeString(str(u)).toTitle(bi, loc))))
        for kind, value in vals:
            value = unicodedata.normalize("NFC", value)
            k = (loc_tag, value)
            if k not in seen:
                seen.add(k)
                out.append((kind, value, loc_tag))
    return out


def normalize_locked(text: str) -> str:
    return base.strip_locked_separators(unicodedata.normalize("NFC", text))


def normalize_permissive_punctuation(text: str) -> str:
    # Deliberately broader than the locked proof normalization.  Remove every
    # Unicode separator and punctuation code point, but not letters, marks,
    # numbers, or symbols.  Results from this pass are quarantine-only.
    return "".join(
        ch for ch in unicodedata.normalize("NFC", text)
        if unicodedata.category(ch)[0] not in {"P", "Z"}
    )


def _is_palindrome_normalized(norm: str) -> tuple[bool, int]:
    gs = base.graphemes(norm)
    folded = base.folded_graphemes(gs)
    return folded == folded[::-1], len(gs)


def _could_locale_case_rescue(norm: str) -> bool:
    """Return True only when a failed pair can plausibly be locale-casing-sensitive.

    Unicode default casefold already handles ordinary case equivalence and case
    expansions (for example sharp-s). The materially locale-specific ICU rules
    we probe are Turkic dotted/dotless I and Lithuanian I/J dot handling. If a
    failed mirrored pair contains an unrelated mismatch, those locale rules
    cannot turn the whole string into a palindrome, so an ICU probe would be
    pure cost rather than a safety check.
    """
    gs = base.graphemes(norm)
    fg = base.folded_graphemes(gs)
    mismatches = []
    for i in range(len(gs) // 2):
        j = len(gs) - 1 - i
        if fg[i] != fg[j]:
            mismatches.append((gs[i], gs[j]))
    if not mismatches:
        return False
    sensitive = {"i", "I", "j", "J", "İ", "ı", "\u0307"}
    def sensitive_grapheme(g: str) -> bool:
        return any(ch in sensitive for ch in unicodedata.normalize("NFD", g))
    return all(sensitive_grapheme(a) or sensitive_grapheme(b) for a, b in mismatches)


@dataclass(frozen=True)
class TextualVariant:
    raw_name: str
    normalized_name: str
    length: int
    textual_case_variant: str
    case_locale: str
    normalization_mode: str
    proof_eligible: bool
    quarantine_reason: str


def textual_palindrome_variants(
    raw_name: str,
    lang: str | None,
    min_len: int = 9,
    include_permissive: bool = True,
) -> list[TextualVariant]:
    """Return every unique palindrome form after ICU-before-rejection safety.

    Locked-normalization hits are proof-eligible.  Permissive-only hits are kept
    as a manual-review queue and are explicitly barred from proof results.
    """
    modes = [("locked", normalize_locked, True, "")]
    if include_permissive:
        modes.append((
            "permissive_punctuation",
            normalize_permissive_punctuation,
            False,
            "non-locked punctuation normalization; manual validation required",
        ))

    hits: dict[tuple[str, str, str], TextualVariant] = {}
    source_lang = normalized_language(lang)
    unresolved_modes = []

    # Cheap, exhaustive default-Unicode casefold pass. A hit here does not need
    # locale rescue to be accepted and avoids millions of unnecessary ICU calls.
    for mode, normalizer, mode_proof_eligible, reason in modes:
        norm = normalizer(raw_name)
        ok, n = _is_palindrome_normalized(norm)
        if ok and n >= min_len:
            hits[(mode, norm, source_lang)] = TextualVariant(
                raw_name=raw_name, normalized_name=norm, length=n,
                textual_case_variant="recorded_casefold", case_locale=source_lang,
                normalization_mode=mode, proof_eligible=mode_proof_eligible,
                quarantine_reason=reason,
            )
        elif n >= min_len and _could_locale_case_rescue(norm):
            unresolved_modes.append((mode, normalizer, mode_proof_eligible, reason))

    # Crucially, a candidate is not rejected while a plausible locale-specific
    # casing rescue remains. ICU is invoked only for those mismatch patterns.
    if unresolved_modes:
        for case_kind, cased, case_locale in _icu_case_forms_raw(raw_name, source_lang):
            for mode, normalizer, mode_proof_eligible, reason in unresolved_modes:
                norm = normalizer(cased)
                ok, n = _is_palindrome_normalized(norm)
                if not ok or n < min_len:
                    continue
                locale_proof_eligible = source_lang != "und" or case_locale == "und"
                proof_eligible = mode_proof_eligible and locale_proof_eligible
                reasons = [reason] if reason else []
                if not locale_proof_eligible:
                    reasons.append("locale-specific casing fallback with unknown source language; manual validation required")
                tv = TextualVariant(
                    raw_name=raw_name, normalized_name=norm, length=n,
                    textual_case_variant=case_kind, case_locale=case_locale,
                    normalization_mode=mode, proof_eligible=proof_eligible,
                    quarantine_reason="; ".join(reasons),
                )
                k = (mode, norm, case_locale)
                if k not in hits or (tv.proof_eligible and not hits[k].proof_eligible):
                    hits[k] = tv

    # If a permissive hit is identical to a locked hit, the locked result wins
    # and the duplicate quarantine row is unnecessary.
    locked_norms = {v.normalized_name for v in hits.values() if v.proof_eligible}
    out = [
        v for v in hits.values()
        if v.proof_eligible or v.normalized_name not in locked_norms
    ]
    return sorted(out, key=lambda v: (not v.proof_eligible, -v.length, v.normalized_name, v.case_locale))


def variant_dicts(*args, **kwargs) -> list[dict]:
    return [asdict(v) for v in textual_palindrome_variants(*args, **kwargs)]
