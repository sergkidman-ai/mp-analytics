# поток: mkt / ozon
"""Offline summary of existing guard snapshots. No API calls or database writes."""
import collections
import datetime as dt
import json
from decimal import Decimal as D
from pathlib import Path

ROOT = Path('/opt/mp-analytics')
OUT = Path(__file__).parent
RUNTIME = ROOT / 'runtime/ozon_experiment_20261106'
TZ = dt.timezone(dt.timedelta(hours=3))


def read(path):
    return json.loads(path.read_text())


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def number(value):
    return D(str(value or 0).replace(' ', '').replace('\xa0', '').replace(',', '.'))


status = read(RUNTIME / 'latest.json')
assert status['status'] == 'CHECKED', 'Guard telemetry is not complete'
stamp = dt.datetime.fromisoformat(status['checked_at_utc'])
msk = stamp.astimezone(TZ)
snapshot = OUT / msk.strftime('monitoring_%Y%m%d_%H%M')
snapshot.mkdir(exist_ok=True)
activation = read(OUT / 'activation_status.json')
first = dt.datetime.fromisoformat(activation['first_running_confirmed_at_utc'])
ids = ['49852543', '49852681', '49852725', '49852748']
names = ['Новинки', 'Бандлы X2', 'Старые', 'Дочерние']
sources = []
for path in sorted((RUNTIME / 'raw').glob(stamp.strftime('%Y%m%d_%H') + '*performance.json')):
    data = read(path)
    if dt.datetime.fromisoformat(data['at_utc']) >= stamp:
        sources.append((path, data))
sku_path, sku_response = next((p, d) for p, d in sources if d['path'] == '/api/client/statistics/products/sku')
daily_path, daily_response = next((p, d) for p, d in sources if d['path'] == '/api/client/statistics/daily/json'
    and set(d.get('params', {}).get('campaignIds', [])) == set(ids))
guard_first_day = dt.date.fromisoformat(status['from_msk'])
first_day = first.astimezone(TZ).date()
last_day = dt.date.fromisoformat(status['through_msk'])
dates = [(first_day + dt.timedelta(days=i)).isoformat() for i in range((last_day-first_day).days + 1)]
# Replace whole requested days with their latest complete response. This also
# removes obsolete SKU rows when late attribution changes, rather than appending.
history_paths = list((RUNTIME / 'raw').glob('*performance.json'))
history_paths += list(OUT.glob('monitoring_*/day0_refresh/raw/*performance.json'))
by_day = {'sku': {}, 'daily': {}}
for path in history_paths:
    data = read(path)
    if data.get('http') != 200 or not isinstance(data.get('response', {}).get('rows'), list):
        continue
    kind = 'sku' if data['path'] == '/api/client/statistics/products/sku' else 'daily' if data['path'] == '/api/client/statistics/daily/json' else None
    if not kind:
        continue
    request = data.get('request') or data.get('params') or {}
    if set(map(str, request.get('campaignIds', []))) != set(ids):
        continue
    for day in dates:
        if request['dateFrom'][:10] <= day <= request['dateTo'][:10]:
            previous = by_day[kind].get(day)
            if previous is None or data['at_utc'] > previous['at_utc']:
                by_day[kind][day] = {'at_utc': data['at_utc'], 'path': str(path),
                    'rows': [r for r in data['response']['rows'] if r['date'][:10] == day]}
assert all(day in by_day[kind] for day in dates for kind in ['sku', 'daily']), 'A cumulative date has no source'
sku_response = {'response': {'rows': [r for day in dates for r in by_day['sku'][day]['rows']]}}
daily_response = {'response': {'rows': [r for day in dates for r in by_day['daily'][day]['rows']]}}
keys = [(str(r['campaignId']), r['date'], str(r['sku'])) for r in sku_response['response']['rows']]
assert len(keys) == len(set(keys)), 'Duplicate cumulative SKU observations'
provenance = {day: {kind: {k: v for k, v in by_day[kind][day].items() if k != 'rows'} for kind in ['sku', 'daily']} for day in dates}
save(snapshot / 'cumulative_source_catalog.json', provenance)


def aggregate(days):
    result = []
    for cid, name in zip(ids, names):
        sr = [r for r in sku_response['response']['rows'] if str(r['campaignId']) == cid and r['date'][:10] in days]
        dr = [r for r in daily_response['response']['rows'] if str(r['id']) == cid and r['date'][:10] in days]
        total = lambda rows, key: sum((number(r.get(key)) for r in rows), D(0))
        result.append({'campaign_id': cid, 'group': name,
            'state': next(c['state'] for c in status['campaigns'] if c['campaign_id'] == cid),
            'sku_report_rows': len(sr), 'sku_views': int(total(sr, 'views')), 'sku_clicks': int(total(sr, 'clicks')),
            'sku_spend_rub': str(total(sr, 'expense')), 'sku_ad_orders': int(total(sr, 'orders')),
            'sku_ad_sales_rub': str(total(sr, 'sales')),
            'sku_model_orders': int(total(sr, 'modelOrders')),
            'sku_model_sales_rub': str(total(sr, 'modelSales')), 'sku_to_cart': int(total(sr, 'toCart')),
            'skus_with_views': len({str(r['sku']) for r in sr if number(r['views']) > 0}),
            'skus_with_clicks': len({str(r['sku']) for r in sr if number(r['clicks']) > 0}),
            'daily_report_rows': len(dr), 'daily_views': int(total(dr, 'views')),
            'daily_clicks': int(total(dr, 'clicks')), 'daily_spend_rub': str(total(dr, 'moneySpent')),
            'daily_orders': int(total(dr, 'orders'))})
    return result


fields = ['sku_views', 'sku_clicks', 'sku_spend_rub', 'sku_ad_orders', 'sku_ad_sales_rub',
          'sku_model_orders', 'sku_model_sales_rub', 'sku_to_cart',
          'skus_with_views', 'skus_with_clicks', 'daily_views', 'daily_clicks', 'daily_spend_rub', 'daily_orders']
totals = lambda rows: {k: str(sum(number(r[k]) for r in rows)) for k in fields}
rows = aggregate(dates)
days = {day: {'rows': aggregate([day]), 'totals': totals(aggregate([day]))} for day in dates}
guard_dates = [day for day in dates if day >= status['from_msk']]
assert number(totals(aggregate(guard_dates))['sku_spend_rub']) == number(status['new_campaign_CPC_spend_rub'])
policy = read(OUT / 'risk_policy.json')
maps = read(Path(policy['family_maps_path']))
binding = read(Path(policy['bindings_path']))
target = set()
for c in binding['campaigns']:
    if c['wave'] == 1:
        target.update(str(r['sku']) for r in read(OUT / c['readback_file'])['products'])
assert len(target) == 3451
models = {maps['model_by_sku_alias'].get(k) for k in target} - {None, ''}
articles = {maps['article_family_by_sku_alias'].get(k) for k in target} - {None, ''}
window_covers_activation = first_day <= first.astimezone(TZ).date()
window_start = max(first, dt.datetime.combine(first_day, dt.time.min, TZ))
own, halo = set(), set()
own_by_day, halo_by_day = collections.defaultdict(set), collections.defaultdict(set)
raw_post_files = list((RUNTIME / 'raw').glob('*_postings_*.json'))
raw_post_files += list(OUT.glob('monitoring_*/day0_refresh/postings_*.json'))
latest_postings = {}
for path in raw_post_files:
    data = read(path)
    if not data.get('complete'):
        continue
    observed_at = data.get('captured_at_utc') or data.get('retrieved_at_utc')
    assert observed_at, 'Posting snapshot lacks observation time'
    scheme = data.get('scheme') or path.stem.rsplit('_', 1)[-1]
    assert scheme in ['fbs', 'fbo']
    for post in data['postings']:
        at = dt.datetime.fromisoformat((post.get('in_process_at') or post['created_at']).replace('Z', '+00:00'))
        if at.astimezone(TZ).date() < first_day or at > stamp:
            continue
        key = (scheme, post['posting_number'])
        if key not in latest_postings or observed_at > latest_postings[key]['observed_at']:
            latest_postings[key] = {'post': post, 'observed_at': observed_at, 'source': str(path)}
post_files = []
for scheme in ['fbs', 'fbo']:
    selected = [v for (sc, _), v in latest_postings.items() if sc == scheme]
    path = snapshot / f'cumulative_postings_{scheme}.json'
    save(path, {'scheme': scheme, 'complete': True, 'derived_from_raw': True,
        'from_msk': str(first_day), 'through_msk': msk.isoformat(),
        'postings': [v['post'] for v in selected],
        'latest_source_by_posting': {v['post']['posting_number']: {'source': v['source'], 'observed_at': v['observed_at']} for v in selected}})
    post_files.append(path)
for path in post_files:
    data = read(path)
    assert data['complete']
    for post in data['postings']:
        if post['status'].lower().startswith('cancel'):
            continue
        at = dt.datetime.fromisoformat((post.get('in_process_at') or post['created_at']).replace('Z', '+00:00'))
        if at < window_start:
            continue
        oid = str(post['order_id'])
        day = at.astimezone(TZ).date().isoformat()
        for product in post['products']:
            raw = str(product['sku'])
            sku = maps['canonical_by_alias'].get(raw, raw)
            if sku in target:
                own.add(oid); own_by_day[day].add(oid)
            if maps['model_by_sku_alias'].get(raw) in models or maps['article_family_by_sku_alias'].get(raw) in articles:
                halo.add(oid); halo_by_day[day].add(oid)
prior = read(OUT / 'first_delivery_latest.json') if (OUT / 'first_delivery_latest.json').exists() else {}
sku_spend_cumulative = collections.defaultdict(lambda: D(0))
for r in sku_response['response']['rows']:
    sku_spend_cumulative[str(r['sku'])] += number(r['expense'])
summary = {'checked_at_utc': status['checked_at_utc'],
    'period_msk': window_start.astimezone(TZ).strftime('%d.%m.%Y %H:%M') + ' — ' + msk.strftime('%d.%m.%Y %H:%M') + ' МСК',
    'window_covers_activation': window_covers_activation,
    'guard_window_covers_activation': guard_first_day <= first.astimezone(TZ).date(),
    'ad_source_by_day': provenance,
    'primary_source': 'SKU report /api/client/statistics/products/sku',
    'source_sku_raw': str(sku_path), 'source_daily_raw': str(daily_path),
    'rows': rows, 'totals': totals(rows), 'days': days,
    'guard_status': status['status'], 'guard_actions': status['actions'],
    'max_sku_spend_rub': str(max([number(r['spend']) for r in status.get('sku_metrics', [])] or [D(0)])),
    'cumulative_max_sku_spend_rub': str(max(sku_spend_cumulative.values(), default=D(0))),
    'max_sku_spend_period': {'from_msk': status['from_msk'], 'through_msk': status['through_msk']},
    'no_telemetry_rows_is_not_proof_of_no_delivery': True, 'source_discrepancy_preserved': True,
    'previous_snapshot_at_utc': prior.get('previous_snapshot_at_utc') if prior.get('checked_at_utc') == status['checked_at_utc'] else prior.get('checked_at_utc'),
    'general_sales': {'source_postings': [str(p) for p in post_files], 'from_utc': window_start.isoformat(),
        'through_utc': stamp.isoformat(), 'own_target_unique_orders': len(own),
        'model_or_article_family_unique_orders': len(halo),
        'own_orders_by_msk_day': {k: len(v) for k, v in own_by_day.items()},
        'family_orders_by_msk_day': {k: len(v) for k, v in halo_by_day.items()},
        'causal_advertising_effect': False,
        'note': 'Family intersections deduplicated. Family includes pre-existing advertised and organic neighbors.'},
    'wave1_weekly_budget': status['wave1_weekly_budget']}
save(snapshot / 'delivery_check.json', summary)
save(snapshot / 'guard_snapshot.json', status)
save(OUT / 'first_delivery_latest.json', summary)
for path, data in sources:
    if data['path'] == '/api/client/campaign':
        bound_ids = {c['campaign_id'] for c in status['campaigns']}
        live = [c for c in data['response']['list'] if str(c['id']) in bound_ids]
        assert len(live) == 12
        save(OUT / 'live_campaign_states.json', {'checked_at_utc': data['at_utc'], 'campaigns': live})
        break
print(json.dumps({'snapshot': str(snapshot), 'period': summary['period_msk'], 'covers_launch': window_covers_activation,
    'rows': rows, 'totals': summary['totals'], 'daily_totals': {k: v['totals'] for k, v in days.items()},
    'max_sku_spend_rub': summary['max_sku_spend_rub'], 'general_own_orders': len(own), 'general_family_orders': len(halo),
    'guard_actions': status['actions'], 'weekly_reference': status['wave1_weekly_budget']}, ensure_ascii=False))
