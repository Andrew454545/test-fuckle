#!/usr/bin/env python3
"""Broad OSM current/full-history feature-name palindrome scanner.

Upstream osmium filtering should use ``nwr/*name*`` in addition to the explicit
name families.  This scanner then separates documented/renderable feature-name
families from a deliberately broad quarantine safety net.  Sort-only names are
hard-excluded and never presented as rendered geographic names.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_mirror_palindromes as base
import palindrome_safety as safety

base.MIRROR_THRESHOLD = 0.05

DOCUMENTED_ROOTS = (
    "name", "official_name", "loc_name", "alt_name", "short_name",
    "int_name", "nat_name", "reg_name", "full_name", "long_name", "ref_name", "nickname",
    "old_name", "historic_name", "former_name", "previous_name",
)
CONTEXTUAL_NAME_KEYS = ("bridge:name", "tunnel:name")
LIFECYCLE_PREFIXES = (
    "was", "abandoned", "disused", "demolished", "razed", "removed",
    "destroyed", "proposed", "construction",
)
LIFECYCLE_SUFFIXES = (
    "name", "official_name", "loc_name", "alt_name", "short_name",
    "full_name", "long_name", "ref_name", "nickname",
)
BLOCKED_METADATA_TOKENS = {
    "source", "etymology", "pronunciation", "signed", "prefix", "suffix",
    "wikidata", "wikipedia", "ipa", "transcription", "note", "fixme",
}
SORT_ONLY_KEYS = {"sorting_name", "sort_name", "name:sorting", "name:sort"}
DATEISH_RE = re.compile(r"^(?:\d{4}(?:[-_/]\d{2,4})?|before[_:-]?\d{4}|after[_:-]?\d{4})$", re.I)
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*$")
ESC_RE = re.compile(r"%([0-9A-Fa-f]{1,6})%")
CLASS_KEYS = (
    "place", "natural", "waterway", "landuse", "boundary", "historic",
    "amenity", "leisure", "tourism", "man_made", "railway", "highway",
    "aeroway", "building", "shop", "office", "military", "power",
    "geological", "seamark:type",
)


def opl_unescape(s: str) -> str:
    return ESC_RE.sub(lambda m: chr(int(m.group(1), 16)), s)


def parse_tags(encoded: str) -> dict[str, str]:
    out = {}
    if not encoded:
        return out
    for kv in encoded.split(","):
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        out[opl_unescape(k)] = opl_unescape(v)
    return out


def _metadata_suffix(suffix: str) -> bool:
    tokens = [t.lower() for t in suffix.split(":") if t]
    return any(t in BLOCKED_METADATA_TOKENS for t in tokens)


def _language_and_date_suffix(suffix: str) -> tuple[bool, str]:
    parts = [p for p in suffix.split(":") if p]
    if not 1 <= len(parts) <= 2:
        return False, "und"
    langs = [p for p in parts if LANG_RE.fullmatch(p)]
    dates = [p for p in parts if DATEISH_RE.fullmatch(p)]
    if len(langs) + len(dates) != len(parts) or len(langs) > 1 or len(dates) > 1:
        return False, "und"
    if not dates:
        return False, "und"
    lang = langs[0].replace("_", "-") if langs else "und"
    return True, lang


def name_key_info(key: str) -> tuple[bool, str, str, str, str]:
    low = key.lower()
    if key in SORT_ONLY_KEYS or "sorting_name" in low or low.endswith(":sort_name"):
        return False, "", "", "sort_only_excluded", "sorting representation is not a rendered feature name"

    for ck in CONTEXTUAL_NAME_KEYS:
        if key == ck:
            return True, "und", ck, "documented_feature_name", ""
        if key.startswith(ck + ":"):
            suffix = key[len(ck) + 1:]
            if _metadata_suffix(suffix):
                return False, "", "", "metadata_excluded", ""
            lang = suffix.replace("_", "-") if LANG_RE.fullmatch(suffix) else "und"
            scope = "documented_feature_name" if lang != "und" else "supplemental_name_namespace"
            reason = "" if scope == "documented_feature_name" else "unclassified contextual name subkey"
            return True, lang, ck, scope, reason

    for prefix in LIFECYCLE_PREFIXES:
        for suffix_root in LIFECYCLE_SUFFIXES:
            root = f"{prefix}:{suffix_root}"
            if key == root:
                return True, "und", root, "documented_feature_name", ""
            if key.startswith(root + ":"):
                suffix = key[len(root) + 1:]
                if _metadata_suffix(suffix):
                    return False, "", "", "metadata_excluded", ""
                if LANG_RE.fullmatch(suffix):
                    return True, suffix.replace("_", "-"), root, "documented_feature_name", ""
                dated, lang = _language_and_date_suffix(suffix)
                if dated:
                    return True, lang, root, "documented_feature_name", ""
                return True, "und", root, "supplemental_name_namespace", "unclassified lifecycle name subkey; manual feature-name validation required"

    for root in DOCUMENTED_ROOTS:
        if key == root:
            return True, "und", root, "documented_feature_name", ""
        if key.startswith(root + ":"):
            suffix = key[len(root) + 1:]
            if _metadata_suffix(suffix):
                return False, "", "", "metadata_excluded", ""
            if LANG_RE.fullmatch(suffix):
                return True, suffix.replace("_", "-"), root, "documented_feature_name", ""
            dated, dated_lang = _language_and_date_suffix(suffix)
            if dated:
                return True, dated_lang, root, "documented_feature_name", ""
            if root == "name" and suffix.lower() in {"left", "right", "forward", "backward"}:
                return True, "und", root, "documented_feature_name", ""
            return True, "und", root, "supplemental_name_namespace", "unclassified name subkey; manual feature-name validation required"

    if "name" in low and not any(t in low.split(":") for t in BLOCKED_METADATA_TOKENS):
        return True, "und", key, "broad_name_like_safety", "name-like OSM key outside documented feature-name families"

    return False, "", "", "not_name", ""


def classify(tags: dict[str, str]) -> str:
    bits = [f"{k}={tags[k]}" for k in CLASS_KEYS if tags.get(k)]
    return ";".join(bits[:8]) or "unclassified"


@dataclass
class Candidate:
    osm_type: str
    osm_id: int
    version: str
    timestamp: str
    visible: str
    name_key: str
    name_family: str
    key_scope: str
    language: str
    raw_name: str
    normalized_name: str
    length: int
    textual_case_variant: str
    case_locale: str
    normalization_mode: str
    proof_eligible: bool
    quarantine: bool
    quarantine_reason: str
    classification: str


def write_csv(path: Path, rows, fallback_fields=None) -> None:
    rows = list(rows)
    fields = sorted({k for r in rows for k in (r.keys() if isinstance(r, dict) else asdict(r).keys())}) if rows else (fallback_fields or list(Candidate.__dataclass_fields__))
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r if isinstance(r, dict) else asdict(r))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--source-label", default="OSM")
    ap.add_argument("--history", action="store_true")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    candidates: list[Candidate] = []
    object_versions = 0
    name_values = 0
    key_counts: dict[str, int] = {}
    excluded_sort_keys: dict[str, int] = {}
    scope_counts: dict[str, int] = {}

    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        fields = line.split(" ")
        first = fields[0]
        if len(first) < 2 or first[0] not in "nwr":
            continue
        object_versions += 1
        typ = {"n": "node", "w": "way", "r": "relation"}[first[0]]
        try:
            oid = int(first[1:])
        except ValueError:
            continue
        version = timestamp = visible = ""
        tag_blob = ""
        for field in fields[1:]:
            if field.startswith("v"): version = field[1:]
            elif field.startswith("t"): timestamp = field[1:]
            elif field.startswith("d"): visible = field[1:]
            elif field.startswith("T"): tag_blob = field[1:]
        tags = parse_tags(tag_blob)
        cls = classify(tags)

        for key, value in tags.items():
            include, lang, family, scope, key_reason = name_key_info(key)
            if not include:
                if scope == "sort_only_excluded":
                    excluded_sort_keys[key] = excluded_sort_keys.get(key, 0) + 1
                continue
            if not value:
                continue
            key_counts[key] = key_counts.get(key, 0) + 1
            scope_counts[scope] = scope_counts.get(scope, 0) + 1
            pieces = value.split(";") if ";" in value else [value]
            for raw in pieces:
                raw = raw.strip()
                if not raw:
                    continue
                name_values += 1
                for tv in safety.textual_palindrome_variants(raw, lang, min_len=9, include_permissive=True):
                    key_quarantine = scope != "documented_feature_name"
                    norm_quarantine = not tv.proof_eligible
                    reasons = [x for x in (key_reason, tv.quarantine_reason) if x]
                    candidates.append(Candidate(
                        typ, oid, version, timestamp, visible, key, family, scope, lang,
                        raw, tv.normalized_name, tv.length, tv.textual_case_variant,
                        tv.case_locale, tv.normalization_mode, tv.proof_eligible and not key_quarantine,
                        key_quarantine or norm_quarantine, "; ".join(reasons), cls,
                    ))

    uniq = {}
    for c in candidates:
        k = (c.osm_type, c.osm_id, c.version, c.name_key, c.raw_name, c.normalized_name, c.case_locale, c.normalization_mode)
        if k not in uniq or (c.proof_eligible and not uniq[k].proof_eligible):
            uniq[k] = c
    candidates = sorted(uniq.values(), key=lambda c: (-c.length, c.normalized_name.casefold(), c.osm_type, c.osm_id, c.version, c.name_key))
    write_csv(args.out / "all_textual_palindromes_ge9.csv", candidates)
    write_csv(args.out / "quarantine_textual_candidates.csv", [c for c in candidates if c.quarantine])

    fonts = base.NotoFontIndex()
    mirror_rows = []
    failures = []
    for c in candidates:
        render_lang = c.case_locale or safety.normalized_language(c.language)
        for case_kind, text in base.icu_case_variants(c.normalized_name, render_lang):
            row = asdict(c)
            row.update({"source_label": args.source_label, "render_case_variant": case_kind, "render_text": text})
            try:
                face = fonts.choose(text, render_lang)
                mask = base.render_mask(text, face, render_lang, 2048)
                err, dx, dy, _ = base.best_mirror_error(mask)
                render_size = 2048
                if base.BORDERLINE_LOW <= err <= base.BORDERLINE_HIGH:
                    mask = base.render_mask(text, face, render_lang, 4096)
                    err, dx, dy, _ = base.best_mirror_error(mask)
                    render_size = 4096
                row.update({
                    "font_family": face.family, "font_path": face.path, "font_index": face.index,
                    "render_size": render_size, "mirror_error": err, "align_dx": dx, "align_dy": dy,
                    "render_width": mask.shape[1], "render_height": mask.shape[0],
                    "mirror_pass": err <= base.MIRROR_THRESHOLD,
                })
                mirror_rows.append(row)
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {e}"
                failures.append(row)

    winners = [r for r in mirror_rows if r.get("mirror_pass") and r.get("proof_eligible") and not r.get("quarantine")]
    quarantine_winners = [r for r in mirror_rows if r.get("mirror_pass") and (r.get("quarantine") or not r.get("proof_eligible"))]
    write_csv(args.out / "mirror_results.csv", mirror_rows)
    write_csv(args.out / "results.csv", winners)
    write_csv(args.out / "quarantine_results.csv", quarantine_winners)
    write_csv(args.out / "render_failures.csv", failures)

    manifest = {
        "source_label": args.source_label,
        "history_file": args.history,
        "scope": "all OSM node/way/relation versions selected by broad *name* key filter; documented feature-name namespaces are proof eligible and other name-like keys are quarantined",
        "documented_name_roots": list(DOCUMENTED_ROOTS),
        "contextual_name_keys": list(CONTEXTUAL_NAME_KEYS),
        "lifecycle_prefixes": list(LIFECYCLE_PREFIXES),
        "sorting_name_excluded": True,
        "sort_only_key_counts": excluded_sort_keys,
        "minimum_grapheme_length": 9,
        "unicode_grapheme_clusters": True,
        "upper_length_bound": None,
        "icu_case_before_textual_rejection": True,
        "icu_fallback_locales_for_unknown_language": list(safety.FALLBACK_CASE_LOCALES),
        "permissive_punctuation_pass": True,
        "permissive_results_proof_eligible": False,
        "font_definition": "locked Noto Sans Regular family coverage selected by scan_mirror_palindromes.NotoFontIndex; mirrorability is font-dependent",
        "visual_mirror_threshold": base.MIRROR_THRESHOLD,
        "object_versions_scanned": object_versions,
        "name_values_scanned": name_values,
        "textual_palindrome_records_ge9": len(candidates),
        "proof_textual_records_ge9": sum(c.proof_eligible and not c.quarantine for c in candidates),
        "quarantine_textual_records_ge9": sum(c.quarantine for c in candidates),
        "mirror_render_variants": len(mirror_rows),
        "mirror_pass_variants": len(winners),
        "quarantine_mirror_pass_variants": len(quarantine_winners),
        "render_failures": len(failures),
        "name_key_counts": key_counts,
        "name_scope_counts": scope_counts,
    }
    (args.out / "scan_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 8 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
