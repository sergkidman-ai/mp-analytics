# поток: fin
import datetime as dt
from decimal import Decimal as D
import unittest

from collectors import yandex_closure as closure
from collectors import yandex_finance_basis as basis


class DocumentMovements(unittest.TestCase):
    def document(self, category='revenue', amount='1685'):
        sheet = ('period_closure_income_sold_refunds' if category == 'revenue'
                 else 'period_closure_income_sold_defect_refunds')
        return {'order_id': 'business-sale', 'transaction_date': dt.date(2026, 10, 9),
                'category': category, 'amount': amount, 'source_sheet': sheet,
                'transaction_id': '2026-10:1:' + sheet + ':0'}

    def test_sold_sheet_is_revenue_and_defect_sheet_is_return(self):
        common = {'ORDER_ID': 'business-sale', 'TRANSACTION_DATE': '09.10.2026'}
        rows = closure._parse_csvs({
            'period_closure_income_sold_refunds.csv': [dict(common, TRANSACTION_SUM='1685')],
            'period_closure_income_sold_defect_refunds.csv': [dict(common, TRANSACTION_SUM='-100')]
        }, 'ya_acc1', '2026-10', '1')
        self.assertEqual([(r['category'], r['amount']) for r in rows], [('revenue', 1685), ('returns', -100)])
        self.assertEqual(rows[0]['source_sheet'], 'period_closure_income_sold_refunds')

    def test_document_only_sale_and_refund_reconcile_with_correct_sign(self):
        for category, amount, kind in [('revenue', '1685', 'PAYMENT'), ('returns', '-100', 'REFUND')]:
            with self.subTest(category=category):
                doc = self.document(category, amount)
                ledger = basis.with_document_payments([], [doc], '2026-10')
                self.assertEqual(ledger[0]['type'], kind)
                self.assertEqual(ledger[0]['amount'], abs(D(amount)))
                check = basis.reconcile(ledger, [doc])
                self.assertEqual(check['unmatched_closure'], 0)
                self.assertEqual(check['additional_payments'], 0)

    def test_existing_order_payment_is_not_counted_twice(self):
        ordinary = {'order_id': 'business-sale', 'date': '2026-10-09', 'type': 'PAYMENT',
                    'amount': D('1685'), 'payment_id': 'ordinary'}
        result = basis.with_document_payments([ordinary], [self.document()], '2026-10')
        self.assertEqual(result, [ordinary])

    def test_different_stats_amount_requires_review(self):
        ordinary = {'order_id': 'business-sale', 'date': '2026-10-09', 'type': 'PAYMENT',
                    'amount': D('1600'), 'payment_id': 'ordinary'}
        with self.assertRaisesRegex(RuntimeError, 'overlap'):
            basis.with_document_payments([ordinary], [self.document()], '2026-10')

    def test_missing_ordinary_closure_payment_is_not_supplemented(self):
        doc = self.document(); doc.pop('source_sheet'); doc['transaction_id'] = 'ordinary-id'
        rows = basis.with_document_payments([], [doc], '2026-10')
        self.assertEqual(rows, [])
        self.assertEqual(basis.reconcile(rows, [doc])['unmatched_closure'], 1)

    def test_only_current_month_ordinary_orders_are_required(self):
        rows = [self.document(), {'order_id': 'old', 'transaction_date': '2026-01-01'},
                {'order_id': 'required', 'transaction_date': '2026-10-10'}]
        self.assertEqual(basis.closure_order_ids(rows, '2026-10'), {'required'})

    def test_duplicate_document_or_unexpected_direction_is_rejected(self):
        doc = self.document()
        with self.assertRaisesRegex(RuntimeError, 'duplicate'):
            basis.with_document_payments([], [doc, doc], '2026-10')
        with self.assertRaisesRegex(RuntimeError, 'direction'):
            basis.with_document_payments([], [self.document(amount='-1685')], '2026-10')

    def test_source_sheet_can_be_recovered_from_stored_synthetic_identity(self):
        doc = self.document();doc.pop('source_sheet')
        self.assertEqual(basis.document_sheet(doc), 'period_closure_income_sold_refunds')


if __name__ == '__main__':
    unittest.main()
