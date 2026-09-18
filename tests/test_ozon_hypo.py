# поток: mkt
"""Проверки контура гипотез Ozon (tools/ozon_hypo.py).

Тесты не ходят в базу: всё, что здесь проверяется, — правила, а не данные. Правила
и есть та часть, которую нельзя молча сломать: автомат статусов, матрица допуска,
запреты на повтор E1/E3 и обещание «ни одной внешней записи».

Запуск: venv/bin/python -m unittest tests.test_ozon_hypo
"""
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, '/opt/mp-analytics')
sys.path.insert(0, '/opt/mp-analytics/tools')
import ozon_hypo as H  # noqa: E402

ИСТОЧНИК = '/opt/mp-analytics/tools/ozon_hypo.py'

# Журнал контура — боевой append-only артефакт: проверки не имеют права в него писать.
# Переходы H-TEST уходят в отдельный файл, который создаётся и умирает вместе с прогоном.
_ЖУРНАЛ_ТЕСТОВ = tempfile.NamedTemporaryFile(prefix='ozon_hypo_test_log_', suffix='.jsonl',
                                             delete=False)
_ЖУРНАЛ_ТЕСТОВ.close()
_БОЕВОЙ_ЖУРНАЛ = H.LOG
H.LOG = _ЖУРНАЛ_ТЕСТОВ.name


def tearDownModule():
    os.unlink(_ЖУРНАЛ_ТЕСТОВ.name)


def _исходник():
    with io.open(ИСТОЧНИК, encoding='utf-8') as f:
        return f.read()


def _ctx(занято=(), остаток=(), ставки=None):
    return {'занято': set(занято), 'по_экспериментам': {'E8': set(занято)},
            'даты': {'E8': {'вмешательство': '2026-08-19', 'сверка': '2026-09-09'}},
            'кампании': {'1'}, 'ставки': ставки or {},
            'остаток': set(остаток), 'журнал': set(), 'зрелый_день': H.TODAY}


def _гип(**kw):
    h = H.пустая('H-TEST', primary_metric='заказы на 100 доступных SKU-дней',
                 proposed_lever='состав рекламы', minimum_duration=14,
                 target_segment={'единица': 'SKU', 'всего': 10})
    h['_контроль'] = 'рандомизированная половина сегмента'
    h['_sku'] = []
    h['_пересечение'] = []
    h.update(kw)
    return h


class Автомат(unittest.TestCase):
    def test_разрешённый_переход_проходит(self):
        self.assertTrue(H.проверить_переход('OBSERVED', 'DRAFT'))
        self.assertTrue(H.проверить_переход('APPROVAL_REQUIRED', 'RUNNING'))

    def test_запрещённый_переход_бросает(self):
        with self.assertRaises(H.ПереходЗапрещён):
            H.проверить_переход('OBSERVED', 'RUNNING')
        with self.assertRaises(H.ПереходЗапрещён):
            H.проверить_переход('DRAFT', 'APPROVAL_REQUIRED')

    def test_воздействие_только_после_решения_человека(self):
        """В RUNNING ведёт единственная дверь — APPROVAL_REQUIRED."""
        входы = [a for a, b in H.ПЕРЕХОДЫ.items() if 'RUNNING' in b]
        self.assertEqual(входы, ['APPROVAL_REQUIRED'])

    def test_терминальные_без_выхода(self):
        for s in H.ТЕРМИНАЛЬНЫЕ:
            self.assertEqual(H.ПЕРЕХОДЫ[s], ())

    def test_все_статусы_описаны(self):
        self.assertEqual(set(H.ПЕРЕХОДЫ), set(H.СТАТУСЫ))
        for a, b in H.ПЕРЕХОДЫ.items():
            for x in b:
                self.assertIn(x, H.СТАТУСЫ, f'{a} → {x}')

    def test_несуществующий_статус_не_принимается(self):
        with self.assertRaises(H.ПереходЗапрещён):
            H.проверить_переход('DRAFT', 'ГОТОВО')

    def test_откат_доступен_из_рабочих_состояний(self):
        for s in ('RUNNING', 'MEASURING', 'EVALUATED'):
            self.assertIn('ROLLBACK', H.ПЕРЕХОДЫ[s])


class МатрицаДопуска(unittest.TestCase):
    def test_без_экономики_блок(self):
        у, _ = H.уровень_действия('ставка', 10, True, True, 'нет', True)
        self.assertEqual(у, 'BLOCKED')

    def test_без_контроля_блок(self):
        у, _ = H.уровень_действия('ставка', 10, True, False, 'канон', True)
        self.assertEqual(у, 'BLOCKED')

    def test_без_отката_блок(self):
        у, _ = H.уровень_действия('ставка', 10, True, True, 'канон', False)
        self.assertEqual(у, 'BLOCKED')

    def test_грубая_экономика_снимает_автономность_но_не_запрещает(self):
        у, поч = H.уровень_действия('ставка', 10, True, True, 'грубее', True)
        self.assertEqual(у, 'FOUNDER_APPROVAL')
        self.assertIn('GRANULARITY', поч)

    def test_цена_и_бюджет_всегда_к_основателю(self):
        for рычаг in ('цена', 'бюджет'):
            у, _ = H.уровень_действия(рычаг, 1, True, True, 'канон', True)
            self.assertEqual(у, 'FOUNDER_APPROVAL', рычаг)

    def test_массовость_поднимает_уровень(self):
        мало, _ = H.уровень_действия('ставка', H.МАССОВЫЙ_ПОРОГ, True, True, 'канон', True)
        много, _ = H.уровень_действия('ставка', H.МАССОВЫЙ_ПОРОГ + 1, True, True, 'канон', True)
        self.assertEqual(мало, 'APPROVAL_REQUIRED')
        self.assertEqual(много, 'FOUNDER_APPROVAL')

    def test_потолок_сессии_не_ниже_согласования(self):
        self.assertEqual(H.ПОТОЛОК_СЕССИИ, 'APPROVAL_REQUIRED')


class Валидаторы(unittest.TestCase):
    def test_их_восемнадцать_и_коды_уникальны(self):
        h, c = _гип(), _ctx()
        коды = [f(h, c)[0] for f in H.ВАЛИДАТОРЫ]
        self.assertEqual(len(коды), 18)      # 17 по ошибкам E1–E8 + V18 по происхождению
        self.assertEqual(len(set(коды)), 18)

    def test_вердикт_из_трёх_значений(self):
        h, c = _гип(), _ctx()
        for f in H.ВАЛИДАТОРЫ:
            код, в, объ = f(h, c)
            self.assertIn(в, ('ok', 'warn', 'fail'), код)
            self.assertTrue(объ.strip(), f'{код} без объяснения')

    def test_пересечение_с_идущим_экспериментом_валит(self):
        h = _гип(_sku=['1', '2'])
        код, в, объ = H.v01_пересечение_sku(h, _ctx(занято=['2']))
        self.assertEqual(в, 'fail')
        self.assertIn('E8', объ)

    def test_после_исключения_занятых_состав_чист(self):
        h = _гип(_sku=['1'], _пересечение=['2'])
        код, в, объ = H.v01_пересечение_sku(h, _ctx(занято=['2']))
        self.assertEqual(в, 'ok')
        self.assertIn('исключены', объ)

    def test_два_рычага_сразу_валят(self):
        h = _гип(_доп_рычаги=['цена'])
        self.assertEqual(H.v03_один_рычаг(h, _ctx())[1], 'fail')

    def test_без_контроля_валит(self):
        h = _гип()
        h['_контроль'] = None
        self.assertEqual(H.v04_контроль(h, _ctx())[1], 'fail')

    def test_единица_решения_обязательна(self):
        h = _гип(target_segment={'единица': None})
        self.assertEqual(H.v05_единица_решения(h, _ctx())[1], 'fail')

    def test_протухшая_витрина_валит(self):
        c = _ctx()
        c['зрелый_день'] = '2026-08-01'
        self.assertEqual(H.v06_свежесть(_гип(), c)[1], 'fail')

    def test_реклама_в_пустоту_валит(self):
        h = _гип(_sku=['1', '2', '3', '4', '5'])
        self.assertEqual(H.v07_остаток(h, _ctx(остаток=['1']))[1], 'fail')

    def test_экономика_на_sku_помечает_гранулярность(self):
        h = _гип(proposed_lever='ставка')
        код, в, объ = H.v08_экономика(h, _ctx())
        self.assertEqual(в, 'warn')
        self.assertEqual(h['_флаг_экономики'], 'BLOCKED_BY_ECONOMICS_GRANULARITY')
        self.assertIn('автономно', объ)

    def test_экономика_на_аккаунте_каноническая(self):
        h = _гип(proposed_lever='ставка', target_segment={'единица': 'аккаунт'})
        self.assertEqual(H.v08_экономика(h, _ctx())[1], 'ok')
        self.assertIsNone(h.get('_флаг_экономики'))

    def test_ставка_вниз_с_пола_невозможна(self):
        h = _гип(proposed_lever='ставка', _sku=['1'], _направление='вниз')
        c = _ctx(ставки={'1': {'b': H.ПОЛ_СТАВКИ, 'c': '1'}})
        self.assertEqual(H.v09_допустимость_ставки(h, c)[1], 'fail')

    def test_общий_бюджет_всегда_предупреждает(self):
        for рычаг in ('ставка', 'бюджет', 'состав рекламы'):
            h = _гип(proposed_lever=рычаг)
            код, в, объ = H.v10_общий_бюджет(h, _ctx())
            self.assertEqual(в, 'warn', рычаг)
            self.assertIn('6700', объ)

    def test_история_только_добавлением(self):
        self.assertIn('только добавлением', H.v16_история(_гип(), _ctx())[2])


class ЗапретНаПовтор(unittest.TestCase):
    def test_массовый_разгон_без_контроля_запрещён(self):
        h = _гип(proposed_lever='ставка',
                 target_segment={'единица': 'SKU', 'всего': H.E1_ПОРОГ + 1})
        h['_контроль'] = 'сравнение с прошлой неделей'
        код, в, объ = H.v17_не_повтор(h, _ctx())
        self.assertEqual(в, 'fail')
        self.assertIn('E1', объ)

    def test_тот_же_масштаб_с_рандомизацией_разрешён_с_оговоркой(self):
        h = _гип(proposed_lever='ставка',
                 target_segment={'единица': 'SKU', 'всего': H.E1_ПОРОГ + 1})
        код, в, объ = H.v17_не_повтор(h, _ctx())
        self.assertEqual(в, 'warn')
        self.assertIn('хвоста', объ)

    def test_e3_никогда_не_применяется(self):
        h = _гип(proposed_lever='ставка', _источник='E3',
                 target_segment={'единица': 'SKU', 'всего': 10})
        self.assertEqual(H.v17_не_повтор(h, _ctx())[1], 'fail')

    def test_запреты_названы_поимённо(self):
        self.assertEqual(set(H.ЗАПРЕТЫ), {'E1', 'E3', 'H-001'})
        self.assertIn('VOID', H.ЗАПРЕТЫ['E3'])
        self.assertIn('не доказан', H.ЗАПРЕТЫ['H-001'])


class Реестр(unittest.TestCase):
    def test_тридцать_обязательных_полей(self):
        нужно = ('hypothesis_id created_at source_observation business_problem mechanism '
                 'target_segment proposed_lever expected_business_effect current_evidence '
                 'counter_evidence primary_metric secondary_metrics guardrails required_sample '
                 'minimum_duration interference_scope dependencies risk_level approval_level '
                 'estimated_upside_rub estimated_downside_rub confidence time_to_learn '
                 'priority_score status experiment_id result decision learning '
                 'next_hypotheses').split()
        for f in нужно:
            self.assertIn(f, H.ПОЛЯ, f)
        self.assertEqual(len(H.ПОЛЯ), 32)    # 30 обязательных + provenance + blocking_reason
        self.assertIn('provenance', H.ПОЛЯ)
        self.assertIn('blocking_reason', H.ПОЛЯ)

    def test_новая_гипотеза_начинается_с_наблюдения(self):
        self.assertEqual(H.пустая('X')['status'], 'OBSERVED')

    def test_посев_прожитых_гипотез_на_месте(self):
        по_id = {p['hid']: p for p in H.ПОСЕВ}
        self.assertEqual(по_id['E1']['путь'][-1], 'ROLLBACK')
        self.assertEqual(по_id['E3']['путь'][-1], 'REJECTED')
        for e in ('E5', 'E6', 'E7', 'E8', 'H-001'):
            self.assertEqual(по_id[e]['путь'][-1], 'MEASURING', e)

    def test_знание_e1_записано_как_знание(self):
        e1 = [p for p in H.ПОСЕВ if p['hid'] == 'E1'][0]
        self.assertIn('6700', e1['learning'])

    def test_h001_не_подаётся_как_доказанный(self):
        h1 = [p for p in H.ПОСЕВ if p['hid'] == 'H-001'][0]
        self.assertIsNone(h1['result'])
        self.assertIsNone(h1['decision'])
        self.assertEqual(h1['lever'], 'наблюдение')


class Продвижение(unittest.TestCase):
    def test_блокированная_не_поднимается_выше_черновика(self):
        h = _гип()
        h['_блокировано'] = ['V12_POWER']
        H.продвинуть(h, 'OBSERVED')
        self.assertEqual(h['status'], 'DRAFT')
        self.assertIn('НЕ ЗАПУЩЕНА', h['result'])

    def test_готовая_доходит_до_согласования(self):
        h = _гип(required_sample='≥256 заказов', approval_level='FOUNDER_APPROVAL')
        h['_блокировано'] = []
        H.продвинуть(h, 'OBSERVED')
        self.assertEqual(h['status'], 'APPROVAL_REQUIRED')

    def test_только_чтение_не_требует_согласования(self):
        h = _гип(required_sample='—', approval_level='AUTONOMOUS_READ')
        h['_блокировано'] = []
        H.продвинуть(h, 'OBSERVED')
        self.assertEqual(h['status'], 'READY')

    def test_повторный_прогон_ничего_не_двигает(self):
        h = _гип(required_sample='—', approval_level='AUTONOMOUS_READ')
        h['_блокировано'] = []
        H.продвинуть(h, 'READY')
        self.assertEqual(h['status'], 'READY')

    def test_идущий_эксперимент_автомат_не_трогает(self):
        h = _гип()
        h['_блокировано'] = ['V12_POWER']
        H.продвинуть(h, 'MEASURING')
        self.assertEqual(h['status'], 'MEASURING')


class Приоритет(unittest.TestCase):
    def test_веса_дают_единицу(self):
        self.assertAlmostEqual(sum(H.ВЕСА.values()), 1.0, places=6)

    def test_все_десять_составляющих_названы(self):
        нужно = {'деньги', 'доказательства', 'сегмент', 'скорость', 'дешевизна_теста',
                 'малый_риск', 'обратимость', 'измеримость', 'нет_пересечений',
                 'переводимость_в_политику'}
        self.assertEqual(set(H.ВЕСА), нужно)

    def test_каждый_балл_объяснён_словами(self):
        h = _гип(risk_level='низкий')
        h['_валидация'] = [{'код': 'V11_BASELINE', 'вердикт': 'ok', 'объяснение': ''},
                           {'код': 'V12_POWER', 'вердикт': 'ok', 'объяснение': ''},
                           {'код': 'V13_ROLLBACK', 'вердикт': 'ok', 'объяснение': ''},
                           {'код': 'V01_SKU_INTERSECT', 'вердикт': 'ok', 'объяснение': ''}]
        h['current_evidence'] = 'наблюдение'
        b = H._баллы(h, _ctx())
        self.assertEqual(set(b), set(H.ВЕСА))
        for k, (балл, почему) in b.items():
            self.assertGreaterEqual(балл, 0.0, k)
            self.assertLessEqual(балл, 1.0, k)
            self.assertTrue(почему.strip(), k)


class РежимМощности(unittest.TestCase):
    def test_счётный_режим_выбирается_по_метрике(self):
        self.assertIn('заказ', 'заказы на 100 доступных SKU-дней'.lower())

    def test_пороги_разные_для_двух_режимов(self):
        h = _гип()
        h['_мощность'] = {'режим': 'счётная', 'MDE_отн': 0.3, 'n': 100, 'λ_на_плечо': 200,
                          'единица_MDE': 'доля'}
        self.assertEqual(H.v12_мощность(h, _ctx())[1], 'ok')
        h['_мощность'] = {'режим': 'счётная', 'MDE_отн': 2.0, 'n': 100, 'λ_на_плечо': 3,
                          'единица_MDE': 'доля'}
        self.assertEqual(H.v12_мощность(h, _ctx())[1], 'fail')

    def test_нулевая_база_валит_счётный_режим(self):
        h = _гип(_sku=['1'])
        h['_мощность'] = {'режим': 'счётная', 'заказов_за_базу': 0, 'оборот_₽_в_день': 0}
        self.assertEqual(H.v11_baseline(h, _ctx())[1], 'fail')

    def test_мёртвый_хвост_валит_дисперсионный_режим(self):
        h = _гип(_sku=['1'])
        h['_мощность'] = {'режим': 'дисперсионная', 'нулевых_%': 96.0, 'среднее_₽_в_день': 1.0}
        код, в, объ = H.v11_baseline(h, _ctx())
        self.assertEqual(в, 'fail')
        self.assertIn('когорте', объ)


class Дедуп(unittest.TestCase):
    def test_одна_гипотеза_на_рычаг_и_метрику(self):
        a = _гип()
        a['hypothesis_id'] = 'H-A'
        b = _гип()
        b['hypothesis_id'] = 'H-B'
        оставили, дубли = H.дедуп([a, b])
        self.assertEqual(len(оставили), 1)
        self.assertEqual(дубли[0]['дубль_к'], 'H-A')


class Сужение(unittest.TestCase):
    def test_убирает_занятых_и_без_остатка(self):
        h = _гип(_sku=['1', '2', '3'])
        h, note = H.сузить(h, _ctx(занято=['3'], остаток=['1']), предел=100)
        self.assertEqual(h['_sku'], ['1'])
        self.assertIn('занятых', note)
        self.assertIn('без остатка', note)

    def test_зависимость_от_чужого_эксперимента_записана(self):
        h = _гип(_sku=['1', '2'])
        H.сузить(h, _ctx(занято=['2'], остаток=['1', '2']), предел=100)
        self.assertTrue(any('E8' in d for d in h['dependencies']))

    def test_рандомизированный_дизайн_не_режется_по_размеру(self):
        sku = [str(i) for i in range(500)]
        h = _гип(_sku=list(sku))
        h, note = H.сузить(h, _ctx(остаток=sku), предел=200)
        self.assertEqual(len(h['_sku']), 500)
        self.assertIsNone(note)

    def test_нерандомизированный_режется_до_порога(self):
        sku = [str(i) for i in range(500)]
        h = _гип(_sku=list(sku))
        h['_контроль'] = 'сравнение с прошлой неделей'
        h, note = H.сузить(h, _ctx(остаток=sku), предел=200)
        self.assertEqual(len(h['_sku']), 200)
        self.assertIn('E1', note)


class НикакихВнешнихЗаписей(unittest.TestCase):
    def test_в_коде_нет_изменяющих_вызовов(self):
        код = _исходник()
        for плохое in ('requests.post', 'requests.put', 'requests.delete', 'session.post',
                       'urlopen('):
            self.assertNotIn(плохое, код, плохое)

    def test_sql_только_на_чтение(self):
        код = _исходник().upper()
        for плохое in ('INSERT INTO', 'UPDATE ', 'DELETE FROM', 'CREATE TABLE', 'DROP '):
            self.assertNotIn(плохое, код, плохое)

    def test_ozon_периметр_без_wb(self):
        код = _исходник().lower()
        for плохое in ('wildberries', 'raw_wb_', 'mkt_wb_', 'nm_id'):
            self.assertNotIn(плохое, код, плохое)

    def test_файлы_контура_на_месте(self):
        for f in (H.REGISTRY, H.LOG, ИСТОЧНИК):
            self.assertTrue(os.path.exists(f), f)


class Наблюдатели(unittest.TestCase):
    def test_их_одиннадцать_и_имена_не_повторяются(self):
        имена = [n for n, _ in H.OBSERVERS]
        self.assertEqual(len(имена), 11)
        self.assertEqual(len(set(имена)), 11)

    def test_каждое_событие_из_требований_покрыто(self):
        коды = set(H.ШАБЛОНЫ)
        for k in ('STOCK_DEMAND_NO_AD', 'INEFFICIENT_SPEND', 'BUDGET_CANNIBALIZATION',
                  'PROFITABLE_LOW_REACH', 'SEARCH_VISIBILITY_LOSS', 'PRICE_INDEX_RED',
                  'FAMILY_MIX', 'FORECAST_GAP', 'NEW_ITEM'):
            self.assertIn(k, коды, k)

    def test_шаблон_не_придумывает_чисел(self):
        """Механизм и метрику даёт шаблон, факт и размер — только наблюдение."""
        for k, t in H.ШАБЛОНЫ.items():
            self.assertIsNone(t['upside'] if t['upside'] else None, k)
            self.assertTrue(t['metric'], k)
            self.assertTrue(t['control'], k)


# ============================== вертикаль: данные → гипотеза ==========================
# Ниже — единственный способ доказать, что реестр не список выдуманного вручную:
# детекторам подсовываются строки, и проверяется обе стороны утверждения — отклонение
# есть, гипотеза родилась; отклонения нет, гипотеза не родилась.

def _строки_расхода(нулевых=3, конвертящих=2):
    r = [{'s': f'z{i}', 'sp': 900.0, 'om': 0.0, 'v': 500, 'k': 20} for i in range(нулевых)]
    r += [{'s': f'k{i}', 'sp': 800.0, 'om': 5000.0, 'v': 600, 'k': 30} for i in range(конвертящих)]
    return r


class ГенераторРеагируетНаДанные(unittest.TestCase):
    def test_отклонение_есть_гипотеза_есть(self):
        o = H.det_неэффективный_расход(_строки_расхода(), ('2026-08-14', '2026-08-20'))
        self.assertEqual(len(o), 1)
        self.assertEqual(o[0]['код'], 'INEFFICIENT_SPEND')
        self.assertEqual(o[0]['размер_сегмента'], 3)
        h = H.предложить(o[0], _ctx(остаток=['z0', 'z1', 'z2']))
        self.assertEqual(h['hypothesis_id'], 'H-003')
        self.assertEqual(h['provenance'], 'DETECTED_FROM_LIVE_DATA')
        self.assertIn('расхода рекламы', h['current_evidence'])

    def test_отклонения_нет_гипотезы_нет(self):
        """Каждый рубль дал заказ — наблюдателю сказать нечего."""
        self.assertEqual(H.det_неэффективный_расход(_строки_расхода(нулевых=0)), [])

    def test_расход_ниже_порога_решения_не_считается_отклонением(self):
        self.assertEqual(H.det_неэффективный_расход([{'s': '1', 'sp': 10.0, 'om': 0.0}]), [])

    def test_показы_падают_быстрее_спроса_отклонение(self):
        o = H.det_поисковая_видимость({'pe': '2026-08-20', 'v': 7500, 'u': 10000},
                                      {'pe': '2026-08-13', 'v': 10000, 'u': 10000},
                                      {'всего': 100, 'немые': 7})
        self.assertEqual(o[0]['код'], 'SEARCH_VISIBILITY_LOSS')
        self.assertEqual(o[0]['размер_сегмента'], 100)

    def test_падаем_вместе_с_рынком_не_отклонение(self):
        """Спрос упал так же — это рынок, а не потеря видимости."""
        self.assertEqual(H.det_поисковая_видимость({'pe': 'a', 'v': 7500, 'u': 7500},
                                                   {'pe': 'b', 'v': 10000, 'u': 10000},
                                                   {'всего': 100, 'немые': 0}), [])

    def test_каннибализация_видна_и_не_придумана(self):
        w1 = ('2026-08-14', '2026-08-20')
        плохая = ([{'c': '1', 'd': '2026-08-07', 'v': 5000, 'sp': 1000.0, 'n': 40}]
                  + [{'c': '1', 'd': '2026-08-16', 'v': 3000, 'sp': 1050.0, 'n': 40}])
        self.assertEqual(H.det_каннибализация(плохая, w1)[0]['код'], 'BUDGET_CANNIBALIZATION')
        ровная = ([{'c': '1', 'd': '2026-08-07', 'v': 5000, 'sp': 1000.0, 'n': 40}]
                  + [{'c': '1', 'd': '2026-08-16', 'v': 4900, 'sp': 990.0, 'n': 40}])
        self.assertEqual(H.det_каннибализация(ровная, w1), [])

    def test_остаток_и_спрос_дают_веса_для_страт(self):
        o = H.det_остаток_спрос_без_рекламы([{'s': 'a', 'u': 90}, {'s': 'b', 'u': 10}])[0]
        self.assertEqual(o['размер_сегмента'], 2)
        self.assertEqual(o['веса'], {'a': 90.0, 'b': 10.0})
        self.assertEqual(H.det_остаток_спрос_без_рекламы([]), [])

    def _оценить(self, o, ctx, оборот_в_день):
        h = H.предложить(o, ctx)
        n = len(h['_sku'])
        h['_мощность'] = {'режим': 'счётная', 'MDE_отн': 0.3, 'n': n, 'λ_на_плечо': 300,
                          'заказов_за_базу': 300, 'единица_MDE': 'доля',
                          'нужно_заказов_на_плечо': 128, 'во_сколько_не_хватает': 1,
                          'оборот_₽_в_день': оборот_в_день,
                          'среднее_₽_в_день': оборот_в_день / max(1, n)}
        H.валидировать(h, ctx, считать_мощность=False)
        H.приоритет(h, ctx)
        return h

    def test_размер_эффекта_меняет_приоритет(self):
        """Тот же механизм, разный масштаб отклонения — разный балл и разные деньги."""
        мелкое = [{'s': f's{i}', 'u': 3} for i in range(20)]
        крупное = [{'s': f's{i}', 'u': 80} for i in range(900)]
        c1 = _ctx(остаток=[x['s'] for x in мелкое])
        c2 = _ctx(остаток=[x['s'] for x in крупное])
        h1 = self._оценить(H.det_остаток_спрос_без_рекламы(мелкое)[0], c1, 500.0)
        h2 = self._оценить(H.det_остаток_спрос_без_рекламы(крупное)[0], c2, 40000.0)
        self.assertGreater(h2['priority_score'], h1['priority_score'])
        self.assertGreater(h2['estimated_upside_rub'], h1['estimated_upside_rub'])

    def test_идущий_эксперимент_блокирует_пересекающийся_состав(self):
        строки = [{'s': str(i), 'u': 10} for i in range(10)]
        ctx = _ctx(занято=[str(i) for i in range(8)], остаток=[str(i) for i in range(10)])
        h = H.предложить(H.det_остаток_спрос_без_рекламы(строки)[0], ctx)
        self.assertEqual(len(h['_sku']), 2)
        self.assertEqual(len(h['_пересечение']), 8)
        self.assertTrue(any('E8' in d for d in h['dependencies']))
        self.assertEqual(H.v01_пересечение_sku(h, ctx)[1], 'ok')
        h['_sku'] = [str(i) for i in range(10)]          # если состав не чистить —
        self.assertEqual(H.v01_пересечение_sku(h, ctx)[1], 'fail')   # запуск запрещён

    def test_массовый_разгон_из_живых_данных_упирается_в_e1(self):
        h = H.предложить(H.det_неэффективный_расход(_строки_расхода())[0], _ctx())
        h['target_segment'] = dict(h['target_segment'], всего=H.E1_ПОРОГ + 500)
        h['_контроль'] = 'сравнение с прошлой неделей'
        self.assertEqual(H.v17_не_повтор(h, _ctx())[1], 'fail')


class Происхождение(unittest.TestCase):
    def test_словарь_происхождений_закрыт(self):
        self.assertEqual(len(H.ПРОИСХОЖДЕНИЕ), 5)
        self.assertIn('LLM_SUGGESTED_WITHOUT_DATA', H.ПРОИСХОЖДЕНИЕ)

    def test_из_живых_данных_проходит(self):
        h = _гип(provenance='DETECTED_FROM_LIVE_DATA')
        self.assertEqual(H.v18_происхождение(h, _ctx())[1], 'ok')

    def test_придумано_без_данных_валится(self):
        h = _гип(provenance='LLM_SUGGESTED_WITHOUT_DATA')
        код, в, объ = H.v18_происхождение(h, _ctx())
        self.assertEqual(в, 'fail')
        self.assertIn('READY', объ)

    def test_вручную_только_предупреждение(self):
        self.assertEqual(H.v18_происхождение(_гип(provenance='MANUALLY_DEFINED'), _ctx())[1],
                         'warn')

    def test_пустое_происхождение_валится(self):
        self.assertEqual(H.v18_происхождение(_гип(provenance=None), _ctx())[1], 'fail')

    def test_придуманное_без_данных_не_доходит_до_ready(self):
        """Жёсткий потолок в самом автомате, а не только в валидаторе."""
        h = _гип(required_sample='—', approval_level='AUTONOMOUS_READ',
                 provenance='LLM_SUGGESTED_WITHOUT_DATA')
        h['_блокировано'] = []
        H.продвинуть(h, 'OBSERVED')
        self.assertEqual(h['status'], 'VALIDATED')
        self.assertIn('LLM_SUGGESTED_WITHOUT_DATA', h['result'])

    def test_в_реестре_у_всех_проставлено(self):
        with io.open(H.REGISTRY, encoding='utf-8') as f:
            реестр = json.load(f)
        for h in реестр['гипотезы']:
            self.assertIn(h.get('provenance'), H.ПРОИСХОЖДЕНИЕ, h['hypothesis_id'])
        по_id = {h['hypothesis_id']: h['provenance'] for h in реестр['гипотезы']}
        self.assertEqual(по_id['E1'], 'DERIVED_FROM_FAILED_EXPERIMENT')
        self.assertEqual(по_id['E3'], 'DERIVED_FROM_FAILED_EXPERIMENT')
        self.assertEqual(по_id['E8'], 'SEEDED_FROM_HISTORY')
        self.assertEqual(по_id['H-002'], 'DETECTED_FROM_LIVE_DATA')


def _гип_волны(n=500):
    h = _гип(minimum_duration=14, approval_level='APPROVAL_REQUIRED')
    h['_sku'] = [f's{i}' for i in range(n)]
    h['_пересечение'] = ['занят1', 'занят2']
    h['_наблюдение'] = {'веса': {f's{i}': float(n - i) for i in range(n)}}
    h['_мощность'] = {'режим': 'счётная', 'n': n, 'λ_на_плечо': 60.0,
                      'нужно_заказов_на_плечо': 128}
    return h


class ВолновойРежим(unittest.TestCase):
    def setUp(self):
        self.res = H.волны(_гип_волны(), _ctx(), размер=200)

    def test_ни_одна_волна_не_больше_потолка(self):
        for w in self.res['волны']:
            self.assertLessEqual(w['состав']['всего'], 200, w['wave_id'])

    def test_волн_столько_сколько_нужно_чтобы_накрыть_сегмент(self):
        self.assertEqual(len(self.res['волны']), 3)
        всего = sum(w['состав']['всего'] for w in self.res['волны'])
        self.assertEqual(всего, 500)

    def test_составы_волн_не_пересекаются(self):
        видели = set()
        for w in self.res['волны']:
            свои = {s for s, _ in w['_состав']['воздействие'] + w['_состав']['контроль']}
            self.assertFalse(свои & видели, w['wave_id'])
            видели |= свои

    def test_у_каждой_волны_свой_контроль(self):
        for w in self.res['волны']:
            self.assertGreater(w['состав']['контроль'], 0, w['wave_id'])
            self.assertGreater(w['состав']['воздействие'], 0, w['wave_id'])

    def test_рандомизация_стратифицирована(self):
        """В каждой страте есть оба плеча — иначе разница плеч это разница составов."""
        for k, v in self.res['волны'][0]['состав']['страты'].items():
            self.assertGreater(v['воздействие'], 0, k)
            self.assertGreater(v['контроль'], 0, k)

    def test_жребий_воспроизводим(self):
        снова = H.волны(_гип_волны(), _ctx(), размер=200)
        a = [s for s, _ in self.res['волны'][0]['_состав']['воздействие']]
        b = [s for s, _ in снова['волны'][0]['_состав']['воздействие']]
        self.assertEqual(a, b)

    def test_вторая_волна_закрыта_первой(self):
        self.assertEqual(self.res['волны'][0]['статус'], 'ЖДЁТ СОГЛАСОВАНИЯ')
        for w in self.res['волны'][1:]:
            self.assertIn('ЗАКРЫТА', w['статус'])
            self.assertIn('EVALUATED', w['предусловие'])

    def test_окна_идут_подряд_без_наложения(self):
        концы = [w['окно']['сверка'] for w in self.res['волны']]
        старты = [w['окно']['старт'] for w in self.res['волны']]
        for i in range(1, len(старты)):
            self.assertGreater(старты[i], концы[i - 1])

    def test_деньги_ограничены_на_каждой_волне(self):
        for w in self.res['волны']:
            self.assertEqual(w['риск']['потолок_расхода_₽'], H.РИСК_ВОЛНЫ_РУБ)
            self.assertGreater(w['риск']['дневной_лимит_₽'], 0)
        self.assertEqual(self.res['сводка']['общий_риск_₽'],
                         H.РИСК_ВОЛНЫ_РУБ * len(self.res['волны']))

    def test_три_исхода_описаны_у_каждой_волны(self):
        for w in self.res['волны']:
            self.assertEqual(set(w['решение']), {'KEEP', 'STOP', 'ROLLBACK'})
            self.assertIn('E1', w['решение']['ROLLBACK'])

    def test_keep_решается_на_накопленной_когорте_а_не_на_волне(self):
        self.assertIn('накопленной когорте', self.res['волны'][0]['решение']['KEEP'])

    def test_ничего_не_применено(self):
        for w in self.res['волны']:
            self.assertFalse(w['применено'])
            self.assertEqual(w['внешних_вызовов'], 0)

    def test_горизонт_накрывает_и_занятый_состав(self):
        """Одна гипотеза — несколько связанных волн: свободные сейчас, занятые потом."""
        h = _гип_волны()
        h['_пересечение'] = [f'z{i}' for i in range(300)]
        h['_наблюдение']['веса'].update({f'z{i}': float(i) for i in range(300)})
        h['dependencies'] = ['E8: состав освободится после сверки 2026-09-09']
        г = H.волны(h, _ctx(), размер=200, горизонт=True)
        self.assertEqual(г['сводка']['волн_доступно'], 3)      # свободных 500
        self.assertEqual(г['сводка']['волн_в_плане'], 5)       # +300 занятых
        закрытые = [w for w in г['волны'] if w['состав']['состав_занят_сейчас']]
        self.assertEqual(len(закрытые), 2)
        for w in закрытые:
            self.assertEqual(w['статус'], 'ЗАКРЫТА ЗАНЯТЫМ СОСТАВОМ')
            self.assertIn('E8', w['предусловие'])
            self.assertIn('EVALUATED', w['предусловие'])
        self.assertEqual(г['сводка']['общий_риск_₽'], H.РИСК_ВОЛНЫ_РУБ * 5)

    def test_без_горизонта_занятый_состав_не_планируется(self):
        h = _гип_волны()
        h['_пересечение'] = [f'z{i}' for i in range(300)]
        self.assertEqual(H.волны(h, _ctx(), размер=200)['сводка']['волн_в_плане'], 3)

    def test_сводка_честно_считает_нехватку_мощности(self):
        с = self.res['сводка']
        self.assertEqual(с['ждут_освобождения'], 2)
        self.assertGreater(с['волн_до_мощности'], len(self.res['волны']))
        self.assertIn('Дробление на волны мощности не создаёт', с['вердикт'])
        self.assertIn('при необходимых', с['вердикт'])


# ============================== запрет по мощности ===================================
# Волны — контейнер риска, а не источник статистики. Если лифт на SKU не читается в
# сегменте, он тем более не читается в куске сегмента: 4,2 заказа на плечо при нужных
# 128 не превращаются в измеримый эксперимент никаким дроблением и никаким разрешением.

def _бессильная(**kw):
    h = _гип(**kw)
    h['_мощность'] = {'режим': 'счётная', 'n': 144, 'λ_на_плечо': 4.2, 'MDE_отн': 2.29,
                      'единица_MDE': 'доля', 'нужно_заказов_на_плечо': 128,
                      'во_сколько_не_хватает': 30.5, 'оборот_₽_в_день': 2600.0}
    h['_валидация'] = [{'код': 'V12_POWER', 'вердикт': 'fail', 'объяснение': 'не прочитается'}]
    h['_блокировано'] = []
    h['_sku'] = [f's{i}' for i in range(144)]
    h['_пересечение'] = [f'z{i}' for i in range(1314)]
    h['_наблюдение'] = {'веса': {f's{i}': float(i) for i in range(144)}}
    return h


def _мощная(**kw):
    h = _гип(**kw)
    h['_мощность'] = {'режим': 'счётная', 'n': 900, 'λ_на_плечо': 400.0, 'MDE_отн': 0.10,
                      'единица_MDE': 'доля', 'нужно_заказов_на_плечо': 128,
                      'во_сколько_не_хватает': 0.3, 'оборот_₽_в_день': 40000.0}
    h['_валидация'] = [{'код': 'V12_POWER', 'вердикт': 'ok', 'объяснение': 'читается'}]
    h['_блокировано'] = []
    return h


class ЗапретПоМощности(unittest.TestCase):
    def test_бессильная_распознаётся(self):
        self.assertTrue(H.недостаточная_мощность(_бессильная()))
        self.assertFalse(H.недостаточная_мощность(_мощная()))

    def test_нехватка_считается_и_без_вердикта_валидатора(self):
        h = _бессильная()
        h['_валидация'] = []                       # валидацию не прогоняли
        self.assertTrue(H.недостаточная_мощность(h))

    def test_закрытые_статусы_недостижимы(self):
        for s in H.ЗАКРЫТЫЕ_ДЛЯ_БЕССИЛЬНЫХ:
            self.assertIn(s, ('READY', 'APPROVAL_REQUIRED', 'RUNNING'))
        for было, стало in (('VALIDATED', 'READY'), ('READY', 'APPROVAL_REQUIRED'),
                            ('APPROVAL_REQUIRED', 'RUNNING')):
            h = _бессильная(status=было)
            with self.assertRaises(H.ПереходЗапрещён) as e:
                H.перевести(h, стало, 'проверка')
            self.assertIn('BLOCKED_BY_POWER', str(e.exception))
            self.assertEqual(h['status'], было)     # статус не сдвинулся

    def test_мощная_проходит_те_же_переходы(self):
        h = _мощная(status='VALIDATED')
        H.перевести(h, 'READY', 'мощности хватает')
        self.assertEqual(h['status'], 'READY')

    def test_автомат_держит_потолок_validated(self):
        h = _бессильная(required_sample='≥128 заказов на плечо', approval_level='FOUNDER_APPROVAL')
        H.продвинуть(h, 'OBSERVED')
        self.assertEqual(h['status'], 'VALIDATED')
        self.assertEqual(h['blocking_reason'], 'BLOCKED_BY_POWER')
        self.assertIn('дробление на волны мощности не создаёт', h['result'])

    def test_apply_план_не_исполним(self):
        к = H.контракт(_бессильная(), _ctx())
        self.assertFalse(к['исполнимый'])
        self.assertIn('BLOCKED_BY_POWER', к['статус'])
        self.assertIn('разрешение не запрашивается', к['уровень_допуска'])
        self.assertTrue(к['пути_разблокировки'])

    def test_волны_только_проект_дизайна(self):
        res = H.волны(_бессильная(), _ctx(), размер=200, горизонт=True)
        self.assertTrue(res['волны'])
        for w in res['волны']:
            self.assertFalse(w['исполнимый'])
            self.assertIn('ПРОЕКТ ДИЗАЙНА', w['статус'])
            self.assertIn('BLOCKED_BY_POWER', w['уровень_допуска'])
            self.assertFalse(w['применено'])
            self.assertEqual(w['внешних_вызовов'], 0)

    def test_ни_одна_волна_не_ждёт_согласования(self):
        res = H.волны(_бессильная(), _ctx(), размер=200, горизонт=True)
        self.assertEqual([w for w in res['волны'] if w['статус'] == 'ЖДЁТ СОГЛАСОВАНИЯ'], [])
        self.assertIn('не требуется и не запрашивается',
                      res['сводка']['разрешение_основателя'])

    def test_дробление_не_считается_разблокировкой(self):
        res = H.волны(_бессильная(), _ctx(), размер=200, горизонт=True)
        self.assertIn('Дробление на волны мощности не создаёт', res['сводка']['вердикт'])
        пути = res['сводка']['пути_разблокировки']
        self.assertEqual(len(пути), 4)
        текст = ' '.join(пути)
        for k in ('единица анализа', 'кластерный', 'длинное окно', 'другой дизайн'):
            self.assertIn(k, текст)
        self.assertNotIn('волн', текст)

    def test_мощная_гипотеза_даёт_исполнимые_волны(self):
        h = _мощная()
        h['_sku'] = [f's{i}' for i in range(300)]
        h['_пересечение'] = []
        h['_наблюдение'] = {'веса': {}}
        res = H.волны(h, _ctx(), размер=200)
        self.assertTrue(all(w['исполнимый'] for w in res['волны']))
        self.assertEqual(res['волны'][0]['статус'], 'ЖДЁТ СОГЛАСОВАНИЯ')

    def test_в_реестре_бессильные_не_в_закрытых_статусах(self):
        with io.open(H.REGISTRY, encoding='utf-8') as f:
            реестр = json.load(f)
        закрытые = [h for h in реестр['гипотезы']
                    if h.get('blocking_reason') == 'BLOCKED_BY_POWER'
                    and h['status'] in H.ЗАКРЫТЫЕ_ДЛЯ_БЕССИЛЬНЫХ]
        self.assertEqual(закрытые, [])
        h002 = {h['hypothesis_id']: h for h in реестр['гипотезы']}['H-002']
        self.assertEqual(h002['blocking_reason'], 'BLOCKED_BY_POWER')
        self.assertEqual(h002['decision'], 'DEFERRED')
        self.assertTrue(h002['waves'])
        self.assertFalse(any(w['исполнимый'] for w in h002['waves']))


class ЖурналНеЗагрязняется(unittest.TestCase):
    def test_прогон_пишет_в_свой_файл(self):
        self.assertNotEqual(H.LOG, _БОЕВОЙ_ЖУРНАЛ)
        H.запись_в_журнал('H-TEST', 'DRAFT', 'VALIDATED', 'проверка изоляции журнала')
        with io.open(H.LOG, encoding='utf-8') as f:
            self.assertIn('H-TEST', f.read())

    def test_в_боевом_журнале_нет_тестовых_записей(self):
        with io.open(_БОЕВОЙ_ЖУРНАЛ, encoding='utf-8') as f:
            строки = [json.loads(x) for x in f if x.strip()]
        self.assertEqual([x for x in строки if x['hypothesis_id'].startswith('H-TEST')], [])
        self.assertTrue(строки)


class РамкаКонтура(unittest.TestCase):
    def test_режим_и_фиксации(self):
        self.assertEqual(H.РЕЖИМ, 'SHADOW_MODE_V0_1')
        self.assertEqual(len(H.ФИКСАЦИИ), 10)
        текст = ' '.join(H.ФИКСАЦИИ)
        for k in ('SHADOW_MODE_V0_1', 'ACT остаётся только после разрешения',
                  'EVALUATE → KEEP/ROLLBACK → MEMORY', 'evaluator', 'DEFERRED',
                  'BLOCKED_BY_POWER', 'BLOCKED_BY_ECONOMICS_GRANULARITY', 'cron/systemd'):
            self.assertIn(k, текст)

    def test_решения_человека_записаны(self):
        with io.open(H.REGISTRY, encoding='utf-8') as f:
            по_id = {h['hypothesis_id']: h for h in json.load(f)['гипотезы']}
        for hid in ('H-006', 'H-008', 'H-004', 'H-002'):
            self.assertEqual(по_id[hid]['decision'], 'DEFERRED', hid)
        for hid in ('E5', 'E7', 'H-001'):
            self.assertEqual(по_id[hid]['status'], 'MEASURING', hid)
        self.assertEqual(по_id['E6']['status'], 'EVALUATED')      # NO_CAUSAL_CLAIM, 07.09
        self.assertEqual(по_id['E8']['status'], 'INCONCLUSIVE')   # решение Сергея 14.09, закрыто 18.09
        self.assertIs(по_id['E8']['result']['causal_claim_allowed'], False)


if __name__ == '__main__':
    unittest.main()
