#!/usr/bin/env python3
# поток: card
"""Fresh read-only check of both TN VED fields across both active WB catalogs."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import requests
from tools.card_wb_tnved_remaining import page
from tools.card_wb_tnved_rule_recheck import char

OUT=ROOT/'docs/reports/wb_tnved_final_catalog_audit_2026-10-05'
FIELDS=(15000001,15004139)

def now():return datetime.now(timezone.utc).isoformat()

def save(name,data):
    p=OUT/name;p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(json.dumps(data,ensure_ascii=False)+'\n')

def account_scan(account):
    start=now();cursor={'limit':100};seen_cursors=set();indexed={};observations=0
    with requests.Session() as session:
        for index in range(1,1001):
            data=page(session,account,cursor)
            save(f'raw/{account}_{index:04d}.json',{'read_at':now(),'response':data})
            for card in data['cards']:
                first,second=[char(card,field).strip() for field in FIELDS]
                status='BOTH_FILLED' if first and second else 'BOTH_EMPTY' if not first and not second else 'ONE_EMPTY'
                indexed[card['nmID']]={'account':account,'nmID':card['nmID'],'vendorCode':card['vendorCode'],
                    'title':card.get('title',''),'subjectID':card['subjectID'],'subjectName':card.get('subjectName',''),
                    'tnved_15000001':first,'tnved_15004139':second,'status':status,
                    'held_by_owner':account=='wb_acc2' and card['vendorCode']=='4434UU1ZP70G',
                    'values_differ':bool(first and second and first!=second),'updatedAt':card.get('updatedAt','')}
                observations+=1
            if index%25==0:print(now(),account,'pages',index,'unique',len(indexed),flush=True)
            if len(data['cards'])<100:
                result={'account':account,'read_started_at':start,'read_finished_at':now(),
                    'active_cards':len(indexed),'pages':index,'complete':True,
                    'repeated_observations':observations-len(indexed),'counts':dict(Counter(r['status'] for r in indexed.values())),
                    'both_filled_different_values':sum(r['values_differ'] for r in indexed.values())}
                save(f'{account}_summary.json',result)
                return result,list(indexed.values())
            cur=data.get('cursor',{});marker=(cur.get('updatedAt'),cur.get('nmID'))
            if not all(marker) or marker in seen_cursors:raise RuntimeError('Pagination failed to advance')
            seen_cursors.add(marker);cursor={'limit':100,'updatedAt':marker[0],'nmID':marker[1]}
    raise RuntimeError('Catalog scan was not exhausted')

def csv_save(name,rows,cols):
    with (OUT/name).open('w',encoding='utf-8-sig',newline='') as h:
        w=csv.DictWriter(h,fieldnames=cols,delimiter=';');w.writeheader();w.writerows(rows)

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        tasks=[pool.submit(account_scan,a) for a in ('wb_acc1','wb_acc2')]
        results=[task.result() for task in tasks]
    rows=sorted([r for _,values in results for r in values],key=lambda r:(r['account'],r['nmID']))
    cols=list(rows[0]);csv_save('all_active_cards.csv',rows,cols)
    missing=[r for r in rows if r['status']!='BOTH_FILLED']
    csv_save('missing_fields.csv',missing,cols)
    csv_save('different_values.csv',[r for r in rows if r['values_differ']],cols)
    summary={'built_at':now(),'complete':True,'accounts':[r for r,_ in results],
        'total_active':len(rows),'counts':dict(Counter(r['status'] for r in rows)),
        'missing_held_by_owner':sum(r['held_by_owner'] for r in missing),
        'missing_not_held':sum(not r['held_by_owner'] for r in missing),
        'external_writes':0,'db_writes':0}
    save('summary.json',summary);save('missing_examples.json',missing[:10])
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
