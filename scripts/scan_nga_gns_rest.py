#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import time
from pathlib import Path

import requests

import scan_mirror_palindromes as base

BASE_URL = "https://geonames.nga.mil/geon-ags/rest/services/RESEARCH/GIS_OUTPUT/MapServer/0/query"
FIELDS = [
    "objectid", "ufi", "uni", "full_name", "nt", "lang_cd", "transl_cd",
    "script_cd", "fc", "desig_cd", "cc_nm", "term_dt_n", "term_dt_f",
]
PAGE_SIZE = 3000


def write_csv(path: Path, rows: list[dict]):
    fields = sorted({k for row in rows for k in row}) if rows else FIELDS + [
        "normalized_name", "length", "case_variant", "render_text", "font_family",
        "font_path", "font_index", "render_size", "mirror_error", "align_dx",
        "align_dy", "render_width", "render_height", "mirror_pass",
    ]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def fetch_page(offset: int) -> list[dict]:
    params = {
        "f": "json",
        "where": "1=1",
        "outFields": ",".join(FIELDS),
        "returnGeometry": "false",
        "orderByFields": "objectid ASC",
        "resultOffset": offset,
        "resultRecordCount": PAGE_SIZE,
    }
    for attempt in range(8):
        try:
            r = requests.get(BASE_URL, params=params, timeout=120)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                raise RuntimeError(data["error"])
            return [x.get("attributes", {}) for x in data.get("features", [])]
        except Exception:
            if attempt == 7:
                raise
            time.sleep(min(2 ** attempt, 60))
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    base.MIRROR_THRESHOLD = 0.05
    fonts = base.NotoFontIndex()

    textual: list[dict] = []
    mirror_rows: list[dict] = []
    failures: list[dict] = []
    total_rows = 0
    offset = 0
    page_count = 0

    while True:
        page = fetch_page(offset)
        if not page:
            break
        page_count += 1
        total_rows += len(page)
        for a in page:
            name = (a.get("full_name") or "").strip()
            if not name:
                continue
            ok, norm, gs = base.is_palindrome(name)
            if not ok or len(gs) < 9:
                continue
            row = dict(a)
            row.update({"normalized_name": norm, "length": len(gs), "source": "NGA-GNS-REST"})
            textual.append(row)
        offset += len(page)
        if len(page) < PAGE_SIZE:
            break
        if page_count % 100 == 0:
            print(json.dumps({"rows_scanned": total_rows, "textual_candidates": len(textual)}), flush=True)

    textual.sort(key=lambda r: (-int(r["length"]), str(r["normalized_name"]).casefold(), int(r.get("objectid") or 0)))
    write_csv(args.out / "all_textual_palindromes_ge9.csv", textual)

    for cand in textual:
        lang = (cand.get("lang_cd") or "und").lower()
        for case_kind, text in base.icu_case_variants(str(cand["normalized_name"]), lang):
            row = dict(cand)
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
                row.update({
                    "font_family": face.family,
                    "font_path": face.path,
                    "font_index": face.index,
                    "render_size": render_size,
                    "mirror_error": err,
                    "align_dx": dx,
                    "align_dy": dy,
                    "render_width": mask.shape[1],
                    "render_height": mask.shape[0],
                    "mirror_pass": err <= base.MIRROR_THRESHOLD,
                })
                mirror_rows.append(row)
            except Exception as e:
                row["error"] = f"{type(e).__name__}: {e}"
                failures.append(row)

    winners = [r for r in mirror_rows if r.get("mirror_pass")]
    write_csv(args.out / "mirror_results.csv", mirror_rows)
    write_csv(args.out / "results.csv", winners)
    write_csv(args.out / "render_failures.csv", failures)

    manifest = {
        "source": "NGA GNS RESEARCH/GIS_OUTPUT MapServer layer 0",
        "endpoint": BASE_URL,
        "scope": "all foreign GNS name records, all name types/languages/scripts/transliterations, including terminated/historical names",
        "rows_scanned": total_rows,
        "pages_scanned": page_count,
        "minimum_grapheme_length": 9,
        "mirror_threshold": 0.05,
        "textual_palindrome_records_ge9": len(textual),
        "mirror_render_variants": len(mirror_rows),
        "mirror_pass_variants": len(winners),
        "render_failures": len(failures),
    }
    (args.out / "scan_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False), flush=True)
    if failures:
        return 8
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
