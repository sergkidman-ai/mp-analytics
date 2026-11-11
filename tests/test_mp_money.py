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

    def test_overdue_uses_original_due_date_not_shifted_cashflow_day(self):
        row=payout('440149.85',org='digital',account='wb_acc1',week='2026-10-05',
                   day=dt.date(2026,11,11))
        r=compose_mp_money([], [row], ACCOUNTS, today=dt.date(2026,11,11))
        self.assertEqual(r['wb_overdue_payments'][0]['due_date'],'2026-11-09')
        self.assertEqual(r['wb_overdue_payments'][0]['days_overdue'],2)
        self.assertEqual(r['wb_overdue_max_days'],2)
        next_day=compose_mp_money([], [row], ACCOUNTS, today=dt.date(2026,11,12))
        self.assertEqual(next_day['wb_overdue_payments'][0]['days_overdue'],3)

    def test_details_preserve_separate_payments_and_filter_organization(self):
        rows=[payout('10',week='2026-10-05'),payout('20',week='2026-09-28'),
              payout('30',org='digital',account='wb_acc1',week='2026-10-05')]
        r=compose_mp_money([],rows,ACCOUNTS,'diskver',today=dt.date(2026,11,11))
        self.assertEqual([p['days_overdue'] for p in r['wb_overdue_payments']],[9,2])
        self.assertEqual(sum(p['amount'] for p in r['wb_overdue_payments']),r['wb_overdue'])
        self.assertEqual(r['wb_overdue_count'],len(r['wb_overdue_payments']))

    def test_unknown_due_date_not_guessed(self):
        r=compose_mp_money([], [payout('10',week='unknown')],ACCOUNTS,today=DAY)
        self.assertIsNone(r['wb_overdue_payments'][0]['days_overdue'])
        self.assertIsNone(r['wb_overdue_max_days'])


if __name__ == '__main__':
    unittest.main()
