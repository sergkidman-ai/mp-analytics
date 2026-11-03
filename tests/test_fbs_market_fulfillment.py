# поток: inv
from contextlib import nullcontext
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from ops import fbs_market_fulfillment as f
from shipment_guard.config import AGENTS, ORGS


def order(platform='ozon', number=None, account=1, gtd=None):
    def ref(kind, uid):
        return {'meta': {'href': 'https://example/entity/' + kind + '/' + uid}}
    number = number or ('123456-0010-1' if platform == 'ozon' else '12345')
    assortment = {'id': 'product-id', 'externalCode': '100', 'attributes': []}
    if gtd:
        assortment['attributes'].append({'id': f.GTD_ATTR, 'name': 'последний ГТД', 'value': gtd})
    return {'id': 'ms-' + number, 'name': number, 'state': {'name': 'Собирается'},
        'deliveryPlannedMoment': '2026-11-03 00:00:00.000',
        'organization': ref('organization', list(ORGS)[account - 1]),
        'agent': ref('counterparty', next(k for k, v in AGENTS.items() if v == platform)),
        'positions': {'meta': {'size': 1}, 'rows': [{'quantity': 1, 'assortment': assortment}]}}


def response(body, http=200):
    result = Mock(status_code=http, headers={})
    result.json.return_value = body
    result.text = json.dumps(body)
    return result


def ozon_payload(o, status='awaiting_packaging', required=False):
    return response({'result': {'posting_number': o['name'], 'status': status,
        'products': [{'sku': 101, 'offer_id': '100', 'quantity': 1}],
        'requirements': {'products_requiring_gtd': [101] if required else []}}})


def seed(o, platform, status, **extra):
    o['_market_fulfillment'] = {'platform': platform, 'account': 'acc1', 'number': o['name'],
                                 'status': status, **extra}
    return o


class FulfillmentTests(TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'OZON_CLIENT_ID_ACC1': 'test-client',
            'OZON_API_KEY_ACC1': 'test-secret', 'OZON_CLIENT_ID_ACC2': 'test-client-2',
            'OZON_API_KEY_ACC2': 'test-secret-2', 'YANDEX_API_KEY_ACC1': 'test-market',
            'YANDEX_BUSINESS_ID_ACC1': '456', 'WB_TOKEN_ACC1': 'test-wb', 'WB_TOKEN_ACC2': 'test-wb2'})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.lock = patch.object(f, 'account_lock', side_effect=lambda *a, **k: nullcontext(Mock()))
        self.lock.start()
        self.addCleanup(self.lock.stop)
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name) / 'order'
        self.registry = patch.object(f.wb_supplies, 'RUNTIME', Path(self.temp.name) / 'supply-registry')
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def session(self, replies):
        request = Mock(side_effect=replies)
        return f.FulfillmentSession(request, sleep=lambda _: None), request

    def test_ozon_single_full_package_proof_and_durable_idempotence(self):
        o = order(); before = deepcopy(o)
        payload = ozon_payload(o)
        payload.json.return_value['result']['products'].append({'sku': 102, 'offer_id': '101', 'quantity': 3})
        session, request = self.session([payload, response({'result': [o['name']]})])
        result = session.fulfill(o, self.folder)
        self.assertEqual(result['result'], 'ready')
        self.assertEqual(request.call_count, 2)
        body = request.call_args.kwargs['json']
        self.assertEqual(body['packages'], [{'products': [{'product_id': 101, 'quantity': 1}, {'product_id': 102, 'quantity': 3}]}])
        self.assertEqual(session.fulfill(o, self.folder), result)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(o, before)
        self.assertEqual(self.folder.stat().st_mode & 0o777, 0o700)
        for p in self.folder.iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertNotIn('test-secret', ''.join(p.read_text() for p in self.folder.glob('*.json')))
        self.assertTrue(all('moysklad' not in c.args[1] and 'purchaseorder' not in c.args[1] for c in request.call_args_list))

    def test_ozon_cancelled_or_already_ready_never_ship(self):
        for status, expected in [('cancelled', 'blocked'), ('awaiting_deliver', 'already_ready'), ('delivering', 'blocked')]:
            with self.subTest(status=status):
                o = order(); session, request = self.session([ozon_payload(o, status)])
                self.assertEqual(session.fulfill(o, Path(self.temp.name) / status)['result'], expected)
                self.assertEqual(request.call_count, 1)

    def test_timeout_has_one_point_check_and_no_second_ship(self):
        o = order()
        session, request = self.session([ozon_payload(o), requests.ReadTimeout(), ozon_payload(o, 'awaiting_deliver')])
        self.assertEqual(session.fulfill(o, self.folder)['result'], 'ready')
        self.assertEqual(sum('/ship' in c.args[1] for c in request.call_args_list), 1)
        self.assertEqual(request.call_count, 3)

    def test_unconfirmed_timeout_is_sticky_without_blind_retry(self):
        o = order()
        session, request = self.session([ozon_payload(o), requests.ReadTimeout(), ozon_payload(o)])
        result = session.fulfill(o, self.folder)
        self.assertEqual(result['result'], 'uncertain')
        self.assertEqual(session.fulfill(o, self.folder), result)
        self.assertEqual(request.call_count, 3)

    def test_payload_read_timeout_has_no_write_intent(self):
        session, request = self.session([requests.ReadTimeout()])
        self.assertEqual(session.fulfill(order(), self.folder)['result'], 'blocked')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(list(self.folder.glob('mut_*')), [])

    def test_busy_ozon_account_returns_a_result_without_network(self):
        session, request = self.session([])
        with patch.object(f, 'account_lock', side_effect=f.OzonLockBusy()):
            self.assertEqual(session.fulfill(order(), self.folder)['result'], 'blocked')
        request.assert_not_called()

    def test_crash_after_intent_recovers_without_write(self):
        o = order(); self.folder.mkdir()
        f._save(self.folder / 'mut_ship_intent.json', {'body': {'posting_number': o['name']}}, exclusive=True)
        session, request = self.session([ozon_payload(o, 'awaiting_deliver')])
        self.assertEqual(session.fulfill(o, self.folder)['result'], 'ready')
        self.assertEqual(request.call_count, 1)
        self.assertIn('/fbs/get', request.call_args.args[1])

    def test_crash_after_success_uses_saved_response_without_api(self):
        o = order(); self.folder.mkdir()
        f._save(self.folder / 'mut_ship_intent.json', {'body': {'posting_number': o['name']}}, exclusive=True)
        f._save(self.folder / 'ship_response.json', {'http': 200, 'body': {'result': [o['name']]}})
        session, request = self.session([])
        self.assertEqual(session.fulfill(o, self.folder)['result'], 'ready')
        request.assert_not_called()

    def test_real_single_gtd_is_validated_before_ship(self):
        gtd = '10702070/100326/5092739'; o = order(gtd=gtd)
        session, request = self.session([ozon_payload(o, required=True),
            response({'posting_number': o['name'], 'products': [{'product_id': 101, 'quantity': 1, 'exemplars': [{'exemplar_id': 50}]}]}),
            response({}), response({'products': [{'product_id': 101, 'exemplars': [
                {'exemplar_id': 50, 'gtd': gtd, 'gtd_check_status': 'passed', 'gtd_error_codes': []}]}]}),
            response({'result': [o['name']]})])
        self.assertEqual(session.fulfill(o, self.folder)['result'], 'ready')
        set_body = request.call_args_list[2].kwargs['json']
        self.assertEqual(set_body['products'][0]['exemplars'], [{'exemplar_id': 50, 'gtd': gtd, 'is_gtd_absent': False}])
        self.assertEqual(request.call_count, 5)

    def ambiguous_component_order(self):
        first = order(gtd='10132160/080726/5263446')
        a = first['positions']['rows'][0]
        a['assortment']['externalCode'] = 'part-a'
        b = deepcopy(a)
        b['assortment']['id'] = 'second-component'
        b['assortment']['externalCode'] = 'part-b'
        b['assortment']['attributes'][0]['value'] = '10702070/160426/5144453'
        first['positions'] = {'meta': {'size': 2}, 'rows': [a, b]}
        return first

    def authorize_gtd_selection(self, o, **changes):
        data = {'source': 'explicit_user', 'order_id': o['id'], 'number': o['name'],
                'account': f.identity(o)[1], 'product_ids': [101],
                'gtd': '10132160/080726/5263446'}
        data.update(changes)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / 'gtd_selection_authorization.json').write_text(json.dumps(data))

    def test_scoped_user_choice_resolves_kit_gtd_without_changing_components(self):
        o = self.ambiguous_component_order()
        original = deepcopy(o)
        gtd = '10132160/080726/5263446'
        self.authorize_gtd_selection(o)
        session, request = self.session([ozon_payload(o, required=True),
            response({'posting_number': o['name'], 'products': [{'product_id': 101, 'quantity': 1,
                       'exemplars': [{'exemplar_id': 50}]}]}), response({}),
            response({'products': [{'product_id': 101, 'exemplars': [
                {'exemplar_id': 50, 'gtd': gtd, 'gtd_check_status': 'passed', 'gtd_error_codes': []}]}]}),
            response({'result': [o['name']]})])
        self.assertEqual(session.fulfill(o, self.folder)['result'], 'ready')
        self.assertEqual(request.call_args_list[2].kwargs['json']['products'][0]['exemplars'],
                         [{'exemplar_id': 50, 'gtd': gtd, 'is_gtd_absent': False}])
        self.assertEqual(o, original)

    def test_gtd_selection_cannot_leak_to_another_order(self):
        o = self.ambiguous_component_order()
        self.authorize_gtd_selection(o, number='another-order')
        session, request = self.session([ozon_payload(o, required=True)])
        self.assertEqual(session.fulfill(o, self.folder)['reason'], 'gtd_selection_authorization_scope_mismatch')
        self.assertEqual(request.call_count, 1)

    def test_gtd_selection_requires_a_real_component_number(self):
        o = self.ambiguous_component_order()
        self.authorize_gtd_selection(o, gtd='12345678/123456/1234567')
        session, request = self.session([ozon_payload(o, required=True)])
        self.assertEqual(session.fulfill(o, self.folder)['reason'], 'gtd_selection_not_in_saved_source')
        self.assertEqual(request.call_count, 1)

    def authorize_gtd_absence(self, o, **changes):
        data = {'source': 'explicit_user', 'order_id': o['id'], 'number': o['name'],
                'account': f.identity(o)[1], 'product_ids': [101]}
        data.update(changes)
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / 'gtd_absence_authorization.json').write_text(json.dumps(data))

    def test_explicit_scoped_absence_sends_no_gtd_and_requires_validation(self):
        o = order()
        self.authorize_gtd_absence(o)
        session, request = self.session([ozon_payload(o, required=True),
            response({'posting_number': o['name'], 'products': [{'product_id': 101, 'quantity': 1,
                      'exemplars': [{'exemplar_id': 50}]}]}), response({}),
            response({'products': [{'product_id': 101, 'exemplars': [{'exemplar_id': 50,
                      'gtd': '', 'is_gtd_absent': True, 'gtd_check_status': 'passed', 'gtd_error_codes': []}]}]}),
            response({'result': [o['name']]})])
        self.assertEqual(session.fulfill(o, self.folder)['result'], 'ready')
        exemplar = request.call_args_list[2].kwargs['json']['products'][0]['exemplars'][0]
        self.assertEqual(exemplar, {'exemplar_id': 50, 'is_gtd_absent': True})

    def test_absence_authorization_does_not_apply_to_another_order(self):
        o = order()
        self.authorize_gtd_absence(o, number='another-order')
        session, request = self.session([ozon_payload(o, required=True)])
        self.assertEqual(session.fulfill(o, self.folder)['reason'], 'gtd_absence_authorization_scope_mismatch')
        self.assertEqual(request.call_count, 1)

    def test_explicit_absence_cannot_override_known_gtd(self):
        o = order(gtd='10702070/100326/5092739')
        self.authorize_gtd_absence(o)
        session, request = self.session([ozon_payload(o, required=True)])
        self.assertEqual(session.fulfill(o, self.folder)['reason'], 'gtd_absence_conflicts_with_real_gtd')
        self.assertEqual(request.call_count, 1)

    def test_missing_or_multiple_gt_does_not_invent_absence_or_select_component(self):
        for ambiguous in [False, True]:
            with self.subTest(ambiguous=ambiguous):
                o = order(gtd='10702070/100326/5092739' if ambiguous else None)
                if ambiguous:
                    other = deepcopy(o['positions']['rows'][0]); other['assortment']['externalCode'] = '102'
                    other['assortment']['attributes'][0]['value'] = '10228020/131223/5103614'
                    o['positions']['rows'][0]['assortment']['externalCode'] = '101'
                    o['positions']['rows'].append(other); o['positions']['meta']['size'] = 2
                session, request = self.session([ozon_payload(o, required=True)])
                result = session.fulfill(o, Path(self.temp.name) / str(ambiguous))
                self.assertEqual(result['result'], 'blocked')
                self.assertIn('gtd', result['reason'])
                self.assertEqual(request.call_count, 1)

    def test_gtd_validation_failure_never_ship(self):
        o = order(gtd='10702070/100326/5092739')
        session, request = self.session([ozon_payload(o, required=True),
            response({'posting_number': o['name'], 'products': [{'product_id': 101, 'quantity': 1, 'exemplars': [{'exemplar_id': 50}]}]}),
            response({}), response({'products': []}), response({'products': []}), response({'products': []})])
        self.assertEqual(session.fulfill(o, self.folder)['result'], 'blocked')
        self.assertFalse(any(c.args[1].endswith('/ship') for c in request.call_args_list))

    def test_exemplar_500_is_uncertain_and_reconciled_once_without_repeat(self):
        o = order(gtd='10702070/100326/5092739')
        session, request = self.session([ozon_payload(o, required=True), response({}, 500), ozon_payload(o)])
        self.assertEqual(session.fulfill(o, self.folder)['result'], 'uncertain')
        self.assertEqual(request.call_count, 3)
        self.assertEqual(sum('/exemplar/create-or-get' in c.args[1] for c in request.call_args_list), 1)

    def test_real_import_seed_acc1_uses_yandex_campaign_without_lookup(self):
        o = seed(order('yandex'), 'yandex', ['PROCESSING', 'STARTED'], campaign_id=700)
        session, request = self.session([response({'order': {'id': 12345, 'status': 'PROCESSING', 'substatus': 'READY_TO_SHIP'}})])
        self.assertEqual(session.fulfill(o, self.folder)['result'], 'ready')
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], 'PUT')
        self.assertIn('/campaigns/700/orders/12345/status', request.call_args.args[1])
        self.assertEqual(request.call_args.kwargs['json'], {'order': {'status': 'PROCESSING', 'substatus': 'READY_TO_SHIP'}})

    def test_seed_with_wrong_platform_prefix_is_blocked(self):
        o = seed(order('yandex'), 'yandex', ['PROCESSING', 'STARTED'], campaign_id=700)
        o['_market_fulfillment']['account'] = 'oz_acc1'
        session, request = self.session([])
        self.assertEqual(session.fulfill(o, self.folder)['reason'], 'cached_market_identity_mismatch')
        request.assert_not_called()

    def test_yandex_already_ready_or_cancelled_has_no_mutation(self):
        for status, expected in [(['PROCESSING', 'READY_TO_SHIP'], 'already_ready'), (['CANCELLED', 'USER_CHANGED_MIND'], 'blocked')]:
            with self.subTest(status=status):
                o = seed(order('yandex'), 'yandex', status, campaign_id=700)
                session, request = self.session([])
                self.assertEqual(session.fulfill(o, Path(self.temp.name) / expected)['result'], expected)
                request.assert_not_called()

    def test_yandex_missing_binding_has_one_exact_business_lookup(self):
        o = order('yandex')
        session, request = self.session([response({'orders': [{'orderId': 12345, 'campaignId': 700,
            'fake': False, 'programType': 'FBS', 'status': 'PROCESSING', 'substatus': 'STARTED'}]}),
            response({'order': {'id': 12345, 'status': 'PROCESSING', 'substatus': 'READY_TO_SHIP'}})])
        self.assertEqual(session.fulfill(o, self.folder)['result'], 'ready')
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs['json'], {'orderIds': [12345]})

    def test_yandex_timeout_reads_only_the_bound_order_once(self):
        o = seed(order('yandex'), 'yandex', ['PROCESSING', 'STARTED'], campaign_id=700)
        session, request = self.session([requests.Timeout(), response({'order': {'id': 12345, 'status': 'PROCESSING', 'substatus': 'READY_TO_SHIP'}})])
        self.assertEqual(session.fulfill(o, self.folder)['result'], 'ready')
        self.assertEqual([c.args[0] for c in request.call_args_list], ['PUT', 'GET'])

    def test_yandex_batch_missing_campaigns_uses_one_exact_business_lookup(self):
        orders = [order('yandex', '12345'), order('yandex', '12346')]
        rows = [{'orderId': int(o['name']), 'campaignId': 700, 'fake': False, 'programType': 'FBS',
                 'status': 'PROCESSING', 'substatus': 'STARTED'} for o in orders]
        session, request = self.session([response({'orders': rows}), *[
            response({'order': {'id': int(o['name']), 'status': 'PROCESSING', 'substatus': 'READY_TO_SHIP'}}) for o in orders]])
        result = session.fulfill_many(orders, self.folder)
        self.assertTrue(all(r['result'] == 'ready' for r in result.values()))
        self.assertEqual(request.call_count, 3)
        self.assertEqual(request.call_args_list[0].kwargs['json'], {'orderIds': [12345, 12346]})

    def wb_replies(self, *, closed=False, included=False, supply_id=None, cargo=1):
        return [response({'orders': [{'id': 12345, 'wbStatus': 'waiting', 'supplierStatus': 'confirm' if included else 'new'}]}),
            response({'id': supply_id or f.WB_SUPPLIES['wb_acc1'], 'done': closed, 'cargoType': cargo, 'crossBorderType': 0}),
            response({'orderIds': [12345] if included else []})]

    def wb_order(self):
        return seed(order('wb'), 'wb', {'wbStatus': 'waiting', 'supplierStatus': 'new'},
            raw={'id': 12345, 'cargoType': 1, 'crossBorderType': 0, 'options': {}})

    def test_real_wb_missing_top_level_b2b_is_accepted_by_patch(self):
        session, request = self.session(self.wb_replies() + [response({}, 204)])
        self.assertEqual(session.fulfill(self.wb_order(), self.folder)['result'], 'ready')
        self.assertEqual(request.call_args.args[0], 'PATCH')
        self.assertEqual(request.call_args.kwargs['json'], {'orders': [12345]})
        self.assertEqual(request.call_count, 4)
        self.assertFalse(any('/deliver' in c.args[1] or c.args[0] == 'DELETE' for c in request.call_args_list))

    def test_wb_wrong_supply_or_incompatible_cargo_blocks_patch(self):
        for options in [{'supply_id': 'another-account-supply'}, {'cargo': 2}]:
            with self.subTest(options=options):
                session, request = self.session(self.wb_replies(**options))
                result = session.fulfill(self.wb_order(), Path(self.temp.name) / str(options))
                self.assertEqual(result['result'], 'blocked')
                self.assertFalse(any(c.args[0] == 'PATCH' for c in request.call_args_list))

    def test_wb_cancelled_has_only_exact_status_read(self):
        session, request = self.session([response({'orders': [{'id': 12345, 'wbStatus': 'canceled', 'supplierStatus': 'cancel'}]})])
        self.assertEqual(session.fulfill(self.wb_order(), self.folder)['result'], 'blocked')
        self.assertEqual(request.call_count, 1)

    def test_wb_existing_confirmed_membership_is_already_ready(self):
        session, request = self.session(self.wb_replies(included=True))
        self.assertEqual(session.fulfill(self.wb_order(), self.folder)['result'], 'already_ready')
        self.assertEqual(request.call_count, 3)

    def test_wb_timeout_checks_membership_and_status_without_second_patch(self):
        session, request = self.session(self.wb_replies() + [requests.ReadTimeout(),
            response({'orders': [{'id': 12345, 'wbStatus': 'waiting', 'supplierStatus': 'confirm'}]}),
            response({'orderIds': [12345]})])
        self.assertEqual(session.fulfill(self.wb_order(), self.folder)['result'], 'ready')
        self.assertEqual(sum(c.args[0] == 'PATCH' for c in request.call_args_list), 1)

    def test_wb_supply_is_cached_once_per_session_ttl(self):
        one = self.wb_order(); two = deepcopy(one); two['name'] = '12346'; two['id'] = 'ms-12346'
        two['_market_fulfillment']['number'] = '12346'; two['_market_fulfillment']['raw']['id'] = 12346
        session, request = self.session(self.wb_replies() + [response({}, 204),
            response({'orders': [{'id': 12346, 'wbStatus': 'waiting', 'supplierStatus': 'new'}]}), response({}, 204)])
        self.assertEqual(session.fulfill(one, self.folder / 'one')['result'], 'ready')
        self.assertEqual(session.fulfill(two, self.folder / 'two')['result'], 'ready')
        self.assertEqual(sum(c.args[1].endswith('/api/v3/supplies/' + f.WB_SUPPLIES['wb_acc1']) for c in request.call_args_list), 1)
        self.assertEqual(sum('/order-ids' in c.args[1] for c in request.call_args_list), 1)

    def test_wb_batch_reads_exact_status_ids_together_and_preserves_supply_cache(self):
        one = self.wb_order(); two = deepcopy(one); two['name'] = '12346'; two['id'] = 'ms-12346'
        two['_market_fulfillment']['number'] = '12346'; two['_market_fulfillment']['raw']['id'] = 12346
        session, request = self.session([response({'orders': [
            {'id': 12345, 'wbStatus': 'waiting', 'supplierStatus': 'new'},
            {'id': 12346, 'wbStatus': 'waiting', 'supplierStatus': 'new'}]}),
            *self.wb_replies()[1:], response({}, 204), response({}, 204)])
        results = session.fulfill_many(iter([one, two]), self.folder)
        self.assertTrue(all(r['result'] == 'ready' for r in results.values()))
        self.assertEqual(request.call_args_list[0].kwargs['json'], {'orders': [12345, 12346]})
        self.assertEqual(sum(c.args[1].endswith('/orders/status') for c in request.call_args_list), 1)

    def test_checkpoint_cannot_be_reused_for_another_order(self):
        o = order(); session, request = self.session([ozon_payload(o, 'awaiting_deliver')])
        session.fulfill(o, self.folder)
        o['name'] = '123456-0011-1'
        self.assertEqual(session.fulfill(o, self.folder)['reason'], 'checkpoint_identity_mismatch')
        self.assertEqual(request.call_count, 1)

    def future_wb_order(self, number='12345', account=1):
        o = seed(order('wb', number=number, account=account), 'wb',
                 {'wbStatus': 'waiting', 'supplierStatus': 'new'},
                 account='acc' + str(account),
                 raw={'id': int(number), 'cargoType': 1, 'crossBorderType': 0, 'options': {}})
        o['deliveryPlannedMoment'] = '2026-11-05 00:00:00.000'
        return o

    def future_supply(self, sid='WB-GI-505', account=1, **changes):
        info = {'id': sid, 'name': '05.11.2026 ' + ('Цифровой' if account == 1 else 'Дисквер') + ' FBS',
                'done': False, 'closedAt': None, 'cargoType': 0, 'crossBorderType': 0,
                'createdAt': '2026-11-03T12:00:00Z'}
        info.update(changes)
        return info

    def wb_status(self, number=12345, status='new'):
        return response({'orders': [{'id': number, 'wbStatus': 'waiting', 'supplierStatus': status}]})

    def test_future_date_uses_existing_supply_not_creation_day(self):
        info = self.future_supply()
        session, request = self.session([self.wb_status(), response({'supplies': [info], 'next': 0}),
            response(info), response({'orderIds': []}), response({}, 204)])
        result = session.fulfill(self.future_wb_order(), self.folder)
        self.assertEqual(result['result'], 'ready')
        self.assertEqual(result['supply_id'], info['id'])
        self.assertFalse(any(c.args[0] == 'POST' and c.args[1].endswith('/supplies') for c in request.call_args_list))

    def test_missing_future_supply_created_separately_in_each_account(self):
        for account in (1, 2):
            info = self.future_supply('WB-GI-' + str(500 + account), account)
            session, request = self.session([self.wb_status(), response({'supplies': [], 'next': 0}),
                response({'id': info['id']}, 201), response(info), response({'orderIds': []}), response({}, 204)])
            result = session.fulfill(self.future_wb_order(account=account), self.folder / str(account))
            self.assertEqual(result['result'], 'ready')
            create = [c for c in request.call_args_list if c.args[0] == 'POST' and c.args[1].endswith('/supplies')]
            self.assertEqual(len(create), 1)
            self.assertEqual(create[0].kwargs['json'], {'name': info['name']})
            self.assertEqual(create[0].kwargs['headers']['Authorization'], 'test-wb' if account == 1 else 'test-wb2')

    def test_next_process_uses_saved_dated_supply_without_catalog_scan(self):
        info = self.future_supply()
        session, _ = self.session([self.wb_status(), response({'supplies': [info], 'next': 0}),
            response(info), response({'orderIds': []}), response({}, 204)])
        self.assertEqual(session.fulfill(self.future_wb_order(), self.folder / 'first')['result'], 'ready')
        second, request = self.session([self.wb_status(12346), response(info),
            response({'orderIds': [12345]}), response({}, 204)])
        self.assertEqual(second.fulfill(self.future_wb_order('12346'), self.folder / 'next')['result'], 'ready')
        self.assertFalse(any(c.args[1].endswith('/supplies') for c in request.call_args_list))

    def test_multiple_suitable_future_supplies_never_create_or_assign(self):
        session, request = self.session([self.wb_status(), response({'supplies': [
            self.future_supply(), self.future_supply('WB-GI-506')], 'next': 0})])
        result = session.fulfill(self.future_wb_order(), self.folder)
        self.assertEqual(result['reason'], 'wb_multiple_open_supplies_for_planned_day')
        self.assertFalse(any(c.args[0] == 'PATCH' or c.args[1].endswith('/supplies') and c.args[0] == 'POST'
                             for c in request.call_args_list))

    def test_supply_search_follows_pagination_before_creation(self):
        info = self.future_supply()
        old = self.future_supply('WB-GI-1', name='03.11.2026')
        session, request = self.session([self.wb_status(), response({'supplies': [old], 'next': 12}),
            response({'supplies': [info], 'next': 0}), response(info), response({'orderIds': []}), response({}, 204)])
        self.assertEqual(session.fulfill(self.future_wb_order(), self.folder)['result'], 'ready')
        self.assertEqual(request.call_args_list[2].kwargs['params']['next'], 12)
        self.assertFalse(any(c.args[0] == 'POST' and c.args[1].endswith('/supplies') for c in request.call_args_list))

    def test_uncertain_supply_creation_is_found_without_second_post(self):
        info = self.future_supply()
        session, request = self.session([self.wb_status(), response({'supplies': [], 'next': 0}),
            requests.ReadTimeout(), response({'supplies': [info], 'next': 0}), response(info),
            response({'orderIds': []}), response({}, 204)])
        self.assertEqual(session.fulfill(self.future_wb_order(), self.folder)['result'], 'ready')
        self.assertEqual(sum(c.args[0] == 'POST' and c.args[1].endswith('/supplies') for c in request.call_args_list), 1)

    def test_unresolved_creation_blocks_next_order_from_creating_duplicate(self):
        session, request = self.session([self.wb_status(), response({'supplies': [], 'next': 0}),
            requests.ReadTimeout(), response({'supplies': [], 'next': 0}),
            self.wb_status(12346), response({'supplies': [], 'next': 0})])
        first = session.fulfill(self.future_wb_order(), self.folder / 'one')
        second = session.fulfill(self.future_wb_order('12346'), self.folder / 'two')
        self.assertEqual(first['reason'], 'wb_supply_create_unconfirmed_no_retry')
        self.assertEqual(second['reason'], 'wb_supply_create_unconfirmed_no_retry')
        self.assertEqual(sum(c.args[0] == 'POST' and c.args[1].endswith('/supplies') for c in request.call_args_list), 1)

    def test_assignment_timeout_reconciles_actual_future_supply(self):
        info = self.future_supply()
        session, request = self.session([self.wb_status(), response({'supplies': [info], 'next': 0}),
            response(info), response({'orderIds': []}), requests.ReadTimeout(),
            self.wb_status(status='confirm'), response({'orderIds': [12345]})])
        self.assertEqual(session.fulfill(self.future_wb_order(), self.folder)['result'], 'ready')
        self.assertEqual(sum(c.args[0] == 'PATCH' for c in request.call_args_list), 1)
        self.assertIn('/WB-GI-505/order-ids', request.call_args_list[-1].args[1])

    def test_cache_does_not_mix_today_and_future_supply(self):
        info = self.future_supply()
        session, request = self.session(self.wb_replies() + [response({}, 204), self.wb_status(12346),
            response({'supplies': [info], 'next': 0}), response(info), response({'orderIds': []}), response({}, 204)])
        self.assertEqual(session.fulfill(self.wb_order(), self.folder / 'today')['result'], 'ready')
        result = session.fulfill(self.future_wb_order('12346'), self.folder / 'future')
        self.assertEqual(result['supply_id'], 'WB-GI-505')

    def test_parallel_creator_is_blocked_before_supply_mutation(self):
        session, request = self.session([self.wb_status()])
        with patch.object(f.wb_supplies, 'LOCK_WAIT_SECONDS', 0):
            with f.wb_supplies.account_lock('wb_acc1'):
                result = session.fulfill(self.future_wb_order(), self.folder)
        self.assertEqual(result['reason'], 'wb_supply_account_busy')
        self.assertEqual(request.call_count, 1)

    def test_closed_dated_supply_is_not_reused(self):
        info = self.future_supply()
        closed = self.future_supply('WB-GI-1', done=True, closedAt='2026-11-03T10:00:00Z')
        session, request = self.session([self.wb_status(), response({'supplies': [closed], 'next': 0}),
            response({'id': info['id']}, 201), response(info), response({'orderIds': []}), response({}, 204)])
        self.assertEqual(session.fulfill(self.future_wb_order(), self.folder)['supply_id'], 'WB-GI-505')

    def test_dated_names_do_not_confuse_receipt_creation_day_or_year(self):
        for name in ('05.11', '5.11', '05.11.26', '5.11.2026', '2026-11-05'):
            with self.subTest(name=name):
                self.assertTrue(f.wb_supplies.dated(self.future_supply(name=name), '2026-11-05'))
        self.assertFalse(f.wb_supplies.dated(self.future_supply(name='05.11.2025'), '2026-11-05'))
        self.assertFalse(f.wb_supplies.dated(self.future_supply(name='03.11 / 05.11'), '2026-11-05'))

    def test_closed_legacy_today_supply_can_be_replaced(self):
        info = self.future_supply(name='03.11.2026 Цифровой FBS')
        session, request = self.session([self.wb_status(), self.wb_replies(closed=True)[1],
            response({'supplies': [], 'next': 0}), response({'id': info['id']}, 201),
            response(info), response({'orderIds': []}), response({}, 204)])
        self.assertEqual(session.fulfill(self.wb_order(), self.folder)['supply_id'], info['id'])

    def test_missing_planned_day_never_creates_supply(self):
        o = self.future_wb_order(); o.pop('deliveryPlannedMoment')
        session, request = self.session([])
        self.assertEqual(session.fulfill(o, self.folder)['reason'], 'wb_planned_shipping_day_missing')
        request.assert_not_called()
