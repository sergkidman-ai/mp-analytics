# поток: inv
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from ops import fbs_ms_post_import_shipments as shipment

NOW = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)
BASE = shipment.ms_api.BASE + '/entity/'


def ref(kind, uid):
    return {'meta': {'href': BASE + kind + '/' + uid, 'type': kind, 'mediaType': 'application/json'}}


def make_order(number='123', uid='order-1', positions=None):
    if positions is None:
        positions = [{**{'assortment': ref('product', 'product-1')}, 'quantity': 2,
                      'price': 12500, 'discount': 0, 'vat': 5, 'vatEnabled': True, 'reserve': 2}]
    return {**ref('customerorder', uid), 'id': uid, 'name': number,
            'state': {'name': 'Собирается'}, 'store': ref('store', 'store-1'),
            'organization': ref('organization', 'org-1'), 'agent': ref('counterparty', 'agent-1'),
            'sum': 25000, 'deliveryPlannedMoment': '2026-11-03 00:00:00', 'updated': '2026-11-03 11:00:00',
            'positions': {'meta': {'size': len(positions)}, 'rows': positions}, 'demands': []}


def make_demand(order, uid='demand-1'):
    demand = deepcopy(order)
    demand.update(**ref('demand', uid), id=uid, customerOrder=ref('customerorder', order['id']), applicable=True)
    return demand


class ShipmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.status = self.root / 'status'
        self.runtime = self.status / 'shipment'
        values = {'STATUS_RUNTIME': self.status, 'RUNTIME': self.runtime, 'ORDER_PAUSE': 0}
        self.addCleanup(patch.stopall)
        patch.multiple(shipment, **values).start()
        self.clock = patch.object(shipment, 'now', return_value=NOW).start()
        self.sleep = patch.object(shipment.time, 'sleep').start()
        self.get = patch.object(shipment.ms_api, 'get', side_effect=AssertionError('unexpected GET')).start()
        self.put = patch.object(shipment.ms_api, 'put', side_effect=AssertionError('unexpected template')).start()
        self.write = patch.object(shipment.batch, 'one_write', side_effect=AssertionError('unexpected write')).start()
        self.stock = patch.object(shipment.batch, 'stock_check').start()
        self.market = patch.object(shipment, 'fulfill_many', side_effect=lambda orders, folder: {
            o['id']: {'order_id': o['id'], 'number': o['name'], 'result': 'ready'} for o in orders}).start()
        self.order = make_order()
        self.proof = self.save_status(self.order)

    def save_status(self, order, changed=None, shipment_due=True):
        proof = {'name': order['name'], 'id': order['id'], 'state': order['state']['name'],
                 'planned': order['deliveryPlannedMoment'], 'sum': order['sum'], 'updated': order['updated'],
                 'positions': [(shipment.ms_api.meta_id(position, 'assortment'), position['quantity'])
                               for position in order['positions']['rows']],
                 'shipment_due': shipment_due, 'changed_at': (changed or NOW - timedelta(minutes=10)).isoformat()}
        folder = self.status / order['name']
        folder.mkdir(parents=True, exist_ok=True)
        stock = {cid: {'quantity': str(quantity), 'own_reserve': str(quantity)}
                 for cid, quantity in shipment.quantities(order['positions']['rows']).items()}
        (folder / 'readback.json').write_text(json.dumps(proof))
        (folder / 'response.json').write_text(json.dumps({'status': 200, 'body': order}))
        (folder / 'intent.json').write_text(json.dumps({'decision': {'stock': stock}}))
        return proof

    def prepare_create(self, order=None):
        order = order or self.order
        template = make_demand(order)
        template.pop('id')
        template['applicable'] = False
        self.put.side_effect = None
        self.put.return_value = template
        self.write.side_effect = None
        self.write.return_value = {'status': 200, 'body': make_demand(order)}

    def one(self, existing=None, apply=True):
        return shipment.apply_one(self.order, self.proof, existing or [], apply)

    def test_queue_excludes_old_confirmed_and_missing_due_flag(self):
        old = make_order('old', 'old-id')
        old['state']['name'] = 'Подтвержден'
        self.save_status(old)
        self.save_status(make_order('manual', 'manual-id'), shipment_due=False)
        self.assertEqual([item['name'] for item in shipment.pending()], ['123'])

    def test_existing_link_is_verified_without_creation_or_stock(self):
        demand = make_demand(self.order)
        self.order['demands'] = [ref('demand', demand['id'])]
        self.assertEqual(self.one([demand])['result'], 'existing')
        self.write.assert_not_called()
        self.put.assert_not_called()
        self.stock.assert_not_called()

    def test_existing_demand_not_yet_in_order_links_prevents_duplicate(self):
        self.assertEqual(self.one([make_demand(self.order)])['result'], 'existing')
        self.write.assert_not_called()

    def test_duplicate_demands_are_manual_without_creation(self):
        self.assertEqual(self.one([make_demand(self.order), make_demand(self.order, 'second')])['result'], 'pending_manual')
        self.write.assert_not_called()

    def test_wrong_number_link_is_manual_without_creation(self):
        self.order['demands'] = [ref('demand', 'other-number')]
        self.assertEqual(self.one()['reason'], 'linked_demand_number_or_org_mismatch')
        self.write.assert_not_called()

    def test_same_number_other_order_is_manual(self):
        demand = make_demand(self.order)
        demand['customerOrder'] = ref('customerorder', 'unrelated')
        self.assertEqual(self.one([demand])['result'], 'pending_manual')
        self.write.assert_not_called()

    def test_post_once_and_response_proof_without_extra_get(self):
        self.prepare_create()
        self.assertEqual(self.one()['result'], 'created')
        self.assertEqual(self.one()['reason'], 'prior_creation_intent_no_retry')
        self.write.assert_called_once()
        self.get.assert_not_called()
        self.stock.assert_not_called()
        folder = self.runtime / '123'
        self.assertTrue((folder / 'create_guard.json').exists())
        self.assertTrue((folder / 'response.json').exists())
        self.assertEqual((folder / 'create_guard.json').stat().st_mode & 0o777, 0o600)

    def test_template_can_omit_computed_totals_but_final_document_is_checked(self):
        self.order.update(vatSum=1190, vatIncluded=True, vatEnabled=True)
        self.save_status(self.order)
        self.prepare_create()
        self.put.return_value.pop('vatSum')
        self.put.return_value['sum'] = 0
        self.assertEqual(self.one()['result'], 'created')
        self.write.assert_called_once()
        self.stock.assert_not_called()

    def test_template_still_rejects_changed_vat_mode(self):
        self.order.update(vatSum=1190, vatIncluded=True, vatEnabled=True)
        self.save_status(self.order)
        self.prepare_create()
        self.put.return_value['vatIncluded'] = False
        self.assertEqual(self.one()['reason'], 'template_vat_mode_mismatch')
        self.write.assert_not_called()

    def test_legacy_computed_vat_failure_is_requeued_before_any_post(self):
        shipment.durable_save(self.runtime / '123' / 'result.json',
                              {'result': 'pending_manual', 'reason': 'template_vat_mismatch'})
        self.assertEqual([p['name'] for p in shipment.pending()], ['123'])

    def test_legacy_vat_failure_with_creation_guard_is_not_requeued(self):
        shipment.durable_save(self.runtime / '123' / 'result.json',
                              {'result': 'pending_manual', 'reason': 'template_vat_mismatch'})
        shipment.durable_save(self.runtime / '123' / 'create_guard.json', {})
        self.assertEqual(shipment.pending(), [])

    def test_real_vat_mode_failure_stays_manual(self):
        shipment.durable_save(self.runtime / '123' / 'result.json',
                              {'result': 'pending_manual', 'reason': 'template_vat_mode_mismatch'})
        self.assertEqual(shipment.pending(), [])

    def test_partial_own_reserve_never_rechecks_stock_or_reserves(self):
        self.prepare_create()
        path = self.status / '123' / 'intent.json'
        path.write_text(json.dumps({'decision': {'stock': {'product-1': {'quantity': 2, 'own_reserve': 1}}}}))
        self.assertEqual(self.one()['result'], 'created')
        self.stock.assert_not_called()
        guard = json.loads((self.runtime / '123' / 'create_guard.json').read_text())
        self.assertEqual(guard['availability'], 'inherited_from_import')

    def test_timeout_fact_check_without_repeat_post(self):
        self.prepare_create()
        self.write.return_value = {'uncertain': True, 'exception': 'ReadTimeout'}
        self.get.side_effect = None
        self.get.return_value = {'meta': {'size': 0}, 'rows': []}
        self.assertEqual(self.one()['reason'], 'creation_uncertain_no_retry')
        self.assertEqual(self.one()['reason'], 'prior_creation_intent_no_retry')
        self.write.assert_called_once()
        self.get.assert_called_once()
        self.assertTrue((self.runtime / '123' / 'resolve.json').exists())

    def test_timeout_can_resolve_one_exact_matching_demand(self):
        self.prepare_create()
        self.write.return_value = {'uncertain': True, 'exception': 'ReadTimeout'}
        self.get.side_effect = None
        self.get.return_value = {'meta': {'size': 1}, 'rows': [make_demand(self.order)]}
        self.assertEqual(self.one()['result'], 'created_resolved')
        self.get.assert_called_once()
        self.write.assert_called_once()

    def test_non_json_write_exception_still_blocks_retries(self):
        self.prepare_create()
        self.write.side_effect = ValueError('not JSON')
        self.get.side_effect = RuntimeError('read unavailable')
        self.assertEqual(self.one()['result'], 'pending_manual')
        self.assertEqual(self.one()['reason'], 'prior_creation_intent_no_retry')
        self.write.assert_called_once()

    def test_date_plan_price_and_order_are_preserved(self):
        self.prepare_create()
        before = deepcopy(self.order)
        self.assertEqual(self.one()['result'], 'created')
        payload = self.write.call_args.args[4]
        self.assertEqual(payload['name'], '123')
        self.assertTrue(payload['applicable'])
        self.assertEqual(payload['moment'], '2026-11-03 12:00:00')
        self.assertEqual(payload['deliveryPlannedMoment'], '2026-11-03 00:00:00')
        self.assertEqual(payload['positions'], before['positions']['rows'])
        self.assertEqual(self.order, before)
        self.assertIn('expand=positions.assortment', self.write.call_args.args[3])

    def test_set_repeated_card_rows_are_preserved_with_prices_and_vat(self):
        first = deepcopy(self.order['positions']['rows'][0])
        first.update(quantity=1, price=10000, reserve=1)
        second = deepcopy(first)
        second.update(price=15000)
        third = deepcopy(first)
        third['assortment'] = ref('product', 'component-two')
        third.update(price=5000, quantity=2)
        self.order = make_order(positions=[first, second, third])
        self.proof = self.save_status(self.order)
        self.prepare_create()
        self.assertEqual(self.one()['result'], 'created')
        self.assertEqual(len(self.write.call_args.args[4]['positions']), 3)
        self.stock.assert_not_called()

    def test_changed_price_is_skipped(self):
        self.order['positions']['rows'][0]['price'] += 1
        self.assertEqual(self.one()['reason'], 'order_positions_changed')
        self.write.assert_not_called()

    def test_changed_state_store_sum_or_plan_are_skipped(self):
        for key, value, reason in [('state', {'name': 'В пути'}, 'order_state_changed'),
                                    ('store', ref('store', 'other'), 'store_changed'),
                                    ('sum', 1, 'order_sum_changed'),
                                    ('deliveryPlannedMoment', '2026-11-05 00:00:00', 'order_plan_changed')]:
            with self.subTest(key=key):
                order = deepcopy(self.order)
                order[key] = value
                result = shipment.apply_one(order, self.proof, [], True)
                self.assertEqual(result['reason'], reason)
        self.write.assert_not_called()

    def test_template_mismatch_blocks_creation(self):
        self.prepare_create()
        self.put.return_value['positions']['rows'][0]['quantity'] = 99
        self.assertEqual(self.one()['reason'], 'template_positions_mismatch')
        self.write.assert_not_called()

    def test_success_response_must_prove_posted_link_and_composition(self):
        self.prepare_create()
        self.write.return_value['body']['applicable'] = False
        self.assertEqual(self.one()['result'], 'pending_manual')
        self.get.assert_not_called()

    def test_demand_response_must_match_document_vat(self):
        self.order.update(vatSum=1190, vatIncluded=True, vatEnabled=True)
        for key in ('vatSum', 'vatIncluded', 'vatEnabled'):
            with self.subTest(key=key):
                demand = make_demand(self.order)
                self.assertTrue(shipment.demand_matches(demand, self.order))
                demand.pop(key)
                self.assertFalse(shipment.demand_matches(demand, self.order))

    def test_run_uses_one_order_batch_and_one_demand_batch_and_persists(self):
        self.prepare_create()
        self.get.side_effect = [ {'meta': {'size': 1}, 'rows': [self.order]},
                                {'meta': {'size': 0}, 'rows': []} ]
        old_limits = shipment.ms_api.TIMEOUT, shipment.ms_api.RETRIES
        summary = shipment.run(True)
        self.assertEqual(summary['created'], 1)
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(self.get.call_args_list[0].args[0], '/entity/customerorder')
        self.assertEqual(self.get.call_args_list[0].args[1]['filter'], ['name=123'])
        self.assertEqual((shipment.ms_api.TIMEOUT, shipment.ms_api.RETRIES), old_limits)
        self.assertEqual(shipment.pending(), [])
        self.assertEqual(json.loads((self.runtime / 'latest.json').read_text())['created'], 1)

    def prepare_run(self, existing=None):
        self.prepare_create()
        self.get.side_effect = [{'meta': {'size': 1}, 'rows': [self.order]},
                                {'meta': {'size': len(existing or [])}, 'rows': existing or []}]

    def test_market_runs_only_after_durable_verified_shipment(self):
        self.prepare_run()

        def ready(orders, folder):
            saved = json.loads((self.runtime / '123' / 'result.json').read_text())
            self.assertEqual(saved['demand_id'], 'demand-1')
            self.assertTrue(saved['market_due'])
            self.assertEqual(self.write.call_count, 1)
            self.assertEqual(folder, self.runtime / 'market')
            return {'order-1': {'order_id': 'order-1', 'number': '123', 'result': 'ready'}}

        self.market.side_effect = ready
        result = shipment.run(True)
        self.assertEqual(result['market_ready'], 1)
        self.assertEqual(result['market_pending'], [])
        self.assertEqual(self.get.call_count, 2)
        self.stock.assert_not_called()
        self.assertEqual(shipment.pending(), [])

    def test_unverified_ms_shipment_never_changes_marketplace(self):
        self.prepare_run()
        self.write.return_value['body']['applicable'] = False
        result = shipment.run(True)
        self.market.assert_not_called()
        self.assertEqual(result['results'][0]['result'], 'pending_manual')

    def test_dry_run_after_guard_does_not_change_marketplace(self):
        self.prepare_run()
        result = shipment.run(False)
        self.market.assert_not_called()
        self.write.assert_not_called()
        self.assertEqual(result['results'][0]['result'], 'would_create')

    def test_market_block_keeps_ms_success_and_stops_automatic_retry(self):
        self.prepare_run()
        self.market.side_effect = lambda orders, folder: {
            'order-1': {'order_id': 'order-1', 'number': '123', 'result': 'blocked', 'reason': 'gtd_missing'}}
        result = shipment.run(True)
        self.assertEqual(result['created'], 1)
        self.assertEqual(len(result['market_pending']), 1)
        self.assertEqual(len(result['errors']), 1)
        self.assertEqual(result['results'][0]['demand_id'], 'demand-1')
        self.assertFalse(result['results'][0]['market_due'])
        self.assertEqual(shipment.pending(), [])
        self.write.assert_called_once()

    def test_restart_after_ms_before_market_does_not_create_duplicate(self):
        self.prepare_run()
        self.market.side_effect = KeyboardInterrupt('simulated process stop')
        with self.assertRaises(KeyboardInterrupt):
            shipment.run(True)
        self.assertEqual([p['name'] for p in shipment.pending()], ['123'])
        self.get.side_effect = [{'meta': {'size': 1}, 'rows': [self.order]},
                               {'meta': {'size': 1}, 'rows': [make_demand(self.order)]}]
        self.market.side_effect = lambda orders, folder: {
            'order-1': {'order_id': 'order-1', 'number': '123', 'result': 'already_ready'}}
        result = shipment.run(True)
        self.assertEqual(result['existing'], 1)
        self.assertEqual(result['market_ready'], 1)
        self.write.assert_called_once()
        self.assertEqual(shipment.pending(), [])

    def test_historical_ms_success_without_market_due_is_not_backfilled(self):
        shipment.durable_save(self.runtime / '123' / 'result.json',
                              {'number': '123', 'result': 'created', 'demand_id': 'demand-1'})
        self.assertEqual(shipment.pending(), [])

    def test_partial_market_batch_failure_preserves_completed_adapter_result(self):
        other = make_order('second', 'order-2')
        results = [{'number': o['name'], 'result': 'created', 'demand_id': 'd-' + o['id']}
                   for o in (self.order, other)]

        def partial(orders, folder):
            shipment.durable_save(folder / 'order-1' / 'result.json',
                                  {'order_id': 'order-1', 'number': '123', 'result': 'ready'})
            raise TimeoutError('simulated failure in the next order')

        self.market.side_effect = partial
        shipment.finish_market([self.order, other], results)
        self.assertEqual(results[0]['market']['result'], 'ready')
        self.assertEqual(results[1]['market']['result'], 'uncertain')
        self.assertTrue(all(r['result'] == 'created' and r['demand_id'] for r in results))

    def test_shared_wait_uses_latest_selected_change_once(self):
        self.proof = self.save_status(self.order, changed=NOW - timedelta(minutes=1))
        other = make_order('second', 'order-2')
        self.save_status(other, changed=NOW)
        self.get.side_effect = [ {'meta': {'size': 2}, 'rows': [self.order, other]},
                                {'meta': {'size': 0}, 'rows': []} ]
        with patch.object(shipment, 'wait_until') as wait, patch.object(shipment, 'apply_one', return_value={'result': 'skipped'}) as one:
            shipment.run(True)
        wait.assert_called_once_with(NOW + timedelta(minutes=5))
        self.assertEqual(one.call_count, 2)

    def test_wait_is_split_into_at_most_thirty_seconds(self):
        current = [NOW]
        self.clock.side_effect = lambda: current[0]
        self.sleep.side_effect = lambda seconds: current.__setitem__(0, current[0] + timedelta(seconds=seconds))
        shipment.wait_until(NOW + timedelta(seconds=65))
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [30, 30, 5])

    def test_dry_run_before_deadline_does_not_read_ms_or_wait(self):
        self.save_status(self.order, changed=NOW)
        summary = shipment.run(False)
        self.assertEqual(summary['results'][0]['result'], 'waiting_delay')
        self.sleep.assert_not_called()
        self.get.assert_not_called()

    def test_batch_limit_ten_defers_remaining(self):
        for index in range(11):
            self.save_status(make_order('n' + str(index), 'id-' + str(index)), changed=NOW)
        summary = shipment.run(False)
        self.assertEqual(summary['selected'], 10)
        self.assertEqual(summary['deferred'], 2)
        self.assertEqual(len(summary['results']), 10)

    def test_existing_durable_guard_survives_process_restart(self):
        folder = self.runtime / '123'
        shipment.durable_save(folder / 'create_guard.json', {'attempt': 'recorded'}, exclusive=True)
        self.assertEqual(self.one()['result'], 'pending_manual')
        self.write.assert_not_called()


if __name__ == '__main__':
    unittest.main()
