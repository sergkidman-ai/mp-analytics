# поток: card
"""Read-only weight audit: original supplier cells versus saved MS cards."""
import csv
import hashlib
import gzip
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.card_dimensions_source_inventory import sparse_xlsx
from prices import blacklist

OUT = ROOT / 'docs/reports/card_supplier_weight_audit_2026-10-15'
AUDIT = ROOT / 'docs/reports/card_all_supplier_dimensions_2026-10-15'
GROUPS = ['Кактус', 'Сакура', 'Изипринт', 'Рапид / Суперфайн']


def number(v):
    try:
        d = Decimal(str(v).strip().replace(',', '.'))
        return Decimal(format(d, '.15g')) if d.is_finite() and d > 0 else None
    except InvalidOperation:
        return None


def key(v):
    return str(v or '').strip().upper()


def fmt(v):
    return format(v.normalize(), 'f') if v is not None else ''


def save(name, data):
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')


def loadcsv(path):
    with path.open(encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def csvout(name, rows):
    if not rows:
        return
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def sources():
    result, manifest = [], []
    for supplier, filename in [('Кактус', 'Вся расходка Китай РФ Cactus GG PR.xlsx'),
                               ('Сакура', 'САКУРА АБДУЛ 05.06.xlsx'), ('Изипринт', 'Изи.xlsx')]:
        path = ROOT / 'incoming/gab' / filename
        manifest.append({'supplier': supplier, 'file': str(path.relative_to(ROOT)),
                         'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                         'freshness': 'original archive; supplier publication date not confirmed'})
        headers = {}
        for sheet, rn, cells in sparse_xlsx(path):
            if rn == 1:
                headers[sheet] = cells
                continue
            if supplier == 'Кактус':
                ac, wc, nc, units, packaging = 'D', 'I', 'F', 'кг', 'explicit_unit_gross'
            elif supplier == 'Сакура':
                mappings = {'Лазерная Sakura': ('E', 'AD', 'I', 'кг', 'gross_net_unspecified'),
                            'Струйная Sakura': ('A', 'Y', 'H', 'кг', 'explicit_individual_box'),
                            'Матричная Sakura': ('A', 'S', 'F', '', 'explicit_box_unit_unspecified'),
                            'Наклейки SOK': ('A', 'Z', 'F', '', 'explicit_box_unit_unspecified')}
                if sheet not in mappings:
                    continue
                ac, wc, nc, units, packaging = mappings[sheet]
            elif sheet == 'Данные по картриджам':
                ac, wc, nc, units, packaging = 'A', 'J', 'K', '', 'gross_net_unspecified'
            elif sheet == 'Чернила':
                ac, wc, nc, units, packaging = 'B', 'I', 'K', '', 'gross_net_unspecified'
            else:
                continue
            if not cells.get(ac):
                continue
            raw = cells.get(wc, '')
            result.append({'supplier': supplier, 'article': cells[ac], 'name': cells.get(nc, ''),
                           'file': str(path.relative_to(ROOT)), 'sheet': sheet, 'row': rn,
                           'field': wc + ': ' + headers.get(sheet, {}).get(wc, ''), 'raw_weight': raw,
                           'positive_numeric': number(raw) is not None, 'weight_numeric': fmt(number(raw)),
                           'units': units, 'packaging': packaging})
    path = OUT / 'rapid_live.json.gz'
    data = json.loads(gzip.decompress(path.read_bytes()))
    manifest.append({'supplier': GROUPS[-1], 'file': str(path.relative_to(ROOT)),
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'supplier_date': data.get('date'),
                     'units': 'Weight has no units; supplied API instructions have no field definition'})
    for idx, obj in enumerate(data['price']):
        raw = obj.get('Weight', '')
        result.append({'supplier': GROUPS[-1], 'article': obj.get('CodeID', ''), 'name': obj.get('Name', ''),
                       'file': str(path.relative_to(ROOT)), 'sheet': 'price', 'row': idx,
                       'field': 'Weight; ID=' + str(obj.get('ID')), 'raw_weight': raw,
                       'positive_numeric': number(raw) is not None, 'weight_numeric': fmt(number(raw)),
                       'units': '', 'packaging': 'gross_net_unspecified'})
    save('source_manifest.json', manifest)
    csvout('original_weight_cells.csv', result)
    return result


def main():
    OUT.mkdir(exist_ok=True)
    policy_path = OUT / 'all_suppliers_weight_policy_user_2026-10-15.json'
    accept_gross_or_net = policy_path.exists() and json.loads(policy_path.read_text()).get('accept_gross_or_net') is True
    units_path = OUT / 'weight_units_user_confirmed_2026-10-15.json'
    unit_policy = json.loads(units_path.read_text()) if units_path.exists() else {}
    kg_suppliers = set(unit_policy.get('confirmed_suppliers', [])) if unit_policy.get('weight_units') == 'кг' else set()
    src = sources()
    index = defaultdict(list)
    for s in src:
        index[(s['supplier'], key(s['article']))].append(s)
    cards = {r['ms_id']: r for r in loadcsv(AUDIT / 'products_review.csv') if r['group'] in GROUPS}
    products = {p['id']: p for p in json.loads((AUDIT / 'products.json').read_text()) if p['id'] in cards}
    prepared = json.loads((ROOT / 'docs/reports/card_dimensions_own_source_plan_2026-10-15/candidate_fill.json').read_text())
    plan_ids = {r['ms_id'] for r in prepared}
    for p in (ROOT / 'docs/reports/card_dimensions_own_source_night_2026-10-15/preflight').glob('*.json'):
        live = json.loads(p.read_text())
        if live['id'] in products:
            products[live['id']] = live
    live_path = OUT / 'ms_live_1380.json.gz'
    live_collection = None
    if live_path.exists():
        live_collection = json.loads((OUT / 'ms_live_collection.json').read_text())
        for live in json.loads(gzip.decompress(live_path.read_bytes())):
            old = products[live['id']]
            if any(live.get(k) != old.get(k) for k in ('article', 'externalCode', 'supplier', 'archived')):
                raise RuntimeError('Frozen card identity changed; audit scope before comparison')
            products[live['id']] = live
    rapid_written = {r['id'] for r in json.loads((ROOT / 'docs/reports/card_rapid_dimensions_night_2026-10-14/plan.json').read_text())}
    own_dim_review = {r['ms_id']: r for r in loadcsv(ROOT / 'docs/reports/card_dimensions_own_source_plan_2026-10-15/review.csv')}
    black = blacklist.load_set()
    rows = []
    for pid, c in cards.items():
        p = products[pid]
        found = index.get((c['group'], key(c['article'])), [])
        good = [s for s in found if s['positive_numeric']]
        weights = {Decimal(s['weight_numeric']) for s in good}
        one = len(weights) == 1
        target = next(iter(weights)) if one else None
        old = number(p.get('weight'))
        units = {s['units'] for s in good}
        confirmed_kg = one and (units == {'кг'} or c['group'] in kg_suppliers)
        pack_confirmed = bool(good) and all(s['packaging'] in ('explicit_unit_gross', 'explicit_individual_box') for s in good)
        if not found:
            comparison = 'NO_ARTICLE_MATCH'
        elif not good:
            comparison = 'SOURCE_WEIGHT_MISSING_OR_INVALID'
        elif not one:
            comparison = 'AMBIGUOUS_SOURCE_WEIGHT'
        elif old is None:
            comparison = 'MS_WEIGHT_EMPTY'
        elif old == target:
            comparison = 'NUMERIC_EQUAL'
        else:
            comparison = 'NUMERIC_DIFFERENT'
        holds = []
        if not confirmed_kg:
            holds.append('SOURCE_UNITS_UNCONFIRMED' if one else comparison)
        if one and not pack_confirmed and not accept_gross_or_net:
            holds.append('GROSS_PACKAGE_WEIGHT_UNCONFIRMED')
        if blacklist.norm(c['article']) in black:
            holds.append('BLACKLIST')
        if re.search(r'\bбрак\b', c['name'], re.I) or blacklist.in_damaged(c['name']):
            holds.append('DAMAGED')
        if any('стоп' in a.get('name', '').lower() and str(a.get('value')).lower() in ('true','да','1') for a in p.get('attributes', [])):
            holds.append('STOP_LIST')
        if c['group'] == GROUPS[-1] and not key(c['article']).startswith(('SF','OEM')):
            holds.append('RAPID_ORIGINALS_EXCLUDED')
        if target is not None and confirmed_kg and re.search('картридж|фотобарабан|тонер|чернил', c['name'], re.I) and (target > 50 or target < Decimal('.001')):
            holds.append('IMPLAUSIBLE_CONSUMABLE_WEIGHT')
        if c['group'] == 'Изипринт' and key(c['article']) == 'I-B100_SET':
            holds.append('KNOWN_SUSPECT_WEIGHT_6243_0_048KG_FOR_4X100ML')
        diagnostics = ['DUPLICATE_ROW_WITHOUT_WEIGHT'] if one and len(good) < len(found) else []
        scope = 'PLANNED_DIMENSIONS_364' if pid in plan_ids else 'RAPID_DIMENSIONS_WRITTEN_1016' if pid in rapid_written else 'OTHER_ACTIVE'
        if holds:
            decision = 'REVIEW'
        elif comparison == 'NUMERIC_EQUAL':
            decision = 'NO_CHANGE'
        elif comparison == 'MS_WEIGHT_EMPTY':
            decision = 'CANDIDATE_FILL'
        else:
            decision = 'CANDIDATE_REPLACE'
        rows.append({'supplier': c['group'], 'ms_id': pid, 'external_code': c['external_code'],
                     'article': c['article'], 'name': c['name'], 'in_stock': c['any_target_stock'] == 'True',
                     'has_dimensions': c['all_three_filled'] == 'True', 'weight_ms': fmt(old),
                     'weight_source_numeric': fmt(target), 'source_units_confirmed_kg': confirmed_kg,
                     'source_units_basis': 'source_header' if one and units == {'кг'} else 'user_confirmation' if confirmed_kg else 'unconfirmed',
                     'source_package_weight_confirmed': pack_confirmed, 'comparison': comparison,
                     'weight_delta_numeric': fmt(target-old) if target is not None and old is not None else '',
                     'decision': decision, 'holds': ';'.join(holds), 'diagnostic_flags': ';'.join(diagnostics), 'scope': scope,
                     'dimensions_review_status': own_dim_review.get(pid, {}).get('status', ''),
                     'dimensions_review_reasons': own_dim_review.get(pid, {}).get('reasons', ''),
                     'source_rows': len(found), 'source_positive_rows': len(good),
                     'source_values': ';'.join(sorted({s['weight_numeric'] for s in good})),
                     'source_pointers': json.dumps([{k:s[k] for k in ['file','sheet','row','field','raw_weight','units','packaging']} for s in found],ensure_ascii=False)})
    def summarize(rr):
        return {'cards': len(rr), 'stock': sum(r['in_stock'] for r in rr),
                'ms_weight_filled': sum(bool(r['weight_ms']) for r in rr), 'ms_weight_empty': sum(not r['weight_ms'] for r in rr),
                'source_has_positive': sum(r['source_positive_rows'] > 0 for r in rr),
                'source_unique_numeric': sum(bool(r['weight_source_numeric']) for r in rr),
                'source_confirmed_kg': sum(r['source_units_confirmed_kg'] for r in rr),
                'source_confirmed_package_weight': sum(r['source_units_confirmed_kg'] and r['source_package_weight_confirmed'] for r in rr),
                'comparison': dict(Counter(r['comparison'] for r in rr)), 'decision': dict(Counter(r['decision'] for r in rr)),
                'hold_reasons': dict(Counter(v for r in rr for v in r['holds'].split(';') if v))}
    summary = {'status': 'READ_ONLY_NO_WRITE_PLAN_AUTHORIZED', 'at_utc': datetime.now(timezone.utc).isoformat(),
               'ms_snapshot': '2026-10-15 11:43 MSK; use fresh readback for 1380 cards when available',
               'fresh_ms_scope': live_collection,
               'accept_supplier_gross_or_net_by_user': accept_gross_or_net,
               'user_confirmed_weight_units': unit_policy,
               'ms_weight_units': 'kg is local integration convention (prices/ms_import.py); API defines Float without explicit units',
               'comparison_note': 'Source headers plus explicit user unit confirmations; gross/net accepted for all suppliers. Candidates are not authorized writes.',
               'all_active': {g:summarize([r for r in rows if r['supplier']==g]) for g in GROUPS},
               'planned_364': {g:summarize([r for r in rows if r['supplier']==g and r['scope']=='PLANNED_DIMENSIONS_364']) for g in GROUPS[:-1]},
               'rapid_written_1016': summarize([r for r in rows if r['scope']=='RAPID_DIMENSIONS_WRITTEN_1016']),
               'writes': 0, 'night_dimensions_364': 'timer stopped and disabled; authorization revoked pending combined plan'}
    save('summary.json', summary)
    csvout('all_active_weight_review.csv', rows)
    csvout('planned_364_weight_review.csv', [r for r in rows if r['scope']=='PLANNED_DIMENSIONS_364'])
    csvout('rapid_written_1016_weight_review.csv', [r for r in rows if r['scope']=='RAPID_DIMENSIONS_WRITTEN_1016'])
    save('candidate_weight_changes_NOT_AUTHORIZED.json', [r for r in rows if r['decision'].startswith('CANDIDATE_')])
    wb=Workbook();wb.remove(wb.active)
    summaries=[]
    for g,s in summary['all_active'].items():
        summaries.append({'Поставщик':g,'Всего':s['cards'],'Вес МС заполнен':s['ms_weight_filled'],'Вес МС пуст':s['ms_weight_empty'],
                          'Есть вес источника':s['source_has_positive'],'Однозначное число':s['source_unique_numeric'],
                          'Единицы кг подтверждены':s['source_confirmed_kg'],'Кг и упаковка подтверждены':s['source_confirmed_package_weight'],
                          'Численно равны':s['comparison'].get('NUMERIC_EQUAL',0),'Численно отличаются':s['comparison'].get('NUMERIC_DIFFERENT',0)})
    for name,data in [('Сводка',summaries),('План 364',[r for r in rows if r['scope']=='PLANNED_DIMENSIONS_364']),
                      ('Рапид записанные 1016',[r for r in rows if r['scope']=='RAPID_DIMENSIONS_WRITTEN_1016']),('Все карточки',rows)]:
        ws=wb.create_sheet(name);ws.append(list(data[0]))
        for r in data:
            ws.append([("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for v in r.values()])
        ws.freeze_panes='A2';ws.auto_filter.ref=ws.dimensions
        for cell in ws[1]:cell.font=Font(bold=True,color='FFFFFF');cell.fill=PatternFill('solid',fgColor='24476B')
        for i in range(1,len(data[0])+1):ws.column_dimensions[__import__('openpyxl').utils.get_column_letter(i)].width=25
    wb.save(OUT/'supplier_weights_review.xlsx')
    print(json.dumps({'all_active':summary['all_active'],'planned_364':summary['planned_364'],'rapid_written_1016':summary['rapid_written_1016']},ensure_ascii=False))


if __name__=='__main__':
    main()
