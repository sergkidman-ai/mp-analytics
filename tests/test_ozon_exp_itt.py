# поток: mkt
"""ITT в оценке экспериментов Ozon: назначенный SKU не выбывает из группы.

Повод: 19.08.2026 Ozon отклонил ставку по SKU 930641253 из группы A эксперимента E8
(«Ставка не входит в диапазон допустимых значений»). Исключить его из анализа — значит
считать результат по тем, кому воздействие удалось, то есть по выборке, отобранной после
рандомизации. Основной разрез — ITT, per-protocol только вторичный, перенос SKU
в контроль запрещён в любом разрезе.

Тесты офлайн: подставной реестр и подставной db.query, к БД не ходим.
"""
import sys, unittest

sys.path.insert(0, '/opt/mp-analytics')
sys.path.insert(0, '/opt/mp-analytics/tools')
import ozon_exp_eval as E  # noqa: E402

EXP = {
    'id': 'TEST', 'аккаунт': 'oz_acc1',
    'treatment': {'action': 'restore'}, 'control': {'action': 'hold'},
    'не_применено': [{'sku': '930641253', 'назначен': 'treatment', 'группа': 'A',
                      'applied': False, 'причина': 'Ozon отклонил ставку'}],
}
JOURNAL = {'restore': ['111', '222', '930641253'], 'hold': ['333', '444']}


class ПодставнойЖурнал:
    """Заменяет db.query на время теста: отдаёт состав групп из словаря."""

    def __enter__(self):
        self.orig = E.db.query
        E.db.query = lambda sql, params=None: [
            {'account': params[0], 'sku': s} for a in params[1] for s in JOURNAL.get(a, [])]
        return self

    def __exit__(self, *a):
        E.db.query = self.orig


class РазрезITT(unittest.TestCase):
    def test_itt_оставляет_неприменённый_sku_в_назначенной_группе(self):
        with ПодставнойЖурнал():
            t = E.cohort(EXP, 'treatment', 'itt')
        self.assertEqual(len(t), 3)
        self.assertIn(('oz_acc1', '930641253'), t)

    def test_per_protocol_убирает_только_неприменённый(self):
        with ПодставнойЖурнал():
            t = E.cohort(EXP, 'treatment', 'pp')
        self.assertEqual({s for _, s in t}, {'111', '222'})

    def test_неприменённый_не_попадает_в_контроль_ни_в_одном_разрезе(self):
        with ПодставнойЖурнал():
            for mode in ('itt', 'pp'):
                c = E.cohort(EXP, 'control', mode)
                self.assertNotIn(('oz_acc1', '930641253'), c, mode)
                self.assertEqual(len(c), 2, mode)

    def test_причина_отказа_доступна_для_отчёта(self):
        na = E.not_applied(EXP, 'treatment')
        self.assertEqual(na['930641253']['группа'], 'A')
        self.assertFalse(na['930641253']['applied'])
        self.assertIn('отклонил', na['930641253']['причина'])

    def test_знаменатель_метрик_считает_назначенные_а_не_найденные(self):
        # ads() делит на len(skus) переданной когорты: SKU без строк в витрине входит нулём
        with open(E.__file__, encoding='utf-8') as f:
            self.assertIn('out[k] / n if n else 0.0', f.read())


class ПорогиG6(unittest.TestCase):
    def test_зелёный_когда_оба_показателя_в_норме(self):
        self.assertEqual(E._level(0.02, 1.13), 'GREEN')

    def test_жёлтый_по_доле_хвоста(self):
        self.assertEqual(E._level(0.20, 1.00), 'YELLOW')

    def test_красный_по_доле_хвоста(self):
        self.assertEqual(E._level(0.32, 1.00), 'RED')

    def test_исторические_32_процента_хвоста_дают_red(self):
        # порог 35 % не сработал бы на волне 1, которая съедала 32 % бюджета
        self.assertEqual(E._level(0.32, None), 'RED')

    def test_жёлтый_и_красный_по_показам_ядра(self):
        self.assertEqual(E._level(0.01, 0.85), 'YELLOW')
        self.assertEqual(E._level(0.01, 0.60), 'RED')

    def test_нет_данных_по_ядру_не_создаёт_ложный_сигнал(self):
        self.assertEqual(E._level(0.01, None), 'GREEN')


if __name__ == '__main__':
    unittest.main(verbosity=2)
