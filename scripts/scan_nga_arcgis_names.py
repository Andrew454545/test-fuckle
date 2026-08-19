#!/usr/bin/env python3
"""Fallback exhaustive scanner for NGA GNS via its official ArcGIS REST layer.

Used only when the pre-positioned Whole_world ZIP URL is unavailable. The
GIS_OUTPUT layer is the official whole-world GNS name-record service and
contains one row per geographic name with UFI/UNI, name type, language,
script, transliteration, and termination dates.
"""
from __future__ import annotations
import argparse, csv, json, math, re, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import requests
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_mirror_palindromes as base
base.MIRROR_THRESHOLD = 0.05

FIELDS = [
    "objectid","ufi","uni","full_name","nt","lang_cd","script_cd","transl_cd",
    "term_dt_n","term_dt_f","efctv_dt","cc_nm","cc_ft","fc","desig_cd","ft_link","name_link"
]

def get_json(session, url, params, tries=8):
    err=None
    for i in range(tries):
        try:
            r=session.get(url, params=params, timeout=90)
            r.raise_for_status(); j=r.json()
            if "error" in j: raise RuntimeError(j["error"])
            return j
        except Exception as e:
            err=e; time.sleep(min(2**i,30))
    raise RuntimeError(f"ArcGIS request failed after {tries} tries: {err}")

def fetch_page(base_url, offset, page_size):
    s=requests.Session()
    return offset, get_json(s, base_url+"/query", {
        "where":"1=1","outFields":",".join(FIELDS),"returnGeometry":"false",
        "orderByFields":"objectid ASC","resultOffset":offset,
        "resultRecordCount":page_size,"f":"json"
    })

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--url", default="https://geonames.nga.mil/geon-ags/rest/services/RESEARCH/GIS_OUTPUT/MapServer/0")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=12)
    ap.add_argument("--page-size", type=int, default=3000)
    args=ap.parse_args(); args.out.mkdir(parents=True,exist_ok=True)
    s=requests.Session()
    count=get_json(s,args.url+"/query",{"where":"1=1","returnCountOnly":"true","f":"json"})["count"]
    candidates=[]; rows_seen=0; values_seen=0; pages=math.ceil(count/args.page_size)
    offsets=list(range(0,count,args.page_size))
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs={ex.submit(fetch_page,args.url,o,args.page_size):o for o in offsets}
        done=0
        for fut in as_completed(futs):
            off,j=fut.result(); feats=j.get("features",[]); rows_seen+=len(feats); done+=1
            for feat in feats:
                a=feat.get("attributes",{})
                raw=(a.get("full_name") or "").strip()
                if not raw: continue
                values_seen+=1
                pal,norm,gs=base.is_palindrome(raw)
                if pal and len(gs)>=9:
                    lang=(a.get("lang_cd") or "").strip(); render_lang=lang if re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*",lang) else "und"
                    candidates.append({
                        "source":"NGA-GNS-ArcGIS-GIS_OUTPUT","record_id":str(a.get("uni") or a.get("objectid") or ""),
                        "ufi":a.get("ufi") or "","uni":a.get("uni") or "","objectid":a.get("objectid") or "",
                        "name_column":"full_name","language":lang,"script":a.get("script_cd") or "",
                        "transliteration":a.get("transl_cd") or "","name_type":a.get("nt") or "",
                        "raw_name":raw,"normalized_name":norm,"length":len(gs),
                        "country_or_region":a.get("cc_nm") or a.get("cc_ft") or "",
                        "classification":a.get("fc") or "","designation":a.get("desig_cd") or "",
                        "feature_link":a.get("ft_link") or "","name_link":a.get("name_link") or "",
                        "name_termination_date":a.get("term_dt_n") or "","feature_termination_date":a.get("term_dt_f") or "",
                        "effective_date":a.get("efctv_dt") or "","render_language":render_lang,
                    })
            if done % 100 == 0: print(json.dumps({"pages_done":done,"pages_total":pages,"rows_seen":rows_seen,"candidates":len(candidates)}), flush=True)
    if rows_seen != count:
        raise RuntimeError(f"Pagination completeness failure: count={count}, rows_seen={rows_seen}")
    u={(r["objectid"],r["uni"],r["raw_name"]):r for r in candidates}; candidates=sorted(u.values(),key=lambda r:(-r["length"],r["normalized_name"].casefold(),str(r["uni"])))
    fields=list(candidates[0]) if candidates else ["source","record_id","ufi","uni","objectid","name_column","language","script","transliteration","name_type","raw_name","normalized_name","length","country_or_region","classification","designation","feature_link","name_link","name_termination_date","feature_termination_date","effective_date","render_language"]
    with (args.out/"all_textual_palindromes_ge9.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(candidates)
    fonts=base.NotoFontIndex(); renders=[]; failures=[]
    for c in candidates:
        for case_kind,text in base.icu_case_variants(c["normalized_name"],c["render_language"]):
            r=dict(c); r.update(case_variant=case_kind,render_text=text)
            try:
                face=fonts.choose(text,c["render_language"]); mask=base.render_mask(text,face,c["render_language"],2048)
                err,dx,dy,_=base.best_mirror_error(mask)
                r.update(font_family=face.family,font_path=face.path,font_index=face.index,mirror_error=err,align_dx=dx,align_dy=dy,render_width=mask.shape[1],render_height=mask.shape[0],mirror_pass=err<=0.05)
                renders.append(r)
            except Exception as e:
                r["error"]=f"{type(e).__name__}: {e}"; failures.append(r)
    all_fields=sorted({k for r in renders+failures for k in r})
    def wr(fn,rs):
        with (args.out/fn).open("w",encoding="utf-8",newline="") as f:
            w=csv.DictWriter(f,fieldnames=all_fields,extrasaction="ignore"); w.writeheader(); w.writerows(rs)
    wr("mirror_results.csv",renders); wr("results.csv",[r for r in renders if r.get("mirror_pass")]); wr("render_failures.csv",failures)
    manifest={"source":"NGA-GNS official ArcGIS GIS_OUTPUT layer","service_url":args.url,"service_count_at_start":count,"rows_scanned":rows_seen,"name_values_scanned":values_seen,"textual_palindrome_records_ge9":len(candidates),"mirror_render_variants":len(renders),"mirror_pass_variants":sum(bool(r.get("mirror_pass")) for r in renders),"render_failures":len(failures),"visual_mirror_threshold":0.05,"scope_note":"One official GNS name record per GIS_OUTPUT row, retaining approved/variant/historical/non-Roman/transliteration metadata."}
    (args.out/"scan_manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); print(json.dumps(manifest,ensure_ascii=False))
    return 8 if failures else 0
if __name__=="__main__": raise SystemExit(main())
