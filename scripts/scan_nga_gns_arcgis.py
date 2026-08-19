#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,json,time,urllib.parse,urllib.request
from pathlib import Path
import scan_mirror_palindromes as base

SERVICE='https://geonames.nga.mil/geon-ags/rest/services/RESEARCH/GIS_OUTPUT/MapServer/0/query'
FIELDS='objectid,ufi,uni,full_name,nt,lang_cd,transl_cd,script_cd,term_dt_n,term_dt_f,fc,desig_cd,cc_ft,lat_dd,long_dd'

def fetch(params, tries=8):
    url=SERVICE+'?'+urllib.parse.urlencode(params)
    err=None
    for i in range(tries):
        try:
            with urllib.request.urlopen(url,timeout=60) as r:
                return json.load(r)
        except Exception as e:
            err=e; time.sleep(min(2**i,30))
    raise RuntimeError(f'ArcGIS request failed after retries: {err}')

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--out',type=Path,required=True); ap.add_argument('--page-size',type=int,default=3000)
    a=ap.parse_args(); a.out.mkdir(parents=True,exist_ok=True)
    base.MIRROR_THRESHOLD=0.05
    total=fetch({'where':'1=1','returnCountOnly':'true','f':'json'}).get('count')
    if not isinstance(total,int) or total<=0: raise RuntimeError(f'Bad GNS count: {total!r}')
    fonts=base.NotoFontIndex(); candidates=[]; mirrors=[]; failures=[]; seen=0
    for off in range(0,total,a.page_size):
        j=fetch({'where':'1=1','outFields':FIELDS,'returnGeometry':'false','orderByFields':'objectid ASC','resultOffset':off,'resultRecordCount':a.page_size,'f':'json'})
        feats=j.get('features',[])
        if not feats: raise RuntimeError(f'Empty GNS page at offset {off} of {total}')
        for feat in feats:
            seen+=1; r=feat.get('attributes') or {}; name=(r.get('full_name') or '').strip()
            ok,norm,gs=base.is_palindrome(name)
            if not ok or len(gs)<9: continue
            row={k:r.get(k) for k in r}; row.update({'normalized_name':norm,'length':len(gs),'source':'NGA-GNS-ArcGIS'})
            candidates.append(row)
            lang=(r.get('lang_cd') or 'und').strip().lower() or 'und'
            for case_kind,text in base.icu_case_variants(norm,lang):
                out=dict(row); out.update({'case_variant':case_kind,'render_text':text})
                try:
                    face=fonts.choose(text,lang); mask=base.render_mask(text,face,lang,2048); err,dx,dy,_=base.best_mirror_error(mask)
                    out.update({'font_family':face.family,'mirror_error':err,'align_dx':dx,'align_dy':dy,'mirror_pass':err<=0.05})
                    mirrors.append(out)
                except Exception as e:
                    out['error']=f'{type(e).__name__}: {e}'; failures.append(out)
        print(json.dumps({'offset':off,'seen':seen,'total':total,'candidates':len(candidates)}),flush=True)
    if seen!=total: raise RuntimeError(f'GNS completeness mismatch: saw {seen}, expected {total}')
    def write(name,rows):
        fields=sorted({k for r in rows for k in r})
        with (a.out/name).open('w',newline='',encoding='utf-8') as f:
            w=csv.DictWriter(f,fieldnames=fields); w.writeheader(); w.writerows(rows)
    write('all_textual_palindromes_ge9.csv',candidates); write('mirror_results.csv',mirrors); write('results.csv',[r for r in mirrors if r.get('mirror_pass')]); write('render_failures.csv',failures)
    manifest={'source':'NGA GNS RESEARCH/GIS_OUTPUT ArcGIS whole-world layer','service':SERVICE,'total_name_records':total,'records_scanned':seen,'textual_palindrome_records_ge9':len(candidates),'mirror_render_variants':len(mirrors),'mirror_pass_variants':sum(bool(r.get('mirror_pass')) for r in mirrors),'render_failures':len(failures),'threshold':0.05}
    (a.out/'scan_manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    print(json.dumps(manifest),flush=True)
    if failures: return 8
    return 0

if __name__=='__main__': raise SystemExit(main())
