#!/usr/bin/env python3
"""Consolidate the required source artifacts and enforce a 99%-claim proof gate.

This script intentionally refuses to certify readiness if a required source is
missing or if a source-specific completeness invariant is absent.  It preserves
same-name records from different features/sources; deduplication removes only
exact duplicate evidence rows from the same source identity.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

REQUIRED = (
    "geonames_historic",
    "geonames_ascii_inline",
    "osm_current",
    "osm_history",
    "usgs_gnis",
    "usgs_gnis_safety",
    "nga_gns",
    "usgs_antarctica",
    "gebco_scufn",
)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"missing required manifest: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def require(ok: bool, msg: str) -> None:
    if not ok:
        raise RuntimeError(msg)


def intval(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def boolval(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in {"1", "true", "yes"}


def validate_geonames_historic(root: Path) -> dict[str, Any]:
    m = read_json(root / "scan_manifest.json")
    require(intval(m.get("minimum_grapheme_length")) == 9, "GeoNames historic min length is not 9")
    require(intval(m.get("primary_feature_rows")) > 0, "GeoNames historic primary row count missing")
    require(intval(m.get("alternate_name_rows")) > 0, "GeoNames historic alternateNamesV2 row count missing")
    require(m.get("historic_alternate_names_excluded") is False, "GeoNames historic names were excluded")
    require(m.get("alternate_names_with_to_period_excluded") is False, "GeoNames expired/to-period names were excluded")
    require(intval(m.get("render_failures")) == 0, "GeoNames historic render failures")
    require(bool(m.get("all_countries_sha256")) and bool(m.get("alternate_names_v2_sha256")), "GeoNames historic source hashes missing")
    return m


def require_new_safety(m: dict[str, Any], label: str) -> None:
    require(intval(m.get("minimum_grapheme_length")) == 9, f"{label} min grapheme length is not 9")
    require(boolval(m.get("unicode_grapheme_clusters")), f"{label} Unicode grapheme-cluster handling missing")
    require(m.get("upper_length_bound", "sentinel") is None, f"{label} upper length bound present")
    require(boolval(m.get("icu_case_before_textual_rejection")), f"{label} ICU-before-rejection safety missing")
    require(boolval(m.get("permissive_punctuation_pass")), f"{label} punctuation safety pass missing")
    require(boolval(m.get("permissive_punctuation_pass_quarantine_only")), f"{label} punctuation safety is not quarantine-only")
    require("Noto Sans Regular" in str(m.get("font_definition", "")), f"{label} locked Noto Sans Regular definition missing")


def validate_geonames_supp(root: Path) -> dict[str, Any]:
    m = read_json(root / "scan_manifest.json")
    fields = set(m.get("name_fields_scanned", []))
    require({"canonical_name", "asciiname", "alternatenames_inline", "alternateNamesV2"}.issubset(fields), "GeoNames all-representation safety/supplement coverage missing")
    require(boolval(m.get("inline_alternatenames_include_geonames_automatic_ascii_transliterations")), "GeoNames automatic ASCII transliterations not explicitly covered")
    require(intval(m.get("primary_feature_rows")) > 0, "GeoNames supplemental primary rows not scanned")
    require(intval(m.get("alternate_name_rows")) > 0, "GeoNames supplemental alternateNamesV2 rows not scanned")
    require(boolval(m.get("historic_alternate_names_included")), "GeoNames historical alternateNamesV2 safety re-scan missing")
    require(intval(m.get("render_failures")) == 0, "GeoNames supplemental render failures")
    require_new_safety(m, "GeoNames supplemental")
    return m


def validate_osm_current(root: Path) -> dict[str, Any]:
    m = read_json(root / "scan_manifest.json")
    require(intval(m.get("render_failures")) == 0, "OSM current render failures")
    require(intval(m.get("object_versions_scanned")) > 0, "OSM current scan count missing")
    off = (root / "official.md5").read_text(encoding="utf-8").split()[0] if (root / "official.md5").exists() else ""
    comp = (root / "computed.md5").read_text(encoding="utf-8").strip().split()[0] if (root / "computed.md5").exists() else ""
    require(bool(off) and off == comp, "OSM current frozen Planet MD5 mismatch/missing")
    return m


def validate_osm_history(root: Path) -> dict[str, Any]:
    m = read_json(root / "scan_manifest.json")
    require(boolval(m.get("history_file")), "OSM full-history manifest not marked history_file")
    require(intval(m.get("render_failures")) == 0, "OSM history render failures")
    require(boolval(m.get("official_md5_match")), "OSM history official MD5 match not proven")
    require("history-260810.osm.pbf" in str(m.get("history_url", "")), "OSM history frozen history-260810 source not proven")
    require(boolval(m.get("sorting_name_excluded")), "OSM history sorting_name exclusion missing")
    require_new_safety(m, "OSM history")
    required_namespaces = {"full_name", "ref_name", "nickname", "bridge:name", "tunnel:name"}
    declared = set(m.get("explicit_supplemental_namespaces", []))
    require(required_namespaces.issubset(declared), "OSM history supplemental feature-name namespaces incomplete")
    require(boolval(m.get("broad_name_like_safety_filter")), "OSM history broad name-like safety filter missing")
    return m


def validate_gnis(root: Path) -> dict[str, Any]:
    ma = read_json(root / "gnis-all" / "scan_manifest.json")
    mh = read_json(root / "gnis-historical" / "scan_manifest.json")
    require(intval(ma.get("rows_scanned")) > 0, "GNIS All Names rows missing")
    require(intval(mh.get("rows_scanned")) > 0, "GNIS Historical rows missing")
    require(intval(ma.get("render_failures")) == 0 and intval(mh.get("render_failures")) == 0, "GNIS render failures")
    return {"all_names": ma, "historical": mh}


def validate_gnis_safety(root: Path) -> dict[str, Any]:
    ma = read_json(root / "all" / "scan_manifest.json")
    mh = read_json(root / "historical" / "scan_manifest.json")
    for label, m in (("all", ma), ("historical", mh)):
        require(intval(m.get("rows_scanned")) > 0, f"GNIS safety {label} rows missing")
        require(intval(m.get("render_failures")) == 0, f"GNIS safety {label} render failures")
        require_new_safety(m, f"GNIS safety {label}")
    return {"all_names": ma, "historical": mh}


def validate_nga(root: Path) -> dict[str, Any]:
    m = read_json(root / "scan_manifest.json")
    require("RESEARCH/GIS_OUTPUT" in str(m.get("service_url", "")), "NGA wrong ArcGIS service")
    require(boolval(m.get("rows_equal_service_count")), "NGA rows != service count")
    require(boolval(m.get("ids_equal_service_count")), "NGA OBJECTID enumeration != service count")
    require(intval(m.get("service_count")) > 0 and intval(m.get("rows_scanned")) == intval(m.get("service_count")), "NGA exact row count not proven")
    require(boolval(m.get("exact_objectid_coverage")), "NGA exact OBJECTID coverage missing")
    require({"full_name", "full_nm_nd"}.issubset(set(m.get("name_fields_scanned", []))), "NGA full_name/full_nm_nd not both scanned")
    retained = set(m.get("retained_metadata", []))
    needed = {"name_type", "language", "script", "transliteration", "feature_class", "designation", "country", "name_termination_date", "feature_termination_date"}
    require(needed.issubset(retained), "NGA required metadata not retained")
    require(intval(m.get("render_failures")) == 0, "NGA render failures")
    require_new_safety(m, "NGA")
    return m


def validate_antarctica(root: Path) -> dict[str, Any]:
    m = read_json(root / "scan_manifest.json")
    require("Antarctica" in str(m.get("source", "")) or "Antarctic" in str(m.get("source", "")), "USGS Antarctic source identity missing")
    require(intval(m.get("rows_scanned")) > 0, "USGS Antarctic rows missing")
    require("feature_name" in set(m.get("name_fields_scanned", [])), "USGS Antarctic feature_name not scanned")
    require(boolval(m.get("scar_placeid_retained")), "USGS Antarctic SCAR cross-reference id not retained")
    require(boolval(m.get("scar_candidate_crosscheck_attempted")), "USGS Antarctic candidate-level SCAR cross-check was not attempted")
    require(intval(m.get("render_failures")) == 0, "USGS Antarctic render failures")
    require_new_safety(m, "USGS Antarctic")
    return m


def validate_gebco(root: Path) -> dict[str, Any]:
    m = read_json(root / "scan_manifest.json")
    require("SCUFN" in str(m.get("source", "")), "GEBCO/SCUFN source identity missing")
    require(intval(m.get("layers_scanned")) >= 3, "GEBCO/SCUFN point/line/polygon layers not all scanned")
    require(boolval(m.get("all_layer_counts_equal_rows_scanned")), "GEBCO/SCUFN layer count equality missing")
    require(intval(m.get("rows_scanned")) > 0, "GEBCO/SCUFN rows missing")
    require(intval(m.get("render_failures")) == 0, "GEBCO/SCUFN render failures")
    require_new_safety(m, "GEBCO/SCUFN")
    return m


VALIDATORS = {
    "geonames_historic": validate_geonames_historic,
    "geonames_ascii_inline": validate_geonames_supp,
    "osm_current": validate_osm_current,
    "osm_history": validate_osm_history,
    "usgs_gnis": validate_gnis,
    "usgs_gnis_safety": validate_gnis_safety,
    "nga_gns": validate_nga,
    "usgs_antarctica": validate_antarctica,
    "gebco_scufn": validate_gebco,
}


def source_result_files(label: str, root: Path) -> list[Path]:
    if label == "usgs_gnis":
        return [root / "gnis-all" / "results.csv", root / "gnis-historical" / "results.csv"]
    if label == "usgs_gnis_safety":
        return [root / "all" / "results.csv", root / "historical" / "results.csv"]
    return [root / "results.csv"]


def source_quarantine_files(label: str, root: Path) -> list[Path]:
    if label == "usgs_gnis_safety":
        files = [root / "all" / "quarantine_results.csv", root / "historical" / "quarantine_results.csv"]
    else:
        files = [root / "quarantine_results.csv"]
    return [p for p in files if p.exists()]


def stable_identity(label: str, r: dict[str, str]) -> tuple[str, ...]:
    candidates = [
        r.get("geonameid", ""), r.get("alternate_name_id", ""),
        r.get("osm_type", ""), r.get("osm_id", ""), r.get("version", ""), r.get("name_key", ""),
        r.get("objectid", ""), r.get("uni", ""), r.get("ufi", ""),
        r.get("feature_id", ""), r.get("antarctica_id", ""), r.get("scar_placeid", ""),
        r.get("FEATURE_ID", ""), r.get("OBJECTID", ""), r.get("layer_id", ""),
        r.get("feature_name", ""), r.get("raw_name", ""), r.get("normalized_name", ""),
        r.get("render_text", ""), r.get("case_locale", ""), r.get("normalization_mode", ""),
    ]
    return tuple([label] + [str(x) for x in candidates])


def review_id(label: str, r: dict[str, str]) -> str:
    raw = "\x1f".join(stable_identity(label, r)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def load_reviews(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None or not path.exists():
        return {}
    rows = read_csv(path)
    out = {}
    for r in rows:
        rid = (r.get("review_id") or "").strip()
        disposition = (r.get("disposition") or "").strip().lower()
        if not rid or not disposition:
            continue
        if disposition not in {"include", "exclude"}:
            raise RuntimeError(f"invalid quarantine review disposition {disposition!r} for {rid}")
        out[rid] = r
    return out


def grapheme_length(r: dict[str, str]) -> int:
    for k in ("length", "grapheme_length"):
        if r.get(k):
            return intval(r[k])
    return 0


def historical_hint(r: dict[str, str]) -> bool:
    if str(r.get("is_historic", "")).strip() == "1":
        return True
    key = str(r.get("name_key", "")).lower()
    return bool(re.search(r"(?:^|:)(old_name|historic_name|former_name|previous_name|was|abandoned|disused|demolished|razed|removed|destroyed)(?::|$)", key))


def obvious_test_string(r: dict[str, str]) -> str:
    s = r.get("normalized_name", "")
    chars = list(s.casefold())
    if len(chars) >= 9 and len(set(chars)) == 1:
        return "single grapheme repeated >=9 times"
    if len(chars) >= 9 and len(set(chars)) <= 2:
        for n in (1, 2):
            unit = chars[:n]
            if unit and (unit * ((len(chars) + n - 1) // n))[:len(chars)] == chars:
                return "1-2 grapheme test-pattern repetition"
    return ""


def write_union_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = sorted({k for r in rows for k in r})
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def parse_source(spec: str) -> tuple[str, Path]:
    if "=" not in spec:
        raise argparse.ArgumentTypeError("--source must be label=/path")
    label, p = spec.split("=", 1)
    return label, Path(p)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="append", required=True, help="required-source label=/artifact/directory")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--quarantine-review", type=Path, help="CSV with review_id,disposition(include|exclude),notes")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    roots: dict[str, Path] = {}
    for spec in args.source:
        label, root = parse_source(spec)
        if label in roots:
            raise RuntimeError(f"duplicate source label: {label}")
        roots[label] = root
    missing = sorted(set(REQUIRED) - set(roots))
    extra = sorted(set(roots) - set(REQUIRED))
    require(not missing, f"missing required source artifacts: {missing}")
    require(not extra, f"unknown source labels: {extra}")

    validations = {}
    for label in REQUIRED:
        validations[label] = VALIDATORS[label](roots[label])

    proof_rows: list[dict[str, Any]] = []
    quarantine_rows: list[dict[str, Any]] = []
    for label in REQUIRED:
        root = roots[label]
        for fn in source_result_files(label, root):
            for r in read_csv(fn):
                rr: dict[str, Any] = dict(r)
                rr["evidence_source"] = label
                rr["evidence_file"] = str(fn.relative_to(root))
                rr["historic_hint"] = historical_hint(r)
                junk = obvious_test_string(r)
                if junk:
                    rr["quarantine_reason_consolidated"] = junk
                    quarantine_rows.append(rr)
                else:
                    proof_rows.append(rr)
        for fn in source_quarantine_files(label, root):
            for r in read_csv(fn):
                rr = dict(r)
                rr["evidence_source"] = label
                rr["evidence_file"] = str(fn.relative_to(root))
                rr["historic_hint"] = historical_hint(r)
                quarantine_rows.append(rr)

    uniq = {}
    for r in proof_rows:
        uniq[stable_identity(str(r["evidence_source"]), r)] = r
    proof_rows = list(uniq.values())

    quniq = {}
    for r in quarantine_rows:
        label = str(r["evidence_source"])
        r["review_id"] = review_id(label, r)
        quniq[stable_identity(label, r)] = r
    quarantine_rows = list(quniq.values())

    reviews = load_reviews(args.quarantine_review)
    unresolved = []
    resolved_excluded = []
    promoted = []
    for r in quarantine_rows:
        rv = reviews.get(str(r["review_id"]))
        if not rv:
            r["manual_review_status"] = "unresolved"
            unresolved.append(r)
            continue
        disposition = rv["disposition"].strip().lower()
        r["manual_review_status"] = disposition
        r["manual_review_notes"] = rv.get("notes", "")
        if disposition == "include":
            if str(r.get("normalization_mode", "locked")) != "locked":
                r["manual_review_status"] = "requires_normalization_patch_and_rerun"
                unresolved.append(r)
            else:
                r["manual_review_promoted"] = True
                promoted.append(r)
        else:
            resolved_excluded.append(r)

    proof_rows.extend(promoted)
    uniq = {}
    for r in proof_rows:
        uniq[stable_identity(str(r["evidence_source"]), r)] = r
    proof_rows = sorted(uniq.values(), key=lambda r: (-grapheme_length(r), str(r.get("normalized_name", r.get("raw_name", ""))).casefold(), str(r["evidence_source"])))
    quarantine_rows = sorted(quarantine_rows, key=lambda r: (-grapheme_length(r), str(r.get("normalized_name", r.get("raw_name", ""))).casefold(), str(r["evidence_source"])))

    ge10 = [r for r in proof_rows if grapheme_length(r) >= 10]
    historic = [r for r in proof_rows if boolval(r.get("historic_hint"))]

    write_union_csv(args.out / "all_legitimate_results_ge9.csv", proof_rows)
    write_union_csv(args.out / "results_ge10.csv", ge10)
    write_union_csv(args.out / "historical_name_hits_for_review.csv", historic)
    write_union_csv(args.out / "quarantine_results.csv", quarantine_rows)
    review_template = [{
        "review_id": r["review_id"], "disposition": "", "notes": "",
        "evidence_source": r.get("evidence_source", ""), "evidence_file": r.get("evidence_file", ""),
        "raw_name": r.get("raw_name", r.get("feature_name", "")),
        "normalized_name": r.get("normalized_name", ""), "normalization_mode": r.get("normalization_mode", ""),
        "name_key": r.get("name_key", ""), "classification": r.get("classification", ""),
        "quarantine_reason": r.get("quarantine_reason", r.get("quarantine_reason_consolidated", "")),
    } for r in unresolved]
    write_union_csv(args.out / "quarantine_review_template.csv", review_template)

    antarctic = validations["usgs_antarctica"]
    scar_checked = intval(antarctic.get("scar_candidate_placeids_checked"))
    scar_found = intval(antarctic.get("scar_candidate_placeids_found"))
    scar_errors = intval(antarctic.get("scar_candidate_crosscheck_errors"))
    if scar_checked == 0:
        scar_status = "completed_no_palindrome_candidate_placeids_to_crosscheck"
    elif scar_errors == 0 and scar_found == scar_checked:
        scar_status = "completed_all_candidate_placeids_found"
    else:
        scar_status = "partial_candidate_crosscheck_nonblocking"

    ready = len(unresolved) == 0
    gate = {
        "proof_gate_version": 2,
        "ready_for_practical_99_percent_claim": ready,
        "required_sources_validated": list(REQUIRED),
        "source_validations": validations,
        "all_required_source_validations_pass": True,
        "scar_composite_gazetteer_crosscheck": scar_status,
        "scar_candidate_placeids_checked": scar_checked,
        "scar_candidate_placeids_found": scar_found,
        "scar_candidate_crosscheck_errors": scar_errors,
        "scar_crosscheck_required_for_gate": False,
        "wikidata_coordinate_label_crosscheck": "optional_non_authoritative_not_required_for_gate",
        "minimum_grapheme_length": 9,
        "upper_length_bound": None,
        "unicode_grapheme_clusters": True,
        "locale_aware_icu_case_before_textual_rejection": True,
        "permissive_punctuation_pass_is_quarantine_only": True,
        "font_definition": "locked Noto Sans Regular rendering; mirrorability is font-dependent and not generalized to all typefaces",
        "proof_result_rows_ge9": len(proof_rows),
        "proof_result_rows_ge10": len(ge10),
        "historical_name_hits_for_review": len(historic),
        "quarantine_rows": len(quarantine_rows),
        "quarantine_resolved_excluded": len(resolved_excluded),
        "quarantine_promoted_after_manual_review": len(promoted),
        "quarantine_unresolved": len(unresolved),
        "unresolved_quarantine_rows": len(unresolved),
        "deduplication_policy": "only exact duplicate evidence within the same source identity is collapsed; distinct features and cross-source provenance are preserved",
    }
    (args.out / "proof_gate_99pct.json").write_text(json.dumps(gate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(gate, ensure_ascii=False))
    return 0 if gate["ready_for_practical_99_percent_claim"] else 9


if __name__ == "__main__":
    raise SystemExit(main())
