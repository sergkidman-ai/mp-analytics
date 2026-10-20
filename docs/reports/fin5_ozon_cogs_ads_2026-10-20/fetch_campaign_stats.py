# поток: fin
"""Read-only campaign outcome exports for identical 01–17 September/October."""
import concurrent.futures
import csv
import io
import json
from pathlib import Path

import requests
from collectors import ozon_ads as ads

OUT = Path(__file__).resolve().parent
FUNDING = OUT.parent / 'fin5_ozon_oct_vs_sep_2026-10-20'


def fetch(account):
    headers = {'Authorization': 'Bearer ' + ads._token(account)}
    response = requests.get(ads.PERF + '/api/client/campaign', headers=headers, timeout=45)
    response.raise_for_status()
    (OUT / f'{account}_campaign_metadata.json').write_bytes(response.content)
    collected = []
    for month in ('2026-09', '2026-10'):
        funding_name = f'{account}_{month}_01_17_expense.csv'
        raw = (FUNDING / funding_name).read_bytes()
        (OUT / funding_name).write_bytes(raw)
        rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig')), delimiter=';'))
        ids = sorted({x['ID'] for x in rows}, key=int)
        for i in range(0, len(ids), 10):
            response = requests.get(ads.PERF + '/api/client/statistics/daily/json',
                headers=headers, params={'campaignIds': ids[i:i+10],
                'dateFrom': month+'-01', 'dateTo': month+'-17'}, timeout=45)
            if response.status_code != 200:
                raise RuntimeError(f'{account} {month} daily status={response.status_code}')
            name = f'{account}_{month}_01_17_daily_{i//10}.json'
            (OUT / name).write_bytes(response.content)
            collected.append({'account': account, 'month': month,
                              'ids': ids[i:i+10], 'rows': len(response.json().get('rows', [])),
                              'file': name})
    return collected


if __name__ == '__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(fetch, ('oz_acc1', 'oz_acc2')))
    (OUT / 'fetch_summary.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print('Saved campaign metadata and daily outcome exports:', results)
