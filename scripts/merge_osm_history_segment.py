#!/usr/bin/env python3
"""Merge four scanner shard outputs for one block-aligned OSM history segment."""
from __future__ import annotations

import argparse
import csv
import json
import shutil
from pathlib import Path


def rows(shards: list[Path], fn: str) -> list[dict[str, str]]:
    out=[]
    for p in shards:
        q=p/fn
        if q.exists():
            with q.open(encoding='utf-8', newline='') as f:
                out.extend(csv.DictReader(f))
    return out


def uniq(rs, keys):
    d={}
    for r in rs:
        d[tuple(r.get(k,'') for k in keys)] = r
    return list(d.values())


def write(path: Path, rs, fallback=()):
    fields=sorted({k for r in rs for k in r}) if rs else list(fallback)
    with path.open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore'); w.writeheader(); w.writerows(rs)


def add_counts(dest, src):
    for k,v in (src or {}).items():
        dest[k]=dest.get(k,0)+int(v)


def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument('--root', type=Path, required=True)
    ap.add_argument('--shards', type=int, default=4)
    ap.add_argument('--range-manifest', type=Path, required=True)
    ap.add_argument('--md5-state', type=Path, required=True)
    args=ap.parse_args()
    shard_dirs=[args.root/'shards'/str(i) for i in range(args.shards)]
    if any(not p.is_dir() for p in shard_dirs):
        raise SystemExit(f'missing shard directory(s): {[str(p) for p in shard_dirs if not p.is_dir()]}')
    manifests=[]
    for p in shard_dirs:
        q=p/'scan_manifest.json'
        if not q.exists(): raise SystemExit(f'missing {q}')
        manifests.append(json.loads(q.read_text(encoding='utf-8')))

    textual=uniq(rows(shard_dirs,'all_textual_palindromes_ge9.csv'),
        ('osm_type','osm_id','version','name_key','raw_name','normalized_name','case_locale','normalization_mode'))
    mirrors=uniq(rows(shard_dirs,'mirror_results.csv'),
        ('osm_type','osm_id','version','name_key','raw_name','normalized_name','case_locale','normalization_mode','render_case_variant','render_text'))
    failures=uniq(rows(shard_dirs,'render_failures.csv'),
        ('osm_type','osm_id','version','name_key','raw_name','normalized_name','case_locale','normalization_mode','render_case_variant','render_text'))
    proof=[r for r in mirrors if str(r.get('mirror_pass','')).lower() in ('true','1','yes') and str(r.get('proof_eligible','')).lower() in ('true','1','yes') and str(r.get('quarantine','')).lower() not in ('true','1','yes')]
    quarantine=[r for r in mirrors if str(r.get('mirror_pass','')).lower() in ('true','1','yes') and r not in proof]
    if failures:
        raise SystemExit(f'{len(failures)} render failures in segment')

    out=args.root/'segment'; out.mkdir(parents=True,exist_ok=True)
    write(out/'all_textual_palindromes_ge9.csv', textual)
    write(out/'mirror_results.csv', mirrors)
    write(out/'results.csv', proof)
    write(out/'quarantine_results.csv', quarantine)
    write(out/'render_failures.csv', failures)
    key_counts={}; scope_counts={}; sort_counts={}
    for m in manifests:
        add_counts(key_counts,m.get('name_key_counts'))
        add_counts(scope_counts,m.get('name_scope_counts'))
        add_counts(sort_counts,m.get('sort_only_key_counts'))
    rm=json.loads(args.range_manifest.read_text(encoding='utf-8'))
    merged={
        'source_label':'OSM-full-history-260810',
        'segment_start_offset':int(rm['start_offset']),
        'segment_end_offset':int(rm['end_offset']),
        'segment_total_source_bytes':int(rm['total_bytes']),
        'history_file':True,
        'all_node_way_relation_historical_versions_in_segment':True,
        'pbf_block_boundary_start':bool(rm.get('pbf_block_boundary_start')),
        'pbf_block_boundary_end':bool(rm.get('pbf_block_boundary_end')),
        'broad_name_like_safety_filter':True,
        'sorting_name_excluded':True,
        'unknown_name_like_keys_quarantined':True,
        'icu_case_before_textual_rejection':True,
        'permissive_punctuation_pass':True,
        'permissive_punctuation_pass_quarantine_only':True,
        'minimum_grapheme_length':9,
        'unicode_grapheme_clusters':True,
        'upper_length_bound':None,
        'font_definition':'locked Noto Sans Regular; font-dependent result',
        'visual_mirror_threshold':0.05,
        'object_versions_scanned':sum(int(m.get('object_versions_scanned',0)) for m in manifests),
        'name_values_scanned':sum(int(m.get('name_values_scanned',0)) for m in manifests),
        'textual_palindrome_records_ge9':len(textual),
        'mirror_render_variants':len(mirrors),
        'mirror_pass_variants':len(proof),
        'quarantine_mirror_pass_variants':len(quarantine),
        'render_failures':0,
        'name_key_counts':key_counts,
        'name_scope_counts':scope_counts,
        'sort_only_key_counts':sort_counts,
    }
    (out/'scan_manifest.json').write_text(json.dumps(merged,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    shutil.copy2(args.range_manifest,out/'range_manifest.json')
    shutil.copy2(args.md5_state,out/'md5_state.json')
    print(json.dumps(merged,ensure_ascii=False))
    return 0

if __name__=='__main__':
    raise SystemExit(main())
