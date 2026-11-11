# поток: mkt / ozon
"""Offline enrichment of the captured baseline; never calls any API."""
import collections
import csv
from decimal import Decimal as D
import json
from pathlib import Path

OUT = Path(__file__).parent / 'baseline'


def read(name):
    with (OUT / name).open(encoding='utf-8-sig') as f:
        return list(csv.DictReader(f))


def write(name, rows):
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)


ad = read('sku_ad_daily.csv'); sales = collections.defaultdict(lambda: D(0))
for row in ad:
    sales[row['sku']] += D(row['attributed_sales_rub'])
rows = read('sku_baseline_28days.csv')
for row in rows:
    value = sales[row['sku']]; cost = D(row['baseline_cpc_spend_rub'])
    row['baseline_attributed_sales_rub'] = str(value)
    row['baseline_ad_DRR_pct'] = str((cost / value * 100).quantize(D('.01'))) if value > 0 else ''
write('sku_baseline_28days.csv', rows)
maps = json.loads((OUT / 'frozen_family_maps.json').read_text())
models = maps['model_by_sku_alias']; articles = maps['article_family_by_sku_alias']
groups = collections.defaultdict(lambda: {'orders': set(), 'value': D(0), 'spend': D(0), 'views': D(0), 'clicks': D(0)})
for row in read('sales_lines.csv'):
    for kind, key in [('model', row['model_id']), ('article_family', row['article_family'])]:
        if key:
            g = groups[(kind, key)]; g['orders'].add(row['order_id']); g['value'] += D(row['ordered_value_rub'])
for row in ad:
    for kind, key in [('model', models.get(row['sku'])), ('article_family', articles.get(row['sku']))]:
        if key:
            g = groups[(kind, key)]; g['spend'] += D(row['cpc_spend_rub'])
            g['views'] += D(row['ad_views']); g['clicks'] += D(row['ad_clicks'])
with (OUT.parent / 'fresh_eligibility.csv').open(encoding='utf-8-sig') as f:
    targets = list(csv.DictReader(f))
required = {('model', r['fresh_model_id']) for r in targets if r['fresh_model_id']} | {
    ('article_family', r['article_family_key']) for r in targets if r['segment'] == 'children'}
family_rows = []
for kind, key in sorted(required):
    g = groups[(kind, key)]
    family_rows.append({'level': kind, 'family_key': key, 'unique_orders': len(g['orders']),
         'ordered_value_rub': str(g['value']), 'SKU_reported_CPC_expense_rub': str(g['spend']),
         'ad_views': str(g['views']), 'ad_clicks': str(g['clicks']),
         'from_msk': '2026-10-09', 'through_msk': '2026-11-05'})
write('family_baseline_28days.csv', family_rows)
print('Offline baseline enrichment complete:', len(rows), 'SKU;', len(family_rows), 'model/article-family rows')
