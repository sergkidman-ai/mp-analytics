#!/usr/bin/env python3
# поток: card
"""Read-only EAN13 presence in current receipts of four additional suppliers."""
import json,csv,sys
from pathlib import Path
from datetime import datetime,timezone
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core import ms_api
from prices import loader
from prices.profiles import get_identity
from tools.card_supplier_barcode_receipt_audit import valid_gtin
OUT=ROOT/'docs/reports/card_ms_ean13_other_suppliers_2026-10-13'
KEYS={'odissey':'Одиссей','sakura':'Сакура','kaktus_msk':'Кактус','easy_print':'Изипринт'}

def main():
    OUT.mkdir(exist_ok=True)
    docs=[]
    for key in KEYS:
        found=loader.existing_docs(get_identity(key))
        if found:
            prefix=max(d['name'].rsplit('_p',1)[0] for d in found)
            docs.extend({**d,'supplier_key':key} for d in found if d['name'].startswith(prefix+'_p'))
    def read(d):
        r=ms_api.get('/entity/enter/'+d['id']+'/positions',{'limit':1000,'expand':'assortment'});assert r['meta']['size']<=1000
        return {'id':d['id'],'name':d['name'],'supplier_key':d['supplier_key'],'positions':r['rows']}
    with ThreadPoolExecutor(max_workers=3) as pool:positions=list(pool.map(read,docs))
    (OUT/'current_positions_expanded.json').write_text(json.dumps(positions,ensure_ascii=False,indent=2)+'\n')
    summary={};review=[]
    for key,title in KEYS.items():
        cards={p['assortment']['id']:p['assortment'] for d in positions if d['supplier_key']==key for p in d['positions']}
        selected=[c for c in cards.values() if not c.get('archived')]
        rows=[]
        for c in selected:
            ean=[str(b['ean13']) for b in c.get('barcodes',[]) if b.get('ean13')]
            rows.append({'supplier':title,'id':c['id'],'external_code':c.get('externalCode'),'article':c.get('article'),
                         'name':c['name'],'ean13':ean,'valid_ean13':[v for v in ean if len(v)==13 and valid_gtin(v)],'all_barcodes':c.get('barcodes',[])})
        review.extend(rows)
        summary[title]={'current_receipt_documents':sum(d['supplier_key']==key for d in positions),'active_cards':len(rows),
                        'with_ean13':sum(bool(r['ean13']) for r in rows),'with_valid_ean13':sum(bool(r['valid_ean13']) for r in rows),
                        'without_ean13':sum(not r['ean13'] for r in rows),'archived_excluded':sum(bool(c.get('archived')) for c in cards.values())}
    (OUT/'review.json').write_text(json.dumps(review,ensure_ascii=False,indent=2)+'\n')
    (OUT/'summary.json').write_text(json.dumps({'at_utc':datetime.now(timezone.utc).isoformat(),'suppliers':summary,'external_writes':0},ensure_ascii=False,indent=2)+'\n')
    with (OUT/'all_cards.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(review[0]),delimiter=';');w.writeheader();w.writerows(review)
    print(json.dumps(summary,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
