# поток: inv
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
import requests
from ops import ms_market_status_sources as src


def row(platform='ozon', number='123456-0010-1', scheme=None):
    return {'id': 'ms-' + number, 'platform': platform,
            'account': {'ozon': 'oz_acc1', 'wb': 'wb_acc1', 'yandex': 'ya_acc1'}[platform],
            'number': number, 'verified_scheme': scheme}


def reply(data):
    return Mock(status_code=200, json=lambda: data, raise_for_status=lambda: None)


class StatusSourceTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        self.provider = src.Provider(Path(self.temp.name))
        self.post = patch.object(src.requests, 'post', side_effect=AssertionError('unmocked API')).start()
        self.get = patch.object(src.requests, 'get', side_effect=AssertionError('unmocked API')).start()
        self.ozon = patch.object(src, 'request_page').start()
        patch.object(src, '_headers', return_value={}).start()
        patch.object(src, 'account_lock', return_value=nullcontext(Mock())).start()

    def test_ozon_fbo_point_source_has_exact_posting_identity(self):
        value = row(scheme='fbo')
        self.ozon.return_value = {'result': {'posting_number': value['number'], 'status': 'delivered'}}
        result = self.provider.fetch([value])[value['id']]
        self.assertEqual((result['status'], result['scheme']), ('delivered', 'fbo'))
        self.assertTrue(self.ozon.call_args.args[0].endswith('/v2/posting/fbo/get'))

    def test_ozon_unknown_scheme_falls_back_to_fbo_without_assuming_cancel(self):
        value = row()
        self.ozon.side_effect = [ValueError('not found'),
                                {'result': {'posting_number': value['number'], 'status': 'cancelled'}}]
        result = self.provider.fetch([value])[value['id']]
        self.assertEqual((result['status'], result['scheme']), ('cancelled', 'fbo'))
        self.assertEqual(self.ozon.call_count, 2)

    def test_missing_from_both_ozon_apis_remains_unverified(self):
        self.ozon.side_effect = ValueError('not found')
        result = self.provider.fetch([row()])['ms-123456-0010-1']
        self.assertIn('error', result)
        self.assertNotIn('status', result)

    def test_ozon_fbs_does_not_query_fbo_when_current_posting_found(self):
        self.ozon.return_value = {'result': {'posting_number': row()['number'], 'status': 'delivering'}}
        self.assertEqual(self.provider.fetch([row()])[row()['id']]['scheme'], 'fbs')
        self.ozon.assert_called_once()

    def test_yandex_reads_fbs_and_fby_without_enabling_dbs_or_second_account(self):
        value = row('yandex', '12345')
        self.post.side_effect = None
        self.post.return_value = reply({'orders': [{'orderId': 12345, 'programType': 'FBY',
                                                   'fake': False, 'status': 'DELIVERED'}]})
        with patch.dict(src.os.environ, {'YANDEX_BUSINESS_ID_ACC1': '123', 'YANDEX_API_KEY_ACC1': 'test-key'}):
            result = self.provider.fetch([value])[value['id']]
        self.assertEqual((result['status'], result['scheme']), ('delivered', 'fbo'))
        self.assertEqual(self.post.call_args.kwargs['json']['programTypes'], ['FBS', 'FBY'])
        self.assertEqual(self.provider.fetch([dict(value, account='ya_acc2')]), {})

    def test_yandex_missing_or_fake_order_does_not_mean_cancel(self):
        value = row('yandex', '12345'); self.post.side_effect = None
        self.post.return_value = reply({'orders': []})
        with patch.dict(src.os.environ, {'YANDEX_BUSINESS_ID_ACC1': '123', 'YANDEX_API_KEY_ACC1': 'test-key'}):
            self.assertNotIn(value['id'], self.provider.fetch([value]))

    def stats(self, orders, sales):
        self.provider.wb_statistics = {('wb_acc1', 'orders'): orders, ('wb_acc1', 'sales'): sales}

    def test_wb_fbo_uses_srid_and_positive_sale_proof(self):
        value = row('wb', 'stable-srid', 'fbo')
        self.stats([{'srid': 'stable-srid', 'warehouseType': 'Склад WB', 'isCancel': False}],
                   [{'srid': 'stable-srid', 'warehouseType': 'Склад WB', 'saleID': 'S123'}])
        self.assertEqual(self.provider.wb_fbo(value)['status'], 'delivered')
        self.stats([], [])
        with self.assertRaisesRegex(ValueError, 'not_found'):
            self.provider.wb_fbo(value)

    def test_wb_fbo_sale_can_prove_exact_srid_even_if_orders_report_omits_it(self):
        self.stats([], [{'srid': 'stable-srid', 'warehouseType': 'Склад WB', 'saleID': 'S123'}])
        self.assertEqual(self.provider.wb_fbo(row('wb', 'stable-srid', 'fbo'))['status'], 'delivered')

    def test_wb_fbo_basket_requires_all_units_terminal(self):
        value = row('wb', 'basket', 'fbo')
        orders = [{'srid': 'one', 'gNumber': 'basket', 'warehouseType': 'Склад WB', 'isCancel': False},
                  {'srid': 'two', 'gNumber': 'basket', 'warehouseType': 'Склад WB', 'isCancel': False}]
        self.stats(orders, [{'srid': 'one', 'warehouseType': 'Склад WB', 'saleID': 'S123'}])
        self.assertEqual(self.provider.wb_fbo(value)['status'], 'other')

    def test_wb_fbo_return_is_not_invented_as_cancellation_or_delivery(self):
        value = row('wb', 'stable-srid', 'fbo')
        self.stats([{'srid': 'stable-srid', 'warehouseType': 'Склад WB', 'isCancel': False}],
                   [{'srid': 'stable-srid', 'warehouseType': 'Склад WB', 'saleID': 'R123'}])
        with self.assertRaisesRegex(ValueError, 'return_requires'):
            self.provider.wb_fbo(value)

    def test_one_unknown_fbo_does_not_drop_verified_fbs_statuses(self):
        first, second = row('wb', '12345'), row('wb', 'unknown-srid', 'fbo')
        self.stats([], [])
        with patch.object(src.WBClient, 'call', return_value={'orders': [
                {'id': 12345, 'supplierStatus': 'complete', 'wbStatus': 'sold'}]}):
            result = self.provider.fetch([first, second])
        self.assertEqual(result[first['id']]['status'], 'delivered')
        self.assertIn('error', result[second['id']])

    def test_wb_unknown_fbs_status_can_resolve_numeric_fbo_identity(self):
        value = row('wb', '12345')
        self.stats([{'srid': 'stable', 'gNumber': '12345', 'warehouseType': 'Склад WB', 'isCancel': True}], [])
        with patch.object(src.WBClient, 'call', return_value={'orders': [
                {'id': 12345, 'supplierStatus': 'unknown', 'wbStatus': 'unknown'}]}):
            result = self.provider.fetch([value])[value['id']]
        self.assertEqual((result['scheme'], result['status']), ('fbo', 'cancelled'))

    def test_wb_statistics_pagination_uses_change_cursor_and_waits_for_empty_page(self):
        self.get.side_effect = [reply([{'srid': 'one', 'lastChangeDate': '2026-11-03T12:00:00'}]), reply([])]
        with patch.dict(src.os.environ, {'WB_TOKEN_ACC1': 'test-statistics'}), patch.object(src.time, 'sleep') as wait:
            data = self.provider.wb_stats('wb_acc1', 'orders')
        self.assertEqual(len(data), 1)
        self.assertEqual(self.get.call_args_list[1].kwargs['params']['dateFrom'], '2026-11-03T12:00:00')
        wait.assert_called_once_with(60)
