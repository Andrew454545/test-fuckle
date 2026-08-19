#!/usr/bin/env python3
from __future__ import annotations
import argparse,csv,hashlib,json,os,platform,re as std_re,subprocess,sys,unicodedata
from collections import Counter
from dataclasses import dataclass,asdict
from pathlib import Path
from typing import Optional
import numpy as np, osmium, regex
from fontTools.ttLib import TTCollection,TTFont
from PIL import Image,ImageDraw,ImageFont,features
try: import icu
except Exception: icu=None
PLACE_TYPES={"city","town","village","hamlet","isolated_dwelling"}
NAME_BASES=("name","official_name","loc_name","alt_name","short_name","int_name","nat_name","reg_name")
IGNORED_CODEPOINTS={0x0027,0x2018,0x2019,0x02BC,0xFF07,0x002E,0xFF0E,0x00B7,0x0387,0x2027,0x30FB}
MIRROR_THRESHOLD=0.001; BORDERLINE_LOW=0.0005; BORDERLINE_HIGH=0.002

def sha256_file(path:Path):
 h=hashlib.sha256();
 with path.open('rb') as f:
  for b in iter(lambda:f.read(8*1024*1024),b''): h.update(b)
 return h.hexdigest()
def eligible_name_key(k): return any(k==b or k.startswith(b+':') for b in NAME_BASES)
def is_ignored_separator(ch): return ord(ch) in IGNORED_CODEPOINTS or unicodedata.category(ch) in {'Zs','Zl','Zp','Pd'}
def strip_locked_separators(t): return ''.join(ch for ch in unicodedata.normalize('NFC',t) if not is_ignored_separator(ch))
def graphemes(t): return regex.findall(r'\X',t)
def folded_graphemes(gs): return [unicodedata.normalize('NFC',g.casefold()) for g in gs]
def is_palindrome(t):
 n=strip_locked_separators(t); gs=graphemes(n); fg=folded_graphemes(gs); return fg==fg[::-1],n,gs
def language_from_key(k):
 for b in NAME_BASES:
  p=b+':'
  if k.startswith(p):
   s=k[len(p):]
   if s and ':' not in s and std_re.fullmatch(r'[A-Za-z]{2,8}(?:[-_][A-Za-z0-9]{1,8})*',s): return s.replace('_','-')
 return 'und'
def split_name_values(k,v): return [x.strip() for x in v.split(';') if x.strip()] if k=='alt_name' or k.startswith('alt_name:') else ([v.strip()] if v.strip() else [])
def get_tag_dict(o): return {t.k:t.v for t in o.tags}
@dataclass
class NameRecord:
 osm_type:str; osm_id:int; place_type:str; tag_key:str; raw_value:str; split_index:int; normalized_name:str; length:int; language:str; addr_country:str; is_in_country_code:str; lat:Optional[float]; lon:Optional[float]
class SettlementHandler(osmium.SimpleHandler):
 def __init__(self,min_len):
  super().__init__(); self.min_len=min_len; self.object_counts=Counter(); self.name_key_counts=Counter(); self.total_name_values=0; self.eligible_long_names=0; self.palindromes=[]; self.benchmark_hits=[]; self.type_id_seen=set(); self.duplicate_object_visits=0
 def _handle(self,o,typ):
  tags=get_tag_dict(o); place=tags.get('place','')
  if place not in PLACE_TYPES:return
  key=(typ,int(o.id)); self.duplicate_object_visits += key in self.type_id_seen; self.type_id_seen.add(key)
  self.object_counts[f'place:{place}']+=1; self.object_counts[f'osm_type:{typ}']+=1; self.object_counts['total']+=1
  lat=lon=None
  if typ=='node':
   try:
    if o.location.valid(): lat,lon=float(o.location.lat),float(o.location.lon)
   except Exception: pass
  for tk,raw in tags.items():
   if not eligible_name_key(tk):continue
   self.name_key_counts[tk]+=1
   for i,v in enumerate(split_name_values(tk,raw)):
    self.total_name_values+=1; ok,n,gs=is_palindrome(v); folded=''.join(folded_graphemes(gs))
    if folded=='owomomowo': self.benchmark_hits.append({'osm_type':typ,'osm_id':int(o.id),'place_type':place,'tag_key':tk,'raw_value':v,'normalized_name':n,'lat':lat,'lon':lon})
    if len(gs)<self.min_len: continue
    self.eligible_long_names+=1
    if ok:self.palindromes.append(NameRecord(typ,int(o.id),place,tk,v,i,n,len(gs),language_from_key(tk),tags.get('addr:country',''),tags.get('is_in:country_code',''),lat,lon))
 def node(self,o):self._handle(o,'node')
 def way(self,o):self._handle(o,'way')
 def relation(self,o):self._handle(o,'relation')
def icu_case_variants(text,lang):
 if icu is None: raise RuntimeError('PyICU required')
 loc=icu.Locale(lang if lang!='und' else ''); vals=[('recorded',text),('uppercase',str(icu.UnicodeString(text).toUpper(loc))),('lowercase',str(icu.UnicodeString(text).toLower(loc)))]
 bi=icu.BreakIterator.createWordInstance(loc); vals.append(('titlecase',str(icu.UnicodeString(text).toTitle(bi,loc))))
 out=[]; seen=set()
 for kind,v in vals:
  v=strip_locked_separators(unicodedata.normalize('NFC',v))
  if v not in seen: seen.add(v); out.append((kind,v))
 return out
def first_strong_direction(text):
 for ch in text:
  b=unicodedata.bidirectional(ch)
  if b in {'R','AL'}:return 'rtl'
  if b=='L':return 'ltr'
 return 'ltr'
@dataclass(frozen=True)
class FontFace: path:str; index:int; family:str; subfamily:str; postscript:str; coverage:frozenset[int]
class NotoFontIndex:
 def __init__(self): self.faces=[]; self._build()
 @staticmethod
 def _name(font,i):
  for n in font['name'].names if 'name' in font else []:
   if n.nameID==i:
    try:return n.toUnicode()
    except:pass
  return ''
 def _accept(self,f,s,p):
  x=f'{f} {s} {p}'.lower()
  if 'noto sans' not in x and 'notosans' not in x:return False
  if any(w in x for w in ('italic','oblique','bold','black','thin','light','medium','semibold','extrabold')):return False
  return 'regular' in x or not s.strip()
 def _add(self,path,idx,font):
  f,s,p=self._name(font,1),self._name(font,2),self._name(font,6)
  if self._accept(f,s,p): self.faces.append(FontFace(path,idx,f,s,p,frozenset((font.getBestCmap() or {}).keys())))
 def _build(self):
  paths=sorted({p.strip() for p in subprocess.check_output(['fc-list','-f','%{file}\n'],text=True).splitlines() if p.strip() and 'noto' in p.lower()})
  for path in paths:
   try:
    if path.lower().endswith(('.ttc','.otc')):
     c=TTCollection(path,lazy=True)
     for i,f in enumerate(c.fonts):self._add(path,i,f)
     c.close()
    elif path.lower().endswith(('.ttf','.otf')):
     f=TTFont(path,lazy=True,fontNumber=0); self._add(path,0,f); f.close()
   except Exception as e: print('FONT_INDEX_WARNING',path,e,file=sys.stderr)
  if not self.faces:raise RuntimeError('No Noto Sans Regular faces found')
 def choose(self,text,lang):
  req={ord(c) for c in text}; cand=[f for f in self.faces if req.issubset(f.coverage)]
  if not cand:raise RuntimeError(f'No Noto Sans Regular face covers {text!r}')
  return min(cand,key=lambda f:(len(f.family),f.family,f.path,f.index))
def render_mask(text,face,lang,size):
 if not features.check('raqm'):raise RuntimeError('Pillow RAQM required')
 font=ImageFont.truetype(face.path,size=size,index=face.index,layout_engine=ImageFont.Layout.RAQM); direction=first_strong_direction(text); language=None if lang=='und' else lang
 probe=Image.new('L',(16,16),0); d=ImageDraw.Draw(probe); bbox=d.textbbox((0,0),text,font=font,direction=direction,language=language); pad=max(32,size//32)
 im=Image.new('L',(max(1,bbox[2]-bbox[0]+2*pad),max(1,bbox[3]-bbox[1]+2*pad)),0); d=ImageDraw.Draw(im); d.text((pad-bbox[0],pad-bbox[1]),text,font=font,fill=255,direction=direction,language=language)
 arr=np.asarray(im,dtype=np.uint8); ys,xs=np.where(arr>0)
 if len(xs)==0:raise RuntimeError('No rendered ink')
 return arr[ys.min():ys.max()+1,xs.min():xs.max()+1]>=128
def shifted(a,dx,dy):
 h,w=a.shape; o=np.zeros_like(a); sx0,sx1=max(0,-dx),min(w,w-dx); sy0,sy1=max(0,-dy),min(h,h-dy); dx0,dx1=max(0,dx),min(w,w+dx); dy0,dy1=max(0,dy),min(h,h+dy)
 if sx1>sx0 and sy1>sy0:o[dy0:dy1,dx0:dx1]=a[sy0:sy1,sx0:sx1]
 return o
def best_mirror_error(mask):
 m=np.fliplr(mask); best=(1.0,0,0,m)
 for dy in (-1,0,1):
  for dx in (-1,0,1):
   x=shifted(m,dx,dy); u=np.logical_or(mask,x); err=0 if not u.sum() else np.logical_xor(mask,x).sum()/u.sum()
   if err<best[0]:best=(float(err),dx,dy,x)
 return best
def save_rendering(mask,mir,out,stem,size):
 out.mkdir(parents=True,exist_ok=True); Image.fromarray((mask*255).astype('uint8')).save(out/f'{stem}-{size}-original.png'); Image.fromarray((mir*255).astype('uint8')).save(out/f'{stem}-{size}-mirrored.png'); Image.fromarray((np.logical_xor(mask,mir)*255).astype('uint8')).save(out/f'{stem}-{size}-diff.png')
def mirror_test(text,face,lang,out,stem):
 mask=render_mask(text,face,lang,2048); err,dx,dy,mir=best_mirror_error(mask); save_rendering(mask,mir,out,stem,2048); res={'size':2048,'error':err,'dx':dx,'dy':dy,'height':mask.shape[0],'width':mask.shape[1]}
 if BORDERLINE_LOW<=err<=BORDERLINE_HIGH:
  mask=render_mask(text,face,lang,4096); err,dx,dy,mir=best_mirror_error(mask); save_rendering(mask,mir,out,stem,4096); res={'size':4096,'error':err,'dx':dx,'dy':dy,'height':mask.shape[0],'width':mask.shape[1]}
 res['pass']=res['error']<=MIRROR_THRESHOLD; return res
def write_csv(path,rows,fields):
 with path.open('w',newline='',encoding='utf-8') as f:w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
def main():
 p=argparse.ArgumentParser();p.add_argument('pbf',type=Path);p.add_argument('--out',type=Path,default=Path('artifacts'));p.add_argument('--min-len',type=int,default=9);a=p.parse_args();a.out.mkdir(parents=True,exist_ok=True)
 if icu is None or not features.check('raqm'):raise RuntimeError('Required ICU/RAQM unavailable')
 h=SettlementHandler(a.min_len);h.apply_file(str(a.pbf),locations=False)
 fields=list(asdict(NameRecord('',0,'','','',0,'',0,'','','',None,None)).keys()); pals=[asdict(r) for r in h.palindromes];pals.sort(key=lambda r:(-r['length'],r['normalized_name'],r['osm_type'],r['osm_id']));write_csv(a.out/'all_palindromes_ge9.csv',pals,fields)
 fonts=NotoFontIndex(); rows=[];fails=[];rdir=a.out/'renderings'
 for rec in h.palindromes:
  for kind,text in icu_case_variants(rec.normalized_name,rec.language):
   row=asdict(rec);row.update({'case_variant':kind,'render_text':text})
   try:
    face=fonts.choose(text,rec.language); stem=f'{rec.osm_type}-{rec.osm_id}-'+hashlib.sha256((rec.tag_key+'|'+rec.raw_value+'|'+kind).encode()).hexdigest()[:16];res=mirror_test(text,face,rec.language,rdir,stem);row.update({'font_family':face.family,'font_path':face.path,'font_index':face.index,'render_size':res['size'],'mirror_error':res['error'],'align_dx':res['dx'],'align_dy':res['dy'],'render_width':res['width'],'render_height':res['height'],'mirror_pass':res['pass'],'render_stem':stem});rows.append(row)
   except Exception as e:row['error']=f'{type(e).__name__}: {e}';fails.append(row)
 allfields=sorted({k for r in rows for k in r}) if rows else fields+['case_variant','render_text','mirror_pass'];write_csv(a.out/'mirror_results.csv',rows,allfields); winners=[r for r in rows if r.get('mirror_pass')];write_csv(a.out/'results.csv',winners,allfields)
 if fails:write_csv(a.out/'render_failures.csv',fails,sorted({k for r in fails for k in r}))
 manifest={'locked_rules_version':1,'min_length':a.min_len,'place_types':sorted(PLACE_TYPES),'name_bases':list(NAME_BASES),'pbf_sha256':sha256_file(a.pbf),'object_counts':dict(h.object_counts),'unique_object_ids':len(h.type_id_seen),'duplicate_object_visits':h.duplicate_object_visits,'name_key_counts':dict(h.name_key_counts),'total_name_values':h.total_name_values,'names_length_ge_min':h.eligible_long_names,'palindromes_ge_min':len(h.palindromes),'mirror_render_variants':len(rows),'mirror_pass_variants':len(winners),'render_failures':len(fails),'benchmark_owomomowo_hits':h.benchmark_hits,'source_url':os.environ.get('SOURCE_URL',''),'source_sha256':os.environ.get('SOURCE_SHA256',''),'source_last_modified':os.environ.get('SOURCE_LAST_MODIFIED',''),'source_etag':os.environ.get('SOURCE_ETAG',''),'software':{'python':sys.version,'unicode':unicodedata.unidata_version,'platform':platform.platform(),'raqm':bool(features.check('raqm')),'icu':getattr(icu,'ICU_VERSION','missing')}}; (a.out/'scan_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({'objects':h.object_counts.get('total',0),'name_values':h.total_name_values,'palindromes_ge9':len(h.palindromes),'mirror_pass_variants':len(winners),'render_failures':len(fails),'owomomowo_hits':len(h.benchmark_hits)}))
 return 4 if h.duplicate_object_visits else (5 if fails else 0)
if __name__=='__main__':raise SystemExit(main())
