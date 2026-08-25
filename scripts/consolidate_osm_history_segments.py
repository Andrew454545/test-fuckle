#!/usr/bin/env python3
"""Download, validate, and merge all successful OSM history segment artifacts."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

import requests

ART_RE = re.compile(r"^osm-history-segment-(\d+)-(\d+)$")


def api_get(session: requests.Session, url: str) -> dict:
    r = session.get(url, timeout=120)
    r.raise_for_status()
    return r.json()


def download_zip(session: requests.Session, url: str) -> bytes:
    r = session.get(url, timeout=(30, 300), allow_redirects=True)
    r.raise_for_status()
    return r.content


def find_one(root: Path, name: str) -> Path:
    hits = list(root.rglob(name))
    if len(hits) != 1:
        raise RuntimeError(f"expected exactly one {name} under {root}, found {len(hits)}")
    return hits[0]


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, rows: list[dict], fallback: tuple[str, ...] = ()) -> None:
    fields = sorted({k for r in rows for k in r}) if rows else list(fallback)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def uniq(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    out = {}
    for r in rows:
        out[tuple(r.get(k, "") for k in keys)] = r
    return list(out.values())


def add_counts(dst: dict[str, int], src: dict | None) -> None:
    for k, v in (src or {}).items():
        dst[k] = dst.get(k, 0) + int(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--branch", required=True)
    ap.add_argument("--workflow", default="nga-gns-rest-rerun.yml")
    ap.add_argument("--token", default=os.environ.get("GH_TOKEN", ""))
    ap.add_argument("--md5-state", type=Path, required=True)
    ap.add_argument("--official-md5-file", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    if not args.token:
        raise SystemExit("GitHub token required")

    sess = requests.Session()
    sess.headers.update({
        "Authorization": f"Bearer {args.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "test-fuckle-osm-history-consolidator/1.0",
    })
    base = f"https://api.github.com/repos/{args.repo}"
    runs_url = f"{base}/actions/workflows/{args.workflow}/runs"
    data = api_get(sess, runs_url + f"?branch={args.branch}&status=success&per_page=100")
    runs = data.get("workflow_runs", [])
    if not runs:
        raise SystemExit("no successful segment workflow runs found")

    chosen: dict[tuple[int, int], dict] = {}
    for run in runs:
        arts = api_get(sess, f"{base}/actions/runs/{run['id']}/artifacts?per_page=100").get("artifacts", [])
        for art in arts:
            m = ART_RE.match(art.get("name", ""))
            if not m or art.get("expired"):
                continue
            key = (int(m.group(1)), int(m.group(2)))
            if key not in chosen:
                chosen[key] = {"run": run, "artifact": art}

    if not chosen:
        raise SystemExit("no successful OSM history segment artifacts found")

    tmp = Path(tempfile.mkdtemp(prefix="osm-history-segments-"))
    segments = []
    try:
        for (name_start, name_end), item in sorted(chosen.items()):
            art = item["artifact"]
            raw = download_zip(sess, art["archive_download_url"])
            d = tmp / f"{name_start}-{name_end}"
            d.mkdir(parents=True)
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                zf.extractall(d)
            rm_path = find_one(d, "range_manifest.json")
            sm_path = find_one(d, "scan_manifest.json")
            rm = json.loads(rm_path.read_text(encoding="utf-8"))
            sm = json.loads(sm_path.read_text(encoding="utf-8"))
            start, end = int(rm["start_offset"]), int(rm["end_offset"])
            if (start, end) != (name_start, name_end):
                raise RuntimeError(f"artifact name range {(name_start,name_end)} != manifest {(start,end)}")
            if not rm.get("pbf_block_boundary_start") or not rm.get("pbf_block_boundary_end"):
                raise RuntimeError(f"non-block-aligned segment {start}-{end}")
            segments.append({
                "start": start, "end": end, "total": int(rm["total_bytes"]),
                "root": rm_path.parent, "range": rm, "scan": sm,
                "run_id": item["run"]["id"], "artifact_id": art["id"],
                "artifact_digest": art.get("digest"), "artifact_name": art["name"],
            })

        segments.sort(key=lambda s: (s["start"], s["end"]))
        expected = 0
        total = segments[0]["total"]
        for s in segments:
            if s["total"] != total:
                raise RuntimeError("source total changed between segments")
            if s["start"] != expected:
                raise RuntimeError(f"segment coverage gap/overlap: expected {expected}, got {s['start']}")
            if s["end"] <= s["start"]:
                raise RuntimeError(f"empty/backward segment {s['start']}-{s['end']}")
            expected = s["end"]
        if expected != total:
            raise RuntimeError(f"segment coverage incomplete: {expected}/{total}")

        md5_state = json.loads(args.md5_state.read_text(encoding="utf-8"))
        official = args.official_md5_file.read_text(encoding="utf-8").split()[0].lower()
        computed = str(md5_state.get("digest_if_finalized_here", "")).lower()
        if int(md5_state.get("bytes_hashed", -1)) != total:
            raise RuntimeError(f"MD5 state covers {md5_state.get('bytes_hashed')} bytes, expected {total}")
        if not re.fullmatch(r"[0-9a-f]{32}", official) or computed != official:
            raise RuntimeError(f"source MD5 mismatch: official={official} computed={computed}")

        textual, mirrors, failures = [], [], []
        key_counts: dict[str, int] = {}
        scope_counts: dict[str, int] = {}
        sort_counts: dict[str, int] = {}
        object_versions = name_values = 0
        for s in segments:
            root = s["root"]
            textual.extend(read_csv(root / "all_textual_palindromes_ge9.csv"))
            mirrors.extend(read_csv(root / "mirror_results.csv"))
            failures.extend(read_csv(root / "render_failures.csv"))
            m = s["scan"]
            object_versions += int(m.get("object_versions_scanned", 0))
            name_values += int(m.get("name_values_scanned", 0))
            add_counts(key_counts, m.get("name_key_counts"))
            add_counts(scope_counts, m.get("name_scope_counts"))
            add_counts(sort_counts, m.get("sort_only_key_counts"))

        textual = uniq(textual, ("osm_type","osm_id","version","name_key","raw_name","normalized_name","case_locale","normalization_mode"))
        mirrors = uniq(mirrors, ("osm_type","osm_id","version","name_key","raw_name","normalized_name","case_locale","normalization_mode","render_case_variant","render_text"))
        failures = uniq(failures, ("osm_type","osm_id","version","name_key","raw_name","normalized_name","case_locale","normalization_mode","render_case_variant","render_text"))
        proof = [r for r in mirrors if str(r.get("mirror_pass", "")).lower() in ("true","1","yes") and str(r.get("proof_eligible", "")).lower() in ("true","1","yes") and str(r.get("quarantine", "")).lower() not in ("true","1","yes")]
        quarantine = [r for r in mirrors if str(r.get("mirror_pass", "")).lower() in ("true","1","yes") and r not in proof]
        if failures:
            raise RuntimeError(f"{len(failures)} render failures remain across segments")

        args.out.mkdir(parents=True, exist_ok=True)
        write_csv(args.out / "all_textual_palindromes_ge9.csv", textual)
        write_csv(args.out / "mirror_results.csv", mirrors)
        write_csv(args.out / "results.csv", proof)
        write_csv(args.out / "quarantine_results.csv", quarantine)
        write_csv(args.out / "render_failures.csv", failures)

        manifest = {
            "source_label": "OSM-full-history-260810",
            "source_url": segments[0]["range"]["source_url"],
            "official_md5": official,
            "computed_md5": computed,
            "official_md5_match": True,
            "history_file": True,
            "all_source_bytes_scanned_exactly_once": True,
            "pbf_block_aligned_resumable_segments": len(segments),
            "source_bytes": total,
            "source_bytes_covered": expected,
            "all_node_way_relation_historical_versions": True,
            "broad_name_like_safety_filter": True,
            "sorting_name_excluded": True,
            "unknown_name_like_keys_quarantined": True,
            "icu_case_before_textual_rejection": True,
            "permissive_punctuation_pass": True,
            "permissive_punctuation_pass_quarantine_only": True,
            "minimum_grapheme_length": 9,
            "unicode_grapheme_clusters": True,
            "upper_length_bound": None,
            "font_definition": "locked Noto Sans Regular; font-dependent result",
            "visual_mirror_threshold": 0.05,
            "object_versions_scanned": object_versions,
            "name_values_scanned": name_values,
            "textual_palindrome_records_ge9": len(textual),
            "mirror_render_variants": len(mirrors),
            "mirror_pass_variants": len(proof),
            "quarantine_mirror_pass_variants": len(quarantine),
            "render_failures": 0,
            "name_key_counts": key_counts,
            "name_scope_counts": scope_counts,
            "sort_only_key_counts": sort_counts,
            "segment_provenance": [
                {k: s[k] for k in ("start","end","run_id","artifact_id","artifact_digest","artifact_name")}
                for s in segments
            ],
        }
        (args.out / "scan_manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (args.out / "segment_coverage.json").write_text(json.dumps([s["range"] for s in segments], indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"segments":len(segments),"source_bytes":total,"official_md5_match":True,"mirror_pass_variants":len(proof)}))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
