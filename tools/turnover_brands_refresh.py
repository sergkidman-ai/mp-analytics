# поток: fin
"""Дополнить бренды только товаров из отчётов роялти. Полный каталог не загружается."""
import json
from pathlib import Path
import sys

import requests

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from core import db
from collectors.ozon import _headers
from collectors.ozon_attributes import load_raw


def refresh():
    needed = db.query("""select distinct r.account, item->'item'->>'offer_id' offer_id
        from raw_ozon_realization r
        cross join lateral jsonb_array_elements(r.payload->'rows') item
        where make_date(year,month,1)>='2026-07-01'
          and nullif(item->'item'->>'offer_id','') is not null
          and not exists (select 1 from raw_ozon_attributes a where a.account=r.account
                          and a.offer_id=item->'item'->>'offer_id')
        order by 1,2""")
    stats = {'needed': len(needed), 'saved': 0, 'unavailable': 0, 'errors': 0}
    for account in sorted({r['account'] for r in needed}):
        offers = [r['offer_id'] for r in needed if r['account'] == account]
        for start in range(0, len(offers), 20):
            batch = offers[start:start + 20]
            try:
                response = requests.post('https://api-seller.ozon.ru/v4/product/info/attributes',
                    headers=_headers(account), json={'filter': {'offer_id': batch, 'visibility': 'ALL'},
                    'limit': 20, 'sort_dir': 'ASC'}, timeout=45)
                response.raise_for_status()
                items = response.json().get('result') or []
                returned = [item.get('offer_id') for item in items]
                if len(set(returned)) != len(returned) or not set(returned).issubset(batch):
                    raise ValueError('Unexpected Ozon product identity')
                if items:
                    load_raw(account, items)
                stats['saved'] += len(items)
                stats['unavailable'] += len(batch) - len(items)
            except (requests.RequestException, ValueError):
                # Финансовые отчёты остаются доступны; UI явно покажет неизвестные бренды.
                # Секреты и ответы API в журнал не попадают. Следующий штатный запуск повторит.
                stats['errors'] += 1
                break
    return stats


if __name__ == '__main__':
    print(json.dumps(refresh(), ensure_ascii=False))
