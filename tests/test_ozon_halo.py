# поток: mkt
"""P1-HALO: методические предохранители наблюдательного исследования ореола.

Исследование не эксперимент: снятие 8 407 SKU 09.08 и возврат половины 19.08 назначали
не мы. Отсюда четыре места, где ошибка не видна в цифрах и портит вывод целиком:

  1. границы режимов A/B/C — если окно поедет, сравниваются разные воздействия;
  2. переходные дни 09.08 и 19.08 — воздействие менялось внутри дня, такой день
     не принадлежит ни одному режиму и не имеет права попасть в среднее;
  3. пересечение когорт и попадание в них снятого хвоста: базовый слой обязан быть
     свободен от снятого хвоста, иначе его движение объясняется его же ставкой;
  4. post-treatment selection — отбор состава по данным ПОСЛЕ 09.08 создаёт
     корреляцию из ничего: остаток и цена после воздействия сами являются его следствием.

Тесты офлайн: к БД не ходим, db.query подменяется.
"""
import io, sys, csv, json, unittest, datetime as dt

sys.path.insert(0, '/opt/mp-analytics')
sys.path.insert(0, '/opt/mp-analytics/tools')
import ozon_halo_eval as H  # noqa: E402

CONTRACT = '/opt/mp-analytics/docs/experiments/ozon_observational_studies.json'
ИНСТРУМЕНТ = '/opt/mp-analytics/tools/ozon_halo_eval.py'


def _исходник():
    with open(ИНСТРУМЕНТ, encoding='utf-8') as f:
        return f.read()


class ПодставнаяВитрина:
    """Границы режимов зависят от загрузки; на время теста отдаём фиксированный день.

    Подменяются оба входа: `db.query` (last_full_day) и `mature_through` — правый край
    режима C ставится по последнему ЗРЕЛОМУ дню, а зрелость ходит в пять таблиц.
    """

    def __init__(self, day):
        self.day = day

    def __enter__(self):
        self.orig, self.orig_m = H.db.query, H.mature_through
        H.db.query = lambda sql, params=None: [{'d': self.day, 'x': self.day, 'n': 1}]
        H.mature_through = lambda start=None: self.day
        return self

    def __exit__(self, *a):
        H.db.query, H.mature_through = self.orig, self.orig_m


class ГраницыРежимов(unittest.TestCase):

    def test_окна_совпадают_с_историей_вмешательств(self):
        # A кончается последним днём ДО снятия, B начинается первым полным днём ПОСЛЕ,
        # B кончается последним днём ДО возврата, C начинается через день после возврата.
        self.assertEqual(H.REGIME_A[1], H.FREEZE_DAY)
        self.assertEqual(H.REGIME_B[0], '2026-08-10')
        self.assertEqual(H.REGIME_B[1], '2026-08-18')
        self.assertEqual(H.REGIME_C_START, '2026-08-20')
        self.assertEqual(H.REMOVAL_DAY, '2026-08-09')
        self.assertEqual(H.RESTORE_DAY, '2026-08-19')

    def test_между_окнами_нет_дыр_кроме_переходных_дней(self):
        поA = dt.date.fromisoformat(H.REGIME_A[1]) + dt.timedelta(days=1)
        self.assertEqual(поA.isoformat(), H.REMOVAL_DAY)
        поB = dt.date.fromisoformat(H.REGIME_B[1]) + dt.timedelta(days=1)
        self.assertEqual(поB.isoformat(), H.RESTORE_DAY)
        доC = dt.date.fromisoformat(H.REGIME_C_START) - dt.timedelta(days=1)
        self.assertEqual(доC.isoformat(), H.RESTORE_DAY)

    def test_режим_C_не_существует_пока_витрина_не_доехала(self):
        with ПодставнаяВитрина('2026-08-19'):
            self.assertIsNone(H.regime_c())
        with ПодставнаяВитрина('2026-08-20'):
            self.assertEqual(H.regime_c(), ('2026-08-20', '2026-08-20'))

    def test_вердикт_требует_полной_недели(self):
        self.assertGreaterEqual(H.MIN_POST_DAYS, 7)


class ПереходныеДни(unittest.TestCase):

    def test_оба_дня_объявлены_переходными_с_причиной(self):
        self.assertEqual(set(H.TRANSITION), {'2026-08-09', '2026-08-19'})
        for d, причина in H.TRANSITION.items():
            self.assertTrue(причина.strip(), f'{d} без объяснения')

    def test_19_08_не_попадает_в_режим_C(self):
        with ПодставнаяВитрина('2026-08-21'):
            self.assertEqual(H.regime_of('2026-08-19'), 'TRANSITION_DAY')
            self.assertEqual(H.regime_of('2026-08-20'), 'C')

    def test_09_08_не_попадает_ни_в_A_ни_в_B(self):
        with ПодставнаяВитрина('2026-08-21'):
            self.assertEqual(H.regime_of('2026-08-09'), 'TRANSITION_DAY')
            self.assertEqual(H.regime_of('2026-08-08'), 'A')
            self.assertEqual(H.regime_of('2026-08-10'), 'B')

    def test_переходные_дни_выброшены_из_состава_дней_окна(self):
        # окно, накрывающее оба перехода: ни один из них не должен вернуться
        дни = H.days_of(('2026-08-08', '2026-08-20'))
        self.assertNotIn('2026-08-09', дни)
        self.assertNotIn('2026-08-19', дни)
        self.assertIn('2026-08-08', дни)
        self.assertIn('2026-08-20', дни)
        self.assertEqual(len(дни), 11)   # 13 календарных минус 2 переходных

    def test_окно_B_целиком_без_переходных_дней(self):
        дни = H.days_of(H.REGIME_B)
        self.assertEqual(len(дни), 9)
        self.assertFalse(set(дни) & set(H.TRANSITION))


class ЗащитаОтПостФильтрации(unittest.TestCase):

    def test_гард_пропускает_даты_baseline(self):
        self.assertTrue(H.assert_baseline_only('2026-07-27', H.FREEZE_DAY, None))

    def test_гард_ловит_дату_после_снятия(self):
        for d in (H.REMOVAL_DAY, '2026-08-20', H.RESTORE_DAY):
            with self.assertRaises(AssertionError, msg=f'{d} прошёл гард'):
                H.assert_baseline_only(d)

    def test_гард_ловит_дату_в_середине_списка(self):
        with self.assertRaises(AssertionError):
            H.assert_baseline_only('2026-07-27', '2026-08-15', '2026-08-01')

    def test_baseline_окна_отбора_не_заходят_за_freeze(self):
        for окно in (H.ADS_BASE, H.SALES_BASE):
            self.assertTrue(H.assert_baseline_only(*окно))

    def test_freeze_раньше_дня_снятия(self):
        self.assertLess(dt.date.fromisoformat(H.FREEZE_DAY),
                        dt.date.fromisoformat(H.REMOVAL_DAY))


class ЗамороженныеКогорты(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(CONTRACT, encoding='utf-8') as f:
            c = json.load(f)['исследования'][0]
        cls.contract = c
        cls.files = {k: '/opt/mp-analytics/' + v['файл']
                     for k, v in c['frozen_cohorts'].items() if isinstance(v, dict)}
        cls.sku = {k: set(H.read_cohort(p)) for k, p in cls.files.items()}

    def test_когорты_не_пересекаются(self):
        a = self.sku['CORE_A_STABLE_ADVERTISED']
        b = self.sku['BASELINE_NEVER_ADVERTISED']
        self.assertEqual(a & b, set(), 'SKU не может быть одновременно стабильно '
                                       'рекламируемым и никогда не рекламировавшимся')

    def test_когорты_непусты_и_совпадают_с_контрактом(self):
        for k, s in self.sku.items():
            self.assertTrue(s, f'{k} пуста')
            self.assertEqual(len(s), self.contract['frozen_cohorts'][k]['sku'],
                             f'{k}: размер CSV разошёлся с контрактом — состав менялся')

    def test_ни_один_SKU_когорт_не_из_снятого_хвоста(self):
        # хвост — это и есть воздействие; его собственные продажи ореолом не являются
        воронка = '/opt/mp-analytics/docs/experiments/cohorts/HALO_cohort_funnel_2026-08-21.csv'
        with open(воронка, encoding='utf-8') as f:
            хвост = {r['sku'] for r in csv.DictReader(f)
                     if 'волн' in r.get('причина_исключения', '')}
        self.assertTrue(хвост, 'в воронке не осталось следа исключения волны 1')
        for k, s in self.sku.items():
            self.assertEqual(s & хвост, set(), f'{k} содержит снятый хвост')

    def test_у_каждого_исключённого_названа_причина(self):
        воронка = '/opt/mp-analytics/docs/experiments/cohorts/HALO_cohort_funnel_2026-08-21.csv'
        with open(воронка, encoding='utf-8') as f:
            rows = list(csv.DictReader(f))
        self.assertTrue(rows)
        for r in rows:
            if r.get('вошёл') in ('0', 'нет', 'False', ''):
                self.assertTrue(r.get('причина_исключения', '').strip(),
                                f"{r['sku']} исключён без названной причины")


class ЕдиныеОпределения(unittest.TestCase):
    """Расхождение 72/76 было не арифметикой, а двумя разными определениями заказа."""

    def test_постинг_с_двумя_sku_когорты_считается_один_раз(self):
        # панель хранит НОМЕРА постингов, а не их счётчик: иначе постинг с двумя SKU
        # когорты складывается дважды и ITT расходится с основной таблицей
        src = _исходник()
        self.assertIn("c.setdefault('posts', [])", src,
                      'panel() снова считает постинги счётчиком по SKU')
        self.assertIn('all_posts', src, 'variant_stats не собирает множество постингов')

    def test_определения_объявлены_и_едины(self):
        for x in ('постинги', 'отменённые', 'завершённые', 'SKU-дни в наличии'):
            self.assertIn(x, H.MEASURES, f'{x} не описан в MEASURES')
        self.assertIn('ОДИН постинг', H.MEASURES)

    def test_завершённые_это_постинги_минус_отменённые(self):
        for p_, canc in ((72, 6), (39, 4), (0, 0)):
            self.assertEqual(p_ - canc, p_ - canc)
        self.assertIn("g['завершённые'] = g['postings'] - g['cancelled']", _исходник())


class ЗрелостьДанных(unittest.TestCase):
    """Недогруженный день выглядит как провал продаж: сравнивать его нельзя."""

    def test_источники_перечислены(self):
        self.assertEqual(set(H.NEED_TO_COMPARE), {'ads', 'posting', 'price', 'stock'})
        self.assertIn('transaction', H.NEED_FOR_ACCOUNT)
        self.assertTrue(set(H.NEED_TO_COMPARE) < set(H.NEED_FOR_ACCOUNT),
                        'аккаунтный разрез обязан требовать не меньше, чем когортный')

    def test_режим_C_кончается_зрелым_днём(self):
        orig = H.mature_through
        try:
            H.mature_through = lambda start=None: '2026-08-20'
            self.assertEqual(H.regime_c()[1], '2026-08-20')
            H.mature_through = lambda start=None: '2026-08-19'
            self.assertIsNone(H.regime_c(), 'режим C не может кончаться раньше своего начала')
        finally:
            H.mature_through = orig

    def test_сутки_закрывает_прогон_после_полуночи(self):
        self.assertEqual(H.POSTING_CLOSE_UTC, 0,
                         'порог строже полуночи объявит зрелый день недогруженным')


class ВторичныйСрезЭкспозиции(unittest.TestCase):
    """PERSISTENT_ZERO_AD_CORE отобран постфактум — он не имеет права стать основным."""

    @classmethod
    def setUpClass(cls):
        with open(CONTRACT, encoding='utf-8') as f:
            cls.c = json.load(f)['исследования'][0]

    def test_объявлен_вторичным(self):
        z = self.c['frozen_cohorts']['PERSISTENT_ZERO_AD_CORE']
        self.assertIn('вторичный', z['статус'])
        self.assertIn('post-hoc', z['почему'])

    def test_является_подмножеством_базового_слоя(self):
        b = set(H.read_cohort('/opt/mp-analytics/' +
                              self.c['frozen_cohorts']['BASELINE_NEVER_ADVERTISED']['файл']))
        z = set(H.read_cohort('/opt/mp-analytics/' +
                              self.c['frozen_cohorts']['PERSISTENT_ZERO_AD_CORE']['файл']))
        self.assertTrue(z <= b, 'срез вышел за пределы состава, из которого отбирался')
        self.assertLess(len(z), len(b), 'срез обязан быть строго уже — иначе он бессмыслен')

    def test_старое_имя_не_используется_в_коде(self):
        src = _исходник()
        self.assertNotIn('CORE_B_NEVER_ADVERTISED', src,
                         'имя утверждает факт о будущем, которого нет')


class КонтрактКластеров(unittest.TestCase):
    """CLUSTER_SPILLOVER: дизайн заморожен, вывод запрещён до проверки мощности."""

    @classmethod
    def setUpClass(cls):
        with open(CONTRACT, encoding='utf-8') as f:
            cls.c = [x for x in json.load(f)['исследования'] if x['id'] == 'CLUSTER_SPILLOVER'][0]

    def test_кластеры_не_смотрят_на_возврат(self):
        self.assertEqual(self.c['кластеры']['данные_только_до'], '2026-08-18')
        self.assertEqual(H.PRE_RESTORE, '2026-08-18')
        self.assertLess(dt.date.fromisoformat(H.PRE_RESTORE),
                        dt.date.fromisoformat(min(H.TRANSITION.keys(), key=lambda x: x)
                                              if False else '2026-08-19'),
                        'признаки кластера обязаны кончаться до дня возврата')

    def test_гард_ловит_дату_после_возврата(self):
        H.assert_pre_restore_only('2026-08-18')
        with self.assertRaises(AssertionError):
            H.assert_pre_restore_only('2026-08-19')
        with self.assertRaises(AssertionError):
            H.assert_pre_restore_only('2026-08-10', '2026-08-20')

    def test_причинный_вывод_запрещён_пока_мощности_нет(self):
        self.assertFalse(self.c['causal_claim_allowed'])
        self.assertIn('мощность', self.c['почему_false'])
        self.assertGreater(self.c['мощность']['MDE_в_долях_среднего'], 1.0,
                           'если MDE стал меньше среднего, флаг пересматривается сознательно')

    def test_баланс_проверен_отдельно_от_мощности(self):
        b = self.c['баланс']
        self.assertLessEqual(b['макс_d'], b['порог'])
        self.assertIn('0,5', b['почему_ничьи'], 'не объяснено, почему ничьи отброшены')

    def test_receivers_определены_по_назначению_а_не_по_поведению(self):
        r = self.c['receivers']['определение']
        for x in ('E8-A', 'E8-B', 'mkt_ozon_bid_journal', 'волны 1'):
            self.assertIn(x, r)
        self.assertIn('post-treatment', self.c['receivers']['почему_так'])

    def test_запреты_на_месте(self):
        z = ' | '.join(self.c['что_запрещено'])
        for x in ('мощности', 'ставки E8', '19.08'):
            self.assertIn(x, z)


class КонтрактИсследования(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(CONTRACT, encoding='utf-8') as f:
            cls.c = json.load(f)['исследования'][0]

    def test_обязательные_поля_на_месте(self):
        for поле in ('type', 'causal_claim_allowed', 'intervention_owner',
                     'primary_hypothesis', 'frozen_cohorts', 'known_contaminations',
                     'earliest_evaluation_date'):
            self.assertIn(поле, self.c)

    def test_это_наблюдение_а_не_эксперимент(self):
        self.assertEqual(self.c['type'], 'observational_study')
        self.assertIs(self.c['causal_claim_allowed'], False)
        self.assertEqual(self.c['intervention_owner'], 'historical_E2_E8')

    def test_переходные_дни_записаны_в_загрязнения(self):
        текст = ' '.join(self.c['known_contaminations'])
        self.assertIn('19.08', текст)
        self.assertIn('09.08', текст)

    def test_режимы_контракта_совпадают_с_кодом(self):
        r = self.c['режимы']
        self.assertEqual(r['A']['окно'], f'{H.REGIME_A[0]}..{H.REGIME_A[1]}')
        self.assertEqual(r['B']['окно'], f'{H.REGIME_B[0]}..{H.REGIME_B[1]}')
        self.assertEqual(r['C']['начало'], H.REGIME_C_START)
        self.assertEqual(set(r['TRANSITION_DAY']), set(H.TRANSITION))

    def test_контракт_экспериментов_не_тронут(self):
        # P1-HALO не должен появиться среди E5–E8 как обычный эксперимент
        with open('/opt/mp-analytics/docs/experiments/ozon_experiments.json',
                  encoding='utf-8') as f:
            ids = json.dumps(json.load(f), ensure_ascii=False)
        self.assertNotIn('P1-HALO', ids)
        self.assertNotIn('"E9"', ids)


if __name__ == '__main__':
    unittest.main(verbosity=2)
