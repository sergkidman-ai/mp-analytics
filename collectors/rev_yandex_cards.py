# поток: rev
"""Refresh only native Market offers referenced by active unanswered cases."""
import datetime
import os

import requests
from psycopg2.extras import Json
from core import db


def refresh():
    ids = [r['item_id'] for r in db.query('''SELECT DISTINCT f.item_id
        FROM raw_feedback f LEFT JOIN raw_yandex_offer o
          ON o.account=f.account AND o.offer_id=f.item_id
        WHERE f.platform='yandex' AND f.account='ya_acc1'
          AND NOT f.is_answered AND NOT coalesce(f.posted_ok,false)
          AND coalesce(f.recovery_active_unanswered,true) AND nullif(trim(f.item_id),'') IS NOT NULL
          AND (o.loaded_at IS NULL OR o.loaded_at < now()-interval '6 hours')''')]
    result = {'requested':len(ids), 'refreshed':0, 'missing':0, 'errors':[]}
    key, biz = os.getenv('YANDEX_API_KEY_ACC1'), os.getenv('YANDEX_BUSINESS_ID_ACC1')
    if ids and not (key and biz):
        result['errors'].append('configuration_missing')
        return result
    for offset in range(0,len(ids),20):
        batch = ids[offset:offset+20]
        try:
            response = requests.post(f'https://api.partner.market.yandex.ru/v2/businesses/{biz}/offer-mappings',
                headers={'Api-Key':key}, params={'limit':100}, json={'offerIds':batch}, timeout=40)
            if not response.ok:
                result['errors'].append(f'HTTP_{response.status_code}')
                continue
            mappings = response.json().get('result',{}).get('offerMappings',[])
            found = set()
            now = datetime.datetime.now(datetime.timezone.utc)
            for mapping in mappings:
                code = str((mapping.get('offer') or {}).get('offerId') or '')
                if code not in batch:
                    continue
                db.upsert('raw_yandex_offer',[dict(account='ya_acc1',offer_id=code,
                          payload=Json(mapping),loaded_at=now)],['account','offer_id'],['payload','loaded_at'])
                found.add(code)
            result['refreshed'] += len(found)
            result['missing'] += len(set(batch)-found)
        except (requests.RequestException, ValueError) as error:
            result['errors'].append(type(error).__name__)
    return result
