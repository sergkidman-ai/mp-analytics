# поток: mkt / WB read-only audit
"""Offline per-card spend/general-orders analysis; attribution is not incremental lift."""
import collections
import csv
import datetime as dt
from decimal import Decimal as D
import json
from pathlib import Path

P = Path(__file__).parent
load = lambda file: json.loads(file.read_text())
save = lambda file, z: file.write_text(json.dumps(z, ensure_ascii=False, indent=2, default=str) + '\n')
details = load(P / 'current_campaigns.json')['campaigns']
members = collections.defaultdict(list)
for c in details:
    for nm in c['nm_settings']:
        members[int(nm['nm_id'])].append({'campaign_id': c['id'], 'campaign_state': c['status'],
                                        'payment_type': c['settings']['payment_type'], 'bid_type': c['bid_type'],
                                        'search_bid_kopecks': nm['bids_kopecks'].get('search'),
                                        'recommendations_bid_kopecks': nm['bids_kopecks'].get('recommendations')})
daily = collections.defaultdict(lambda: {k: D(0) for k in ['spend', 'ad_revenue', 'ad_orders', 'clicks', 'views', 'carts', 'ad_canceled']})
campaigns = []
seen_campaigns = set()
for file in sorted(P.glob('fullstats_batch_*.json')):
    z = load(file)
    assert z['account'] == 'wb_acc1'
    for c in z['response']:
        cid = int(c['advertId'])
        assert cid not in seen_campaigns
        seen_campaigns.add(cid)
        campaigns.append({'campaign_id': cid, 'spend_rub': D(str(c['sum'])), 'ad_revenue_rub': D(str(c['sum_price'])),
                          'ad_orders': c['orders'], 'views': c['views'], 'clicks': c['clicks']})
        for day in c.get('days') or []:
            date = day['date'][:10]
            assert '2026-10-12' <= date <= '2026-11-08'
            for app in day.get('apps') or []:
                for nm in app.get('nms') or []:
                    row = daily[date, cid, int(nm['nmId'])]
                    for ours, source in [('spend', 'sum'), ('ad_revenue', 'sum_price'), ('ad_orders', 'orders'),
                                         ('clicks', 'clicks'), ('views', 'views'), ('carts', 'atbs'), ('ad_canceled', 'canceled')]:
                        row[ours] += D(str(nm.get(source) or 0))

orders = load(P / 'general_orders_raw.json')['orders']
latest = {}
for row in orders:
    assert row.get('srid'), 'Missing unique general order identity'
    key = str(row['srid'])
    if key not in latest or row['lastChangeDate'] > latest[key]['lastChangeDate']:
        latest[key] = row
all_nms = set(members) | {key[2] for key in daily}
ads_by_nm = collections.defaultdict(list)
orders_by_nm = collections.defaultdict(list)
for (date, cid, nm), values in daily.items():
    ads_by_nm[nm].append((date, values))
for row in latest.values():
    if not row['isCancel']:
        orders_by_nm[int(row['nmId'])].append(row)
per_nm = {}
for nm in all_nms:
    r = {'account': 'wb_acc1', 'nm_id': nm, 'current_placements': members[nm]}
    for suffix, start in [('28d', '2026-10-12'), ('7d', '2026-11-02'), ('prior7d', '2026-10-26')]:
        end = '2026-11-01' if suffix == 'prior7d' else '2026-11-08'
        ads = [v for date, v in ads_by_nm[nm] if start <= date <= end]
        for k in ['spend', 'ad_revenue', 'ad_orders', 'clicks', 'views', 'carts', 'ad_canceled']:
            r[k + '_' + suffix] = str(sum((x[k] for x in ads), D(0)))
        r['ad_drr_' + suffix] = str(D(r['spend_' + suffix]) / D(r['ad_revenue_' + suffix]) * 100) if D(r['ad_revenue_' + suffix]) else None
        r['actual_CPC_' + suffix] = str(D(r['spend_' + suffix]) / D(r['clicks_' + suffix])) if D(r['clicks_' + suffix]) else None
        own = [x for x in orders_by_nm[nm] if start <= x['date'][:10] <= end]
        r['general_orders_' + suffix] = len(own)
        r['general_ordered_value_' + suffix] = str(sum((D(str(x['priceWithDisc'])) for x in own), D(0)))
    per_nm[nm] = r
save(P / 'per_nm_ads_orders.json', {'account': 'wb_acc1', 'from_msk': '2026-10-12', 'through_msk': '2026-11-08', 'cards': per_nm})
save(P / 'daily_ad_rows.json', [{'date_msk': day, 'campaign_id': cid, 'nm_id': nm, **values} for (day, cid, nm), values in daily.items()])
campaign_spend = sum((c['spend_rub'] for c in campaigns), D(0))
nm_spend = sum((v['spend'] for v in daily.values()), D(0))
scope = load(P / 'stats_scope.json')
save(P / 'ad_coverage.json', {'account': 'wb_acc1', 'campaigns_requested': len(scope['campaign_ids']), 'campaigns_returned': len(seen_campaigns),
     'omitted_campaign_ids_not_assumed_zero': sorted(set(scope['campaign_ids']) - seen_campaigns),
     'campaign_spend_rub': str(campaign_spend), 'nm_spend_rub': str(nm_spend), 'difference_not_allocated_rub': str(campaign_spend - nm_spend),
     'unique_cards_with_stats': len({k[2] for k in daily}), 'daily_nm_campaign_rows': len(daily),
     'general_orders_unique_raw': len(latest), 'database_writes': 0, 'marketplace_mutations': 0})
preliminary = [r for r in per_nm.values() if any(x['campaign_state'] == 9 for x in r['current_placements']) and D(r['spend_28d']) >= 300]
preliminary.sort(key=lambda x: D(x['spend_28d']), reverse=True)
save(P / 'preliminary_top_spend.json', preliminary[:60])
print(json.dumps({'campaign_spend_rub': str(campaign_spend), 'SKU_spend_rub': str(nm_spend),
                  'campaigns_returned': len(seen_campaigns), 'unique_cards_with_stats': len({k[2] for k in daily}),
                  'current_spend_over300_cards': len(preliminary)}, ensure_ascii=False))
for r in preliminary[:15]:
    print(r['nm_id'], 'spend', r['spend_28d'], 'adOrders', r['ad_orders_28d'], 'adDRR', r['ad_drr_28d'],
          'generalOrders', r['general_orders_28d'], 'currentBid', [x['search_bid_kopecks'] for x in r['current_placements'] if x['campaign_state'] == 9])
