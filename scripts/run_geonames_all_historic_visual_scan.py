#!/usr/bin/env python3
"""Run the global GeoNames scan including current AND historical/former names.

This deliberately broadens the previous audit:
- all feature classes from allCountries
- all language/script alternateNamesV2 names
- isHistoric=1 names
- names with expired `to` periods
- French Revolutionary fr_1793 names
- excludes only pseudo-name metadata (postal/airport codes, links, Wikidata ids, abbreviations)
- 5% visual mirror threshold established by the global candidate distribution
"""
from __future__ import annotations

import csv
import json
import sys
import zipfile
from pathlib import Path

import scan_mirror_palindromes as base
base.MIRROR_THRESHOLD = 0.05

import scan_geonames_mirror_palindromes as scan

NON_NAME_CODES = {"post", "iata", "icao", "faac", "link", "wkdt", "abbr"}
scan.EXCLUDED_ALT_LANGS.clear()
scan.EXCLUDED_ALT_LANGS.update(NON_NAME_CODES)


def eligible_all_name_codes(lang: str) -> bool:
    if not lang:
        return True
    low = lang.lower()
    if low in NON_NAME_CODES:
        return False
    if low == "fr_1793":
        return True
    return bool(scan.LANG_RE.fullmatch(lang))


def include_current_or_historic(cols) -> bool:
    return eligible_all_name_codes(cols[2].strip())

scan.eligible_alt_language = eligible_all_name_codes
scan.current_alt_name = include_current_or_historic


def arg_value(flag: str, default: str | None = None):
    try:
        return sys.argv[sys.argv.index(flag) + 1]
    except (ValueError, IndexError):
        return default


def annotate_outputs(out_dir: Path, alt_zip: Path) -> None:
    ids = set()
    for fn in ("all_textual_palindromes_ge9.csv", "mirror_results.csv", "results.csv"):
        p = out_dir / fn
        if not p.exists():
            continue
        with p.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                aid = row.get("alternate_name_id", "")
                if aid:
                    ids.add(aid)
    if not ids:
        return

    metadata = {}
    with zipfile.ZipFile(alt_zip) as z:
        member = next(n for n in z.namelist() if n.endswith("alternateNamesV2.txt"))
        with z.open(member) as raw:
            for bline in raw:
                line = bline.decode("utf-8").rstrip("\r\n")
                c = line.split("\t")
                if len(c) < 4 or c[0] not in ids:
                    continue
                c += [""] * (10 - len(c))
                metadata[c[0]] = {
                    "is_preferred_name": c[4],
                    "is_short_name": c[5],
                    "is_colloquial": c[6],
                    "is_historic": c[7],
                    "from_period": c[8],
                    "to_period": c[9],
                }
                if len(metadata) == len(ids):
                    break

    extra = ["is_preferred_name", "is_short_name", "is_colloquial", "is_historic", "from_period", "to_period"]
    for fn in ("all_textual_palindromes_ge9.csv", "mirror_results.csv", "results.csv", "owomomowo_benchmark.csv"):
        p = out_dir / fn
        if not p.exists():
            continue
        with p.open(encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
            fields = list(rows[0].keys()) if rows else []
        for x in extra:
            if x not in fields:
                fields.append(x)
        for row in rows:
            row.update(metadata.get(row.get("alternate_name_id", ""), {k: "" for k in extra}))
        with p.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader(); w.writerows(rows)


if __name__ == "__main__":
    rc = scan.main()
    out_dir = Path(arg_value("--out", "artifacts/geonames-historic"))
    alt_zip = Path(arg_value("--alternate-names", "geonames/alternateNamesV2.zip"))
    if out_dir.exists() and alt_zip.exists():
        annotate_outputs(out_dir, alt_zip)
        mp = out_dir / "scan_manifest.json"
        if mp.exists():
            m = json.loads(mp.read_text(encoding="utf-8"))
            m["scope"] = "GeoNames allCountries all feature classes + all eligible current and historical alternateNamesV2 names across languages/scripts"
            m["historic_alternate_names_excluded"] = False
            m["alternate_names_with_to_period_excluded"] = False
            m["fr_1793_historic_names_included"] = True
            m["visual_mirror_threshold"] = 0.05
            mp.write_text(json.dumps(m, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    raise SystemExit(rc)
