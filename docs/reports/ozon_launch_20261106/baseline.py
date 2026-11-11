# поток: mkt / ozon
"""Freeze 28 prelaunch full days using existing posting client and saved ad history."""
import collections
import csv
import datetime as dt
from decimal import Decimal as D
import json
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

sys.path.insert(0, '/opt/mp-analytics')
import requests
from collectors.ozon_ads import _token, PERF
from collectors.ozon_postings import fetch_postings
from core.ozon_posting import product_price

OUT = Path(__file__).parent / 'baseline'
OUT.mkdir(exist_ok=True)
TZ = ZoneInfo('Europe/Moscow')
END = dt.datetime.now(TZ).date() - dt.timedelta(days=1)
START = END - dt.timedelta(days=27)
assert END == dt.date(2026, 11, 5), 'Frozen 6Nov baseline: use a new dated directory before collecting a later baseline'


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def save(name, z):
    (OUT / name).write_text(json.dumps(z, ensure_ascii=False, indent=2, default=str) + '\n')


def read(p):
    with p.open(encoding='utf-8-sig') as f:
        yield from csv.DictReader(f)


def write(name, rows, fields=None):
    with (OUT / name).open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields or list(rows[0])); writer.writeheader(); writer.writerows(rows)


def num(x):
    return D(str(x or 0).replace('\xa0', '').replace(' ', '').replace(',', '.'))


catalog = OUT.parent.parent / 'ozon_full_inventory_20261105'
aliases = {}; models = {}; article_family = {}
for p in (catalog / 'raw').glob('info_*.json'):
    for x in json.loads(p.read_text())['response']['items']:
        sku = str(x.get('sku') or '')
        model = str((x.get('model_info') or {}).get('model_id') or '')
        offer = str(x['offer_id']); family = offer[:4] if offer.isascii() and offer.isdigit() and len(offer) >= 4 else ''
        for s in [x.get('sku')] + [v.get('sku') for v in x.get('sources') or []]:
            if s:
                aliases[str(s)] = sku; models[str(s)] = model; article_family[str(s)] = family
targets = list(read(OUT.parent / 'fresh_eligibility.csv'))
for r in targets:
    for s in r['fresh_sku_aliases'].split(';'):
        aliases[s] = r['sku']; models[s] = r['fresh_model_id']; article_family[s] = r['article_family_key']
save('frozen_family_maps.json', {'captured_at_utc': now(), 'model_by_sku_alias': models,
                              'article_family_by_sku_alias': article_family, 'canonical_by_alias': aliases,
                              'source': 'full saved 5Nov catalog; refreshed 6Nov target cards only'})
sales = []; dedup = set()
for scheme in ['fbs', 'fbo']:
    p = OUT / f'postings_{scheme}.json'
    if p.exists():
        obj = json.loads(p.read_text()); assert obj['complete']
    else:
        posts = fetch_postings('oz_acc1', scheme, f'{START}T00:00:00+03:00',
                               f'{END}T23:59:59.999000+03:00', limit=100, window_budget=600)
        obj = {'complete': True, 'from_msk': str(START), 'through_msk': str(END),
               'retrieved_at_utc': now(), 'postings': posts}
        save(p.name, obj)
    for post in obj['postings']:
        key = (scheme, post['posting_number'])
        assert key not in dedup; dedup.add(key)
        if post['status'].lower().startswith('cancel'):
            continue
        day = dt.datetime.fromisoformat((post.get('in_process_at') or post['created_at']).replace('Z', '+00:00')).astimezone(TZ).date()
        if not START <= day <= END:
            continue
        for x in post['products']:
            raw = str(x['sku']); sku = aliases.get(raw, raw)
            sales.append({'date_msk': str(day), 'order_id': str(post['order_id']), 'sku': sku,
                          'model_id': models.get(raw, ''), 'article_family': article_family.get(raw, ''),
                          'units': str(x['quantity']), 'ordered_value_rub': str(product_price(x) * D(str(x['quantity'])))})
    print('Fresh baseline postings', scheme, len(obj['postings']), flush=True)
write('sales_lines.csv', sales)
ads = collections.defaultdict(lambda: [D(0), D(0), D(0), D(0)])
year = OUT.parent.parent / 'ozon_year_review_20261105'
for r in read(year / 'sku_positive_cpc_daily_year.csv'):
    day = dt.date.fromisoformat(r['date_msk'])
    if START <= day <= min(END, dt.date(2026, 11, 4)):
        key = (r['sku'], r['date_msk']); v = ads[key]
        for i, field in enumerate(['views', 'clicks', 'spend_rub', 'attributed_sales_rub']):
            v[i] += num(r[field])
h = {'Authorization': 'Bearer ' + _token('oz_acc1')}
before = json.loads((OUT.parent / 'campaigns_before.json').read_text())['campaigns']
ids = [str(c['id']) for c in before if c.get('advObjectType') == 'SKU' and c.get('PaymentType', 'CPC') == 'CPC']
newday = dt.date(2026, 11, 5)
assert END == newday, 'Additional days need a dated source, never silently assume coverage'
for i in range(0, len(ids), 10):
    p = OUT / f'fast_stats_{i // 10:02}.json'
    if p.exists():
        z = json.loads(p.read_text())
    else:
        body = {'campaignIds': ids[i:i + 10], 'dateFrom': str(newday), 'dateTo': str(newday)}
        for attempt in range(8):
            r = requests.post(PERF + '/api/client/statistics/products/sku', headers=h, json=body, timeout=120)
            if r.status_code != 429:
                break
            time.sleep(5)
        r.raise_for_status(); z = {'at_utc': now(), 'request': body, 'response': r.json()}; save(p.name, z)
    for r in z['response']['rows']:
        assert r['date'][:10] == str(newday)
        sku = aliases.get(str(r['sku']), str(r['sku'])); v = ads[(sku, str(newday))]
        for idx, field in enumerate(['views', 'clicks', 'expense', 'sales']):
            v[idx] += num(r[field])
adrows = [{'sku': s, 'date_msk': day, 'ad_views': str(v[0]), 'ad_clicks': str(v[1]),
           'cpc_spend_rub': str(v[2]), 'attributed_sales_rub': str(v[3])} for (s, day), v in ads.items()]
write('sku_ad_daily.csv', adrows)
order_sets = collections.defaultdict(set); values = collections.defaultdict(lambda: D(0))
for r in sales:
    day = r['date_msk']; order = r['order_id']; value = num(r['ordered_value_rub'])
    for kind, key in [('sku', r['sku']), ('model', r['model_id']), ('article_family', r['article_family']), ('account', 'oz_acc1')]:
        if key:
            order_sets[(kind, key, day)].add(order); values[(kind, key, day)] += value
target_skus = {r['sku'] for r in targets}
target_models = {r['fresh_model_id'] for r in targets}
target_families = {r['article_family_key'] for r in targets if r['segment'] == 'children'}
sales_rows = [{'level': kind, 'key': key, 'date_msk': day, 'unique_orders': len(order_sets[(kind, key, day)]),
               'ordered_value_rub': str(value)} for (kind, key, day), value in values.items()
              if kind == 'account' or (kind == 'sku' and key in target_skus) or (kind == 'model' and key in target_models)
              or (kind == 'article_family' and key in target_families)]
write('general_sales_daily.csv', sales_rows)
ad_lookup = {(r['sku'], r['date_msk']): r for r in adrows}
daily = []
account_cost_reference = collections.defaultdict(lambda: D(0))
for r in read(year / 'campaign_daily_year.csv'):
    if r['type'] == 'SKU' and r['payment_type'] == 'CPC' and str(START) <= r['date_msk'] <= str(END):
        account_cost_reference[r['date_msk']] += num(r['spend_rub'])
for file in OUT.glob('campaign_reference_*.json'):
    for r in json.loads(file.read_text()).get('rows', []):
        account_cost_reference[r['date'][:10]] += num(r['moneySpent'])
for off in range(28):
    day = str(START + dt.timedelta(days=off))
    spend = account_cost_reference.get(day)
    if spend is None:
        spend = sum(num(r['cpc_spend_rub']) for r in adrows if r['date_msk'] == day)
    daily.append({'date_msk': day, 'unique_orders': len(order_sets[('account', 'oz_acc1', day)]),
                  'ordered_value_rub': str(values[('account', 'oz_acc1', day)]), 'cpc_spend_rub': str(spend),
                  'cpc_spend_source': 'campaign daily/json; historical campaign_daily_year reused',
                  'cpo_spend_rub': '', 'cpo_spend_status': 'NOT_MEASURED_NOT_ZERO'})
write('account_daily.csv', daily)
summary = []
sales_by_sku = collections.defaultdict(list)
ads_by_sku = collections.defaultdict(list)
for x in sales:
    sales_by_sku[x['sku']].append(x)
for x in adrows:
    ads_by_sku[x['sku']].append(x)
for r in targets:
    sku = r['sku']; own = sales_by_sku[sku]
    av = ads_by_sku[sku]
    summary.append({'sku': sku, 'offer_id': r['offer_id'], 'wave': r['draft_wave'], 'segment': r['segment'],
        'fresh_model_id': r['fresh_model_id'], 'article_family_key': r['article_family_key'],
        'baseline_unique_orders': len({x['order_id'] for x in own}),
        'baseline_ordered_value_rub': str(sum((num(x['ordered_value_rub']) for x in own), D(0))),
        'baseline_cpc_spend_rub': str(sum((num(x['cpc_spend_rub']) for x in av), D(0))),
        'baseline_ad_views': str(sum((num(x['ad_views']) for x in av), D(0))),
        'baseline_ad_clicks': str(sum((num(x['ad_clicks']) for x in av), D(0)))})
write('sku_baseline_28days.csv', summary)
save('coverage.json', {'captured_at_utc': now(), 'from_msk': str(START), 'through_msk': str(END), 'full_days': 28,
    'all_9242_sku_rows': len(summary), 'fresh_fbs_fbo_orders': True, 'late_orders_and_cancellations_recomputed': True,
    'ad_history_through_4Nov_reused': True, '5Nov_fast_statistics_refreshed': True,
    'CPO_cost_unknown_not_zero': True, 'actual_launch_date': None,
    'refresh_before_actual_activation_if_date_changes': True, 'marketplace_mutations': 0, 'database_writes': 0})
print('BASELINE COMPLETE', START, END, len(summary), 'SKU', flush=True)
