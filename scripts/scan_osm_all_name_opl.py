#!/usr/bin/env python3
"""Scan an OPL stream of OSM objects for 9+ textual + vertical-mirror palindromes.

Designed for both current Planet and full-history Planet streams. The upstream
osmium tags-filter should select objects with naming keys; this script then
accepts current, alternate, localized and historical name families while
rejecting obvious metadata subkeys such as name:etymology/source/pronunciation.
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

base.MIRROR_THRESHOLD = 0.05

NAME_ROOTS = (
    "name", "official_name", "loc_name", "alt_name", "short_name",
    "int_name", "nat_name", "reg_name",
    "old_name", "historic_name", "former_name", "previous_name",
    "was:name", "was:official_name", "was:loc_name", "was:alt_name",
)
BLOCKED_SUFFIX_PREFIXES = (
    "source", "etymology", "pronunciation", "signed", "prefix", "suffix",
    "wikidata", "wikipedia", "ipa", "transcription", "note", "fixme",
)
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
    if not encoded:
        return {}
    out = {}
    for kv in encoded.split(","):
        if "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        out[opl_unescape(k)] = opl_unescape(v)
    return out


def name_key_language(key: str):
    for root in sorted(NAME_ROOTS, key=len, reverse=True):
        if key == root:
            return True, "und", root
        p = root + ":"
        if key.startswith(p):
            suffix = key[len(p):]
            low = suffix.lower()
            if any(low == b or low.startswith(b + ":") for b in BLOCKED_SUFFIX_PREFIXES):
                return False, "", ""
            lang = suffix.replace("_", "-") if LANG_RE.fullmatch(suffix) else "und"
            return True, lang, root
    return False, "", ""


def classify(tags: dict[str, str]) -> str:
    bits = []
    for k in CLASS_KEYS:
        if tags.get(k):
            bits.append(f"{k}={tags[k]}")
    return ";".join(bits[:5]) or "unclassified"


@dataclass
class Candidate:
    osm_type: str
    osm_id: int
    version: str
    timestamp: str
    visible: str
    name_key: str
    name_family: str
    language: str
    raw_name: str
    normalized_name: str
    length: int
    classification: str


def write_csv(path: Path, rows) -> None:
    rows = list(rows)
    fields = list(Candidate.__dataclass_fields__) if not rows else list(rows[0].keys()) if isinstance(rows[0], dict) else list(asdict(rows[0]).keys())
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

    for line_no, line in enumerate(sys.stdin, 1):
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
            if not field:
                continue
            if field.startswith("v"):
                version = field[1:]
            elif field.startswith("t"):
                timestamp = field[1:]
            elif field.startswith("d"):
                visible = field[1:]
            elif field.startswith("T"):
                tag_blob = field[1:]
        tags = parse_tags(tag_blob)
        cls = classify(tags)
        for key, value in tags.items():
            ok_key, lang, family = name_key_language(key)
            if not ok_key or not value:
                continue
            key_counts[key] = key_counts.get(key, 0) + 1
            pieces = value.split(";") if ";" in value else [value]
            for raw in pieces:
                raw = raw.strip()
                if not raw:
                    continue
                name_values += 1
                ispal, norm, gs = base.is_palindrome(raw)
                if not ispal or len(gs) < 9:
                    continue
                candidates.append(Candidate(
                    typ, oid, version, timestamp, visible, key, family, lang,
                    raw, norm, len(gs), cls,
                ))

    # Exact duplicate name records can occur when multiple filter expressions hit the same object;
    # osmium itself should only emit an object once, but dedupe defensively.
    uniq = {}
    for c in candidates:
        k = (c.osm_type, c.osm_id, c.version, c.name_key, c.raw_name)
        uniq[k] = c
    candidates = sorted(uniq.values(), key=lambda c: (-c.length, c.normalized_name.casefold(), c.osm_type, c.osm_id, c.version, c.name_key))
    write_csv(args.out / "all_textual_palindromes_ge9.csv", candidates)

    fonts = base.NotoFontIndex()
    mirror_rows = []
    failures = []
    for c in candidates:
        lang = c.language or "und"
        for case_kind, text in base.icu_case_variants(c.normalized_name, lang):
            row = asdict(c)
            row.update({"source_label": args.source_label, "case_variant": case_kind, "render_text": text})
            try:
                face = fonts.choose(text, lang)
                mask = base.render_mask(text, face, lang, 2048)
                err, dx, dy, _ = base.best_mirror_error(mask)
                render_size = 2048
                if base.BORDERLINE_LOW <= err <= base.BORDERLINE_HIGH:
                    mask = base.render_mask(text, face, lang, 4096)
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

    all_fields = sorted({k for r in mirror_rows + failures for k in r})
    def write_rows(fn: str, rows):
        with (args.out / fn).open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=all_fields, extrasaction="ignore")
            w.writeheader(); w.writerows(rows)

    write_rows("mirror_results.csv", mirror_rows)
    winners = [r for r in mirror_rows if r.get("mirror_pass")]
    write_rows("results.csv", winners)
    write_rows("render_failures.csv", failures)
    manifest = {
        "source_label": args.source_label,
        "history_file": args.history,
        "scope": "all OSM node/way/relation versions selected by current/localized/alternate/historical naming-key families; no place=* restriction",
        "name_roots": list(NAME_ROOTS),
        "blocked_metadata_suffixes": list(BLOCKED_SUFFIX_PREFIXES),
        "minimum_grapheme_length": 9,
        "visual_mirror_threshold": base.MIRROR_THRESHOLD,
        "object_versions_scanned": object_versions,
        "name_values_scanned": name_values,
        "textual_palindrome_records_ge9": len(candidates),
        "mirror_render_variants": len(mirror_rows),
        "mirror_pass_variants": len(winners),
        "render_failures": len(failures),
        "name_key_counts": key_counts,
    }
    (args.out / "scan_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    if failures:
        return 8
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
