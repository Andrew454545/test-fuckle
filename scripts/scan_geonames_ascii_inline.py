#!/usr/bin/env python3
"""GeoNames safety/supplement scan for every relevant name representation.

The successful historic GeoNames artifact remains immutable evidence. This
supplement independently closes its omitted ``asciiname``/inline-alternate gap
*and* rechecks canonical + alternateNamesV2 names with ICU locale-aware casing
before textual rejection and a quarantine-only permissive punctuation pass.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_mirror_palindromes as base
import palindrome_safety as safety

base.MIRROR_THRESHOLD = 0.05
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*$")
NON_NAME_ALT_CODES = {"post", "iata", "icao", "faac", "link", "wkdt", "abbr"}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def iter_allcountries(path: Path):
    with zipfile.ZipFile(path) as z:
        members = [n for n in z.namelist() if n.endswith(".txt") and not n.endswith("/")]
        if len(members) != 1:
            raise RuntimeError(f"expected one allCountries text member, got {members}")
        with z.open(members[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="")
            for i, line in enumerate(text, 1):
                c = line.rstrip("\r\n").split("\t")
                if len(c) < 19:
                    raise RuntimeError(f"bad allCountries row {i}: {len(c)} columns")
                yield c


def iter_alt_names(path: Path):
    with zipfile.ZipFile(path) as z:
        members = [n for n in z.namelist() if n.endswith("alternateNamesV2.txt")]
        if len(members) != 1:
            raise RuntimeError(f"expected alternateNamesV2.txt, got {members}")
        with z.open(members[0]) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8", errors="strict", newline="")
            for i, line in enumerate(text, 1):
                c = line.rstrip("\r\n").split("\t")
                if len(c) < 4:
                    raise RuntimeError(f"bad alternateNamesV2 row {i}: {len(c)} columns")
                c += [""] * (10 - len(c))
                yield c[:10]


def eligible_alt_language(lang: str) -> bool:
    lang = (lang or "").strip()
    if not lang:
        return True
    if lang.lower() in NON_NAME_ALT_CODES:
        return False
    if lang.lower() == "fr_1793":
        return True
    return bool(LANG_RE.fullmatch(lang))


def parse_country_info(path: Path):
    d = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip() or line.startswith("#"):
                continue
            c = line.rstrip("\r\n").split("\t")
            if len(c) >= 5:
                d[c[0]] = c[4]
    return d


def parse_feature_codes(path: Path):
    d = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            c = line.rstrip("\r\n").split("\t")
            if len(c) >= 2:
                d[c[0]] = c[1]
    return d


def feature_meta(c, countries, feature_codes):
    return {
        "geonameid": int(c[0]), "canonical_name": c[1],
        "latitude": c[4], "longitude": c[5],
        "feature_class": c[6], "feature_code": c[7], "country_code": c[8],
        "country": countries.get(c[8], c[8]),
        "classification": feature_codes.get(f"{c[6]}.{c[7]}", f"{c[6]}.{c[7]}"),
        "modification_date": c[18],
    }


def add_variants(candidates: list[dict], meta: dict, source_field: str, raw: str, lang: str = "und", **extra):
    for tv in safety.textual_palindrome_variants(raw, lang, 9, True):
        r = dict(meta)
        r.update(extra)
        r.update({
            "source": "GeoNames", "source_field": source_field, "language": lang,
            "raw_name": raw, "normalized_name": tv.normalized_name, "length": tv.length,
            "textual_case_variant": tv.textual_case_variant, "case_locale": tv.case_locale,
            "normalization_mode": tv.normalization_mode, "proof_eligible": tv.proof_eligible,
            "quarantine": not tv.proof_eligible, "quarantine_reason": tv.quarantine_reason,
        })
        candidates.append(r)


def write_csv(path: Path, rows: list[dict], fallback=None):
    fields = sorted({k for r in rows for k in r}) if rows else (fallback or [])
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-countries", type=Path, required=True)
    ap.add_argument("--alternate-names", type=Path, required=True)
    ap.add_argument("--country-info", type=Path, required=True)
    ap.add_argument("--feature-codes", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(); args.out.mkdir(parents=True, exist_ok=True)
    countries = parse_country_info(args.country_info)
    feature_codes = parse_feature_codes(args.feature_codes)

    primary_rows = canonical_values = ascii_values = inline_values = 0
    alt_rows = eligible_alt_rows = 0
    candidates: list[dict] = []
    alt_candidates: list[dict] = []
    alt_candidate_ids: set[int] = set()

    for c in iter_allcountries(args.all_countries):
        primary_rows += 1
        meta = feature_meta(c, countries, feature_codes)
        canonical = (c[1] or "").strip()
        ascii_name = (c[2] or "").strip()
        inline = c[3] or ""
        if canonical:
            canonical_values += 1
            add_variants(candidates, meta, "canonical_name", canonical, "und")
        if ascii_name:
            ascii_values += 1
            add_variants(candidates, meta, "asciiname", ascii_name, "und")
        for v in inline.split(",") if inline else ():
            v = v.strip()
            if v:
                inline_values += 1
                add_variants(candidates, meta, "alternatenames_inline", v, "und")

    for c in iter_alt_names(args.alternate_names):
        alt_rows += 1
        lang = c[2].strip()
        if not eligible_alt_language(lang):
            continue
        eligible_alt_rows += 1
        raw = (c[3] or "").strip()
        if not raw:
            continue
        gid = int(c[1])
        base_meta = {"geonameid": gid}
        before = len(alt_candidates)
        add_variants(
            alt_candidates, base_meta, "alternateNamesV2", raw,
            lang if LANG_RE.fullmatch(lang) else "und",
            alternate_name_id=c[0], alternate_language_code=lang,
            is_preferred_name=c[4], is_short_name=c[5], is_colloquial=c[6],
            is_historic=c[7], from_period=c[8], to_period=c[9],
        )
        if len(alt_candidates) != before:
            alt_candidate_ids.add(gid)

    alt_meta = {}
    if alt_candidate_ids:
        for c in iter_allcountries(args.all_countries):
            gid = int(c[0])
            if gid in alt_candidate_ids:
                alt_meta[gid] = feature_meta(c, countries, feature_codes)
    require_missing = alt_candidate_ids - set(alt_meta)
    if require_missing:
        raise RuntimeError(f"missing GeoNames master metadata for {len(require_missing)} candidate feature IDs")
    for r in alt_candidates:
        meta = alt_meta[int(r["geonameid"])]
        merged = dict(meta); merged.update(r); candidates.append(merged)

    d = {}
    for r in candidates:
        k = (
            r.get("geonameid", ""), r.get("source_field", ""), r.get("alternate_name_id", ""),
            r.get("raw_name", ""), r.get("normalized_name", ""),
            r.get("normalization_mode", ""), r.get("case_locale", ""),
        )
        if k not in d or (r.get("proof_eligible") and not d[k].get("proof_eligible")):
            d[k] = r
    candidates = sorted(d.values(), key=lambda r: (-int(r["length"]), r["normalized_name"].casefold(), int(r["geonameid"]), r["source_field"]))
    write_csv(args.out / "all_textual_palindromes_ge9.csv", candidates)
    write_csv(args.out / "quarantine_textual_candidates.csv", [r for r in candidates if r["quarantine"]])

    fonts = base.NotoFontIndex(); renders = []; failures = []
    for c in candidates:
        lang = c["case_locale"] or safety.normalized_language(c.get("language"))
        for kind, text in base.icu_case_variants(c["normalized_name"], lang):
            r = dict(c); r.update(render_case_variant=kind, render_text=text)
            try:
                face = fonts.choose(text, lang)
                mask = base.render_mask(text, face, lang, 2048)
                err, dx, dy, _ = base.best_mirror_error(mask); size = 2048
                if base.BORDERLINE_LOW <= err <= base.BORDERLINE_HIGH:
                    mask = base.render_mask(text, face, lang, 4096)
                    err, dx, dy, _ = base.best_mirror_error(mask); size = 4096
                r.update(
                    font_family=face.family, font_path=face.path, font_index=face.index,
                    render_size=size, mirror_error=err, align_dx=dx, align_dy=dy,
                    render_width=mask.shape[1], render_height=mask.shape[0],
                    mirror_pass=err <= base.MIRROR_THRESHOLD,
                )
                renders.append(r)
            except Exception as e:
                r["error"] = f"{type(e).__name__}: {e}"; failures.append(r)

    proof = [r for r in renders if r.get("mirror_pass") and r.get("proof_eligible") and not r.get("quarantine")]
    quarantine = [r for r in renders if r.get("mirror_pass") and (r.get("quarantine") or not r.get("proof_eligible"))]
    write_csv(args.out / "mirror_results.csv", renders)
    write_csv(args.out / "results.csv", proof)
    write_csv(args.out / "quarantine_results.csv", quarantine)
    write_csv(args.out / "render_failures.csv", failures)

    manifest = {
        "source": "GeoNames allCountries + alternateNamesV2 99pct safety/supplement",
        "all_countries_sha256": sha256_file(args.all_countries),
        "alternate_names_v2_sha256": sha256_file(args.alternate_names),
        "primary_feature_rows": primary_rows,
        "alternate_name_rows": alt_rows,
        "eligible_alternate_name_rows": eligible_alt_rows,
        "name_fields_scanned": ["canonical_name", "asciiname", "alternatenames_inline", "alternateNamesV2"],
        "inline_alternatenames_include_geonames_automatic_ascii_transliterations": True,
        "canonical_name_values_scanned": canonical_values,
        "asciiname_values_scanned": ascii_values,
        "inline_alternatename_values_scanned": inline_values,
        "historic_alternate_names_included": True,
        "alternate_names_with_to_period_included": True,
        "excluded_non_name_alternate_codes": sorted(NON_NAME_ALT_CODES),
        "preserved_immutable_historic_artifact_role": "baseline authoritative evidence; not modified by this supplement",
        "minimum_grapheme_length": 9, "unicode_grapheme_clusters": True, "upper_length_bound": None,
        "icu_case_before_textual_rejection": True,
        "unknown_language_locale_rescue_hits_quarantined": True,
        "permissive_punctuation_pass": True, "permissive_punctuation_pass_quarantine_only": True,
        "font_definition": "locked Noto Sans Regular; font-dependent result",
        "visual_mirror_threshold": base.MIRROR_THRESHOLD,
        "textual_palindrome_records_ge9": len(candidates), "mirror_render_variants": len(renders),
        "mirror_pass_variants": len(proof), "quarantine_mirror_pass_variants": len(quarantine),
        "render_failures": len(failures),
    }
    (args.out / "scan_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 8 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
