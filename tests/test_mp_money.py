# поток: fin
import datetime as dt
import unittest
from decimal import Decimal
from web.mp_money import compose_mp_money

ACCOUNTS = {'wb_acc1':'digital', 'wb_acc2':'diskver', 'oz_acc1':'digital', 'oz_acc2':'diskver'}
DAY = dt.date(2026, 10, 5)


def balance(platform, account, value):
    return dict(platform=platform, account=account, balance=Decimal(value), day=DAY)


def payout(value, org='diskver', account='wb_acc2', week='2026-08-17', day=DAY):
    return dict(org_inn=org, ref=f'{account}:{week}', amount=Decimal(value), day=day)


class MpMoneyTest(unittest.TestCase):
    def test_overdue_added_once_to_wb_and_total(self):
        r = compose_mp_money([balance('wb','wb_acc1','100'),balance('wb','wb_acc2','200'),
                              balance('ozon','oz_acc1','400')],
                             [payout('97.42'),payout('81.74',week='2026-08-24')],ACCOUNTS)
        self.assertEqual(r['wb_account_balance'],300)
        self.assertEqual(r['wb_overdue'],179.16)
        self.assertEqual(r['wb'],479.16)
        self.assertEqual(r['total'],879.16)
        self.assertEqual(r['balance_total'],700)
        self.assertEqual(r['wb_overdue_count'],2)

    def test_organization_filter_and_duplicate_reference(self):
        rows=[balance('wb','wb_acc1','100'),balance('wb','wb_acc2','200')]
        overdue=[payout('10',day=DAY-dt.timedelta(days=1)),payout('12'),payout('12')]
        a=compose_mp_money(rows,overdue,ACCOUNTS,'digital')
        b=compose_mp_money(rows,overdue,ACCOUNTS,'diskver')
        self.assertEqual(a['total'],100)
        self.assertEqual(a['wb_overdue'],0)
        self.assertEqual(b['total'],212)
        self.assertEqual(b['wb_overdue_count'],1)

    def test_paid_removed_from_cashflow_no_longer_increases_total(self):
        rows=[balance('wb','wb_acc2','200')]
        self.assertEqual(compose_mp_money(rows,[payout('12')],ACCOUNTS)['total'],212)
        self.assertEqual(compose_mp_money(rows,[],ACCOUNTS)['total'],200)

    def test_debt_visible_without_balance_and_invalid_refs_excluded(self):
        r=compose_mp_money([], [payout('12'),payout('-5',week='2026-08-24'),
                              payout('100',org='digital')], ACCOUNTS,'diskver')
        self.assertEqual(r['total'],12)
        self.assertIsNone(r['wb_account_balance'])
        self.assertIn('wb_acc2',r['missing'])
        self.assertIsNone(compose_mp_money([],[],ACCOUNTS))


if __name__ == '__main__':
    unittest.main()
