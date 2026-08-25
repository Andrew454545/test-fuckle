#!/usr/bin/env python3
"""Reclassify only locked-Noto coverage failures as explicit proof quarantine rows.

All other rendering failures remain fatal. This is deliberately narrow: a row
is eligible only when the scanner reached NotoFontIndex.choose() and reported
that no single locked Noto Sans Regular face covers the exact render text.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

LOCKED_FONT_MARKER = "No Noto Sans Regular face covers"


def read_csv(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    if not path.exists():
        return [], []
    with path.open(encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        return list(r), list(r.fieldnames or [])


def write_csv(path: Path, rows: list[dict[str, str]], fallback: list[str] | None = None) -> None:
    fields = sorted({k for row in rows for k in row}) if rows else list(fallback or [])
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shards-root", type=Path, required=True)
    ap.add_argument("--shards", type=int, default=4)
    args = ap.parse_args()

    total = 0
    for i in range(args.shards):
        shard = args.shards_root / str(i)
        failures_path = shard / "render_failures.csv"
        failures, failure_fields = read_csv(failures_path)
        if not failures:
            continue

        unknown = [r for r in failures if LOCKED_FONT_MARKER not in (r.get("error") or "")]
        if unknown:
            for r in unknown:
                print(json.dumps({
                    "unclassified_render_failure": True,
                    "shard": i,
                    "osm_type": r.get("osm_type"),
                    "osm_id": r.get("osm_id"),
                    "version": r.get("version"),
                    "name_key": r.get("name_key"),
                    "raw_name": r.get("raw_name"),
                    "render_text": r.get("render_text"),
                    "error": r.get("error"),
                }, ensure_ascii=False))
            raise SystemExit(f"refusing to quarantine {len(unknown)} non-font-coverage rendering failure(s) in shard {i}")

        qpath = shard / "quarantine_results.csv"
        existing, _ = read_csv(qpath)
        for r in failures:
            q = dict(r)
            q["quarantine"] = "True"
            q["proof_eligible"] = "False"
            q["render_unavailable"] = "True"
            q["mirror_pass"] = ""
            extra = (
                "locked Noto Sans Regular has no single covering face; "
                "representation excluded from font-dependent proof and retained for manual review"
            )
            q["quarantine_reason"] = "; ".join(
                x for x in ((q.get("quarantine_reason") or "").strip(), extra) if x
            )
            existing.append(q)
        write_csv(qpath, existing)
        write_csv(failures_path, [], failure_fields)

        mpath = shard / "scan_manifest.json"
        if not mpath.exists():
            raise SystemExit(f"missing shard manifest: {mpath}")
        manifest = json.loads(mpath.read_text(encoding="utf-8"))
        if int(manifest.get("render_failures", -1)) != len(failures):
            raise SystemExit(
                f"shard {i} manifest render_failures={manifest.get('render_failures')} "
                f"does not match CSV rows={len(failures)}"
            )
        manifest["render_failures"] = 0
        manifest["render_unavailable_quarantine_rows"] = int(
            manifest.get("render_unavailable_quarantine_rows", 0)
        ) + len(failures)
        manifest["render_unavailable_quarantine_requires_manual_review"] = True
        manifest["locked_font_failure_reclassification"] = (
            "only RuntimeError rows containing 'No Noto Sans Regular face covers'"
        )
        mpath.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        total += len(failures)

    if total == 0:
        raise SystemExit("scanner returned render-failure status but no failure rows were found")
    print(json.dumps({"reclassified_locked_font_rows": total, "remaining_render_failures": 0}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
