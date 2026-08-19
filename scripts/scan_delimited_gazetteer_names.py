#!/usr/bin/env python3
"""Scan official delimited gazetteer exports (GNIS/GNS) for mirror palindromes."""
from __future__ import annotations
import argparse, csv, io, json, re, sys, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import scan_mirror_palindromes as base
base.MIRROR_THRESHOLD = 0.05

NAME_HEADERS = {
    "name", "feature_name", "full_name", "full_nm", "full_name_nd", "full_nm_nd",
    "official_name", "variant_name", "alternate_name", "historical_name",
}
ID_HEADERS = ("feature_id", "featureid", "ufi", "uni", "ft_link", "name_link", "objectid", "id")
CLASS_HEADERS = ("feature_class", "featureclass", "fc", "dsg", "designation", "feature_type")
COUNTRY_HEADERS = ("country", "country_name", "cc_nm", "cc_ft", "state_name", "state_alpha")
LANG_HEADERS = ("language", "language_code", "lang_cd", "lc")
SCRIPT_HEADERS = ("script", "script_code", "script_cd")
TYPE_HEADERS = ("name_type", "name_type_code", "nt")


def norm_header(h):
    return re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")

def first(row, headers, mapping):
    for h in headers:
        if h in mapping:
            return row[mapping[h]].strip()
    return ""

def choose_member(z):
    members = [i for i in z.infolist() if not i.is_dir() and i.filename.lower().endswith((".txt", ".csv"))]
    if not members:
        raise RuntimeError("No text/csv data member in archive")
    return max(members, key=lambda i: i.file_size)

def detect_delim(header):
    return "|" if header.count("|") >= header.count("\t") else "\t"

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--zip", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--source-label", required=True)
    args=ap.parse_args(); args.out.mkdir(parents=True, exist_ok=True)
    candidates=[]; rows_seen=0; values_seen=0; selected_headers=[]
    with zipfile.ZipFile(args.zip) as z:
        member=choose_member(z)
        with z.open(member) as raw:
            text=io.TextIOWrapper(raw, encoding="utf-8-sig", errors="replace", newline="")
            header_line=text.readline().rstrip("\r\n")
            delim=detect_delim(header_line)
            headers=next(csv.reader([header_line], delimiter=delim))
            nh=[norm_header(h) for h in headers]; mapping={h:i for i,h in enumerate(nh)}
            name_idx=[i for i,h in enumerate(nh) if h in NAME_HEADERS]
            if not name_idx:
                raise RuntimeError(f"No recognized name column. Headers={headers}")
            selected_headers=[headers[i] for i in name_idx]
            reader=csv.reader(text, delimiter=delim)
            for row in reader:
                rows_seen+=1
                if len(row)<len(headers): row += [""]*(len(headers)-len(row))
                ident=first(row, ID_HEADERS, mapping); cls=first(row, CLASS_HEADERS, mapping)
                country=first(row, COUNTRY_HEADERS, mapping); lang=first(row, LANG_HEADERS, mapping)
                script=first(row, SCRIPT_HEADERS, mapping); ntype=first(row, TYPE_HEADERS, mapping)
                render_lang=lang if re.fullmatch(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{1,8})*",lang or "") else "und"
                for idx in name_idx:
                    raw_name=row[idx].strip()
                    if not raw_name: continue
                    values_seen+=1
                    pal,norm,gs=base.is_palindrome(raw_name)
                    if pal and len(gs)>=9:
                        candidates.append({"source":args.source_label,"record_id":ident,"name_column":headers[idx],"language":lang,"script":script,"name_type":ntype,"raw_name":raw_name,"normalized_name":norm,"length":len(gs),"country_or_region":country,"classification":cls,"render_language":render_lang})
    # dedupe identical record/name-column/name
    u={(r["record_id"],r["name_column"],r["raw_name"],r["language"],r["script"]):r for r in candidates}
    candidates=sorted(u.values(),key=lambda r:(-r["length"],r["normalized_name"].casefold(),r["record_id"],r["name_column"]))
    if candidates:
        fields=list(candidates[0])
    else:
        fields=["source","record_id","name_column","language","script","name_type","raw_name","normalized_name","length","country_or_region","classification","render_language"]
    with (args.out/"all_textual_palindromes_ge9.csv").open("w",encoding="utf-8",newline="") as f:
        w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(candidates)
    fonts=base.NotoFontIndex(); renders=[]; failures=[]
    for c in candidates:
        for case_kind,text in base.icu_case_variants(c["normalized_name"],c["render_language"]):
            r=dict(c); r.update(case_variant=case_kind,render_text=text)
            try:
                face=fonts.choose(text,c["render_language"]); mask=base.render_mask(text,face,c["render_language"],2048)
                err,dx,dy,_=base.best_mirror_error(mask)
                r.update(font_family=face.family,font_path=face.path,font_index=face.index,mirror_error=err,align_dx=dx,align_dy=dy,render_width=mask.shape[1],render_height=mask.shape[0],mirror_pass=err<=base.MIRROR_THRESHOLD)
                renders.append(r)
            except Exception as e:
                r["error"]=f"{type(e).__name__}: {e}"; failures.append(r)
    all_fields=sorted({k for r in renders+failures for k in r})
    def wr(fn,rs):
        with (args.out/fn).open("w",encoding="utf-8",newline="") as f:
            w=csv.DictWriter(f,fieldnames=all_fields,extrasaction="ignore"); w.writeheader(); w.writerows(rs)
    wr("mirror_results.csv",renders); wr("results.csv",[r for r in renders if r.get("mirror_pass")]); wr("render_failures.csv",failures)
    manifest={"source":args.source_label,"archive_member":member.filename,"rows_scanned":rows_seen,"name_values_scanned":values_seen,"name_columns":selected_headers,"textual_palindrome_records_ge9":len(candidates),"mirror_render_variants":len(renders),"mirror_pass_variants":sum(bool(r.get("mirror_pass")) for r in renders),"render_failures":len(failures),"visual_mirror_threshold":0.05}
    (args.out/"scan_manifest.json").write_text(json.dumps(manifest,indent=2,ensure_ascii=False)+"\n",encoding="utf-8"); print(json.dumps(manifest,ensure_ascii=False))
    return 8 if failures else 0
if __name__=="__main__": raise SystemExit(main())
