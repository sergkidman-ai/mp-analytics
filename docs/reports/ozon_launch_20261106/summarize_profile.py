# поток: mkt / ozon
"""Offline matched-window sales comparison from saved postings; no API or DB writes."""
import datetime as dt
import json
from decimal import Decimal as D
from pathlib import Path
import sys

sys.path.insert(0, '/opt/mp-analytics')

from core.ozon_posting import product_price

OUT = Path(__file__).parent
TZ = dt.timezone(dt.timedelta(hours=3))
read = lambda p: json.loads(Path(p).read_text())
save = lambda p, x: Path(p).write_text(json.dumps(x, ensure_ascii=False, indent=2) + '\n')
delivery = read(OUT / 'first_delivery_latest.json')
assert delivery['window_covers_activation'], 'Cumulative postings are required'
start = dt.datetime.fromisoformat(delivery['general_sales']['from_utc'])
end = dt.datetime.fromisoformat(delivery['checked_at_utc'])
policy = read(OUT / 'risk_policy.json')
maps = read(policy['family_maps_path'])
binding = read(OUT / 'created_campaign_bindings.json')
groups = {c['campaign_id']: {str(r['sku']) for r in read(OUT / c['readback_file'])['products']}
          for c in binding['campaigns'] if c['wave'] == 1}
target = set.union(*groups.values())
models = {maps['model_by_sku_alias'].get(k) for k in target} - {None, ''}
articles = {maps['article_family_by_sku_alias'].get(k) for k in groups['49852748']} - {None, ''}
assert len(target) == 3451 and len(models) == 2926 and len(articles) == 348
baseline_files = [OUT / 'baseline/postings_fbs.json', OUT / 'baseline/postings_fbo.json']
baseline_end = dt.datetime.fromisoformat('2026-11-06T00:00:00+03:00')
assert end - dt.timedelta(days=7) < baseline_end, 'Prior-window baseline would overlap the experiment; choose a new explicit baseline'


def load(files):
    rows, seen = [], set()
    for path in files:
        data = read(path)
        assert data['complete']
        scheme = data.get('scheme') or Path(path).stem.rsplit('_', 1)[-1]
        for post in data['postings']:
            key = (scheme, post['posting_number'])
            assert key not in seen, 'Duplicate posting'
            seen.add(key)
            if post['status'].lower().startswith('cancel'):
                continue
            at = dt.datetime.fromisoformat((post.get('in_process_at') or post['created_at']).replace('Z', '+00:00'))
            for product in post['products']:
                raw = str(product['sku'])
                rows.append({'at': at, 'order': str(post['order_id']),
                    'sku': maps['canonical_by_alias'].get(raw, raw),
                    'model': maps['model_by_sku_alias'].get(raw),
                    'article': maps['article_family_by_sku_alias'].get(raw),
                    'value': product_price(product) * D(str(product['quantity']))})
    return rows


base = load(baseline_files)
actual = load(delivery['general_sales']['source_postings'])
predicates = {'sku': lambda x: x['sku'] in target, 'ozon_models': lambda x: x['model'] in models,
              'children_article_families': lambda x: x['article'] in articles, 'account': lambda x: True}


def metric(rows, lo, hi, scope):
    selected = [x for x in rows if lo <= x['at'] <= hi and predicates[scope](x)]
    return {'orders': len({x['order'] for x in selected}),
            'ordered_value_rub': str(sum((x['value'] for x in selected), D(0)))}


def compare(lo, hi, scope):
    current = metric(actual, lo, hi, scope)
    refs = [metric(base, lo - dt.timedelta(days=7*i), hi - dt.timedelta(days=7*i), scope) for i in range(1, 5)]
    mean = {k: sum(D(str(r[k])) for r in refs) / 4 for k in current}
    return {'actual': current, 'prior_windows': refs, 'baseline4_mean': {k: str(v) for k, v in mean.items()},
            'change_percent': {k: str((D(str(current[k])) / mean[k] - 1) * 100) if mean[k] else None for k in current}}


scopes = {scope: compare(start, end, scope) for scope in predicates}
day = start.astimezone(TZ).date()
days = []
while day <= end.astimezone(TZ).date():
    lo = dt.datetime.combine(day, dt.time.min, TZ)
    hi = min(end, lo + dt.timedelta(days=1) - dt.timedelta(microseconds=1))
    days.append({'day': str(day), **compare(lo, hi, 'account'), 'through_msk': hi.astimezone(TZ).isoformat()})
    day += dt.timedelta(days=1)
output = {'checked_at_utc': delivery['checked_at_utc'], 'from_utc': start.isoformat(), 'through_utc': end.isoformat(),
    'scopes': scopes, 'account_days': days, 'unique_target_skus': len(target), 'unique_ozon_models': len(models),
    'children_article_families': len(articles),
    'matching': 'Four prior windows shifted by 7,14,21,28 days, same weekdays and hours',
    'reference_windows_msk': [{'from': (start-dt.timedelta(days=7*i)).astimezone(TZ).isoformat(),
                             'through': (end-dt.timedelta(days=7*i)).astimezone(TZ).isoformat()} for i in range(1, 5)],
    'causal_effect_measured': False, 'source_actual_postings': delivery['general_sales']['source_postings'],
    'source_baseline': [str(p) for p in baseline_files], 'source_family_map': policy['family_maps_path'],
    'unmapped_current_lines': sum(not r['model'] for r in actual)}
interventions = OUT / 'external_interventions.jsonl'
output['known_additional_interventions_within_observed_window'] = [event for event in
    (json.loads(line) for line in interventions.read_text().splitlines())
    if start <= dt.datetime.fromisoformat(event['at_utc']) <= end] if interventions.exists() else []
assert scopes['sku']['actual']['orders'] == delivery['general_sales']['own_target_unique_orders']
snapshot = OUT / end.astimezone(TZ).strftime('monitoring_%Y%m%d_%H%M')
save(snapshot / 'family_account_comparison.json', output)
save(OUT / 'family_account_latest.json', output)
print(json.dumps({'scopes': scopes, 'account_days': days, 'unmapped_current_lines': output['unmapped_current_lines']}, ensure_ascii=False))
