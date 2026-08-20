#!/usr/bin/env python3
"""Resumable/two-pass NGA GNS scanner using the official GIS_OUTPUT layer.

Pass 1 enumerates the complete OBJECTID universe and fetches only objectid,
full_name and full_nm_nd.  Candidate rows are then hydrated with full proof
metadata.  Coverage is accepted only when service count, enumerated unique IDs
and fetched shard IDs agree exactly.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_mirror_palindromes as base
import palindrome_safety as safety

base.MIRROR_THRESHOLD = 0.05

SERVICE = "https://geonames.nga.mil/geon-ags/rest/services/RESEARCH/GIS_OUTPUT/MapServer/0"
MIN_FIELDS = ["objectid", "full_name", "full_nm_nd"]
META_FIELDS = [
    "objectid", "ufi", "uni", "full_name", "full_nm_nd", "nt", "lang_cd",
    "script_cd", "transl_cd", "term_dt_n", "term_dt_f", "efctv_dt", "cc_nm",
    "cc_ft", "fc", "desig_cd", "ft_link", "name_link", "lat_dd", "long_dd",
]
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*$")


def get_json(url: str, params: dict, tries: int = 20, read_timeout: int = 180):
    last = None
    for i in range(tries):
        try:
            with requests.Session() as s:
                s.headers.update({"User-Agent": "test-fuckle-gns-99pct/1.0"})
                r = s.get(url, params=params, timeout=(30, read_timeout))
                r.raise_for_status()
                j = r.json()
            if "error" in j:
                raise RuntimeError(j["error"])
            return j
        except Exception as e:
            last = e
            if i + 1 == tries:
                break
            delay = min(2 ** min(i, 6), 60) + random.random() * 2
            print(json.dumps({"arcgis_retry": i + 1, "delay": round(delay, 2), "error": f"{type(e).__name__}: {e}"}), flush=True)
            time.sleep(delay)
    raise RuntimeError(f"ArcGIS failed after {tries} attempts: {last}")


def enumerate_ids(url: str) -> tuple[int, list[int]]:
    count = int(get_json(url + "/query", {"where": "1=1", "returnCountOnly": "true", "f": "json"})["count"])
    j = get_json(url + "/query", {"where": "1=1", "returnIdsOnly": "true", "f": "json"}, read_timeout=900)
    ids = [int(x) for x in j.get("objectIds", [])]
    if len(ids) != count or len(set(ids)) != count:
        raise RuntimeError(f"OBJECTID enumeration mismatch: service_count={count}, ids={len(ids)}, unique={len(set(ids))}")
    ids.sort()
    return count, ids



def save_ids(path: Path, service_count: int, ids: list[int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for oid in ids:
            f.write(f"{oid}\n")
    (path.parent / "id_manifest.json").write_text(json.dumps({
        "source": "NGA-GNS official RESEARCH/GIS_OUTPUT ArcGIS whole-world layer",
        "service_url": SERVICE, "service_count": service_count,
        "ids_enumerated": len(ids), "unique_ids_enumerated": len(set(ids)),
        "ids_equal_service_count": len(ids) == service_count == len(set(ids)),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_ids(path: Path) -> list[int]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        ids = [int(line.strip()) for line in f if line.strip()]
    if ids != sorted(ids) or len(ids) != len(set(ids)):
        raise RuntimeError("OBJECTID file is not sorted unique")
    return ids

def chunks(xs, n):
    for i in range(0, len(xs), n):
        yield i // n, xs[i:i+n]


def fetch_object_ids(url: str, ids: list[int], fields: list[str]):
    if not ids:
        return []
    params = {
        "objectIds": ",".join(map(str, ids)),
        "outFields": ",".join(fields),
        "returnGeometry": "false",
        "f": "json",
    }
    try:
        return get_json(url + "/query", params).get("features", [])
    except Exception:
        if len(ids) <= 25:
            raise
        mid = len(ids) // 2
        return fetch_object_ids(url, ids[:mid], fields) + fetch_object_ids(url, ids[mid:], fields)


def write_csv(path: Path, rows: list[dict], fallback: list[str] | None = None):
    fields = sorted({k for r in rows for k in r}) if rows else (fallback or [])
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)


def checkpoint_paths(root: Path, batch_no: int):
    b = root / "checkpoints" / f"batch-{batch_no:06d}"
    return b.with_suffix(".json"), b.with_suffix(".candidates.jsonl")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=SERVICE)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--shard-index", type=int, default=0)
    ap.add_argument("--shard-count", type=int, default=1)
    ap.add_argument("--batch-size", type=int, default=1000)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--ids-file", type=Path)
    ap.add_argument("--id-manifest", type=Path)
    ap.add_argument("--enumerate-only", action="store_true")
    ap.add_argument("--ids-out", type=Path)
    args = ap.parse_args()
    if not 0 <= args.shard_index < args.shard_count:
        raise SystemExit("invalid shard index/count")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "checkpoints").mkdir(exist_ok=True)

    if args.enumerate_only:
        if not args.ids_out:
            raise SystemExit("--enumerate-only requires --ids-out")
        service_count, all_ids = enumerate_ids(args.url)
        save_ids(args.ids_out, service_count, all_ids)
        print(json.dumps({"service_url": args.url, "service_count": service_count, "ids_enumerated": len(all_ids), "ids_equal_service_count": True}))
        return 0

    if args.ids_file:
        all_ids = load_ids(args.ids_file)
        id_manifest_path = args.id_manifest or (args.ids_file.parent / "id_manifest.json")
        idm = json.loads(id_manifest_path.read_text(encoding="utf-8"))
        service_count = int(idm["service_count"])
        if len(all_ids) != service_count or len(set(all_ids)) != service_count:
            raise RuntimeError("enumerated OBJECTID artifact does not equal captured service count")
        current_count = int(get_json(args.url + "/query", {"where":"1=1","returnCountOnly":"true","f":"json"})["count"])
        if current_count != service_count:
            raise RuntimeError(f"NGA service changed after ID capture: captured={service_count} current={current_count}")
    else:
        service_count, all_ids = enumerate_ids(args.url)

    shard_ids = all_ids[args.shard_index::args.shard_count]
    expected = set(shard_ids)
    print(json.dumps({
        "service_url": args.url, "service_count": service_count,
        "ids_enumerated": len(all_ids), "shard_index": args.shard_index,
        "shard_count": args.shard_count, "shard_expected_ids": len(shard_ids),
    }), flush=True)

    candidates: list[dict] = []
    fetched_ids: set[int] = set()
    full_name_values = 0
    full_nm_nd_values = 0

    todo = []
    for batch_no, batch_ids in chunks(shard_ids, args.batch_size):
        meta_path, cand_path = checkpoint_paths(args.out, batch_no)
        if meta_path.exists() and cand_path.exists():
            meta = json.loads(meta_path.read_text())
            ids_done = {int(x) for x in meta.get("objectids", [])}
            if ids_done == set(batch_ids):
                fetched_ids.update(ids_done)
                with cand_path.open(encoding="utf-8") as f:
                    for line in f:
                        if line.strip(): candidates.append(json.loads(line))
                full_name_values += int(meta.get("full_name_values", 0))
                full_nm_nd_values += int(meta.get("full_nm_nd_values", 0))
                continue
        todo.append((batch_no, batch_ids))

    def scan_batch(batch_no, batch_ids):
        feats = fetch_object_ids(args.url, batch_ids, MIN_FIELDS)
        got = {int(f["attributes"]["objectid"]) for f in feats}
        if got != set(batch_ids):
            raise RuntimeError(f"batch {batch_no} ID mismatch expected={len(batch_ids)} got={len(got)}")
        batch_candidates=[]; n_full=0; n_nd=0
        for feat in feats:
            a=feat.get("attributes", {})
            oid=int(a["objectid"])
            for col in ("full_name", "full_nm_nd"):
                raw=(a.get(col) or "").strip()
                if not raw: continue
                if col == "full_name": n_full += 1
                else: n_nd += 1
                for tv in safety.textual_palindrome_variants(raw, "und", 9, True):
                    batch_candidates.append({
                        "objectid": oid, "name_column": col, "raw_name": raw,
                        **asdict(tv),
                    })
        meta_path, cand_path = checkpoint_paths(args.out, batch_no)
        cand_path.write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in batch_candidates), encoding="utf-8")
        meta_path.write_text(json.dumps({"objectids": sorted(got), "full_name_values": n_full, "full_nm_nd_values": n_nd}, ensure_ascii=False), encoding="utf-8")
        return got, batch_candidates, n_full, n_nd

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures={ex.submit(scan_batch, bno, bids): bno for bno,bids in todo}
        done=0
        for fut in as_completed(futures):
            got, cs, n_full, n_nd = fut.result()
            fetched_ids.update(got); candidates.extend(cs)
            full_name_values += n_full; full_nm_nd_values += n_nd
            done += 1
            if done % 50 == 0 or done == len(todo):
                print(json.dumps({"batches_completed_this_run":done,"batches_remaining":len(todo)-done,"rows_scanned":len(fetched_ids),"candidates":len(candidates)}), flush=True)

    if fetched_ids != expected:
        raise RuntimeError(f"shard completeness failure expected={len(expected)} fetched={len(fetched_ids)} missing={len(expected-fetched_ids)} extra={len(fetched_ids-expected)}")

    candidate_ids = sorted({int(c["objectid"]) for c in candidates})
    metadata={}
    for _, ids in chunks(candidate_ids, 500):
        for feat in fetch_object_ids(args.url, ids, META_FIELDS):
            a=feat.get("attributes", {})
            metadata[int(a["objectid"])] = a
    if set(metadata) != set(candidate_ids):
        raise RuntimeError("candidate metadata hydration incomplete")

    raw_candidate_keys = sorted({
        (int(c["objectid"]), c["name_column"], c["raw_name"]) for c in candidates
    })
    enriched=[]
    for oid, name_column, raw_name in raw_candidate_keys:
        a=metadata[oid]
        lang=(a.get("lang_cd") or "").strip()
        for tv in safety.textual_palindrome_variants(raw_name, lang, 9, True):
            render_lang = lang.replace("_", "-") if LANG_RE.fullmatch(lang) else tv.case_locale or "und"
            row={
                "objectid": oid, "name_column": name_column, "raw_name": raw_name,
                **asdict(tv),
                "source":"NGA-GNS-ArcGIS-GIS_OUTPUT",
                "record_id":str(a.get("uni") or a.get("objectid") or ""),
                "ufi":a.get("ufi") or "", "uni":a.get("uni") or "",
                "full_name":a.get("full_name") or "", "full_nm_nd":a.get("full_nm_nd") or "",
                "name_type":a.get("nt") or "", "language":lang,
                "script":a.get("script_cd") or "", "transliteration":a.get("transl_cd") or "",
                "country_or_region":a.get("cc_nm") or a.get("cc_ft") or "",
                "classification":a.get("fc") or "", "designation":a.get("desig_cd") or "",
                "feature_link":a.get("ft_link") or "", "name_link":a.get("name_link") or "",
                "name_termination_date":a.get("term_dt_n") or "", "feature_termination_date":a.get("term_dt_f") or "",
                "effective_date":a.get("efctv_dt") or "", "latitude":a.get("lat_dd") or "", "longitude":a.get("long_dd") or "",
                "render_language":render_lang,
                "quarantine": not bool(tv.proof_eligible),
            }
            enriched.append(row)

    d={}
    for r in enriched:
        k=(r["objectid"], r["name_column"], r["raw_name"], r["normalized_name"], r["normalization_mode"], r["case_locale"])
        d[k]=r
    enriched=sorted(d.values(), key=lambda r:(-int(r["length"]), str(r["normalized_name"]).casefold(), int(r["objectid"]), r["name_column"]))
    write_csv(args.out/"all_textual_palindromes_ge9.csv", enriched)
    write_csv(args.out/"quarantine_textual_candidates.csv", [r for r in enriched if r["quarantine"]])

    fonts=base.NotoFontIndex(); renders=[]; failures=[]
    for c in enriched:
        lang=c["render_language"] or "und"
        for kind,text in base.icu_case_variants(c["normalized_name"], lang):
            r=dict(c); r.update(render_case_variant=kind, render_text=text)
            try:
                face=fonts.choose(text,lang)
                mask=base.render_mask(text,face,lang,2048)
                err,dx,dy,_=base.best_mirror_error(mask)
                size=2048
                if base.BORDERLINE_LOW <= err <= base.BORDERLINE_HIGH:
                    mask=base.render_mask(text,face,lang,4096); err,dx,dy,_=base.best_mirror_error(mask); size=4096
                r.update(font_family=face.family,font_path=face.path,font_index=face.index,render_size=size,
                         mirror_error=err,align_dx=dx,align_dy=dy,render_width=mask.shape[1],render_height=mask.shape[0],
                         mirror_pass=err<=base.MIRROR_THRESHOLD)
                renders.append(r)
            except Exception as e:
                r["error"]=f"{type(e).__name__}: {e}"; failures.append(r)

    proof=[r for r in renders if r.get("mirror_pass") and r.get("proof_eligible") and not r.get("quarantine")]
    quarantine=[r for r in renders if r.get("mirror_pass") and (r.get("quarantine") or not r.get("proof_eligible"))]
    write_csv(args.out/"mirror_results.csv", renders)
    write_csv(args.out/"results.csv", proof)
    write_csv(args.out/"quarantine_results.csv", quarantine)
    write_csv(args.out/"render_failures.csv", failures)

    manifest={
        "source":"NGA-GNS official RESEARCH/GIS_OUTPUT ArcGIS whole-world layer",
        "service_url":args.url,
        "service_count_at_start":service_count,
        "ids_enumerated":len(all_ids),
        "unique_ids_enumerated":len(set(all_ids)),
        "ids_equal_service_count":len(all_ids)==service_count==len(set(all_ids)),
        "shard_index":args.shard_index,"shard_count":args.shard_count,
        "shard_expected_ids":len(shard_ids),"rows_scanned":len(fetched_ids),
        "rows_equal_shard_expected":len(fetched_ids)==len(shard_ids),"id_coverage_exact":fetched_ids==expected,
        "full_name_values_scanned":full_name_values,"full_nm_nd_values_scanned":full_nm_nd_values,
        "name_columns_scanned":["full_name","full_nm_nd"],
        "two_pass_candidate_metadata_hydration":True,"checkpointed_batches":True,
        "minimum_grapheme_length":9,"unicode_grapheme_clusters":True,"upper_length_bound":None,
        "icu_case_before_textual_rejection":True,"permissive_punctuation_pass":True,"permissive_punctuation_pass_quarantine_only":True,
        "font_definition":"locked Noto Sans Regular; font-dependent result","visual_mirror_threshold":base.MIRROR_THRESHOLD,
        "textual_palindrome_records_ge9":len(enriched),"mirror_render_variants":len(renders),
        "mirror_pass_variants":len(proof),"quarantine_mirror_pass_variants":len(quarantine),"render_failures":len(failures),
        "retained_metadata":["full_name","full_nm_nd","name_type","language","script","transliteration","feature_class","designation","country","name_termination_date","feature_termination_date","effective_date","latitude","longitude"],
    }
    (args.out/"scan_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(manifest,ensure_ascii=False))
    return 8 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
