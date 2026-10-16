#!/usr/bin/env python3
# поток: card
"""Transcribe owner-provided TC type reference; no external/DB mutations."""
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'docs/reports/wb_okpd2_inventory_2026-10-05'
PHOTO = ROOT / 'dropbox/20261005_140535_Natalie_Pro_natalya_photo.jpg'
# Transcribed visually from the rightmost Окпд2 column, preserving TC type IDs.
REFERENCE = [
    (1, 'Картридж', '26.20.40.190'),
    (2, 'Бункер отработанного тонера', '26.20.40.190'),
    (3, 'Девелопер', '26.20.40.190'),
    (4, 'Комплект картриджей', '26.20.40.190'),
    (5, 'Краска полиграфическая', '20.59.30.190'),
    (6, 'Лента для принтера', '26.20.40.190'),
    (7, 'Печатающая головка', '26.20.40.190'),
    (8, 'СНПЧ', '26.20.40.190'),
    (9, 'Твердые чернила', '26.20.40.190'),
    (10, 'Тонер', '28.23.25.000'),
    (11, 'Тонер-картридж', '26.20.40.190'),
    (12, 'Фотобарабан', '26.20.40.190'),
    (13, 'Чернила', '20.59.30.190'),
    (14, 'Фьюзерное масло', ''),
    (15, 'Заправочный комплект', '28.23.25.000'),
    (17, 'Блок проявки', '26.20.40.190'),
    (19, 'Сервисный комплект', '26.20.40.190'),
    (20, 'Комплект чернил', '20.59.30.190'),
    (21, 'Комплект фотобарабанов', '26.20.40.190'),
    (22, 'Блок переноса', '26.20.40.190'),
    (23, 'Фотобарабан + картридж', '26.20.40.190'),
]

def csv_save(name, rows, columns):
    with (OUT / name).open('w',encoding='utf-8-sig',newline='') as h:
        w=csv.DictWriter(h,fieldnames=columns,delimiter=';'); w.writeheader(); w.writerows(rows)

def main():
    models=json.loads((OUT/'tc_models.json').read_text())
    by_type={name:(ident,code) for ident,name,code in REFERENCE}
    assert len(by_type)==len(REFERENCE)==21
    counts=Counter(m['consumable_type'] for m in models)
    reference=[{'tc_type_id':ident,'tc_type':typ,'okpd2':code,'active_models':counts[typ],
                'source':'TC reference screenshot 20261005_140535','status':'SOURCE_FILLED' if code else 'SOURCE_EMPTY'}
               for ident,typ,code in REFERENCE]
    csv_save('tc_type_reference.csv',reference,list(reference[0]))
    result=[]
    for model in models:
        ident,code=by_type.get(model['consumable_type'],('', ''))
        result.append({'tc_base':model['external_code'],'tc_title':model['title'],
                       'tc_type':model['consumable_type'],'tc_type_id':ident,'okpd2':code,
                       'status':'SOURCE_MATCH' if code else 'REVIEW_NO_CODE'})
    assert len(result)==6735 and all(r['status']=='SOURCE_MATCH' for r in result)
    csv_save('tc_models_with_okpd2.csv',result,list(result[0]))
    assigned={r['tc_base']:r for r in result}
    wb=[]
    for row in csv.DictReader((OUT/'wb_cards.csv').open(encoding='utf-8-sig'),delimiter=';'):
        source=assigned.get(row['tc_base']) if row['mapping']!='UNRESOLVED' else None
        wb.append({'account':row['account'],'nmID':row['nmID'],'vendorCode':row['vendorCode'],
                   'tc_base':row['tc_base'],'tc_type':row['tc_type'],'existing_okpd2':row['okpd2'],
                   'source_okpd2':source['okpd2'] if source else '',
                   'status':'SOURCE_MATCH' if source else 'REVIEW_TC_LINK'})
    csv_save('wb_codes_from_tc_reference.csv',wb,list(wb[0]))
    summary={'at':datetime.now(timezone.utc).isoformat(),'source':str(PHOTO.relative_to(ROOT)),
             'source_sha256':hashlib.sha256(PHOTO.read_bytes()).hexdigest(),
             'reference_types':len(REFERENCE),'active_types':len(counts),
             'tc_models_matched':len(result),'tc_codes':dict(Counter(r['okpd2'] for r in result)),
             'wb_source_matched':sum(r['status']=='SOURCE_MATCH' for r in wb),
             'wb_tc_link_review':sum(r['status']=='REVIEW_TC_LINK' for r in wb),
             'source_empty_types':[r['tc_type'] for r in reference if not r['okpd2']],
             'external_writes':0,'db_writes':0,'supersedes':'tc_mapping_draft.csv'}
    (OUT/'tc_reference_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__':main()
