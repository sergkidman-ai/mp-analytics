"""Read-only, fully paginated native marketplace catalog recovery."""
import sys
import time
import json
from pathlib import Path
from psycopg2.extras import Json

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from collectors import feedback_recovery as cr
from collectors import wb_card_content as wc, ozon_attributes as oa
from collectors.ozon import _headers
from core import db

report = []


def wb(account):
    cursor, seen, total = {'limit': 100}, set(), 0
    checkpoint = Path('/root/rev-recovery/catalog-cursor-' + account + '.json')
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text())
        if not saved.get('done'):
            cursor = saved['cursor']
    cursor['limit'] = 100
    while True:
        data = cr.read('POST', wc.CARDS_URL, headers={'Authorization': wc._content_token(account)},
                       json={'settings': {'cursor': cursor, 'filter': {'withPhoto': -1}}})
        cards = data.get('cards') or []
        wc.load_raw(account, cards)
        rows = [{'account': account, 'nm_id': c['nmID'], 'vendor_code': c.get('vendorCode'),
                 'title': c.get('title'), 'brand': c.get('brand'), 'subject': c.get('subjectName')} for c in cards]
        if rows:
            db.upsert('wb_cards', rows, ['account', 'nm_id'], ['vendor_code', 'title', 'brand', 'subject'])
        total += len(rows)
        if len(cards) < 100:
            checkpoint.write_text(json.dumps({'done':True,'cursor':cursor}))
            break
        cur = data.get('cursor') or {}
        marker = (cur.get('updatedAt'), cur.get('nmID'))
        if marker in seen or not all(marker):
            raise RuntimeError('RepeatedOrMissingCursor')
        seen.add(marker)
        cursor = {'limit': 100, 'updatedAt': marker[0], 'nmID': marker[1]}
        checkpoint.write_text(json.dumps({'done':False,'cursor':cursor}))
        time.sleep(0.35)
    return total


def ozon(account):
    total = 0
    for visibility in ['ALL', 'ARCHIVED']:
        last, seen = '', set()
        while True:
            data = cr.read('POST', oa.ATTR_URL, headers=_headers(account),
                           json={'filter': {'visibility': visibility}, 'limit': 200, 'last_id': last, 'sort_dir': 'ASC'})
            items = data.get('result') or []
            oa.load_raw(account, items)
            products = [{'account': account, 'sku': str(c['sku']), 'offer_id': str(c['offer_id']),
                         'name': c.get('name'), 'is_archived': visibility == 'ARCHIVED'} for c in items if c.get('sku') and c.get('offer_id')]
            if products:
                db.upsert('ozon_product', products, ['account', 'sku'], ['offer_id', 'name', 'is_archived'])
            total += len(items)
            last = data.get('last_id') or ''
            if len(items) < 200 or not last:
                break
            if last in seen:
                raise RuntimeError('RepeatedCursor')
            seen.add(last)
            time.sleep(0.35)
    return total


def yandex():
    key, business = cr.yq._cfg()
    token, seen, total = None, set(), 0
    while True:
        params = {'limit': 200}
        if token:
            params['page_token'] = token
        data = cr.read('POST', cr.yq.API + '/v2/businesses/' + business + '/offer-mappings',
                       headers={'Api-Key': key}, params=params, json={}).get('result') or {}
        items = data.get('offerMappings') or []
        rows = [{'account': 'ya_acc1', 'offer_id': str(c['offer']['offerId']), 'payload': Json(c)}
                for c in items if (c.get('offer') or {}).get('offerId')]
        if rows:
            db.upsert('raw_yandex_offer', rows, ['account', 'offer_id'], ['payload'])
        total += len(rows)
        token = (data.get('paging') or {}).get('nextPageToken')
        if not items or not token:
            break
        if token in seen:
            raise RuntimeError('RepeatedCursor')
        seen.add(token)
        time.sleep(0.35)
    return total


if __name__ == '__main__':
    for label, fn in [('wb_acc1', lambda: wb('wb_acc1')), ('wb_acc2', lambda: wb('wb_acc2')),
                      ('oz_acc1', lambda: ozon('oz_acc1')), ('oz_acc2', lambda: ozon('oz_acc2')), ('ya_acc1', yandex)]:
        try:
            row = {'channel': label, 'status': 'ok', 'rows': fn()}
        except Exception as error:
            row = {'channel': label, 'status': 'failed', 'error': type(error).__name__}
        report.append(row)
        Path('/root/rev-recovery/catalog-progress.json').write_text(json.dumps(report, indent=2))
        print(json.dumps(row), flush=True)
