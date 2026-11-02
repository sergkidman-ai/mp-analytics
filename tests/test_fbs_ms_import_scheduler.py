# поток: inv
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from ops.fbs_ms_import_scheduler import cancelled, exact_ms_numbers, permitted, recoverable_import_race
from ops.fbs_ms_import_next5 import source
from ops.fbs_ms_import_wb_pilot import current_purchase_prices
from ops import fbs_ms_import_scheduler as scheduler


class SchedulerSafetyTests(unittest.TestCase):
    def test_unpaid_yandex_is_importable_and_cancellations_are_not(self):
        self.assertTrue(permitted({'platform': 'yandex', 'status': ['UNPAID', 'WAITING_USER_INPUT']}))
        self.assertFalse(permitted({'platform': 'yandex', 'status': ['CANCELLED', 'USER_CHANGED_MIND']}))
        self.assertTrue(cancelled({'platform': 'wb', 'status': ['new', 'declined_by_client']}))
        self.assertTrue(cancelled({'platform': 'ozon', 'status': 'cancelled'}))

    def test_duplicate_ms_number_stops_before_any_import(self):
        rows = [{'number': '123'}]
        response = {'meta': {'size': 2}, 'rows': [{'name': '123'}, {'name': '123'}]}
        with patch('ops.fbs_ms_import_scheduler.ms_api.get', return_value=response):
            with self.assertRaisesRegex(RuntimeError, 'duplicate_ms_number'):
                exact_ms_numbers(rows)

    def test_pre_post_status_race_is_skipped_but_uncertain_post_is_not(self):
        self.assertEqual(recoverable_import_race(RuntimeError('ozon_status_changed')),
                         'market_status_changed')
        self.assertEqual(recoverable_import_race(RuntimeError('already_in_ms')),
                         'staff_created')
        self.assertIsNone(recoverable_import_race(RuntimeError('post_uncertain_no_retry')))

    def test_yandex_two_units_use_confirmed_line_total_rule(self):
        order = {'orderId': 63905348034, 'status': 'PROCESSING', 'substatus': 'STARTED',
                 'programType': 'FBS', 'fake': False, 'campaignId': 148691041,
                 'creationDate': '2026-11-02T12:00:00+03:00',
                 'items': [{'offerId': '6238', 'count': 2, 'prices': {
                     'payment': {'value': 3158, 'currencyId': 'RUR'},
                     'subsidy': {'value': 3138, 'currencyId': 'RUR'}}}]}
        class Response:
            def raise_for_status(self):
                pass
            def json(self):
                return {'orders': [order]}
        row = {'platform': 'yandex', 'account': 'ya_acc1', 'number': '63905348034',
               'campaign_id': 148691041}
        with patch('ops.fbs_ms_import_next5.requests.post', return_value=Response()):
            result = source(row, {})
        self.assertEqual(result['lines'][0]['quantity'], 2)
        self.assertEqual(result['lines'][0]['price_kopeks'], 314800)

    def test_full_receipt_price_cache_ignores_unrelated_invalid_or_conflicting_cards(self):
        docs = [{'id': 'one', 'name': 'rapid_2026-11-02_p1', 'updated': 'v1'},
                {'id': 'two', 'name': 'easy_print_2026-11-02_p1', 'updated': 'v1'}]
        def position(pid, price):
            return {'assortment': {'meta': {'href': 'https://example/product/' + pid}},
                    'price': price}
        def get(path, params=None):
            if path == '/entity/enter':
                return {'meta': {'size': 2}, 'rows': docs}
            rows = ([position('good', 100), position('conflict', 200), position('bad', 0)]
                    if path.endswith('/one/positions') else [position('conflict', 300)])
            return {'meta': {'size': len(rows)}, 'rows': rows}
        with patch('ops.fbs_ms_import_wb_pilot.ms_api.get', side_effect=get):
            prices, digest = current_purchase_prices(None)
        self.assertEqual(prices, {'good': 100})
        self.assertEqual(len(digest), 64)

    def test_post_intent_error_stops_remaining_batch(self):
        rows = [{'number': n, 'platform': 'ozon', 'account': 'oz_acc1',
                 'status': 'awaiting_packaging'} for n in ('1-1-1', '2-2-2')]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            def uncertain(row, *_):
                folder = root / ('next5_' + row['number'])
                folder.mkdir()
                (folder / 'intent.json').write_text('{}')
                raise RuntimeError('post_uncertain_no_retry')
            with patch.object(scheduler, 'OUT', root), \
                 patch.object(scheduler, 'RUNTIME', root / 'schedule'), \
                 patch.object(scheduler, 'LOCK', root / 'schedule' / 'lock'), \
                 patch.object(scheduler, 'KNOWN', root / 'schedule' / 'known.json'), \
                 patch.object(scheduler, 'STATE', root / 'schedule' / 'state.json'), \
                 patch.object(scheduler, 'source_rows', return_value=(rows, 'start', 'end')), \
                 patch.object(scheduler, 'known_ms_numbers', return_value=set()), \
                 patch.object(scheduler, 'exact_ms_numbers', return_value=set()), \
                 patch.object(scheduler, 'prices_for', return_value=({}, 'digest')), \
                 patch.object(scheduler, 'enter_digest', return_value='digest'), \
                 patch.object(scheduler, 'find_customerorder', return_value=None), \
                 patch('ops.fbs_ms_import_next5.create_one', side_effect=uncertain) as create:
                result = scheduler.run(True)
            self.assertEqual(create.call_count, 1)
            self.assertEqual(len(result['errors']), 1)
            self.assertFalse(result['cursor_advanced'])


if __name__ == '__main__':
    unittest.main()
