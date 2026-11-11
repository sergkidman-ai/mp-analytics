# поток: mkt / ozon
"""Read missing annual campaign-day aggregates through the existing ads client; no DB writes."""
import calendar
import datetime as dt
import json
import pathlib
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, '/opt/mp-analytics')
import requests
from collectors import ozon_ads as ads

OUT = pathlib.Path(__file__).parent
RAW = OUT / 'raw'
RAW.mkdir(exist_ok=True)
campaigns = json.loads((OUT.parent / 'ozon_full_inventory_20261105/current_campaigns.json').read_text())['campaigns']
ids = [str(c['id']) for c in campaigns]
original = requests.sessions.Session.request
window = ''
sequence = 0


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def guarded(self, method, url, **kw):
    global sequence
    method = method.upper()
    assert (method == 'POST' and url == ads.PERF + '/api/client/token') or (method == 'GET' and url == ads.PERF + '/api/client/statistics/daily/json')
    r = original(self, method, url, **kw)
    record = {'at_utc': now(), 'method': method, 'path': url[len(ads.PERF):], 'http': r.status_code}
    if method == 'GET':
        sequence += 1
        record['params'] = kw.get('params')
        filename = RAW / f'campaign_daily_{window}_{sequence:03}.json'
        filename.write_text(json.dumps({**record, 'body': r.json()}, ensure_ascii=False, indent=2) + '\n')
        record['response_file'] = str(filename.relative_to(OUT))
    with (OUT / 'read_requests.jsonl').open('a') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')
    r.raise_for_status()
    return r


requests.sessions.Session.request = guarded
headers = {'Authorization': 'Bearer ' + ads._token('oz_acc1')}
windows = [(dt.date(2025, 11, 5), dt.date(2025, 11, 30)), (dt.date(2025, 12, 1), dt.date(2025, 12, 31))]
windows += [(dt.date(2026, m, 1), dt.date(2026, m, calendar.monthrange(2026, m)[1])) for m in range(1, 10)]
windows += [(dt.date(2026, 10, 1), dt.date(2026, 10, 1))]
coverage = []
for start, end in windows:
    window = start.isoformat() + '_' + end.isoformat()
    marker = OUT / ('window_' + window + '.json')
    if marker.exists():
        coverage.append(json.loads(marker.read_text()))
        continue
    per_campaign, per_day = ads._daily(headers, ids, start, end)
    record = {'account': 'oz_acc1', 'date_from': start.isoformat(), 'date_to': end.isoformat(),
              'campaign_ids_requested': ids, 'campaigns_with_rows': len(per_campaign),
              'days_with_campaign_rows': len(per_day), 'captured_at_utc': now()}
    marker.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    coverage.append(record)
    print('Annual campaign aggregates', window, 'days with rows', len(per_day), 'campaigns with rows', len(per_campaign), flush=True)
(OUT / 'campaign_collection_coverage.json').write_text(json.dumps({'account': 'oz_acc1',
    'requested_year': ['2025-11-05', '2026-11-04'], 'new_windows': coverage,
    'reused_periods': ['2026-10-02..2026-10-31 from ozon_recovery_20261103/raw/daily_*',
                       '2026-10-31..2026-11-04 from account_comparison/api_response_*'],
    'scope_caveat': 'current and retained historical campaign IDs only; deleted campaign completeness not proven',
    'marketplace_mutations': 0, 'database_data_writes': 0}, ensure_ascii=False, indent=2) + '\n')
print('ANNUAL CAMPAIGN COLLECTION COMPLETE', flush=True)
