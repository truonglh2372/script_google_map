import sys,re,time,json
sys.path.insert(0,'/workspace'); sys.path.insert(0,'/workspace/menuocr')
from aqd_api import AQD, API
import menu_ocr as M
a=AQD(); dry='--apply' not in sys.argv
ids=[int(x) for x in sys.argv[1:] if x.isdigit()]
UNIT=r"(đĩa|dĩa|chén|tô|thố|phần|suất|ly|lon|chai|con|kg|cái|cây|bát)"
for rid in ids:
    ds=a.dishes(rid); names_have={d['name'].lower() for d in ds}
    for d in ds:
        if d['available'] is False: continue
        n=d['name']; k=d['kind']; pr=d['price_vnd'] or None
        if re.fullmatch(UNIT,n.strip(),re.I) or not M.refine(n) or n.strip().lower() in ('lắc','mai','nắng'):
            print(rid,'HIDE junk',n); 
            if not dry: a.update_dish(rid,int(d['id']),{'available':False}); time.sleep(1)
            continue
        if pr and not M.price_ok(k,int(pr),d.get('description') or ''):
            print(rid,'HIDE price',n,pr,k)
            if not dry: a.update_dish(rid,int(d['id']),{'available':False}); time.sleep(1)
            continue
        parts=M.refine(n)
        if parts[-1]!=n:
            print(rid,'RENAME',n,'->',parts[-1])
            if not dry: a.update_dish(rid,int(d['id']),{'name':parts[-1]}); time.sleep(1)
        for ex in parts[:-1]:
            if ex.lower() in names_have: continue
            names_have.add(ex.lower()); ke=k
            print(rid,'ADD split',ex,ke,'cat',d['category_name'])
            if not dry:
                body={'name':ex,'kind':ke,'category_id':d['category_id'],'price_vnd':None,'price_on_request':True,'available':True}
                print('  ->',str(a.call('POST',f'{API}/{rid}/dishes',json_body=body))[:120]); time.sleep(1)
