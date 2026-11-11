# поток: prc — независимая проверка восстановления; загрузку выполняет штатный обработчик.
import json
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, '/opt/mp-analytics')
from core import ms_api
from core.db import query
from prices.loader import existing_docs
from prices.profiles import get_profile, STORE_REMOTE, ORG_DIGITAL

ROOT = Path(__file__).resolve().parent
OUT = ROOT / 'after_enters'
OUT.mkdir(exist_ok=True)


def verify(key):
    loads = query("SELECT * FROM prc_price_load WHERE supplier_key=%s AND NOT dry_run AND status='ok' ORDER BY id DESC LIMIT 1", (key,))
    assert loads, key + ': no successful live load'
    load = loads[0]
    documents = existing_docs(get_profile(key))
    assert len(documents) == load['docs'], key + ': unexpected documents'
    old = {d['id'] for s in json.loads((ROOT / 'before_enters.json').read_text()) if s['supplier'] == key for d in s['documents']}
    assert not old.intersection(d['id'] for d in documents), key + ': old documents remain'
    assert all('2026-11-03' in d['name'] and '_old' not in d['name'] for d in documents)

    def fetch(d):
        full = ms_api.get('/entity/enter/' + d['id'], {'expand': 'positions'})
        pos = full.get('positions', {})
        rows = pos.get('rows', [])
        size = pos.get('meta', {}).get('size')
        if len(rows) != size:
            rows = list(ms_api.iter_rows('/entity/enter/' + d['id'] + '/positions', page=1000))
            full['positions']['rows'] = rows
        assert len(rows) == size
        assert ms_api.meta_id(full, 'store') == STORE_REMOTE
        assert ms_api.meta_id(full, 'organization') == ORG_DIGITAL
        assert full.get('applicable') is True
        path = OUT / (d['id'] + '.json')
        path.write_text(json.dumps(full, ensure_ascii=False))
        path.chmod(0o600)
        return rows

    with ThreadPoolExecutor(max_workers=3) as pool:
        positions = [p for rows in pool.map(fetch, documents) for p in rows]
    actual = Counter((p['assortment']['meta']['href'].rstrip('/').split('/')[-1], Decimal(str(p['quantity'])).normalize(), int(p['price'])) for p in positions)
    rows = query("SELECT ms_id,qty,price_rub FROM prc_price_row WHERE load_id=%s AND status='loaded'", (load['id'],))
    expected = Counter((r['ms_id'], Decimal(str(r['qty'])).normalize(), int(r['price_rub'] * 100)) for r in rows)
    assert actual == expected, key + ': position, quantity or price mismatch'
    state = json.loads((Path('/opt/mp-analytics/logs') / ('prc_price_watch_' + key + '.json')).read_text())
    assert state['result'] == 0
    assert load['source_kind'] == 'mail' and load['card_updates'] == 0
    result = {'supplier': key, 'verified_at': datetime.now(timezone.utc).isoformat(), 'load_id': load['id'], 'documents': len(documents), 'positions': len(positions), 'all_ids_quantities_prices_match': True, 'old_documents_remaining': 0, 'card_updates': 0, 'document_ids': [d['id'] for d in documents], 'source_file': load['source_file']}
    (ROOT / (key + '_verification.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != 'document_ids'}, ensure_ascii=False), flush=True)
    return result


if __name__ == '__main__':
    for supplier in sys.argv[1:]:
        verify(supplier)
