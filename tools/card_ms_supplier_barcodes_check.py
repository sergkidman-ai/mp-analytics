#!/usr/bin/env python3
# поток: card
"""Read-only live MS barcode check against supplier codes already audited."""
import json
import csv
import sys
from pathlib import Path
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timezone

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from core import ms_api
from tools.card_supplier_barcode_receipt_audit import valid_gtin

SOURCE=ROOT/'docs/reports/card_supplier_barcode_receipt_audit_2026-10-13'
OUT=ROOT/'docs/reports/card_ms_supplier_barcodes_check_2026-10-13'

def save(name,data):
    (OUT/name).write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')

def main():
    OUT.mkdir(exist_ok=True)
    docs=json.loads((SOURCE/'current_docs.json').read_text())
    baseline=json.loads((SOURCE/'review.json').read_text())
    def read_doc(d):
        r=ms_api.get('/entity/enter/'+d['id']+'/positions',{'limit':1000,'expand':'assortment'})
        assert r['meta']['size']<=1000
        return {'id':d['id'],'name':d['name'],'supplier':d['source_key'],'positions':r['rows']}
    with ThreadPoolExecutor(max_workers=3) as pool:positions=list(pool.map(read_doc,docs))
    save('live_positions_expanded.json',positions)
    live={(d['supplier'],p['assortment']['id']):p['assortment'] for d in positions for p in d['positions']}
    rows=[];delta=[]
    for r in baseline:
        c=live.get((r['supplier'],r['id']))
        row={**r,'live_barcodes':c.get('barcodes',[]) if c else [],'status':'','missing_supplier_ean13':[]}
        if not c or c.get('archived'):row['status']='NO_LONGER_ACTIVE_IN_RECEIPT'
        elif c.get('article')!=r['article'] or c.get('externalCode')!=r['external_code']:row['status']='IDENTITY_CHANGED'
        else:
            current={str(v) for b in c.get('barcodes',[]) for v in b.values()}
            e13={str(b['ean13']) for b in c.get('barcodes',[]) if b.get('ean13')}
            missing=[x for x in r['valid_ean13'] if x not in e13]
            row['missing_supplier_ean13']=missing
            if not r['valid_ean13']:row['status']='NO_SUPPLIER_EAN13_AVAILABLE'
            elif not missing:row['status']='SUPPLIER_EAN13_ALREADY_PRESENT'
            elif any(x in current for x in missing):row['status']='SAME_CODE_OTHER_TYPE_REVIEW'
            elif e13:row['status']='OTHER_EAN13_PRESENT_REVIEW'
            else:
                row['status']='SUPPLIER_EAN13_MISSING'
                # Preview only: preserve every original barcode and original order.
                delta.append({'id':r['id'],'supplier':r['supplier'],'external_code':r['external_code'],'article':r['article'],
                              'old_barcodes':c.get('barcodes',[]),
                              'new_barcodes':c.get('barcodes',[])+[{'ean13':x} for x in missing]})
        rows.append(row)
    save('review.json',rows);save('preview_additions.json',delta)
    summary={'at_utc':datetime.now(timezone.utc).isoformat(),'live_receipt_documents':len(docs),'scoped_cards':len(rows),
             'statuses':dict(Counter(r['status'] for r in rows)),
             'by_supplier':{s:dict(Counter(r['status'] for r in rows if r['supplier']==s)) for s in sorted({r['supplier'] for r in rows})},
             'ms_cards_with_ean8':sum(any('ean8' in b for b in r['live_barcodes']) for r in rows),
             'ms_cards_with_code128':sum(any('code128' in b for b in r['live_barcodes']) for r in rows),
             'ms_cards_with_ean13':sum(any('ean13' in b for b in r['live_barcodes']) for r in rows),
             'existing_ean13_invalid':sum(not valid_gtin(str(b['ean13'])) for r in rows for b in r['live_barcodes'] if b.get('ean13')),
             'new_receipt_cards_outside_previous_scope':len({k for k,c in live.items() if not c.get('archived')}-{(r['supplier'],r['id']) for r in baseline}),
             'archived_receipt_cards_excluded':sum(bool(c.get('archived')) for c in live.values()),
             'preview_additions':len(delta),'external_writes':0,'database_writes':0}
    assert all(r['new_barcodes'][:len(r['old_barcodes'])]==r['old_barcodes'] for r in delta)
    save('summary.json',summary)
    with (OUT/'preview_additions.csv').open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(delta[0]),delimiter=';');w.writeheader();w.writerows(delta)
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
