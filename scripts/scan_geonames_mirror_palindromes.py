#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import unicodedata
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_mirror_palindromes as base

LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*$")
EXCLUDED_ALT_LANGS = {"post", "iata", "icao", "faac", "link", "wkdt", "abbr", "fr_1793"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def open_single_zip_text(path: Path):
    z = zipfile.ZipFile(path)
    names = [n for n in z.namelist() if not n.endswith("/")]
    if len(names) != 1:
        raise RuntimeError(f"Expected one data file in {path}, found {names}")
    raw = z.open(names[0], "r")
    text = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="")
    return z, raw, text


def iter_geonames(path: Path):
    z, raw, text = open_single_zip_text(path)
    try:
        for line_no, line in enumerate(text, 1):
            cols = line.rstrip("\n\r").split("\t")
            if len(cols) < 19:
                raise RuntimeError(f"Bad allCountries row {line_no}: {len(cols)} columns")
            yield cols
    finally:
        text.close(); raw.close(); z.close()


def iter_alt_names(path: Path):
    z, raw, text = open_single_zip_text(path)
    try:
        for line_no, line in enumerate(text, 1):
            cols = line.rstrip("\n\r").split("\t")
            if len(cols) < 4:
                raise RuntimeError(f"Bad alternateNamesV2 row {line_no}: {len(cols)} columns")
            cols += [""] * (10 - len(cols))
            yield cols[:10]
    finally:
        text.close(); raw.close(); z.close()


def parse_country_info(path: Path):
    out = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            c = line.rstrip("\n\r").split("\t")
            if len(c) >= 5:
                out[c[0]] = c[4]
    return out


def parse_feature_codes(path: Path):
    out = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            c = line.rstrip("\n\r").split("\t")
            if len(c) >= 2:
                out[c[0]] = c[1]
    return out


def eligible_alt_language(lang: str) -> bool:
    if not lang:
        return True
    if lang.lower() in EXCLUDED_ALT_LANGS:
        return False
    return bool(LANG_RE.fullmatch(lang))


def current_alt_name(cols) -> bool:
    # alternateNameId, geonameid, isolanguage, alternate name,
    # isPreferredName, isShortName, isColloquial, isHistoric, from, to
    lang = cols[2].strip()
    historic = cols[7].strip()
    to_period = cols[9].strip()
    return eligible_alt_language(lang) and historic != "1" and not to_period


def language_for_render(lang: str) -> str:
    return lang.replace("_", "-") if lang and LANG_RE.fullmatch(lang) else "und"


@dataclass
class Candidate:
    geonameid: int
    source: str
    alternate_name_id: str
    language: str
    raw_name: str
    normalized_name: str
    length: int
    country_code: str = ""
    country: str = ""
    feature_class: str = ""
    feature_code: str = ""
    classification: str = ""
    latitude: str = ""
    longitude: str = ""
    canonical_name: str = ""


def make_candidate(geonameid: int, source: str, alt_id: str, lang: str, name: str):
    ok, norm, gs = base.is_palindrome(name)
    if not ok or len(gs) < 9:
        return None
    return Candidate(geonameid, source, alt_id, lang, name, norm, len(gs))


def write_csv(path: Path, rows):
    rows = list(rows)
    fields = list(asdict(rows[0]).keys()) if rows else list(Candidate.__dataclass_fields__.keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(asdict(r) if isinstance(r, Candidate) else r for r in rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-countries", type=Path, required=True)
    ap.add_argument("--alternate-names", type=Path, required=True)
    ap.add_argument("--country-info", type=Path, required=True)
    ap.add_argument("--feature-codes", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=Path("artifacts/geonames"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    countries = parse_country_info(args.country_info)
    feature_codes = parse_feature_codes(args.feature_codes)
    candidates: list[Candidate] = []
    ids_needed = set()
    primary_rows = 0
    alt_rows = 0
    current_alt_rows = 0

    # Pass 1: canonical GeoNames names for every geographic feature class.
    for c in iter_geonames(args.all_countries):
        primary_rows += 1
        gid = int(c[0]); name = c[1]
        cand = make_candidate(gid, "canonical", "", "und", name)
        if cand:
            candidates.append(cand); ids_needed.add(gid)

    # Pass 2: every current language-coded/unspecified alternate name.
    for c in iter_alt_names(args.alternate_names):
        alt_rows += 1
        if not current_alt_name(c):
            continue
        current_alt_rows += 1
        gid = int(c[1]); lang = c[2].strip(); name = c[3]
        cand = make_candidate(gid, "alternate", c[0], lang, name)
        if cand:
            candidates.append(cand); ids_needed.add(gid)

    # Pass 3: enrich only candidate feature IDs from the master gazetteer.
    meta = {}
    for c in iter_geonames(args.all_countries):
        gid = int(c[0])
        if gid in ids_needed:
            fclass, fcode, cc = c[6], c[7], c[8]
            meta[gid] = {
                "canonical_name": c[1], "latitude": c[4], "longitude": c[5],
                "feature_class": fclass, "feature_code": fcode, "country_code": cc,
                "country": countries.get(cc, cc),
                "classification": feature_codes.get(f"{fclass}.{fcode}", f"{fclass}.{fcode}"),
            }

    for cand in candidates:
        m = meta.get(cand.geonameid, {})
        for k, v in m.items(): setattr(cand, k, v)

    candidates.sort(key=lambda x: (-x.length, x.normalized_name.casefold(), x.geonameid, x.source, x.alternate_name_id))
    write_csv(args.out / "all_textual_palindromes_ge9.csv", candidates)

    fonts = base.NotoFontIndex()
    mirror_rows = []
    failures = []
    benchmark_hits = []
    benchmark_passes = []

    for cand in candidates:
        lang = language_for_render(cand.language)
        for case_kind, text in base.icu_case_variants(cand.normalized_name, lang):
            row = asdict(cand)
            row.update({"case_variant": case_kind, "render_text": text})
            try:
                face = fonts.choose(text, lang)
                mask = base.render_mask(text, face, lang, 2048)
                err, dx, dy, _ = base.best_mirror_error(mask)
                render_size = 2048
                if base.BORDERLINE_LOW <= err <= base.BORDERLINE_HIGH:
                    mask = base.render_mask(text, face, lang, 4096)
                    err, dx, dy, _ = base.best_mirror_error(mask)
                    render_size = 4096
                passed = err <= base.MIRROR_THRESHOLD
                row.update({
                    "font_family": face.family, "font_path": face.path, "font_index": face.index,
                    "render_size": render_size, "mirror_error": err, "align_dx": dx, "align_dy": dy,
                    "render_width": mask.shape[1], "render_height": mask.shape[0], "mirror_pass": passed,
                })
                mirror_rows.append(row)
                if cand.normalized_name.casefold() == "owomomowo":
                    benchmark_hits.append(row)
                    if passed: benchmark_passes.append(row)
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {e}"
                failures.append(row)

    fields = sorted({k for r in mirror_rows + failures for k in r})
    def write_rows(name, rows):
        with (args.out / name).open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader(); w.writerows(rows)

    write_rows("mirror_results.csv", mirror_rows)
    winners = [r for r in mirror_rows if r.get("mirror_pass")]
    write_rows("results.csv", winners)
    write_rows("render_failures.csv", failures)
    write_rows("owomomowo_benchmark.csv", benchmark_hits)

    manifest = {
        "scope": "GeoNames allCountries: every geographic feature class; canonical name plus current alternateNamesV2 language/unspecified names",
        "minimum_grapheme_length": 9,
        "excluded_alternate_language_codes": sorted(EXCLUDED_ALT_LANGS),
        "historic_alternate_names_excluded": True,
        "alternate_names_with_to_period_excluded": True,
        "all_countries_sha256": sha256_file(args.all_countries),
        "alternate_names_v2_sha256": sha256_file(args.alternate_names),
        "primary_feature_rows": primary_rows,
        "alternate_name_rows": alt_rows,
        "current_eligible_alternate_name_rows": current_alt_rows,
        "textual_palindrome_records_ge9": len(candidates),
        "mirror_render_variants": len(mirror_rows),
        "mirror_pass_variants": len(winners),
        "render_failures": len(failures),
        "owomomowo_render_variants": len(benchmark_hits),
        "owomomowo_mirror_pass_variants": len(benchmark_passes),
    }
    (args.out / "scan_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))

    # Hard benchmark: a broadened world-place search is invalid if it misses Owomomowo or cannot mirror it.
    if not benchmark_hits or not benchmark_passes:
        print("FATAL: OWOMOMOWO benchmark missing or did not pass mirror rendering", file=sys.stderr)
        return 7
    if failures:
        print(f"FATAL: {len(failures)} render failures", file=sys.stderr)
        return 8
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
