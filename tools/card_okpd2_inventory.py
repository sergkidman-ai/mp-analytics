#!/usr/bin/env python3
# поток: card
"""Read-only OKPD2 inventory from saved WB snapshots and TC SELECTs."""
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.db import query

OUT = ROOT / 'docs/reports/wb_okpd2_inventory_2026-10-05'
FIELD = 15004292

def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str) + '\n')

def csv_save(name, rows, columns):
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as h:
        writer = csv.DictWriter(h, fieldnames=columns, delimiter=';')
        writer.writeheader(); writer.writerows(rows)

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    models = query("""SELECT external_code,title,consumable_type,ved_code,last_seen,
        raw->>'assignment' AS assignment,raw->>'originality' AS originality,
        raw->>'ink_type' AS ink_type,raw->>'printer_ribbon_types' AS printer_ribbon_types
        FROM prc_tc_model WHERE gone_at IS NULL ORDER BY external_code""")
    save('tc_models.json', models)
    save('tc_export_keys.json', query("SELECT DISTINCT k FROM prc_tc_model CROSS JOIN LATERAL jsonb_object_keys(raw) k WHERE gone_at IS NULL ORDER BY k"))
    tc = {r['external_code']: r for r in models}
    linked = {}
    path = ROOT / 'docs/reports/wb_tnved_dry_run_2026-10-05/plan.csv'
    for row in csv.DictReader(path.open(encoding='utf-8-sig'), delimiter=';'):
        linked[(row['account'], int(row['Артикул ВБ']))] = row['Код основания в ТК']
    inventory = []; sources = []; retired = set()
    trash_source = ROOT / 'docs/reports/wb_tnved_dry_run_2026-10-05/no_source.csv'
    for row in csv.DictReader(trash_source.open(encoding='utf-8-sig'), delimiter=';'):
        retired.add((row['account'], int(row['Артикул ВБ'])))
    for account in ('wb_acc1', 'wb_acc2'):
        source = ROOT / f'backups/wb_bundle_plan_2026-10-04/{account}_active.json'
        raw = json.loads(source.read_text())
        indexed = {int(c['nmID']): c for c in raw['cards']}
        sources.append({'account': account, 'path': str(source.relative_to(ROOT)), 'read_at': raw.get('read_at'), 'cards': len(indexed)})
        del raw
        overlays = 0
        for report in ('wb_tnved_apply_203_2026-10-05', 'wb_tnved_remaining_2026-10-05'):
            for file in sorted((ROOT / f'docs/reports/{report}/after').glob('*.json')):
                if not file.name.startswith(account):
                    continue
                data = json.loads(file.read_text())
                cards = data if isinstance(data, list) else [data]
                for c in cards:
                    if 'nmID' in c: indexed[int(c['nmID'])] = c; overlays += 1
        for sid in (1820, 1907):
            schema = json.loads((ROOT / f'docs/reports/wb_tnved_dry_run_2026-10-05/wb_charcs_{sid}.json').read_text())
            save(f'wb_field_{sid}.json', {'read_at': schema['read_at'], 'field': [c for c in schema['response']['data'] if c['charcID'] == FIELD]})
        for nm, card in indexed.items():
            if (account, nm) in retired: continue
            vendor = card['vendorCode']
            base = linked.get((account, nm))
            method = 'saved_exact_nm_mapping' if base else ''
            if not base and vendor in tc: base = vendor; method = 'exact_TC_external_code'
            model = tc.get(base, {})
            values = next((c.get('value') for c in card.get('characteristics', []) if c['id'] == FIELD), None)
            code = '; '.join(map(str, values)) if isinstance(values, list) else str(values) if values else ''
            inventory.append({'account': account, 'nmID': nm, 'vendorCode': vendor,
                'title': card.get('title', ''), 'subjectID': card.get('subjectID'), 'subjectName': card.get('subjectName', ''),
                'okpd2': code, 'status': 'FILLED' if code else 'EMPTY', 'tc_base': base or '',
                'tc_type': model.get('consumable_type', ''), 'tc_title': model.get('title', ''),
                'tc_assignment': model.get('assignment', ''), 'mapping': method if model else 'UNRESOLVED',
                'wb_updatedAt': card.get('updatedAt', '')})
        sources[-1]['fresh_overlay_observations'] = overlays
        del indexed
    columns = list(inventory[0])
    csv_save('wb_cards.csv', inventory, columns)
    csv_save('wb_existing_codes.csv', [r for r in inventory if r['okpd2']], columns)
    csv_save('wb_empty_codes.csv', [r for r in inventory if not r['okpd2']], columns)
    csv_save('wb_unresolved_tc.csv', [r for r in inventory if r['mapping'] == 'UNRESOLVED'], columns)
    counts = Counter((r['account'], r['subjectID'], r['tc_type'] or 'UNRESOLVED', r['okpd2'] or 'EMPTY') for r in inventory)
    grouped = [{'account': a, 'subjectID': s, 'tc_type': t, 'okpd2': c, 'count': n} for (a,s,t,c), n in sorted(counts.items())]
    csv_save('wb_code_distribution.csv', grouped, ['account','subjectID','tc_type','okpd2','count'])
    type_counts = Counter(r['consumable_type'] for r in models)
    csv_save('tc_types.csv', [{'tc_type':t,'count':n} for t,n in type_counts.items()], ['tc_type','count'])
    groups = defaultdict(list)
    for model in models:
        groups[(model['consumable_type'], model['assignment'])].append(model)
    drafts = []
    for (typ, assignment), items in sorted(groups.items()):
        candidate = ''; confidence = 'REVIEW'; reason = 'Нужна отдельная классификация товара'
        if typ in ('Чернила', 'Комплект чернил'):
            candidate = '20.59.30.190'; confidence = 'DRAFT'; reason = 'Чернила прочие; подтвердить применение к данным товарам'
        elif typ in ('Картридж', 'Тонер-картридж', 'Комплект картриджей') and assignment == 'для лазерного принтера':
            candidate = '26.20.40.120'; confidence = 'DRAFT'; reason = 'Ориентир из письма Минпромторга о лазерных картриджах; комплект и конкретную модель проверить'
        elif typ == 'Тонер':
            candidate = '20.59.12.120'; confidence = 'DRAFT'; reason = 'Группа химических составов; классификацию порошка сверить с документами изготовителя'
        elif typ == 'Заправочный комплект':
            reason = 'Нужно проверить состав набора: тонер, чернила, инструменты, чипы'
        elif typ == 'Лента для принтера':
            reason = 'Разделить красящие ленты, термотрансферные материалы и ленты для маркировки'
        elif typ in ('Фотобарабан', 'Комплект фотобарабанов', 'Блок проявки', 'Печатающая головка', 'Бункер отработанного тонера', 'Блок переноса'):
            reason = 'Проверить варианты 26.20.40.190 / 28.23.25.000 по назначению и документам; код не выбран'
        drafts.append({'tc_type':typ,'assignment':assignment,'tc_models':len(items),'candidate_okpd2':candidate,
                       'status':confidence,'reason':reason,'example_bases':', '.join(r['external_code'] for r in items[:5])})
    csv_save('tc_mapping_draft.csv', drafts, ['tc_type','assignment','tc_models','candidate_okpd2','status','reason','example_bases'])
    summary = {'built_at':datetime.now(timezone.utc).isoformat(),'scope':'saved WB catalogs, excluding retired 23; fresh available after snapshots overlay',
        'sources':sources,'wb_total':len(inventory),'wb_filled':sum(bool(r['okpd2']) for r in inventory),
        'wb_empty':sum(not r['okpd2'] for r in inventory), 'wb_codes':dict(Counter(r['okpd2'] for r in inventory if r['okpd2'])),
        'wb_tc_mapping_unresolved':sum(r['mapping']=='UNRESOLVED' for r in inventory),
        'tc_active_models':len(models),'tc_type_counts':dict(type_counts),'tc_export_has_okpd2':False,
        'wb_field':FIELD,'external_writes':0,'db_writes':0}
    save('summary.json', summary)
    print(json.dumps({k:v for k,v in summary.items() if k != 'sources'},ensure_ascii=False,default=str))

if __name__ == '__main__': main()
