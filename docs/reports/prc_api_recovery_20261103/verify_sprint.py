# поток: prc — независимая проверка результата штатного загрузчика.
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
from prices import solutionsprint as loader
from prices.profiles import STORE_REMOTE, ORG_DIGITAL

ROOT = Path(__file__).resolve().parent


def signature(rows):
    return Counter((p['assortment']['meta']['href'].rstrip('/').split('/')[-1],
                    Decimal(str(p['quantity'])).normalize(), int(p['price'])) for p in rows)


def main():
    state = json.loads(loader.STATE.read_text())
    assert state['status'] == 'complete', 'Loader did not finish'
    plan = json.loads((Path(state['out']) / 'plan.json').read_text())
    current = loader.existing_docs()
    ids = set(state['created'].values())
    assert {d['id'] for d in current} == ids
    old_ids = {d['id'] for d in plan['stale']}
    assert not old_ids.intersection(ids)
    out = ROOT / 'verified_sprint_documents'
    out.mkdir(exist_ok=True)

    def verify(item):
        index, spec = item
        ident = state['created'][str(index)]
        doc = ms_api.get('/entity/enter/' + ident, {'expand': 'positions'})
        rows = doc['positions'].get('rows', [])
        count = doc['positions']['meta']['size']
        if len(rows) != count:
            rows = list(ms_api.iter_rows('/entity/enter/' + ident + '/positions', page=1000))
            doc['positions']['rows'] = rows
        assert len(rows) == count
        assert doc['name'] == spec['final_name'] and doc['applicable'] is True
        assert ms_api.meta_id(doc, 'store') == STORE_REMOTE
        assert ms_api.meta_id(doc, 'organization') == ORG_DIGITAL
        assert signature(rows) == signature(spec['positions']), 'Position mismatch'
        p = out / (ident + '.json')
        p.write_text(json.dumps(doc, ensure_ascii=False))
        p.chmod(0o600)
        return {'id': ident, 'name': doc['name'], 'positions': count}

    with ThreadPoolExecutor(max_workers=3) as pool:
        documents = list(pool.map(verify, enumerate(plan['docs'])))
    for ident in old_ids:
        try:
            ms_api.get('/entity/enter/' + ident)
        except ms_api.MsError as exc:
            assert exc.status == 404
        else:
            raise AssertionError('Previous enter still exists: ' + ident)
    for patch in plan['updates']:
        card = ms_api.get('/entity/product/' + patch['id'])
        assert ms_api.meta_id(card, 'supplier') == loader.SUPPLIER
        if 'buyPrice' in patch:
            assert int(card['buyPrice']['value']) == patch['buyPrice']['value']
            assert ms_api.meta_id(card['buyPrice'], 'currency') == loader.RUB
        if 'description' in patch:
            assert (card.get('description') or '').replace('\xa0', ' ').strip() == patch['description'].replace('\xa0', ' ').strip()
    load = query('SELECT supplier_key,status,source_kind,dry_run,docs,rows_loaded,card_updates FROM prc_price_load WHERE id=%s', (state['load_id'],))[0]
    assert load['supplier_key'] == 's_print_msk' and load['status'] == 'ok'
    assert load['source_kind'] == 'cabinet' and not load['dry_run']
    assert load['docs'] == len(documents) and load['rows_loaded'] == sum(d['positions'] for d in documents)
    result = {'verified_at': datetime.now(timezone.utc).isoformat(), 'status': 'verified',
              'documents': documents, 'documents_total': len(documents),
              'positions_total': sum(d['positions'] for d in documents),
              'ids_quantities_prices_match': True, 'old_documents_remaining': 0,
              'old_documents_removed': len(old_ids), 'card_changes_verified': len(plan['updates']),
              'load_id': state['load_id'], 'source_collected_at': plan['source_at'], 'load': load}
    (ROOT / 'sprint_independent_verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in result.items() if k != 'documents'}, ensure_ascii=False))


if __name__ == '__main__':
    main()
