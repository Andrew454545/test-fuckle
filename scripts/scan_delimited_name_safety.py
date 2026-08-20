#!/usr/bin/env python3
"""Safety re-scan for authoritative delimited gazetteer ZIPs.

Designed for GNIS All Names/Historical Features and similar one-name-per-row
products. It preserves the original successful artifacts while independently
checking ICU-before-rejection and quarantine-only permissive punctuation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_mirror_palindromes as base
import palindrome_safety as safety

base.MIRROR_THRESHOLD = 0.05


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def detect_delimiter(header: str) -> str:
    return "|" if header.count("|") >= header.count("\t") else "\t"


def canonical_key(s: str) -> str:
    return s.strip().lower().replace(" ", "_").replace("-", "_")


def write_csv(path: Path, rows: list[dict]):
    fields = sorted({k for r in rows for k in r})
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--zip", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--source-label", required=True)
    ap.add_argument("--name-column", default="feature_name")
    args = ap.parse_args(); args.out.mkdir(parents=True, exist_ok=True)

    rows_seen = values_seen = 0
    candidates: list[dict] = []
    members_scanned: list[str] = []
    wanted = canonical_key(args.name_column)

    with zipfile.ZipFile(args.zip) as z:
        members = [n for n in z.namelist() if not n.endswith("/") and n.lower().endswith((".txt", ".csv"))]
        if not members:
            raise RuntimeError("no text/csv member found")
        for member in members:
            with z.open(member) as raw:
                text = io.TextIOWrapper(raw, encoding="utf-8-sig", errors="strict", newline="")
                first = text.readline()
                if not first:
                    continue
                delim = detect_delimiter(first)
                headers = [canonical_key(x) for x in first.rstrip("\r\n").split(delim)]
                if wanted not in headers:
                    continue
                members_scanned.append(member)
                idx = headers.index(wanted)
                for line_no, line in enumerate(text, 2):
                    cols = line.rstrip("\r\n").split(delim)
                    if len(cols) <= idx:
                        raise RuntimeError(f"bad row {member}:{line_no}")
                    rows_seen += 1
                    raw_name = cols[idx].strip()
                    if not raw_name:
                        continue
                    values_seen += 1
                    meta = {headers[i]: cols[i] if i < len(cols) else "" for i in range(len(headers))}
                    for tv in safety.textual_palindrome_variants(raw_name, "und", 9, True):
                        r = dict(meta)
                        r.update({
                            "source": args.source_label, "archive_member": member,
                            "raw_name": raw_name, "normalized_name": tv.normalized_name,
                            "length": tv.length, "textual_case_variant": tv.textual_case_variant,
                            "case_locale": tv.case_locale, "normalization_mode": tv.normalization_mode,
                            "proof_eligible": tv.proof_eligible, "quarantine": not tv.proof_eligible,
                            "quarantine_reason": tv.quarantine_reason,
                        })
                        candidates.append(r)
    if not members_scanned:
        raise RuntimeError(f"name column {args.name_column!r} not found in any archive member")

    d = {}
    for r in candidates:
        ident = r.get("feature_id") or r.get("featureid") or r.get("id") or ""
        k = (ident, r["archive_member"], r["raw_name"], r["normalized_name"], r["normalization_mode"], r["case_locale"])
        d[k] = r
    candidates = sorted(d.values(), key=lambda r: (-int(r["length"]), r["normalized_name"].casefold(), r.get("feature_id", "")))
    write_csv(args.out / "all_textual_palindromes_ge9.csv", candidates)
    write_csv(args.out / "quarantine_textual_candidates.csv", [r for r in candidates if r["quarantine"]])

    fonts = base.NotoFontIndex(); renders = []; failures = []
    for c in candidates:
        lang = c["case_locale"] or "und"
        for kind, text in base.icu_case_variants(c["normalized_name"], lang):
            r = dict(c); r.update(render_case_variant=kind, render_text=text)
            try:
                face = fonts.choose(text, lang)
                mask = base.render_mask(text, face, lang, 2048)
                err, dx, dy, _ = base.best_mirror_error(mask); size = 2048
                if base.BORDERLINE_LOW <= err <= base.BORDERLINE_HIGH:
                    mask = base.render_mask(text, face, lang, 4096)
                    err, dx, dy, _ = base.best_mirror_error(mask); size = 4096
                r.update(font_family=face.family, font_path=face.path, font_index=face.index, render_size=size,
                         mirror_error=err, align_dx=dx, align_dy=dy, render_width=mask.shape[1], render_height=mask.shape[0],
                         mirror_pass=err <= base.MIRROR_THRESHOLD)
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
        "source": args.source_label, "archive_sha256": sha256_file(args.zip),
        "archive_members_scanned": members_scanned, "rows_scanned": rows_seen,
        "name_values_scanned": values_seen, "name_fields_scanned": [args.name_column],
        "minimum_grapheme_length": 9, "unicode_grapheme_clusters": True, "upper_length_bound": None,
        "icu_case_before_textual_rejection": True, "unknown_language_locale_rescue_hits_quarantined": True,
        "permissive_punctuation_pass": True, "permissive_punctuation_pass_quarantine_only": True,
        "font_definition": "locked Noto Sans Regular; font-dependent result", "visual_mirror_threshold": base.MIRROR_THRESHOLD,
        "textual_palindrome_records_ge9": len(candidates), "mirror_render_variants": len(renders),
        "mirror_pass_variants": len(proof), "quarantine_mirror_pass_variants": len(quarantine),
        "render_failures": len(failures),
    }
    (args.out / "scan_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))
    return 8 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
