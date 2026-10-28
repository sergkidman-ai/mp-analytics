from datetime import date, datetime, timezone

from collectors.market_ms_return_receipts import normalize, scan_filter
from shipment_guard import config
from web.mp_inventory_api import market_receipt_result


def _doc(**changes):
    agent = next(uid for uid, platform in config.AGENTS.items() if platform == 'yandex')
    org = next(uid for uid, value in config.ORGS.items() if value[0] == '1')
    result = dict(id='ef86f277-4569-4434-826a-f4fb7e939c81',
                  agent={'meta': {'href': f'{config.MS}/entity/counterparty/{agent}'}},
                  organization={'meta': {'href': f'{config.MS}/entity/organization/{org}'}},
                  demand={'name': '63506959552', 'meta': {'href':
                          f'{config.MS}/entity/demand/a20c298a-25d8-4971-bc14-9f0ca49a7611'}},
                  store={'name': 'Звездный'}, applicable=True,
                  moment='2026-10-27 16:28:00.000')
    result.update(changes)
    return result


def test_only_explicit_shipment_link_is_retained():
    assert normalize(_doc(demand=None)) is None
    row = normalize(_doc(store={'name': 'Неизвестный'}))
    assert row['order_number'] == '63506959552'
    assert row['store_name'] == 'Неизвестный'
    assert row['account'] == 'ya_acc1'


def test_receipt_requires_posting_own_store_and_matching_shipment():
    row = dict(salesreturn_id='return', demand_id='demand', known_demand_id='demand',
               applicable=True, store_name='Звездный',
               created_at=datetime(2026, 10, 26, tzinfo=timezone.utc))
    assert market_receipt_result(row, date(2025, 1, 1)) == 'received'
    assert market_receipt_result({**row, 'applicable': False}, date(2025, 1, 1)) == 'draft'
    assert market_receipt_result({**row, 'store_name': 'Озон'}, date(2025, 1, 1)) == 'other_store'
    assert market_receipt_result({**row, 'known_demand_id': 'another'}, date(2025, 1, 1)) == 'mismatch'
    assert market_receipt_result({**row, 'salesreturn_id': None}, date(2025, 1, 1)) == 'waiting'
    assert market_receipt_result({**row, 'salesreturn_id': None}, date(2026, 10, 28)) == 'unknown'


def test_incremental_scan_uses_updated_documents():
    agent = next(uid for uid, platform in config.AGENTS.items() if platform == 'yandex')
    text = scan_filter(agent, date(2025, 1, 1), datetime(2026, 10, 28, 15, tzinfo=timezone.utc))
    assert 'updated>=2026-10-26 15:00:00' in text
    assert 'moment>=' in scan_filter(agent, date(2025, 1, 1), None)
