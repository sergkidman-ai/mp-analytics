#!/usr/bin/env python3
# поток: mkt
"""Тесты H-009 — автоматическая сверка экспериментов Ozon.

Ни один тест не ходит в Postgres: источник данных подставляется (ФейкДанные), журнал
оценок пишется во временный файл. Контракты читаются настоящие — они и есть золотой
набор, по которому проверяется, что оценщик воспроизводит уже известную историю.
"""
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, '/opt/mp-analytics')
sys.path.insert(0, '/opt/mp-analytics/tools')

import ozon_eval_core as E  # noqa: E402

ЖУРНАЛ = tempfile.NamedTemporaryFile(prefix='eval_', suffix='.jsonl', delete=False).name
ПУСТОЙ = tempfile.mkdtemp(prefix='когорты_')       # каталог без снимков: дрейф не ловится


def tearDownModule():
    for p in (ЖУРНАЛ,):
        if os.path.exists(p):
            os.unlink(p)


def _исходник(имя):
    with open(f'/opt/mp-analytics/tools/{имя}', encoding='utf-8') as f:
        return f.read()


class ФейкДанные:
    """Источник данных без БД: ровно тот же интерфейс, что у ЖивыеДанные."""

    def __init__(self, день='2026-08-20', зрелые=True, когорты=None, дельты=None,
                 заказы=1000, цена=100.0, остаток=0.9, показы=1000.0, расход=1.0):
        self.день, self.зрелые = день, зрелые
        self.когорты = когорты or {}
        self.дельты = дельты or {}
        self.заказы, self.цена, self.остаток = заказы, цена, остаток
        self.показы, self.расход = показы, расход

    def зрелость(self, день, источники):
        return {s: (bool(self.зрелые) and день == self.день, f'фейк {день}')
                for s in источники}

    def последний_день_витрины(self):
        return self.день

    def когорта(self, spec, режим='itt'):
        ключ = ','.join(sorted(spec.get('actions') or []))
        return self.когорты.get(ключ, set())

    def чистая_по_sku(self, acc, skus, d0, d1):
        ряд = self.дельты.get((d0, d1), {})
        return {s: float(ряд.get(s, 0.0)) for s in skus}

    def свод(self, acc, skus, d0, d1):
        n = max(1, len(set(skus)))
        выр = 1000.0 * n
        return {'sku_в_группе': len(set(skus)), 'дней': E._дней(d0, d1), 'orders': self.заказы,
                'views': 10000.0, 'clicks': 500.0, 'spend': 100.0 * n, 'revenue': выр,
                'выручка_минус_расход': выр - 100.0 * n, 'spend_на_sku': 100.0 * self.расход,
                'revenue_на_sku': 1000.0, 'выручка_минус_расход_на_sku': 900.0,
                'orders_на_sku': self.заказы / n, 'views_на_sku': 10000.0 / n,
                'clicks_на_sku': 500.0 / n, 'drr': 0.1}

    def средняя_цена(self, acc, skus, день):
        return self.цена

    def доля_с_остатком(self, acc, skus, день):
        return self.остаток

    def поисковые_показы(self, acc, skus, d0, d1):
        return self.показы


def _когорта(n, префикс='s'):
    return {('oz_acc1', f'{префикс}{i}') for i in range(n)}


def _контракт(**kw):
    c = {'id': 'X1', 'hypothesis_id': 'X', 'название': 'синтетический', 'account': 'oz_acc1',
         'тип': 'TREATMENT_CONTROL_DID', 'единица_анализа': 'SKU-день',
         'когорта_treatment': {'account': 'oz_acc1', 'actions': ['t']},
         'когорта_control': {'account': 'oz_acc1', 'actions': ['c']},
         'основной_показатель': 'выручка минус расход на SKU в сутки',
         'baseline_window': ['2026-08-01', '2026-08-07'],
         'measurement_window': ['2026-08-08', '2026-08-14'],
         'дата_сверки': '2026-08-15',
         'минимальная_мощность': {'мин_дней': 7, 'мин_заказов': 10},
         'требуемые_источники': ['ads'], 'guardrails_машинные': [], 'guardrails_текстовые': [],
         'известные_загрязнения': []}
    c.update(kw)
    return c


def _данные(t_дельты, c_дельты, **kw):
    ct, cc = _когорта(len(t_дельты), 't'), _когорта(len(c_дельты), 'c')
    б = ('2026-08-01', '2026-08-07')
    о = ('2026-08-08', '2026-08-14')
    return ФейкДанные(
        когорты={'t': ct, 'c': cc},
        дельты={б: {f't{i}': 0.0 for i in range(len(t_дельты))},
                о: dict({f't{i}': v for i, v in enumerate(t_дельты)},
                        **{f'c{i}': v for i, v in enumerate(c_дельты)})},
        **kw)


def _оценить(c, data, today='2026-08-22', журнал=None):
    return E.оценить(c, data, today=today, журнал=журнал or [], каталог_когорт=ПУСТОЙ)


class Зрелость(unittest.TestCase):
    def test_незрелые_данные_не_дают_финального_вердикта(self):
        d = _данные([100.0] * 20, [0.0] * 20)
        d.зрелые = False
        r = _оценить(_контракт(), d)
        self.assertEqual(r['verdict'], 'CONTINUE_MEASURING')
        self.assertIn('DATA_NOT_MATURE', r['reason_codes'])
        self.assertFalse(r['causal_claim_allowed'])
        self.assertNotIn(r['recommendation'], ('KEEP', 'ROLLBACK'))

    def test_короткое_окно_не_даёт_вердикта(self):
        d = _данные([100.0] * 20, [0.0] * 20)
        r = _оценить(_контракт(measurement_window=['2026-08-18', '2026-08-20']), d)
        self.assertEqual(r['verdict'], 'CONTINUE_MEASURING')
        self.assertIn('WINDOW_TOO_SHORT', r['reason_codes'])

    def test_дата_сверки_не_наступила(self):
        d = _данные([100.0] * 20, [0.0] * 20)
        r = _оценить(_контракт(дата_сверки='2026-09-30'), d)
        self.assertEqual(r['verdict'], 'CONTINUE_MEASURING')
        self.assertIn('BEFORE_CHECK_DATE', r['reason_codes'])


class ЖурналТолькоДозапись(unittest.TestCase):
    def setUp(self):
        open(ЖУРНАЛ, 'w').close()

    def test_повтор_на_тех_же_данных_дубля_не_плодит(self):
        d = _данные([100.0] * 20, [0.0] * 20)
        c = _контракт()
        E.дописать_оценку(_оценить(c, d), ЖУРНАЛ)
        с2, _ = E.дописать_оценку(_оценить(c, d), ЖУРНАЛ)
        self.assertEqual(с2, 'дубль')
        self.assertEqual(len(E.читать_оценки(ЖУРНАЛ)), 1)

    def test_дозагрузка_данных_создаёт_новую_запись_не_переписывая(self):
        c = _контракт()
        d1 = _данные([100.0] * 20, [0.0] * 20)
        r1 = _оценить(c, d1)
        E.дописать_оценку(r1, ЖУРНАЛ)
        d2 = _данные([100.0] * 20, [0.0] * 20)
        d2.день = '2026-08-21'
        r2 = _оценить(c, d2, журнал=E.читать_оценки(ЖУРНАЛ))
        статус, _ = E.дописать_оценку(r2, ЖУРНАЛ)
        журнал = E.читать_оценки(ЖУРНАЛ)
        self.assertEqual(статус, 'добавлено')
        self.assertEqual(len(журнал), 2)
        self.assertEqual(журнал[0], r1)                      # прошлое не тронуто
        self.assertEqual(r2['supersedes_evaluation_id'], r1['evaluation_id'])

    def test_смена_контракта_создаёт_новую_версию_оценки(self):
        d = _данные([100.0] * 20, [0.0] * 20)
        r1 = _оценить(_контракт(), d)
        r2 = _оценить(_контракт(минимальная_мощность={'мин_дней': 7, 'мин_заказов': 25}), d)
        self.assertNotEqual(r1['contract_version'], r2['contract_version'])
        self.assertNotEqual(r1['evaluation_id'], r2['evaluation_id'])

    def test_смена_состава_когорты_меняет_хеш(self):
        r1 = _оценить(_контракт(), _данные([100.0] * 20, [0.0] * 20))
        r2 = _оценить(_контракт(), _данные([100.0] * 21, [0.0] * 20))
        self.assertNotEqual(r1['cohort_hash'], r2['cohort_hash'])


class РекомендацияНеИсполнение(unittest.TestCase):
    def test_keep_остаётся_рекомендацией(self):
        d = _данные([100.0 + i * 0.1 for i in range(30)], [i * 0.1 for i in range(30)])
        r = _оценить(_контракт(), d)
        self.assertEqual(r['verdict'], 'EFFECT_CONFIRMED_POSITIVE')
        self.assertEqual(r['recommendation'], 'KEEP')
        self.assertIsNone(r['decision'])
        self.assertIsNone(r['decision_by'])
        self.assertIsNone(r['execution'])

    def test_rollback_остаётся_рекомендацией(self):
        d = _данные([-100.0 + i * 0.1 for i in range(30)], [i * 0.1 for i in range(30)])
        r = _оценить(_контракт(), d)
        self.assertEqual(r['verdict'], 'EFFECT_CONFIRMED_NEGATIVE')
        self.assertEqual(r['recommendation'], 'ROLLBACK')
        self.assertIsNone(r['decision'])
        self.assertIsNone(r['execution'])

    def test_статус_гипотезы_никогда_не_ставится_в_keep_или_rollback(self):
        текст = _исходник('ozon_hypo.py')
        начало = текст.index('def cmd_evaluate_ready')
        конец = текст.index('# ============================== карта цикла', начало)
        тело = текст[начало:конец]
        self.assertIn("перевести(h, 'EVALUATED'", тело)
        for запрет in ("'KEEP'", "'ROLLBACK'"):
            self.assertNotIn(f'перевести(h, {запрет}', тело)


class Мощность(unittest.TestCase):
    def test_нехватка_мощности_даёт_inconclusive_а_не_нет_эффекта(self):
        шум = [100.0, -100.0, 50.0, -50.0, 120.0, -130.0, 40.0, -20.0, 90.0, -95.0]
        r = _оценить(_контракт(), _данные(шум, [x * -1 for x in шум]))
        self.assertEqual(r['verdict'], 'INCONCLUSIVE')
        self.assertIn('UNDERPOWERED', r['reason_codes'])
        self.assertFalse(r['causal_claim_allowed'])
        self.assertIsNotNone(r['confidence_or_power']['mde_80'])

    def test_мало_событий_даёт_inconclusive(self):
        d = _данные([100.0] * 20, [0.0] * 20, заказы=3)
        r = _оценить(_контракт(), d)
        self.assertEqual(r['verdict'], 'INCONCLUSIVE')
        self.assertIn('TOO_FEW_EVENTS', r['reason_codes'])

    def test_бессильный_дизайн_остаётся_blocked_by_power(self):
        for cid in ('H-002-DESIGN', 'H-003-DESIGN', 'H-005-DESIGN', 'H-007-DESIGN',
                    'H-010-DESIGN'):
            r = _оценить(E.контракт_по_id(cid), _данные([], []))
            self.assertEqual(r['verdict'], 'BLOCKED_BY_POWER', cid)
            self.assertIn('POWER_BLOCKED_DESIGN', r['reason_codes'])
            self.assertFalse(r['causal_claim_allowed'])


class Загрязнения(unittest.TestCase):
    def test_пересечение_когорт_блокирует_вердикт(self):
        d = _данные([100.0 + i * 0.1 for i in range(30)], [i * 0.1 for i in range(30)])
        c = _контракт(_когорты_всех={'X1': _когорта(30, 't'), 'E6': _когорта(30, 't')})
        r = _оценить(c, d)
        self.assertEqual(r['verdict'], 'CONTAMINATED')
        self.assertIn('COHORT_OVERLAP', r['reason_codes'])
        self.assertFalse(r['causal_claim_allowed'])
        self.assertNotIn(r['recommendation'], ('KEEP', 'ROLLBACK'))

    def test_пробитый_сторож_снимает_keep(self):
        d = _данные([100.0 + i * 0.1 for i in range(30)], [i * 0.1 for i in range(30)],
                    остаток=0.1)
        c = _контракт(guardrails_машинные=[{'код': 'STOCK_SHARE_MIN', 'порог': 0.7}])
        r = _оценить(c, d)
        self.assertIn('GUARDRAIL_BREACH', r['reason_codes'])
        self.assertEqual(r['verdict'], 'CONTAMINATED')
        self.assertNotEqual(r['recommendation'], 'KEEP')

    def test_текстовый_сторож_честно_помечается_непроверяемым(self):
        d = _данные([100.0] * 20, [0.0] * 20)
        r = _оценить(_контракт(guardrails_текстовые=['на глаз']), d)
        self.assertIn('GUARDRAIL_UNAVAILABLE', r['reason_codes'])
        self.assertTrue(any(g['статус'] == 'UNAVAILABLE' for g in r['guardrail_results']))


class ЗолотойНабор(unittest.TestCase):
    """Уже прожитая история: оценщик обязан её воспроизвести, а не переписать."""

    def test_e1_не_даёт_причинного_вывода_и_помнит_не_повторять(self):
        d = ФейкДанные(когорты={'up10': _когорта(1232, 't')}, заказы=500)
        r = _оценить(E.контракт_по_id('E1'), d)
        self.assertEqual(r['verdict'], 'NO_CAUSAL_CLAIM')
        self.assertIn('BASELINE_UNAVAILABLE', r['reason_codes'])
        self.assertIn('DO_NOT_REPEAT', r['reason_codes'])
        self.assertIn('NO_CONTROL_BY_DESIGN', r['reason_codes'])
        self.assertFalse(r['causal_claim_allowed'])
        self.assertEqual(r['recommendation'], 'NO_CAUSAL_CLAIM')

    def test_e3_не_оценивается_как_применённый(self):
        r = _оценить(E.контракт_по_id('E3'), _данные([], []))
        self.assertEqual(r['verdict'], 'VOID')
        self.assertIn('NEVER_APPLIED', r['reason_codes'])
        self.assertIn('EXPERIMENT_VOID', r['reason_codes'])
        self.assertFalse(r['causal_claim_allowed'])
        self.assertEqual(r['treatment_result'], {})

    def test_e7_без_контроля_не_получает_причинного_вердикта(self):
        d = ФейкДанные(день='2026-09-05', когорты={'restore_converter': _когорта(21, 't')})
        r = _оценить(E.контракт_по_id('E7'), d, today='2026-09-10')
        self.assertEqual(r['verdict'], 'NO_CAUSAL_CLAIM')
        self.assertIn('CAUSAL_CLAIM_FORBIDDEN', r['reason_codes'])
        self.assertFalse(r['causal_claim_allowed'])

    def test_h001_до_даты_разбора_только_накапливает(self):
        r = _оценить(E.контракт_по_id('P1-HALO'), _данные([], []), today='2026-08-22')
        self.assertEqual(r['verdict'], 'CONTINUE_MEASURING')
        self.assertIn('BEFORE_CHECK_DATE', r['reason_codes'])

    def test_e5_e6_e8_до_своих_дат_вердикта_не_получают(self):
        for cid in ('E5', 'E6', 'E8'):
            r = _оценить(E.контракт_по_id(cid), _данные([100.0] * 20, [0.0] * 20),
                         today='2026-08-22')
            self.assertEqual(r['verdict'], 'CONTINUE_MEASURING', cid)
            self.assertIn('BEFORE_CHECK_DATE', r['reason_codes'], cid)

    def test_h009_самопроверка_видит_собственный_контур(self):
        r = _оценить(E.контракт_по_id('H-009-SELFCHECK'), _данные([], []))
        self.assertEqual(r['verdict'], 'CAPABILITY_PRESENT')
        self.assertIn('CAPABILITY_SELF_CHECK', r['reason_codes'])
        self.assertFalse(r['causal_claim_allowed'])


class Целостность(unittest.TestCase):
    def test_все_коды_причин_из_словаря(self):
        d = _данные([100.0] * 20, [0.0] * 20)
        for c in E.читать_контракты():
            r = _оценить(dict(c), d)
            for код in r['reason_codes']:
                self.assertIn(код, E.ПРИЧИНЫ, f'{c["id"]}: {код}')
            self.assertIn(r['verdict'], E.ВЕРДИКТЫ)
            self.assertIn(r['recommendation'], E.РЕКОМЕНДАЦИИ)

    def test_обязательные_поля_записи(self):
        поля = ('evaluation_id', 'evaluated_at', 'hypothesis_id', 'experiment_id',
                'contract_version', 'cohort_hash', 'data_as_of', 'data_readiness',
                'baseline_window', 'measurement_window', 'primary_metric',
                'treatment_result', 'control_result', 'effect', 'confidence_or_power',
                'guardrail_results', 'contaminations', 'verdict', 'recommendation',
                'causal_claim_allowed', 'reason_codes', 'evaluator_version',
                'supersedes_evaluation_id')

        r = _оценить(_контракт(), _данные([100.0] * 20, [0.0] * 20))
        for п in поля:
            self.assertIn(п, r)

    def test_нет_записей_в_бд_и_внешних_вызовов(self):
        for имя in ('ozon_eval_core.py', 'ozon_hypo.py'):
            т = _исходник(имя).upper()
            for запрет in ('INSERT INTO', 'UPDATE ', 'DELETE FROM', 'REQUESTS.POST',
                           'REQUESTS.PUT', 'REQUESTS.DELETE'):
                self.assertNotIn(запрет, т, f'{имя}: {запрет}')

    def test_контур_ozon_без_wb(self):
        т = _исходник('ozon_eval_core.py').lower()
        for запрет in ('wildberries', 'wb_acc', 'suppliers-api'):
            self.assertNotIn(запрет, т)

    def test_контракты_валидны(self):
        for c in E.читать_контракты():
            self.assertIn(c['тип'], E.ТИПЫ, c['id'])
            self.assertTrue(c.get('основной_показатель'), c['id'])
            self.assertEqual(len(E.версия_контракта(c)), 12)

    def test_g6_не_выносит_вердикт_по_незрелому_дню(self):
        т = _исходник('ozon_exp_eval.py')
        self.assertIn('DATA_NOT_MATURE', т)
        self.assertIn('День не зрелый', т)

    def test_журнал_оценок_только_дозапись(self):
        т = _исходник('ozon_eval_core.py')
        self.assertIn("open(п, 'a'", т)
        self.assertNotIn("open(п, 'w'", т)


if __name__ == '__main__':
    unittest.main(verbosity=2)


# --- дата прогона (дефект «TODAY прибит гвоздём», найден 07.09.2026) -------------------
_КОНТРАКТ_X1 = {'id': 'X1', 'hypothesis_id': 'X1', 'тип': 'TREATMENT_CONTROL_DID',
                'основной_показатель': 'выручка минус расход на SKU в сутки'}


class ДатаПрогона(unittest.TestCase):
    """Прибитая TODAY='2026-08-22' держала evaluate-ready в BEFORE_CHECK_DATE вечно,
    штамповала evaluated_at прошлым числом и гасила исправленную оценку как «дубль»."""

    def test_today_не_константа_прошлого(self):
        self.assertNotEqual(E.TODAY, '2026-08-22')
        self.assertEqual(E.TODAY, dt.datetime.now(dt.timezone.utc).date().isoformat())

    def test_today_берётся_из_override(self):
        сейчас = os.environ.get('OZON_EVAL_TODAY')
        os.environ['OZON_EVAL_TODAY'] = '2026-09-07'
        try:
            src = _исходник('ozon_eval_core.py')
            g = {'__name__': 'проверка_даты'}
            exec(compile(src, 'ozon_eval_core.py', 'exec'), g)
            self.assertEqual(g['TODAY'], '2026-09-07')
            hypo = _исходник('ozon_hypo.py').split('# ============================== '
                                                   'конечный автомат')[0]
            g2 = {'__name__': 'проверка_даты_hypo'}
            exec(compile(hypo, 'ozon_hypo.py', 'exec'), g2)
            self.assertEqual(g2['TODAY'], '2026-09-07')      # D6: второй часов нет
        finally:
            if сейчас is None:
                os.environ.pop('OZON_EVAL_TODAY', None)
            else:
                os.environ['OZON_EVAL_TODAY'] = сейчас

    def test_evaluated_at_берёт_дату_прогона(self):
        rec = E._собрать_запись(
            _КОНТРАКТ_X1,
            'CONTINUE_MEASURING', [], 'ch', '2026-09-06', {}, None, None, {}, {}, {}, [], [],
            [], False, today='2026-09-07')
        self.assertEqual(rec['evaluated_at'], '2026-09-07')

    def test_дата_входит_в_идентичность(self):
        общее = (_КОНТРАКТ_X1,
                 'CONTINUE_MEASURING', [], 'ch', '2026-09-06', {}, None, None, {}, {}, {},
                 [], [], [], False)
        a = E._собрать_запись(*общее, today='2026-09-06')
        b = E._собрать_запись(*общее, today='2026-09-07')
        c = E._собрать_запись(*общее, today='2026-09-07')
        self.assertNotEqual(a['evaluation_id'], b['evaluation_id'])
        self.assertEqual(b['evaluation_id'], c['evaluation_id'])   # повтор в тот же день

    def test_оценить_проносит_дату_в_раннюю_ветку(self):
        c = dict(_КОНТРАКТ_X1, тип='POWER_BLOCKED')
        rec = E.оценить(c, None, today='2026-09-07')
        self.assertEqual(rec['evaluated_at'], '2026-09-07')


class ЗагрязнениеНеТеряется(unittest.TestCase):
    """D5: дрейф когорты находился на шаге 1, а собирался на шаге 7 — все ранние ветки
    возвращали contaminations=[] и 16 дней прятали пустую когорту из-за чужого аккаунта."""

    def setUp(self):
        self.кат = tempfile.mkdtemp(prefix='снимок_')
        with open(os.path.join(self.кат, 'X1_treat.csv'), 'w', encoding='utf-8') as f:
            f.write('account,sku\n')
            for i in range(5):                    # снимок шире живой когорты → дрейф
                f.write(f'oz_acc1,t{i}\n')

    def _контракт_с_дрейфом(self, **kw):
        return _контракт(снимок_treatment='X1_treat.csv', **kw)

    def _оценить(self, c, d):
        return E.оценить(c, d, today='2026-08-22', журнал=[], каталог_когорт=self.кат)

    def test_дрейф_виден_до_даты_сверки(self):
        d = _данные([100.0] * 3, [0.0] * 3)
        r = self._оценить(self._контракт_с_дрейфом(дата_сверки='2026-09-30'), d)
        self.assertEqual(r['verdict'], 'CONTINUE_MEASURING')
        self.assertIn('BEFORE_CHECK_DATE', r['reason_codes'])
        self.assertIn('COHORT_DRIFT', r['reason_codes'])
        self.assertTrue(any(z['код'] == 'COHORT_DRIFT' for z in r['contaminations']))

    def test_дрейф_виден_на_незрелых_данных(self):
        d = _данные([100.0] * 3, [0.0] * 3)
        d.зрелые = False
        r = self._оценить(self._контракт_с_дрейфом(), d)
        self.assertEqual(r['verdict'], 'CONTINUE_MEASURING')
        self.assertIn('DATA_NOT_MATURE', r['reason_codes'])
        self.assertIn('COHORT_DRIFT', r['reason_codes'])

    def test_дрейф_не_дублируется_в_полном_прогоне(self):
        d = _данные([100.0] * 3, [0.0] * 3)
        r = self._оценить(self._контракт_с_дрейфом(), d)
        self.assertEqual(r['reason_codes'].count('COHORT_DRIFT'), 1)
        self.assertEqual(len([z for z in r['contaminations']
                              if z['код'] == 'COHORT_DRIFT']), 1)


class НезамороженныйКонтроль(unittest.TestCase):
    """§3: состав контроля, взятый живым запросом по перезаписываемому журналу ставок,
    прегистрацией не является. Факт объявляется контрактом, а не додумывается кодом."""

    def test_флаг_снимает_причинность_и_даёт_непричинный_итог(self):
        d = _данные([100.0 + i * 0.1 for i in range(30)], [i * 0.1 for i in range(30)])
        r = _оценить(_контракт(контроль_заморожен=False), d)
        self.assertEqual(r['verdict'], 'NO_CAUSAL_CLAIM')
        self.assertIn('MISSING_FROZEN_CONTROL', r['reason_codes'])
        self.assertFalse(r['causal_claim_allowed'])
        self.assertNotIn(r['recommendation'], ('KEEP', 'ROLLBACK'))

    def test_без_флага_поведение_не_меняется(self):
        d = _данные([100.0 + i * 0.1 for i in range(30)], [i * 0.1 for i in range(30)])
        r = _оценить(_контракт(), d)
        self.assertNotIn('MISSING_FROZEN_CONTROL', r['reason_codes'])
        self.assertEqual(r['verdict'], 'EFFECT_CONFIRMED_POSITIVE')

    def test_итог_терминальный_а_не_вечное_измерение(self):
        d = _данные([100.0] * 30, [0.0] * 30)
        r = _оценить(_контракт(контроль_заморожен=False), d)
        self.assertNotIn(r['verdict'], ('CONTINUE_MEASURING', 'CONTAMINATED'))
        self.assertNotEqual(r['verdict'], 'CONTAMINATED')


class АккаунтКонтракта(unittest.TestCase):
    """Аккаунт для проверки зрелости берётся из контракта: E6 идёт на oz_acc2, а прогон
    evaluate-ready стартует с oz_acc1 — готовность данных проверялась у чужих загрузчиков."""

    созданные = []

    class _Источник(ФейкДанные):
        def __init__(self, acc, **kw):
            super().__init__(**kw)
            self.acc = acc
            АккаунтКонтракта.созданные.append(acc)

    def setUp(self):
        АккаунтКонтракта.созданные.clear()

    def test_источник_перепривязывается_к_аккаунту_контракта(self):
        d = self._Источник('oz_acc1')
        c = _контракт(account='oz_acc2', тип='VOID_NEVER_APPLIED')
        r = E.оценить(c, d, today='2026-08-22', журнал=[], каталог_когорт=ПУСТОЙ)
        self.assertEqual(r['verdict'], 'VOID')
        self.assertEqual(АккаунтКонтракта.созданные, ['oz_acc1', 'oz_acc2'])

    def test_совпадающий_аккаунт_источник_не_подменяет(self):
        d = self._Источник('oz_acc1')
        r = E.оценить(_контракт(account='oz_acc1'), d, today='2026-08-22', журнал=[],
                      каталог_когорт=ПУСТОЙ)
        self.assertIsNotNone(r['verdict'])
        self.assertEqual(АккаунтКонтракта.созданные, ['oz_acc1'])   # подмены не было

    def test_источник_без_аккаунта_не_трогается(self):
        d = _данные([100.0] * 20, [0.0] * 20)        # ФейкДанные, поля acc нет
        r = _оценить(_контракт(account='oz_acc2'), d)
        self.assertIsNotNone(r['verdict'])
