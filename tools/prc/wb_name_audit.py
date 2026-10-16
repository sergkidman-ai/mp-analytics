# поток: prc
"""Read-only WB naming audit. Uses saved MS snapshot and the common Pult formula.

This tool has no apply mode and cannot schedule work or change MS cards.
"""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import hashlib
import gzip
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from core import db
from prices import features
from prices.catalog import model_conflict, model_dict
from prices.novelty import volume
from prices.wb_label import MAX_CHARS, RULES_VERSION, wrap_lines, text_width, LINE_WIDTH_PT, FONT_SIZE_PT
from tools.prc.wb_fill import compose_details, brand_of, CHIP_WORD

SOURCE = ROOT / 'docs/reports/card_all_supplier_dimensions_2026-10-15'
PLAN = ROOT / 'docs/reports/card_wb_name_plan_2026-10-15'
TYPE_PATTERNS = [
    (r'^(?:комплект|набор)\s+картридж', 'Комплект картриджей'),
    (r'^(?:комплект|набор)\s+фотобарабан', 'Комплект фотобарабанов'),
    (r'^(?:комплект|набор)\s+чернил', 'Комплект чернил'),
    (r'^блок\s+проявки', 'Блок проявки'),
    (r'^блок\s+фотобарабана\s+в\s+сборе\s+с\s+блоком\s+проявки', 'Фотобарабан + картридж'),
    (r'^(?:фотобарабан|драм[-\s]?(?:картридж|юнит))', 'Фотобарабан'),
    (r'^(?:тонер[-\s]картридж|картридж)', 'Картридж'),
    (r'^тонер\b', 'Тонер'), (r'^чернила\b', 'Чернила'),
    (r'^печатающая\s+головка', 'Печатающая головка'),
]


def identity_issues(product, tc):
    """Only explicit contradictory facts; absence is not invented agreement."""
    issues = []
    name = product.get('name') or ''
    kind = tc.get('consumable_type') or ''
    source_kind = next((k for p, k in TYPE_PATTERNS if re.search(p, name, re.I)), None)
    normalized_kind = 'Картридж' if kind == 'Тонер-картридж' else kind
    if source_kind and source_kind != normalized_kind:
        issues.append(f'тип поставщика {source_kind} против ТК {kind}')
    chip = features.chip(name)
    tc_chip = features.chip(CHIP_WORD.get(tc.get('chip'), ''))
    if chip and (not tc_chip or chip != tc_chip):
        issues.append(f'чип поставщика {chip} против ТК {tc_chip or "не указан"}')
    _, brands = brand_of(tc)
    source_brands = features.brand(name)
    # Synonyms vary; compare canonical forms using the existing brand parser.
    canonical_brands = set().union(*(features.brand(b) for b in brands)) if brands else set()
    if source_brands and canonical_brands and not (source_brands & canonical_brands):
        issues.append('бренд принтера поставщика против ТК')
    frequencies = Counter(p.get('brand') for p in tc.get('printer_models', []) if p.get('brand'))
    if len(frequencies) > 1:
        top = frequencies.most_common()
        if top[0][1] == top[1][1]:
            issues.append('равные голоса брендов принтера')
    if kind in ('Тонер', 'Чернила'):
        amount, unit = volume(name)
        expected_unit = 'г' if kind == 'Тонер' else 'мл'
        tc_amount = tc.get('volume_ml')
        try:
            tc_amount = float(tc_amount) if tc_amount else None
        except (ValueError, TypeError):
            tc_amount = None
        if amount and (unit != expected_unit or tc_amount != amount):
            # Some models already contain their volume verbatim; verify that explicitly.
            model_amount, model_unit = volume(tc.get('title') or '')
            if (amount, unit) != (model_amount, model_unit):
                issues.append('масса/объём содержимого поставщика против ТК')
    if re.search('комплект|набор', kind, re.I):
        issues.append('проверить состав и количество набора')
    return issues


def missing_fields(p, proposed):
    return [key for key, value in (
        ('Наименование', p.get('name')), ('Описание', p.get('description')),
        ('Группа', p.get('productFolder')), ('Поставщик', p.get('supplier')),
        ('Артикул', p.get('article')), ('Код', p.get('code')),
        ('Внешний код', p.get('externalCode')), ('Вес', p.get('weight')),
        ('Ед. измерения', p.get('uom')), ('Название WB', proposed),
        ('Code128', any('code128' in b for b in p.get('barcodes', []))),
    ) if not value]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    catalog_rows = db.query('SELECT external_code, raw, last_seen FROM prc_tc_model WHERE gone_at IS NULL')
    catalog = {r['external_code']: r['raw'] for r in catalog_rows}
    with gzip.open(args.out / 'catalog_snapshot.json.gz', 'wt', encoding='utf-8') as f:
        json.dump(catalog, f, ensure_ascii=False)
    products = json.loads((SOURCE / 'products.json').read_text())
    bundles = json.loads((PLAN / 'live_bundle_probe.json').read_text())['rows']
    counts, reasons, rows = Counter(), Counter(), []
    by_ec, titles = model_dict()
    for entity, p in [('product', p) for p in products] + [('bundle', p) for p in bundles]:
        if p.get('archived'):
            continue
        attrs = {a['name']: a.get('value') for a in p.get('attributes', [])}
        current = str(attrs.get('Название WB') or '').strip()
        ec = (p.get('externalCode') or '').strip()
        tc = catalog.get(ec)
        result = compose_details(tc, p.get('name') or '') if tc else None
        proposed = result.name if result else ''
        issues = []
        if not tc:
            issues.append('нет точного внешнего кода в активном ТК')
        elif not proposed:
            issues.append(result.reason)
        else:
            issues.extend(identity_issues(p, tc))
            conflict = model_conflict(p.get('name') or '', ec, by_ec, titles)
            if conflict:
                issues.append(f'модель поставщика {conflict[0]} против ТК {conflict[1]}')
            old_chip = features.chip(current)
            tc_chip = features.chip(CHIP_WORD.get(tc.get('chip'), ''))
            if old_chip and not tc_chip:
                issues.append('старое Название WB указывает чип, поле ТК пусто')
            if re.search(r'\bXL\b|увеличен', current, re.I) and not re.search(r'XL|увеличен', proposed, re.I):
                issues.append('старое Название WB указывает XL без подтверждения в источниках')
        missing = missing_fields(p, proposed)
        if missing:
            issues.append('обязательные поля: ' + ', '.join(missing))
        if attrs.get('Стоп лист') or re.search(r'брак|стоп.?лист', p.get('pathName', '') + ' ' + (p.get('name') or ''), re.I):
            issues.append('брак/стоп-лист')
        if re.search(r'оригинал|\bOEM\b', p.get('pathName', '') + ' ' + (p.get('name') or ''), re.I):
            issues.append('проверить оригинальность товара')
        if proposed and current == proposed:
            status = 'NOOP'
        elif issues:
            status = 'HOLD'
        else:
            status = 'NEEDS_TEMPLATE_AND_LIVE_CHECK'
        counts[status] += 1
        counts['total'] += 1
        if result and result.actions:
            counts['with_shortening'] += 1
        if proposed:
            assert len(proposed) <= MAX_CHARS
        reasons.update(issues)
        rows.append(dict(entity=entity, ms_id=p['id'], code=p.get('code'), external_code=ec,
                         supplier_name=p.get('name'), current=current, proposed=proposed,
                         chars=len(proposed), full_formula=result.full_name if result else '',
                         geometry_lines=len(wrap_lines(proposed) or ()) if proposed else 0,
                         geometry_max_line_pt=round(max((text_width(line) for line in wrap_lines(proposed)), default=0), 3) if proposed else 0,
                         actions='; '.join(result.actions) if result else '', status=status,
                         holds='; '.join(issues), rules_version=RULES_VERSION))
    with gzip.open(args.out / 'review.csv.gz', 'wt', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    hashes = {}
    for name in ('prices/wb_label.py', 'prices/data/wb_label_font_metrics.json',
                 'tools/prc/wb_fill.py', 'prices/ms_import.py'):
        hashes[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    summary = dict(created_at_utc=datetime.now(timezone.utc).isoformat(), counts=dict(counts),
                   holds=dict(reasons), rules_version=RULES_VERSION, code_sha256=hashes,
                   product_snapshot=json.loads((SOURCE / 'products_collection.json').read_text()),
                   catalog_last_seen=str(max(r['last_seen'] for r in catalog_rows)),
                   sample_geometry_checked=True, sample_line_width_pt=LINE_WIDTH_PT,
                   sample_font_size_pt=FONT_SIZE_PT,
                   layout_verified=False, live_candidate_reread=False, scheduled=False, ms_writes=0)
    (args.out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({'counts': dict(counts), 'layout_verified': False, 'ms_writes': 0}, ensure_ascii=False))


if __name__ == '__main__':
    main()
