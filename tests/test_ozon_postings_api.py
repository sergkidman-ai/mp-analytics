# поток: fin
"""Integration checks for migration consumers, separate from the portable adapter commit."""
import ast
import json
import os
import unittest
from decimal import Decimal
from unittest.mock import patch
from collectors import ozon_postings as api
from shipment_guard.adapters import OrderAdapter
from core.ozon_posting import PostingProtocolError, posting_money_sql
from tests.test_ozon_postings_adapter import posting

class MigrationIntegrationTests(unittest.TestCase):

    def test_shipment_guard_old_and_new_raw_without_enabling_guard(self):
        for price in ('100.01', {'amount': '100.01', 'currency': 'RUB'}):
            p = posting()
            p['products'][0]['price'] = price
            with patch('shipment_guard.adapters.db.query', return_value=[{'payload': p}]), patch.object(OrderAdapter, 'request') as req:
                (_, items, _) = OrderAdapter().ozon('oz_acc2', '1-1-1')
            self.assertEqual(items, [('A', 3, Decimal('300.03'))])
            req.assert_not_called()

    def test_obsolete_finance_entrypoint_never_calls_api(self):
        from collectors.ozon import fetch_transactions
        with patch('collectors.ozon.requests.post') as request, self.assertRaisesRegex(RuntimeError, 'disabled'):
            fetch_transactions('oz_acc2', '2026-10-20', '2026-10-20')
        request.assert_not_called()

@unittest.skipUnless(os.getenv('OZON_POSTING_SQL_TEST') == '1', 'opt-in read-only PostgreSQL fixtures')
class SqlMoneyTests(unittest.TestCase):
    def query(self, sql, payload, params=()):
        from core import db
        with db.get_conn() as conn,conn.cursor() as cur:
            cur.execute('SET TRANSACTION READ ONLY')
            cur.execute(posting_money_sql(sql),(json.dumps(payload),)+tuple(params))
            return cur.fetchone()

    def test_both_schemas_and_sku_quantity_bridge_without_rounding(self):
        for legacy in (True,False):
            p=posting();fd=p['financial_data']['products'][0]
            if legacy:
                p['products'][0]['price']='100.01';p['products'][0]['currency_code']='RUB'
                fd['quantity']=3;fd['customer_price']=70.01
            row=self.query("""WITH p AS (SELECT %s::jsonb payload)
              SELECT RUB(pr.price)*FD_QTY(fd,p.payload), RUB(fd.customer_price),FD_QTY(fd,p.payload)
              FROM p,jsonb_array_elements(p.payload->'products') pr,
                     jsonb_array_elements(p.payload->'financial_data'->'products') fd""",p)
            self.assertEqual(row,(Decimal('300.03'),Decimal('70.01'),Decimal('3')))

    def test_customer_foreign_currency_is_unavailable_not_zero_or_rub(self):
        row=self.query('SELECT RUB(fd.customer_price) FROM (SELECT %s::jsonb fd) p',
                       {'customer_price':{'amount':'100.01','currency':'KZT'}})
        self.assertEqual(row,(None,))

    def test_invalid_money_still_fails_in_sql(self):
        for value in ({'amount':'1'}, {'amount':'1','currency':''}, {'amount':'NaN','currency':'RUB'}, {'amount':'1','currency':'USD'}):
            with self.subTest(value=value),self.assertRaises(Exception):
                self.query('SELECT RUB(pr.price) FROM (SELECT %s::jsonb pr) p',{'price':value})

    def test_multi_product_quantity_uses_sku_not_array_position(self):
        p=posting();p['products'].append({'sku':2,'quantity':2,'price':{'amount':'200','currency':'RUB'}})
        fd=p['financial_data']['products'];fd.append({'product_id':2,'customer_price':{'amount':'5000','currency':'KZT'}})
        fd.reverse()
        row=self.query("""WITH p AS (SELECT %s::jsonb payload)
          SELECT sum(FD_QTY(fd,p.payload)),
                 sum(RUB(fd.customer_price)*FD_QTY(fd,p.payload)) /
                    sum(FD_QTY(fd,p.payload)) FILTER (WHERE RUB(fd.customer_price) IS NOT NULL)
          FROM p,jsonb_array_elements(p.payload->'financial_data'->'products') fd""",p)
        self.assertEqual(row,(Decimal('5'),Decimal('70.01')))

    def test_actual_web_queries_handle_reordered_fbo_financial_products(self):
        tree=ast.parse((api.BASE_DIR/'web/app.py').read_text())
        p=posting();fd=p['financial_data']['products'][0]
        fd.update(old_price=120,actions=['promo'])
        p['products'].append({'sku':2,'quantity':2,'price':{'amount':'200','currency':'RUB'}})
        p['financial_data']['products'].insert(0,{'product_id':2,'price':200,'old_price':260,'actions':['promo']})
        for name in ('_ozon_pricing','ozon_promos'):
            fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==name)
            node=next(n for n in ast.walk(fn) if isinstance(n,ast.Call) and ast.unparse(n.func)=='posting_money_sql')
            sql=node.args[0].value
            if name=='_ozon_pricing':sql=sql.replace('GROUP BY 1',"AND pr.prod->>'sku'='1' GROUP BY 1")
            sql="WITH raw_ozon_posting AS (SELECT %s::jsonb payload,'oz_acc2'::text account,'2026-10-20'::timestamptz in_process_at) "+sql
            row=self.query(sql,p,(['oz_acc2'],'2026-10-20','2026-10-20'))
            if name=='_ozon_pricing':self.assertEqual(row,('1',Decimal(3),Decimal('300.03'),Decimal(360)))
            else:self.assertEqual(row,('promo',Decimal(5),Decimal('700.03'),Decimal('179.97'),2))


if __name__=='__main__':unittest.main()
