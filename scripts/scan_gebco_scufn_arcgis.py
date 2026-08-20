#!/usr/bin/env python3
"""Authoritative IHO-IOC GEBCO/SCUFN undersea-feature gazetteer cross-check."""
from __future__ import annotations
import argparse,csv,json,random,sys,time
from pathlib import Path
import requests
sys.path.insert(0,str(Path(__file__).resolve().parent))
import scan_mirror_palindromes as base
import palindrome_safety as safety
base.MIRROR_THRESHOLD=0.05
DEFAULT='https://services2.arcgis.com/C8EMgrsFcRFL6LrL/arcgis/rest/services/Undersea_Features/FeatureServer'

def get_json(url,params=None,tries=12):
 last=None
 for i in range(tries):
  try:
   r=requests.get(url,params=params or {},timeout=(20,120),headers={'User-Agent':'test-fuckle-gebco-proof/1.0'});r.raise_for_status();j=r.json()
   if 'error' in j:raise RuntimeError(j['error'])
   return j
  except Exception as e:
   last=e
   if i+1==tries:break
   time.sleep(min(2**i,30)+random.random())
 raise RuntimeError(f'ArcGIS failure: {last}')
def write_csv(p,rows):
 fields=sorted({k for r in rows for k in r}) if rows else []
 with p.open('w',encoding='utf-8',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--service',default=DEFAULT);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();a.out.mkdir(parents=True,exist_ok=True)
 candidates=[];layer_manifests=[];meta_snapshot={}
 for layer in (0,1,2):
  url=f'{a.service}/{layer}';meta=get_json(url,{'f':'json'});meta_snapshot[str(layer)]=meta
  count=int(get_json(url+'/query',{'where':'1=1','returnCountOnly':'true','f':'json'})['count']);seen=0;offset=0
  while offset<count:
   j=get_json(url+'/query',{'where':'1=1','outFields':'NAME,TYPE,FEATURE_ID,OBJECTID','returnGeometry':'false','orderByFields':'OBJECTID ASC','resultOffset':offset,'resultRecordCount':2000,'f':'json'})
   feats=j.get('features',[])
   if not feats:raise RuntimeError(f'layer {layer} pagination stopped at {offset}/{count}')
   seen+=len(feats);offset+=len(feats)
   for feat in feats:
    x=feat.get('attributes',{});raw=(x.get('NAME') or '').strip()
    if not raw:continue
    for tv in safety.textual_palindrome_variants(raw,'und',9,True):
     candidates.append({'source':'IHO-IOC GEBCO SCUFN Gazetteer','layer_id':layer,'layer_name':meta.get('name',''),'objectid':x.get('OBJECTID',''),'feature_id':x.get('FEATURE_ID',''),'feature_type':x.get('TYPE',''),'raw_name':raw,'normalized_name':tv.normalized_name,'length':tv.length,'textual_case_variant':tv.textual_case_variant,'case_locale':tv.case_locale,'normalization_mode':tv.normalization_mode,'proof_eligible':tv.proof_eligible,'quarantine':not tv.proof_eligible,'quarantine_reason':tv.quarantine_reason})
  if seen!=count:raise RuntimeError(f'layer {layer} count mismatch {seen}!={count}')
  layer_manifests.append({'layer_id':layer,'layer_name':meta.get('name',''),'service_count':count,'rows_scanned':seen,'rows_equal_service_count':seen==count,'data_last_edit_date':meta.get('editingInfo',{}).get('lastEditDate') or meta.get('lastEditDate')})
 (a.out/'source_layer_metadata.json').write_text(json.dumps(meta_snapshot,ensure_ascii=False,indent=2)+'\n')
 d={}
 for r in candidates:d[(r['layer_id'],r['feature_id'],r['objectid'],r['raw_name'],r['normalized_name'],r['normalization_mode'],r['case_locale'])]=r
 candidates=sorted(d.values(),key=lambda r:(-int(r['length']),r['normalized_name'].casefold(),int(r['layer_id']),str(r['feature_id'])))
 write_csv(a.out/'all_textual_palindromes_ge9.csv',candidates);write_csv(a.out/'quarantine_textual_candidates.csv',[r for r in candidates if r['quarantine']])
 fonts=base.NotoFontIndex();renders=[];fails=[]
 for c in candidates:
  lang=c['case_locale'] or 'und'
  for kind,text in base.icu_case_variants(c['normalized_name'],lang):
   r=dict(c);r.update(render_case_variant=kind,render_text=text)
   try:
    face=fonts.choose(text,lang);mask=base.render_mask(text,face,lang,2048);err,dx,dy,_=base.best_mirror_error(mask);size=2048
    if base.BORDERLINE_LOW<=err<=base.BORDERLINE_HIGH:
     mask=base.render_mask(text,face,lang,4096);err,dx,dy,_=base.best_mirror_error(mask);size=4096
    r.update(font_family=face.family,font_path=face.path,font_index=face.index,render_size=size,mirror_error=err,align_dx=dx,align_dy=dy,render_width=mask.shape[1],render_height=mask.shape[0],mirror_pass=err<=base.MIRROR_THRESHOLD);renders.append(r)
   except Exception as e:r['error']=f'{type(e).__name__}: {e}';fails.append(r)
 proof=[r for r in renders if r.get('mirror_pass') and r.get('proof_eligible') and not r.get('quarantine')];q=[r for r in renders if r.get('mirror_pass') and r not in proof]
 write_csv(a.out/'mirror_results.csv',renders);write_csv(a.out/'results.csv',proof);write_csv(a.out/'quarantine_results.csv',q);write_csv(a.out/'render_failures.csv',fails)
 m={'source':'IHO-IOC GEBCO Gazetteer of Undersea Feature Names (SCUFN), official NCEI ArcGIS FeatureServer','service_url':a.service,'layers':layer_manifests,'layers_scanned':len(layer_manifests),'all_layer_counts_equal':all(x['rows_equal_service_count'] for x in layer_manifests),'all_layer_counts_equal_rows_scanned':all(x['rows_equal_service_count'] for x in layer_manifests),'total_rows_scanned':sum(x['rows_scanned'] for x in layer_manifests),'rows_scanned':sum(x['rows_scanned'] for x in layer_manifests),'minimum_grapheme_length':9,'unicode_grapheme_clusters':True,'upper_length_bound':None,'icu_case_before_textual_rejection':True,'permissive_punctuation_pass':True,'permissive_punctuation_pass_quarantine_only':True,'font_definition':'locked Noto Sans Regular; font-dependent result','visual_mirror_threshold':base.MIRROR_THRESHOLD,'textual_palindrome_records_ge9':len(candidates),'mirror_render_variants':len(renders),'mirror_pass_variants':len(proof),'quarantine_mirror_pass_variants':len(q),'render_failures':len(fails)}
 (a.out/'scan_manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n');print(json.dumps(m,ensure_ascii=False));return 8 if fails else 0
if __name__=='__main__':raise SystemExit(main())
