# поток: mkt
"""Фильтр отката ставок Ozon: единица решения — SKU по аккаунту, а не связка campaign×sku.

Повод: 18.08.2026 откат разгона (E4) отобрал «нулевые» позиции по связке кампания×SKU.
Один и тот же SKU стоит в нескольких кампаниях, а заказ Ozon атрибутирует ровно одной —
поэтому продающийся товар выглядел нулевым в каждой «молчащей» кампании и получал откат
ставки. Так были откачены 21 SKU с 409 325 ₽ рекламной выручки.

Тесты офлайн: чистая функция разбиения плюс сам ROLLBACK_SQL, прогнанный по подставным
CTE вместо витрин (обычный SELECT, ничего не пишется). Без БД SQL-часть пропускается.
"""
import re, sys, unittest, datetime as dt

sys.path.insert(0, '/opt/mp-analytics')
from tools.ozon_bid_ramp import split_converters, stale_stats, ROLLBACK_SQL  # noqa: E402

# Подстановка витрин на фикстуры: имя таблицы -> (SELECT ... ) имя_таблицы.
FIXTURE_TABLES = {
    'mkt_ozon_bid_step_log': 'account, applied, campaign_id, sku, bid_before, bid_after, step_date',
    'ozon_bids': 'account, captured_at, campaign_id, sku, bid',
    'mkt_ozon_ads_sku_daily': ('account, stat_date, campaign_id, sku, money_spent, orders_qty, '
                               'orders_money, clicks, views'),
}


def _values(cols, rows, casts):
    """VALUES-фикстура с явными типами в первой строке (Postgres выводит тип по ней)."""
    def lit(v):
        if v is None:
            return 'NULL'
        if isinstance(v, (dt.date, dt.datetime)):
            return "'" + v.isoformat() + "'"
        if isinstance(v, str):
            return "'" + v.replace("'", "''") + "'"
        if isinstance(v, bool):
            return 'true' if v else 'false'
        return str(v)
    body = []
    for i, r in enumerate(rows):
        cells = [lit(v) + ('::' + casts[j] if i == 0 else '') for j, v in enumerate(r)]
        body.append('(' + ','.join(cells) + ')')
    return f"SELECT * FROM (VALUES {','.join(body)}) _t({cols})"


def build_sql(step_log, bids, daily):
    """ROLLBACK_SQL поверх фикстур: те же CTE, те же join'ы, но читает подставные строки."""
    sql = ROLLBACK_SQL
    subs = {
        'mkt_ozon_bid_step_log': _values(
            FIXTURE_TABLES['mkt_ozon_bid_step_log'], step_log,
            ['text', 'boolean', 'bigint', 'bigint', 'numeric', 'numeric', 'date']),
        'ozon_bids': _values(
            FIXTURE_TABLES['ozon_bids'], bids,
            ['text', 'timestamptz', 'bigint', 'bigint', 'numeric']),
        'mkt_ozon_ads_sku_daily': _values(
            FIXTURE_TABLES['mkt_ozon_ads_sku_daily'], daily,
            ['text', 'date', 'bigint', 'bigint', 'numeric', 'numeric', 'numeric', 'int', 'int']),
    }
    for name, body in subs.items():
        sql = re.sub(rf'FROM {name}\b', f'FROM ({body}) {name}', sql)
    return sql


class РазбиениеКогорты(unittest.TestCase):
    """Чистая функция: кто нулевой, кто конвертер, кто спасён кросс-кампанийным заказом."""

    def test_sku_продаёт_в_другой_кампании_в_откат_не_идёт(self):
        rows = [
            {'campaign_id': '1', 'sku': '111', 'ord': 0, 'ord_sku': 2, 'rev_sku': 19000},
            {'campaign_id': '2', 'sku': '111', 'ord': 2, 'ord_sku': 2, 'rev_sku': 19000},
            {'campaign_id': '1', 'sku': '222', 'ord': 0, 'ord_sku': 0, 'rev_sku': 0},
        ]
        zero, conv, cross = split_converters(rows)
        self.assertEqual([r['sku'] for r in zero], ['222'])
        self.assertEqual({r['sku'] for r in conv}, {'111'})
        self.assertEqual([(r['campaign_id'], r['sku']) for r in cross], [('1', '111')])

    def test_старый_фильтр_откатил_бы_именно_их(self):
        """Контрольный: по связке (`ord`) обе связки SKU 111 попадали в откат."""
        rows = [
            {'campaign_id': '1', 'sku': '111', 'ord': 0, 'ord_sku': 2, 'rev_sku': 19000},
            {'campaign_id': '2', 'sku': '111', 'ord': 2, 'ord_sku': 2, 'rev_sku': 19000},
        ]
        по_связке = [r for r in rows if float(r['ord']) == 0]
        zero, _, _ = split_converters(rows)
        self.assertEqual(len(по_связке), 1)
        self.assertEqual(len(zero), 0)

    def test_пустые_значения_не_роняют(self):
        zero, conv, cross = split_converters([{'campaign_id': '1', 'sku': '1', 'ord': None,
                                               'ord_sku': None, 'rev_sku': None}])
        self.assertEqual((len(zero), len(conv), len(cross)), (1, 0, 0))


class СвежестьВитрины(unittest.TestCase):
    """Откат нельзя отправлять, пока последний день статистики не загружен."""

    def test_вчерашний_день_свежий(self):
        self.assertFalse(stale_stats(dt.date(2026, 8, 19), dt.date(2026, 8, 20)))

    def test_позавчерашний_протух(self):
        self.assertTrue(stale_stats(dt.date(2026, 8, 18), dt.date(2026, 8, 20)))

    def test_пустая_витрина_протухла(self):
        self.assertTrue(stale_stats(None, dt.date(2026, 8, 20)))


class ЗапросКогорты(unittest.TestCase):
    """ROLLBACK_SQL на фикстурах: ord — по связке, ord_sku — по SKU во всех кампаниях."""

    @classmethod
    def setUpClass(cls):
        try:
            from core import db
            db.query('SELECT 1')
            cls.db = db
        except Exception as e:  # noqa: BLE001
            raise unittest.SkipTest(f'нет БД: {e}')

    def _run(self, daily, since='2026-08-08'):
        step = [('oz_acc1', True, 1, 111, 10, 11, dt.date(2026, 8, 8)),
                ('oz_acc1', True, 2, 111, 10, 11, dt.date(2026, 8, 8)),
                ('oz_acc1', True, 1, 222, 10, 11, dt.date(2026, 8, 8))]
        bids = [('oz_acc1', '2026-08-18 06:00+03', 1, 111, 12),
                ('oz_acc1', '2026-08-18 06:00+03', 2, 111, 12),
                ('oz_acc1', '2026-08-18 06:00+03', 1, 222, 12)]
        sql = build_sql(step, bids, daily)
        rows = self.db.query(sql, {'acc': 'oz_acc1', 'since': since})
        return {(r['campaign_id'], r['sku']): r for r in rows}

    def test_заказ_в_соседней_кампании_виден_в_ord_sku(self):
        daily = [('oz_acc1', dt.date(2026, 8, 12), 1, 111, 500, 0, 0, 30, 900),
                 ('oz_acc1', dt.date(2026, 8, 12), 2, 111, 40, 1, 19000, 3, 100),
                 ('oz_acc1', dt.date(2026, 8, 12), 1, 222, 300, 0, 0, 20, 700)]
        r = self._run(daily)
        self.assertEqual(float(r[('1', '111')]['ord']), 0.0)      # в своей кампании тишина
        self.assertEqual(float(r[('1', '111')]['ord_sku']), 1.0)  # но SKU продал
        self.assertEqual(float(r[('1', '222')]['ord_sku']), 0.0)
        zero, _, cross = split_converters(list(r.values()))
        self.assertEqual([x['sku'] for x in zero], ['222'])
        self.assertEqual([x['campaign_id'] for x in cross], ['1'])

    def test_лаг_заказа_в_день_отката(self):
        """Заказ пришёл в день самого отката: строка за этот день уже должна спасать SKU.

        Вторая половина — цена лага: если снимок за день отката ещё не загружен, тот же SKU
        уходит в откат. Поэтому запуск отката без свежей витрины запрещён (см. stale_stats).
        """
        поздний = ('oz_acc1', dt.date(2026, 8, 18), 2, 111, 40, 1, 19000, 3, 100)
        база = [('oz_acc1', dt.date(2026, 8, 12), 1, 111, 500, 0, 0, 30, 900),
                ('oz_acc1', dt.date(2026, 8, 12), 1, 222, 300, 0, 0, 20, 700)]
        с_заказом = self._run(база + [поздний])
        self.assertEqual(float(с_заказом[('1', '111')]['ord_sku']), 1.0)
        zero, _, _ = split_converters(list(с_заказом.values()))
        self.assertNotIn('111', [x['sku'] for x in zero])

        без_заказа = self._run(база)
        zero2, _, _ = split_converters(list(без_заказа.values()))
        self.assertIn('111', [x['sku'] for x in zero2])


if __name__ == '__main__':
    unittest.main()
