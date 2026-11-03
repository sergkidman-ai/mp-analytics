# поток: inv
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ops import ms_supply_fulfillment as worker

NOW = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)
ORG = 'a5e7ee84-8d63-41f2-9fae-0be2ef90462b'
AGENT = 'b7e22828-0b7c-11ee-0a80-11480012ed47'


def uid(number):
    return '00000000-0000-0000-0000-' + str(number).zfill(12)


def ref(kind, value):
    return worker.ms_api.ref(kind, value)


def position(product=201, quantity=1, reserve=1, external='4001', price=10000):
    card = {**ref('product', uid(product)), 'id': uid(product), 'externalCode': external}
    return {'assortment': card, 'quantity': quantity, 'reserve': reserve,
            'price': price, 'discount': 0, 'vat': 5, 'vatEnabled': True}


def order(number=1, positions=None, store=301, day='2026-11-03'):
    values = positions or [position()]
    return {**ref('customerorder', uid(number)), 'id': uid(number), 'name': 'order-' + str(number),
            'moment': '2026-11-03 08:00:00', 'updated': '2026-11-03 08:00:00',
            'state': {**ref('customerorder/metadata/states', worker.INCOMPLETE), 'name': 'Неукомплектован'},
            'store': ref('store', uid(store)), 'organization': ref('organization', ORG),
            'agent': ref('counterparty', AGENT), 'deliveryPlannedMoment': day + ' 00:00:00',
            'positions': {'meta': {'size': len(values)}, 'rows': values},
            'sum': sum(value['price'] * value['quantity'] for value in values),
            'vatEnabled': True, 'vatIncluded': True, 'vatSum': 476, 'demands': []}


def supply(number=101, positions=None, store=301, updated='2026-11-03 11:59:00'):
    values = positions or [position()]
    return {**ref('supply', uid(number)), 'id': uid(number), 'name': 'supply-' + str(number),
            'updated': updated, 'state': {**ref('supply/metadata/states', worker.ACCEPTED), 'name': 'Принят/ на оплату'},
            'applicable': True, 'store': ref('store', uid(store)),
            'positions': {'meta': {'size': len(values)}, 'rows': values}}


class FakeMS:
    def __init__(self):
        self.supplies = {uid(101): supply()}
        self.orders = {uid(1): order()}
        self.stocks = {uid(201): {'stock': 10, 'reserve': 1}}
        self.demands = {}
        self.calls = []
        self.writes = []

    def get(self, path, params=None):
        params = params or {}
        self.calls.append((path, deepcopy(params)))
        filters = params.get('filter', [])
        if path.startswith('/entity/supply/'):
            return deepcopy(self.supplies[path.rsplit('/', 1)[-1]])
        if path.startswith('/entity/customerorder/'):
            return deepcopy(self.orders[path.rsplit('/', 1)[-1]])
        if path == '/entity/customerorder':
            found = list(self.orders.values())
            names = [value.split('=', 1)[1] for value in filters if value.startswith('name=')]
            if names:
                found = [value for value in found if value['name'] in names]
            if any(value.startswith('state=') for value in filters):
                found = [value for value in found if value['state']['name'] == 'Неукомплектован']
                for value in filters:
                    if value.startswith('store='):
                        found = [entry for entry in found if entry['store']['meta']['href'] == value.split('=', 1)[1]]
                    if value.startswith('deliveryPlannedMoment>='):
                        found = [entry for entry in found if entry['deliveryPlannedMoment'] >= value.split('>=', 1)[1]]
                    if value.startswith('deliveryPlannedMoment<'):
                        found = [entry for entry in found if entry['deliveryPlannedMoment'] < value.split('<', 1)[1]]
            offset = params.get('offset', 0)
            return deepcopy({'meta': {'size': len(found)}, 'rows': found[offset:offset + params['limit']]})
        if path == '/report/stock/all':
            products = [value.split('=', 1)[1].rsplit('/', 1)[-1] for value in filters if value.startswith('product=')]
            found = [{**ref('product', product), **self.stocks.get(product, {'stock': 0, 'reserve': 0})}
                     for product in products]
            return deepcopy({'meta': {'size': len(found)}, 'rows': found})
        if path == '/entity/demand':
            names = [value.split('=', 1)[1] for value in filters if value.startswith('name=')]
            organization = next(value.split('=', 1)[1] for value in filters if value.startswith('organization='))
            found = [value for value in self.demands.values() if value['name'] in names
                     and value['organization']['meta']['href'] == organization]
            return deepcopy({'meta': {'size': len(found)}, 'rows': found[:params['limit']]})
        raise AssertionError('Unexpected GET: ' + path)

    def put(self, path, payload):
        if path != '/entity/demand/new':
            raise AssertionError('Unexpected non-one-shot PUT')
        current = self.orders[worker.ms_api.meta_id(payload, 'customerOrder')]
        template = deepcopy(current)
        template.pop('id')
        template.update(**ref('demand', 'new'), customerOrder=deepcopy(current['meta']))
        template['customerOrder'] = {'meta': deepcopy(current['meta'])}
        return template

    def write(self, folder, label, method, path, body):
        self.writes.append((label, method, path, deepcopy(body)))
        if label == 'status':
            if set(body) != {'state'}:
                raise AssertionError('Status PUT must preserve the plan')
            if body['state']['meta']['type'] != 'state':
                raise AssertionError('State reference must use MS type=state')
            cid = path.split('?', 1)[0].rsplit('/', 1)[-1]
            self.orders[cid]['state'] = {**deepcopy(body['state']), 'name': 'Собирается'}
            return {'status': 200, 'body': deepcopy(self.orders[cid])}
        if label == 'create':
            cid = worker.ms_api.meta_id(body, 'customerOrder')
            current = self.orders[cid]
            did = uid(900 + len(self.demands))
            demand = deepcopy(current)
            demand.update(**ref('demand', did), id=did, name=body['name'], applicable=body['applicable'],
                          customerOrder=ref('customerorder', cid), moment=body['moment'],
                          positions={'meta': {'size': len(body['positions'])}, 'rows': deepcopy(body['positions'])})
            self.demands[did] = demand
            current['demands'].append(ref('demand', did))
            need, own = worker.amounts(current)
            for product, quantity in need.items():
                self.stocks[product]['stock'] -= float(quantity)
                self.stocks[product]['reserve'] -= float(own[product])
            return {'status': 201, 'body': deepcopy(demand)}
        raise AssertionError('Unexpected write label')


class SupplyFulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.runtime = Path(self.temp.name) / 'runtime'
        self.clock = NOW
        self.ms = FakeMS()
        self.addCleanup(patch.stopall)
        patch.object(worker, 'RUNTIME', self.runtime).start()
        patch.object(worker, 'IMPORT_SOURCE', Path(self.temp.name) / 'empty').start()
        patch.object(worker, 'now', side_effect=lambda: self.clock).start()
        patch.object(worker.ms_api, 'get', side_effect=self.ms.get).start()
        patch.object(worker.ms_api, 'put', side_effect=self.ms.put).start()
        self.write = patch.object(worker.batch, 'one_write', side_effect=self.ms.write).start()
        self.market = patch.object(worker, 'market_fulfill', return_value={'result': 'ready', 'reason': 'ok'}).start()

    def run_receipt(self, receipt=101, event='event-1', apply=True):
        return worker.fulfill_receipt(uid(receipt), NOW.isoformat(), apply=apply, event_id=event)

    def advance(self):
        self.clock += timedelta(seconds=301)

    def test_two_stages_share_guard_without_sleep_and_preserve_plan(self):
        first = self.run_receipt()
        self.assertEqual(first['status'], 'continue')
        self.assertGreaterEqual(first['delay_seconds'], 300)
        self.assertEqual(first['budget'], {'receipt': 1, 'candidate_lists': 1, 'stock': 1})
        self.assertEqual(self.ms.orders[uid(1)]['deliveryPlannedMoment'], '2026-11-03 00:00:00')
        self.assertEqual([value[0] for value in self.ms.writes], ['status'])
        self.advance()
        final = self.run_receipt()
        self.assertEqual(final['status'], 'done')
        self.assertEqual([value[0] for value in self.ms.writes], ['status', 'create'])
        self.assertEqual(final['budget']['stock'], 1)
        self.assertEqual(final['budget']['order_batches'], 1)
        self.assertEqual(final['budget']['demand_batches'], 1)
        self.market.assert_called_once()
        self.assertEqual(worker.ledger_load()['active'], {})

    def test_demand_template_can_omit_computed_vat_sum(self):
        self.run_receipt()
        self.advance()
        original_put = self.ms.put

        def uncalculated_template(path, body):
            template = original_put(path, body)
            template.pop('vatSum', None)
            template['sum'] = 0
            return template

        with patch.object(worker.ms_api, 'put', side_effect=uncalculated_template):
            final = self.run_receipt()
        self.assertEqual(final['status'], 'done')
        self.assertEqual([value[0] for value in self.ms.writes], ['status', 'create'])
        self.market.assert_called_once()

    def test_demand_template_still_rejects_changed_vat_mode(self):
        self.run_receipt()
        self.advance()
        original_put = self.ms.put

        def changed_vat_template(path, body):
            template = original_put(path, body)
            template['vatIncluded'] = not template.get('vatIncluded', False)
            return template

        with patch.object(worker.ms_api, 'put', side_effect=changed_vat_template):
            final = self.run_receipt()
        self.assertEqual(final['status'], 'manual')
        self.assertEqual([value[0] for value in self.ms.writes], ['status'])
        self.market.assert_not_called()

    def test_repeated_same_event_never_repeats_reads_or_post(self):
        self.run_receipt()
        calls = len(self.ms.calls)
        self.run_receipt()
        self.assertEqual(len(self.ms.calls), calls)
        self.advance()
        self.run_receipt()
        calls = len(self.ms.calls)
        self.run_receipt()
        self.assertEqual(len(self.ms.calls), calls)
        self.assertEqual([value[0] for value in self.ms.writes].count('create'), 1)

    def test_duplicate_notification_new_event_same_version_only_reads_receipt(self):
        self.run_receipt()
        calls = len(self.ms.calls)
        self.run_receipt(event='duplicate-notification')
        self.assertEqual(len(self.ms.calls), calls + 1)
        self.assertEqual(self.ms.calls[-1][0], '/entity/supply/' + uid(101))
        self.assertEqual([value[0] for value in self.ms.writes].count('status'), 1)

    def test_nonaccepted_or_unposted_supply_does_not_scan_orders(self):
        for receipt, change in [(102, {'applicable': False}), (103, {'state': ref('supply/metadata/states', uid(999))})]:
            self.ms.supplies[uid(receipt)] = {**supply(receipt), **change}
            result = self.run_receipt(receipt, str(receipt))
            self.assertEqual(result['status'], 'done')
            self.assertEqual(result['budget'], {'receipt': 1})
        self.write.assert_not_called()

    def test_nonaccepted_receipt_preview_is_empty_without_key_error(self):
        self.ms.supplies[uid(101)].pop('state')
        self.ms.supplies[uid(101)]['applicable'] = False
        result = self.run_receipt(apply=False)
        self.assertEqual(result['results'], {})
        self.assertEqual(result['budget'], {'receipt': 1})
        self.write.assert_not_called()

    def test_market_seed_index_normalizes_accounts_and_reuses_cached_index(self):
        source = Path(self.temp.name) / 'seeds'
        seed_folder = source / 'next5_example'
        seed_folder.mkdir(parents=True)
        seed = {'platform': 'ozon', 'account': 'acc1', 'number': 'order-1', 'status': 'awaiting_packaging'}
        (seed_folder / 'preflight.json').write_text(json.dumps({'market': seed}))
        with patch.object(worker, 'IMPORT_SOURCE', source):
            first = worker.market_index()
            self.assertEqual(first[('ozon', 'oz_acc1', 'order-1')], seed)
            (seed_folder / 'preflight.json').unlink()
            self.assertIs(worker.market_index(), first)

    def test_only_receipt_warehouse_today_and_known_fbs_accounts(self):
        self.ms.orders[uid(2)] = order(2, store=302)
        self.ms.orders[uid(3)] = order(3, day='2026-11-05')
        self.ms.orders[uid(4)] = order(4)
        self.ms.orders[uid(4)]['agent'] = ref('counterparty', uid(999))
        self.ms.orders[uid(5)] = order(5)
        self.ms.orders[uid(5)]['organization'] = ref('organization', uid(888))
        result = self.run_receipt()
        self.assertEqual(list(result['results']), [uid(1)])
        self.assertEqual([value[0] for value in self.ms.writes], ['status'])

    def test_remote_store_is_not_replaced_with_own_store(self):
        self.ms.supplies[uid(101)] = supply(store=777)
        result = self.run_receipt()
        self.assertEqual(result['status'], 'done')
        self.assertEqual(result['results'], {})
        self.write.assert_not_called()

    def test_intersection_requires_positive_own_reserve(self):
        self.ms.orders[uid(1)] = order(positions=[position(reserve=0)])
        result = self.run_receipt()
        self.assertEqual(result['results'], {})
        self.assertNotIn('stock', result['budget'])
        self.write.assert_not_called()

    def test_exact_four_digit_code_can_select_other_card_but_never_substitutes_it(self):
        self.ms.supplies[uid(101)] = supply(positions=[position(product=202, external='4001')])
        self.ms.stocks[uid(201)] = {'stock': 0, 'reserve': 1}
        result = self.run_receipt()
        self.assertEqual(result['results'][uid(1)]['result'], 'shortage')
        stock_call = next(params for path, params in self.ms.calls if path == '/report/stock/all')
        self.assertIn('product=' + ref('product', uid(201))['meta']['href'], stock_call['filter'])
        self.write.assert_not_called()

    def test_all_set_positions_are_checked_and_shortage_changes_nothing(self):
        self.ms.orders[uid(1)] = order(positions=[position(), position(202, external='4002')])
        self.ms.stocks[uid(202)] = {'stock': 0, 'reserve': 1}
        before = deepcopy(self.ms.orders[uid(1)])
        result = self.run_receipt()
        self.assertEqual(result['results'][uid(1)]['result'], 'shortage')
        self.assertEqual(result['budget']['stock'], 1)
        self.assertEqual(self.ms.orders[uid(1)], before)
        self.write.assert_not_called()

    def test_shared_sku_in_two_jobs_uses_global_partial_reserve_claim(self):
        self.ms.orders[uid(1)] = order(positions=[position(quantity=2, reserve=1), position(202, external='4002')])
        self.ms.orders[uid(2)] = order(2, positions=[position(quantity=2, reserve=1), position(203, external='4003')])
        self.ms.supplies[uid(101)] = supply(positions=[position(202, external='4002')])
        self.ms.supplies[uid(102)] = supply(102, positions=[position(203, external='4003')])
        self.ms.stocks = {uid(201): {'stock': 3, 'reserve': 2}, uid(202): {'stock': 1, 'reserve': 1},
                          uid(203): {'stock': 1, 'reserve': 1}}
        first = self.run_receipt()
        self.assertEqual(first['results'][uid(1)]['result'], 'ms_changed')
        second = self.run_receipt(102, 'event-2')
        self.assertEqual(second['results'][uid(2)]['result'], 'shortage')
        self.assertEqual(self.ms.orders[uid(2)]['state']['name'], 'Неукомплектован')
        self.assertEqual(len(worker.ledger_load()['active']), 1)

    def test_full_own_reserve_is_not_subtracted_twice_across_jobs(self):
        self.ms.orders[uid(1)] = order(positions=[position(quantity=2, reserve=2), position(202, external='4002')])
        self.ms.orders[uid(2)] = order(2, positions=[position(quantity=2, reserve=2), position(203, external='4003')])
        self.ms.supplies[uid(101)] = supply(positions=[position(202, external='4002')])
        self.ms.supplies[uid(102)] = supply(102, positions=[position(203, external='4003')])
        self.ms.stocks = {uid(201): {'stock': 4, 'reserve': 4}, uid(202): {'stock': 1, 'reserve': 1},
                          uid(203): {'stock': 1, 'reserve': 1}}
        first = self.run_receipt()
        second = self.run_receipt(102, 'event-2')
        self.assertEqual(first['results'][uid(1)]['result'], 'ms_changed')
        self.assertEqual(second['results'][uid(2)]['result'], 'ms_changed')
        self.assertEqual(worker.ledger_load()['active'][uid(1)]['components'][uid(201)]['unreserved'], '0')

    def test_foreign_reserve_is_not_reallocated(self):
        self.ms.orders[uid(1)] = order(positions=[position(quantity=2, reserve=1)])
        self.ms.stocks[uid(201)] = {'stock': 3, 'reserve': 3}
        result = self.run_receipt()
        self.assertEqual(result['results'][uid(1)]['result'], 'shortage')
        self.write.assert_not_called()

    def test_fixed_job_day_survives_midnight_and_today_is_dynamic(self):
        self.clock = datetime(2026, 11, 3, 20, 59, tzinfo=timezone.utc)
        first = self.run_receipt()
        self.assertEqual(first['target_day'], '2026-11-03')
        self.advance()
        final = self.run_receipt()
        self.assertEqual(final['target_day'], '2026-11-03')
        self.assertEqual(final['status'], 'done')

    def test_existing_correct_demand_does_not_create_another(self):
        self.run_receipt()
        current = self.ms.orders[uid(1)]
        template = self.ms.put('/entity/demand/new', {'customerOrder': {'meta': current['meta']}})
        body = {**template, 'positions': deepcopy(current['positions']['rows']), 'name': current['name'],
                'applicable': True, 'moment': '2026-11-03 12:01:00'}
        self.ms.write(None, 'create', 'POST', '', body)
        self.ms.writes.pop()
        self.advance()
        final = self.run_receipt()
        self.assertEqual(final['status'], 'done')
        self.assertEqual([value[0] for value in self.ms.writes], ['status'])

    def test_wrong_same_number_demand_blocks_creation(self):
        self.run_receipt()
        current = deepcopy(self.ms.orders[uid(1)])
        current.update(id=uid(900), customerOrder=ref('customerorder', uid(999)), applicable=True)
        self.ms.demands[uid(900)] = current
        self.advance()
        final = self.run_receipt()
        self.assertEqual(final['status'], 'manual')
        self.assertEqual([value[0] for value in self.ms.writes], ['status'])
        self.market.assert_not_called()

    def test_post_timeout_no_retry_holds_global_claim(self):
        self.run_receipt()
        self.advance()
        original_write = self.ms.write
        self.write.side_effect = lambda *args: {'uncertain': True, 'exception': 'ReadTimeout'} if args[1] == 'create' else original_write(*args)
        final = self.run_receipt()
        self.assertEqual(final['status'], 'manual')
        self.assertEqual(final['budget']['fact_resolves'], 1)
        self.assertIn(uid(1), worker.ledger_load()['active'])
        count = self.write.call_count
        self.run_receipt()
        self.assertEqual(self.write.call_count, count)
        self.market.assert_not_called()

    def test_post_timeout_with_actual_demand_resolves_once(self):
        self.run_receipt()
        self.advance()
        original_write = self.ms.write
        def lose_response(*args):
            response = original_write(*args)
            return {'uncertain': True, 'exception': 'ReadTimeout'} if args[1] == 'create' else response
        self.write.side_effect = lose_response
        final = self.run_receipt()
        self.assertEqual(final['status'], 'done')
        self.assertEqual(final['budget']['fact_resolves'], 1)
        self.assertEqual(worker.ledger_load()['active'], {})

    def test_mp_error_does_not_stop_other_order(self):
        self.ms.orders[uid(2)] = order(2)
        self.ms.stocks[uid(201)] = {'stock': 4, 'reserve': 2}
        self.run_receipt()
        self.advance()
        self.market.side_effect = [{'result': 'blocked', 'reason': 'specific order'}, {'result': 'ready'}]
        final = self.run_receipt()
        self.assertEqual(final['status'], 'manual')
        self.assertEqual(self.market.call_count, 2)
        self.assertEqual(final['results'][uid(2)]['result'], 'done')
        self.assertEqual(worker.ledger_load()['active'], {})

    def test_ten_per_pass_keeps_snapshot_instead_of_repeating_candidate_scan(self):
        self.ms.orders = {uid(index): order(index) for index in range(1, 13)}
        self.ms.stocks[uid(201)] = {'stock': 20, 'reserve': 12}
        first = self.run_receipt()
        self.assertEqual(first['processed'], 10)
        self.assertEqual(first['delay_seconds'], 0)
        second = self.run_receipt()
        self.assertEqual(second['processed'], 2)
        self.assertEqual(second['budget']['candidate_lists'], 1)
        self.assertEqual(second['budget']['stock'], 1)
        self.assertEqual(second['budget']['order_batches'], 1)

    def test_stock_requests_are_batched_by_twenty_unique_cards(self):
        positions = [position(201 + index, external=str(4001 + index)) for index in range(21)]
        self.ms.orders[uid(1)] = order(positions=positions)
        self.ms.stocks = {uid(201 + index): {'stock': 1, 'reserve': 1} for index in range(21)}
        result = self.run_receipt()
        self.assertEqual(result['budget']['stock'], 2)
        stock_calls = [params for path, params in self.ms.calls if path == '/report/stock/all']
        self.assertEqual([sum(value.startswith('product=') for value in params['filter']) for params in stock_calls], [20, 1])

    def test_changed_composition_before_demand_is_manual_without_post(self):
        self.run_receipt()
        self.ms.orders[uid(1)]['positions']['rows'][0]['price'] += 1
        self.advance()
        final = self.run_receipt()
        self.assertEqual(final['status'], 'manual')
        self.assertEqual([value[0] for value in self.ms.writes], ['status'])
        self.market.assert_not_called()

    def test_dry_run_has_no_writes_or_global_allocations(self):
        result = self.run_receipt(apply=False)
        self.assertTrue(result['preview'])
        self.assertEqual(result['results'][uid(1)]['result'], 'would_assemble')
        self.assertFalse((self.runtime / 'allocations.json').exists())
        self.write.assert_not_called()
        self.market.assert_not_called()

    def test_global_order_owner_prevents_two_receipt_versions(self):
        self.run_receipt()
        self.ms.orders[uid(1)]['state']['name'] = 'Неукомплектован'
        self.ms.supplies[uid(101)]['updated'] = '2026-11-03 12:00:30'
        result = self.run_receipt(event='new-version')
        self.assertEqual(result['results'][uid(1)]['result'], 'owned_by_another_receipt')
        self.assertEqual([value[0] for value in self.ms.writes].count('status'), 1)

    def test_status_timeout_uses_one_fact_query_and_never_repeats_put(self):
        original_write = self.ms.write
        def lose_status_response(*args):
            response = original_write(*args)
            return {'uncertain': True, 'exception': 'ReadTimeout'} if args[1] == 'status' else response
        self.write.side_effect = lose_status_response
        first = self.run_receipt()
        self.assertEqual(first['results'][uid(1)]['result'], 'ms_changed')
        self.assertEqual(first['budget']['fact_resolves'], 1)
        self.run_receipt()
        self.assertEqual([value[0] for value in self.ms.writes], ['status'])

    def test_claim_recovers_if_process_stopped_before_checkpoint_write(self):
        job, folder = worker.new_job(uid(101), NOW.isoformat(), 'event-1', True)
        ready, evidence = worker.availability(self.ms.orders[uid(1)], job, worker.ledger_load())
        self.assertTrue(ready)
        worker.claim(self.ms.orders[uid(1)], job, evidence)
        worker.checkpoint_path(uid(1)).unlink()
        result = self.run_receipt()
        self.assertEqual(result['results'][uid(1)]['result'], 'ms_changed')
        self.assertEqual(len(worker.ledger_load()['active']), 1)
        self.assertEqual([value[0] for value in self.ms.writes], ['status'])

    def test_posted_consumption_does_not_return_to_another_jobs_old_snapshot(self):
        self.ms.orders = {
            uid(1): order(1, positions=[position(quantity=2, reserve=1), position(202, external='4002')]),
            uid(2): order(2, positions=[position(quantity=2, reserve=1), position(203, external='4003')]),
            uid(3): order(3, positions=[position(quantity=2, reserve=1), position(203, external='4003')]),
        }
        self.ms.supplies[uid(101)] = supply(positions=[position(202, external='4002')])
        self.ms.supplies[uid(102)] = supply(102, positions=[position(203, external='4003')])
        self.ms.stocks = {uid(201): {'stock': 5, 'reserve': 3}, uid(202): {'stock': 1, 'reserve': 1},
                          uid(203): {'stock': 2, 'reserve': 2}}
        with patch.object(worker, 'MAX_ORDERS', 1):
            self.run_receipt()
            first_b = self.run_receipt(102, 'event-b')
            self.assertEqual(first_b['results'][uid(2)]['result'], 'ms_changed')
            self.advance()
            self.run_receipt()
            self.assertEqual(worker.ledger_load()['sequence'], 1)
            second_b = self.run_receipt(102, 'event-b')
        self.assertEqual(second_b['results'][uid(3)]['result'], 'shortage')
        self.assertEqual(self.ms.orders[uid(3)]['state']['name'], 'Неукомплектован')
        self.assertEqual(second_b['budget']['candidate_lists'], 1)
        self.assertEqual(second_b['budget']['stock'], 1)


if __name__ == '__main__':
    unittest.main()
