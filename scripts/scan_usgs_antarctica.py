#!/usr/bin/env python3
"""Scan the official USGS/BGN Antarctica GNIS text product."""
from __future__ import annotations
import argparse,csv,hashlib,html,io,json,sys,time,zipfile
from pathlib import Path
import requests
sys.path.insert(0,str(Path(__file__).resolve().parent))
import scan_mirror_palindromes as base
import palindrome_safety as safety
base.MIRROR_THRESHOLD=0.05


SCAR_SEARCH = "https://data.aad.gov.au/aadc/gaz/scar/search_names_action.cfm"

def scar_lookup(placeid, expected_name):
 for attempt in range(5):
  try:
   r=requests.get(SCAR_SEARCH,params={
    'country_id':'0','east':'180.0','feature_type_code':'0','gazetteers':'SCAR',
    'north':'-45.0','radius':'0.5','search_near':'','search_text':str(placeid),
    'south':'-90.0','west':'-180.0'},timeout=(20,90),headers={'User-Agent':'test-fuckle-antarctica-proof/1.0'})
   r.raise_for_status(); text=html.unescape(r.text)
   found=f"Place ID: {placeid}" in text
   return {'scar_placeid':str(placeid),'found':found,'expected_name_present':expected_name.casefold() in text.casefold(),'http_status':r.status_code,'error':''}
  except Exception as e:
   if attempt==4:return {'scar_placeid':str(placeid),'found':False,'expected_name_present':False,'http_status':'','error':f'{type(e).__name__}: {e}'}
   time.sleep(min(2**attempt,16))


def sha256_file(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  for b in iter(lambda:f.read(8*1024*1024),b''):h.update(b)
 return h.hexdigest()

def write_csv(p,rows):
 fields=sorted({k for r in rows for k in r}) if rows else []
 with p.open('w',encoding='utf-8',newline='') as f:
  w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)

def main():
 ap=argparse.ArgumentParser();ap.add_argument('--zip',type=Path,required=True);ap.add_argument('--out',type=Path,required=True);a=ap.parse_args();a.out.mkdir(parents=True,exist_ok=True)
 candidates=[];rows_seen=0;members=[]
 with zipfile.ZipFile(a.zip) as z:
  members=[n for n in z.namelist() if n.lower().endswith(('.txt','.csv')) and not n.endswith('/')]
  if not members: raise RuntimeError('Antarctica ZIP contains no text/CSV member')
  for member in members:
   with z.open(member) as raw:
    text=io.TextIOWrapper(raw,encoding='utf-8-sig',errors='strict',newline='')
    sample=text.read(8192);text.seek(0)
    delim='|' if sample.count('|')>=sample.count('\t') else '\t'
    reader=csv.DictReader(text,delimiter=delim)
    if not reader.fieldnames or 'feature_name' not in {x.strip().lower() for x in reader.fieldnames}:
     continue
    for row in reader:
     rows_seen+=1
     normkeys={k.strip().lower():v for k,v in row.items() if k is not None}
     rawname=(normkeys.get('feature_name') or '').strip()
     if not rawname: continue
     for tv in safety.textual_palindrome_variants(rawname,'und',9,True):
      candidates.append({
       'source':'USGS-BGN-Antarctica','source_member':member,
       'feature_id':normkeys.get('feature_id',''),'antarctica_id':normkeys.get('antarctica_id',''),
       'scar_placeid':normkeys.get('scar_placeid',''),'feature_name':rawname,
       'feature_class':normkeys.get('feature_class',''),'date_created':normkeys.get('date_created',''),
       'date_edited':normkeys.get('date_edited',''),'bgn_type':normkeys.get('bgn_type',''),
       'bgn_authority':normkeys.get('bgn_authority',''),'bgn_date':normkeys.get('bgn_date',''),
       'latitude':normkeys.get('prim_lat_dec',''),'longitude':normkeys.get('prim_long_dec',''),
       'description':normkeys.get('description',''),'history':normkeys.get('history',''),
       'raw_name':rawname,'normalized_name':tv.normalized_name,'length':tv.length,
       'textual_case_variant':tv.textual_case_variant,'case_locale':tv.case_locale,
       'normalization_mode':tv.normalization_mode,'proof_eligible':tv.proof_eligible,
       'quarantine':not tv.proof_eligible,'quarantine_reason':tv.quarantine_reason,
      })
 d={}
 for r in candidates:d[(r['feature_id'],r['raw_name'],r['normalized_name'],r['normalization_mode'],r['case_locale'])]=r
 candidates=sorted(d.values(),key=lambda r:(-int(r['length']),r['normalized_name'].casefold(),r['feature_id']))
 # Candidate-level independent SCAR CGA cross-check using the USGS scar_placeid.
 scar_cache={}
 for c in candidates:
  pid=str(c.get('scar_placeid') or '').strip()
  if not pid:
   c.update(scar_crosscheck_found=False,scar_crosscheck_name_present=False,scar_crosscheck_error='USGS row has no scar_placeid')
   continue
  if pid not in scar_cache: scar_cache[pid]=scar_lookup(pid,c['feature_name'])
  x=scar_cache[pid];c.update(scar_crosscheck_found=x['found'],scar_crosscheck_name_present=x['expected_name_present'],scar_crosscheck_error=x['error'])
 (a.out/'scar_candidate_crosscheck.json').write_text(json.dumps(list(scar_cache.values()),ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
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
 m={'source':'USGS/BGN official Antarctica GNIS text product','source_sha256':sha256_file(a.zip),'text_members_scanned':members,'rows_scanned':rows_seen,'name_fields_scanned':['feature_name'],'scar_placeid_retained':True,'scar_placeid_retained_for_crosscheck':True,'scar_candidate_crosscheck_attempted':True,'scar_candidate_placeids_checked':len(scar_cache),'scar_candidate_placeids_found':sum(1 for x in scar_cache.values() if x['found']),'scar_candidate_crosscheck_errors':sum(1 for x in scar_cache.values() if x['error']),'minimum_grapheme_length':9,'unicode_grapheme_clusters':True,'upper_length_bound':None,'icu_case_before_textual_rejection':True,'permissive_punctuation_pass':True,'permissive_punctuation_pass_quarantine_only':True,'font_definition':'locked Noto Sans Regular; font-dependent result','visual_mirror_threshold':base.MIRROR_THRESHOLD,'textual_palindrome_records_ge9':len(candidates),'mirror_render_variants':len(renders),'mirror_pass_variants':len(proof),'quarantine_mirror_pass_variants':len(q),'render_failures':len(fails)}
 (a.out/'scan_manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n');print(json.dumps(m,ensure_ascii=False));return 8 if fails else 0
if __name__=='__main__':raise SystemExit(main())
