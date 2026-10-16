# поток: card
"""Read-only planning audit. Produces local reports; cannot write to MS or schedule work."""
import collections
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path('/opt/mp-analytics')
sys.path.insert(0, str(ROOT))
from core import db
from prices.ms_import import XL_RE
from tools.prc.wb_fill import compose

OUT = Path(__file__).resolve().parent
SOURCE = ROOT / 'docs/reports/card_all_supplier_dimensions_2026-10-15'


def main():
    catalog_rows = db.query('SELECT external_code, raw, last_seen FROM prc_tc_model WHERE gone_at IS NULL')
    catalog = {r['external_code']: r['raw'] for r in catalog_rows}
    (OUT / 'catalog_snapshot.json').write_text(json.dumps(catalog, ensure_ascii=False))
    products = json.loads((SOURCE / 'products.json').read_text())
    bundles = json.loads((OUT / 'live_bundle_probe.json').read_text())['rows']
    counts = collections.Counter()
    lengths = collections.Counter()
    rows = []
    for entity, p in [('product', p) for p in products] + [('bundle', p) for p in bundles]:
        if p.get('archived'):
            continue
        counts['active_total'] += 1
        attrs = {a['name']: a.get('value') for a in p.get('attributes', [])}
        current = str(attrs.get('Название WB') or '').strip()
        counts['current_filled' if current else 'current_empty'] += 1
        counts['current_over_55'] += len(current) > 55
        code = (p.get('externalCode') or '').strip()
        tc = catalog.get(code)
        value, reason = compose(tc) if tc else ('', 'Нет точного внешнего кода в активном каталоге ТК')
        if value and XL_RE.search(p.get('name') or '') and 'XL' not in value.upper():
            value += ' XL ресурс'
        short = value
        actions = []
        # Preview only the two existing, unambiguous shortening operations.
        # New color/type abbreviations await the user's approval.
        if tc and value and len(short) > 50 and tc.get('additional_title'):
            raw = dict(tc, additional_title='')
            short, why = compose(raw)
            if short and XL_RE.search(p.get('name') or '') and 'XL' not in short.upper():
                short += ' XL ресурс'
            if short != value:
                actions.append('Убрать доп. синоним модели')
        if len(short) > 50 and short.endswith(' XL ресурс'):
            short = short[:-len(' XL ресурс')] + ' XL'
            actions.append('XL ресурс → XL')
        if not value:
            status = 'HOLD_SOURCE'
        elif len(short) > 55:
            status = 'HOLD_LENGTH'
        elif current == short:
            status = 'NOOP_TEXT'
        else:
            status = 'PREVIEW_REQUIRES_LAYOUT_AND_IDENTITY_CHECK'
        counts[status] += 1
        if value:
            lengths[str(len(value))] += 1
            counts['formula_over_60'] += len(value) > 60
            counts['formula_over_55'] += len(value) > 55
            counts['formula_differs_current'] += value != current
            counts['preview_shortened'] += short != value
        missing = [n for n, ok in [
            ('Наименование', p.get('name')), ('Описание', p.get('description')),
            ('Группа', p.get('productFolder')), ('Поставщик', p.get('supplier')),
            ('Артикул', p.get('article')), ('Код', p.get('code')),
            ('Внешний код', code), ('Вес', p.get('weight')), ('Ед. измерения', p.get('uom')),
            ('Название WB', short),
            ('Code128', any('code128' in b for b in p.get('barcodes', []))),
        ] if not ok]
        rows.append(dict(entity=entity, ms_id=p['id'], code=p.get('code'), external_code=code,
                         supplier_name=p.get('name'), current=current, current_chars=len(current),
                         formula=value, formula_chars=len(value), preview=short, preview_chars=len(short),
                         actions='; '.join(actions), status=status, source_issue=reason,
                         missing_required='; '.join(missing)))
    with (OUT / 'review.csv').open('w', newline='', encoding='utf-8-sig') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)
    summary = dict(created_at_utc=datetime.now(timezone.utc).isoformat(), counts=dict(counts),
                   formula_length_distribution=dict(sorted(lengths.items(), key=lambda kv: int(kv[0]))),
                   sources={'products': str(SOURCE / 'products.json'),
                            'collection': json.loads((SOURCE / 'products_collection.json').read_text()),
                            'catalog_last_seen': str(max(r['last_seen'] for r in catalog_rows)),
                            'catalog_models': len(catalog),
                            'products_sha256': hashlib.file_digest((SOURCE / 'products.json').open('rb'), 'sha256').hexdigest()
                            if hasattr(hashlib, 'file_digest') else hashlib.sha256((SOURCE / 'products.json').read_bytes()).hexdigest()},
                   authorized_for_apply=False, scheduled=False, api_writes=0,
                   preview_limitations=['No full-catalogue fresh API reread', 'No layout validation',
                                        'No supplier chip/model/variant identity validation',
                                        'No original/type/stop-list eligibility validation',
                                        'No missing-field remediation', 'No webhook consumer classification'])
    (OUT / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps(dict(counts), ensure_ascii=False))


if __name__ == '__main__':
    main()
