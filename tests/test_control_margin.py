# поток: fin
"""Контрольная цифра финансового пайплайна.

CLAUDE.md, инвариант 4: nmID 216421567 за май 2026 → чистая 3741.77 ₽.
Раньше это было правило в тексте, которое никто не проверял; теперь — детерминированный тест.
Если цифра поехала — поехала модель себестоимости/комиссий, а не тест: подгонять запрещено,
расхождение объяснять (см. `.claude/rules/fin.md`).

Тест ходит в БД: `./venv/bin/python -m unittest tests.test_control_margin -v`.
"""
import unittest
from decimal import Decimal

from core.db import get_conn

ARTICLE = '216421567'
PERIOD_FROM = '2026-05-01'
PERIOD_TO = '2026-05-31'
EXPECTED_NET = Decimal('3741.77')
TOLERANCE = Decimal('0.01')


def control_rows():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT platform, account, qty, net_profit
              FROM margin_by_sku
             WHERE article = %s
               AND period_from >= %s
               AND period_to <= %s
            """,
            (ARTICLE, PERIOD_FROM, PERIOD_TO),
        )
        return cur.fetchall()


class TestControlMargin(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = control_rows()

    def test_single_row(self):
        self.assertEqual(
            len(self.rows), 1,
            f'ожидалась ровно одна строка витрины для {ARTICLE} за май 2026, '
            f'получено {len(self.rows)}',
        )

    def test_platform_account(self):
        self.assertTrue(self.rows, 'нет строки margin_by_sku — витрина не пересобрана')
        platform, account, _, _ = self.rows[0]
        self.assertEqual((platform, account), ('wb', 'wb_acc1'))

    def test_net_profit(self):
        self.assertTrue(self.rows, 'нет строки margin_by_sku — витрина не пересобрана')
        _, _, qty, net = self.rows[0]
        delta = abs(Decimal(net) - EXPECTED_NET)
        self.assertLessEqual(
            delta, TOLERANCE,
            f'контрольная цифра поехала: ожидалось {EXPECTED_NET} ₽, '
            f'получено {Decimal(net):.2f} ₽ (qty={qty}). Подгонять запрещено — '
            f'искать причину в модели себестоимости/комиссий.',
        )


if __name__ == '__main__':
    unittest.main()
