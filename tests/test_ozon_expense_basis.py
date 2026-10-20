# поток: fin
import copy
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from core import db
from core import ozon_expense_basis as B
from ops import ozon_stock_action as stock


def transaction(sale='1000',commission='-300',amount='600',services=None,operation_type='Delivered'):
    return dict(accruals_for_sale=sale,sale_commission=commission,amount=amount,
                services=services if services is not None else [dict(name='MarketplaceServiceItemDirectFlowLogistic',price='-100')],
                operation_type=operation_type,operation_date='2026-09-01')


def basis(account='oz_acc2',source='fixture',transactions=None,complete=True):
    return B.calculate(account,'2026-09-01',transactions if transactions is not None else [transaction()],
                       {'complete':complete,'reasons':[]},source)


class PureTests(unittest.TestCase):
    def test_one_composition_and_no_double_count(self):
        b=basis();self.assertEqual(b['status'],'complete')
        self.assertEqual(Decimal(b['sales']),1000);self.assertEqual(Decimal(b['expenses']),400)
        self.assertEqual(Decimal(b['keep']),Decimal('.6'));self.assertEqual(Decimal(b['net_reconciliation_delta']),0)

    def test_commission_refund_and_services_credit_reduce_net_expense(self):
        rows=[transaction(),transaction(sale='0',commission='20',amount='30',
              services=[dict(name='MarketplaceServiceItemDirectFlowLogistic',price='10')])]
        b=basis(transactions=rows)
        self.assertEqual(Decimal(b['expenses']),370);self.assertEqual(Decimal(b['keep']),Decimal('.63'))
        self.assertEqual(sum(map(Decimal,b['gross_credits'].values())),30)

    def test_returns_compensation_other_are_separate(self):
        rows=[transaction(),transaction(sale='-100',commission='0',amount='-100',services=[]),
              transaction(sale='0',commission='0',amount='30',services=[],operation_type='AccrualConsigDefectiveWriteOff'),
              transaction(sale='0',commission='0',amount='10',services=[],operation_type='MarketplaceSellerCorrectionOperation')]
        b=basis(transactions=rows)
        self.assertEqual(b['status'],'complete');self.assertEqual(Decimal(b['expenses']),400)
        self.assertEqual(Decimal(b['lines']['returns']),100);self.assertEqual(Decimal(b['lines']['compensation']),30)
        self.assertEqual(Decimal(b['lines']['other']),10)

    def test_unknown_cost_is_visible_and_included(self):
        b=basis(transactions=[transaction(amount='590',services=[dict(name='MarketplaceServiceItemDirectFlowLogistic',price='-100'),dict(name='NEW_FEE',price='-10')])])
        self.assertEqual(Decimal(b['expenses']),410);self.assertIn('service:NEW_FEE',b['unknown_codes'])

    def test_empty_incomplete_or_invalid_ratios_do_not_supply_basis(self):
        for b in (basis(transactions=[]),basis(complete=False),basis(transactions=[transaction(commission='-1100',amount='-200')])):
            self.assertEqual(b['status'],'incomplete')
            with self.assertRaises(B.BasisUnavailable):B.floor_price(100,1,b)

    def test_same_inputs_idempotent_and_corrections_create_new_version(self):
        a=basis();self.assertEqual(a['version'],basis()['version'])
        self.assertNotEqual(a['version'],basis(source='correction')['version'])
        self.assertNotEqual(a['version'],basis(account='oz_acc1')['version'])

    def test_month_rollover_leap_year_and_last_closed(self):
        self.assertEqual(B.latest_closed(date(2027,1,1)),date(2026,12,1))
        self.assertEqual(B.latest_closed(date(2026,10,1)),date(2026,9,1))
        self.assertEqual(B.month_bounds('2024-02')[1],date(2024,2,29))

    def test_same_version_and_units_equal_floor_at_kopeck_in_all_interfaces(self):
        b=basis();keep=Decimal(b['keep']);retention=Decimal(b['retention'])
        for cost,units in [('69.28',1),('1200',6),('40000',1),('40000',6)]:
            direct=B.floor_price(cost,units,b)
            self.assertEqual(direct,B.floor_from_retention(cost,units,retention))
            self.assertEqual(direct,Decimal(str(stock.floor_price(float(cost),keep,units))))
        self.assertEqual(B.floor_from_keep(1200,6,'.4'),7500)

    def test_action_keep_has_no_rounding_or_fallback(self):
        b=basis()
        with patch.object(B,'get_basis',return_value=b):
            keep,source=stock.keep_ratio('oz_acc2')
            self.assertIsInstance(keep,Decimal);self.assertIn(b['version'],source)
        with patch.object(B,'get_basis',side_effect=B.BasisUnavailable('incomplete')):
            with self.assertRaises(B.BasisUnavailable):stock.keep_ratio('oz_acc2')

    def test_source_error_is_not_silently_replaced_by_constant(self):
        with patch.object(db,'query',side_effect=RuntimeError('DB unavailable')):
            with self.assertRaises(RuntimeError):B.refresh('oz_acc2')

    def test_stale_or_wrong_account_provider_state(self):
        b=basis()
        with patch.object(db,'query',return_value=[{'payload':b}]):
            self.assertEqual(B.get_basis('oz_acc2',as_of=date(2026,10,20))['version'],b['version'])
            with self.assertRaises(B.BasisUnavailable):B.get_basis('oz_acc2',as_of=date(2026,11,1))
        with patch.object(db,'query',return_value=[]):
            with self.assertRaises(B.BasisUnavailable):B.get_basis('oz_acc1')

    def test_complete_log_requires_final_total_and_all_source_days(self):
        text='Ozon начисления oz_acc2 2026-09-01..2026-09-30\n'
        text+='  [oz accrual] 2026-09-01 записей 1 → raw 1, итог 600.00\nИтого: записей 1, сумма 600.00\n'
        with tempfile.NamedTemporaryFile('w',suffix='.log') as f:
            f.write(text);f.flush();runs=B.successful_log_runs(f.name)
            self.assertEqual(len(runs[('oz_acc2','2026-09-01')]['days']),1)
            # A partial calendar log is not enough even if one day's raw matches.
            with patch.object(db,'query',side_effect=[[],[],[]]):
                b=B.build('oz_acc2','2026-09-01',log_path=f.name,as_of=date(2026,10,20))
            self.assertIn('incomplete_collector_days',b['reasons'])
            self.assertIn('closed_realization_report_missing_or_invalid',b['reasons'])


@unittest.skipUnless(os.getenv('OZON_POSTING_SQL_TEST')=='1','opt-in isolated-schema PostgreSQL tests')
class StoreTests(unittest.TestCase):
    def setUp(self):
        self.conn=__import__('psycopg2').connect(db.DATABASE_URL)
        schema='ozbasis_test_'+uuid4().hex
        with self.conn.cursor() as c:
            c.execute('CREATE SCHEMA '+schema);c.execute('SET LOCAL search_path TO '+schema+',public')
            c.execute('CREATE TABLE ms_shipment_rules (LIKE public.ms_shipment_rules INCLUDING ALL)')
            c.execute("INSERT INTO ms_shipment_rules SELECT * FROM public.ms_shipment_rules WHERE account='oz_acc2'")
            c.execute((Path(__file__).resolve().parents[1]/'migrations/074_ozon_expense_basis.sql').read_text())
        @contextmanager
        def connection():yield self.conn
        self.p=patch.object(db,'get_conn',connection);self.p.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.p.stop();self.conn.rollback();self.conn.close()

    def test_idempotence_and_original_rule_history_are_preserved(self):
        first=basis();B.publish(first);B.publish(first)
        with self.conn.cursor() as c:
            c.execute('SELECT count(*) FROM fin_ozon_expense_basis');self.assertEqual(c.fetchone()[0],1)
            c.execute('SELECT count(*) FROM fin_ozon_rule_history');self.assertEqual(c.fetchone()[0],1)
            c.execute("SELECT retention::text,version FROM ms_shipment_rules WHERE account='oz_acc2'")
            r=c.fetchone();self.assertEqual(Decimal(r[0]),Decimal('.4'));self.assertEqual(r[1],first['version'])
        second=basis(source='corrected');B.publish(second)
        with self.conn.cursor() as c:
            c.execute('SELECT count(*) FROM fin_ozon_rule_history');self.assertEqual(c.fetchone()[0],2)
            c.execute('SELECT count(*) FROM fin_ozon_expense_basis');self.assertEqual(c.fetchone()[0],2)

    def test_immutable_old_snapshot_and_incomplete_current_state(self):
        b=basis();B.publish(b)
        with self.conn.cursor() as c:
            c.execute('SAVEPOINT immutable')
            with self.assertRaises(Exception):c.execute("UPDATE fin_ozon_expense_basis SET status='bad'")
            c.execute('ROLLBACK TO SAVEPOINT immutable')
        B.publish(basis(complete=False))
        with self.conn.cursor() as c:
            c.execute("SELECT valid_until<current_date FROM ms_shipment_rules WHERE account='oz_acc2'")
            self.assertTrue(c.fetchone()[0])
        with self.assertRaises(B.BasisUnavailable):B.get_basis('oz_acc2',as_of=date(2026,10,20))
        state=B.basis_status('oz_acc2',as_of=date(2026,10,20));self.assertEqual(state['last_usable']['version'],b['version'])

    def test_recovery_to_same_confirmed_version_restores_validity(self):
        b=basis();B.publish(b);B.publish(basis(complete=False));B.publish(b)
        with self.conn.cursor() as c:
            c.execute("SELECT valid_until>=current_date,version FROM ms_shipment_rules WHERE account='oz_acc2'")
            valid,version=c.fetchone();self.assertTrue(valid);self.assertEqual(version,b['version'])


if __name__=='__main__':unittest.main()
