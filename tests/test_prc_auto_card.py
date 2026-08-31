# поток: prc
"""Отбор строк под автозаведение карточки МС (prices/auto_card.py).

Автомат заводит карточку без человека, поэтому проверяем не «работает ли вообще», а ровно
те условия, при которых он обязан ОТКАЗАТЬСЯ: неполная карточка (правило 11 обязательных
полей) и любое расхождение источников. Ошибка в эту сторону дешевле: строка просто уедет
человеку на вкладку «Новинки».

Сито (`screen`) проверяется офлайн на подставном черновике; отбор (`pick`) — на живой базе:
он весь в SQL, и подменять там нечего. Нет базы или нет подходящих строк — тест пропускается,
а не врёт зелёным.
"""
import unittest

from prices import auto_card

FIELDS = {"Код": "1234at", "Наименование": "Картридж", "Артикул": "CT-1234",
          "Вес": 0.9, "Штрихкод Code128": "DSJUP0001234"}


def draft(novelty_id=1, **over):
    return {**FIELDS, "_novelty_id": novelty_id, **over}


class FakeBuild:
    """Подмена `ms_import.build`: черновик и замечания задаём сами."""

    def __init__(self, records, notes):
        self.records, self.notes = records, notes

    def __call__(self, key, decisions, ids=None, ms_codes=None):
        return self.records, self.notes


class TestScreen(unittest.TestCase):
    def setUp(self):
        self.cand = [{"id": 1, "article": "CT-1234", "name": "Картридж", "ms_code": "1234gp",
                      "ms_id": "x", "ms_name": "Картридж", "external_code": "1234", "qty": 5}]
        self._build = auto_card.ms_import.build

    def tearDown(self):
        auto_card.ms_import.build = self._build

    def screen(self, records, notes=()):
        auto_card.ms_import.build = FakeBuild(records, list(notes))
        return auto_card.screen("sakura", self.cand, log=lambda *_: None)

    def test_полный_черновик_проходит(self):
        good, held, _ = self.screen([draft()])
        self.assertEqual(len(good), 1)
        self.assertEqual(held, [])

    def test_без_веса_человеку(self):
        good, held, _ = self.screen([draft(**{"Вес": ""})])
        self.assertEqual(good, [])
        self.assertIn("вес", held[0]["why"])

    def test_без_штрихкода_человеку(self):
        good, held, _ = self.screen([draft(**{"Штрихкод Code128": ""})])
        self.assertEqual(good, [])
        self.assertIn("Code128", held[0]["why"])

    def test_расхождение_веса_человеку(self):
        # Вес ТК против веса прайса — выбор между двумя источниками делает человек.
        good, held, _ = self.screen(
            [draft()], [(1, "CT-1234", ["вес ТК 0.9 против 1.6 — прайс поставщика; взял ТК"])])
        self.assertEqual(good, [])
        self.assertIn("вес ТК", held[0]["why"])

    def test_вес_не_с_чем_сверить_человеку(self):
        # Единственный источник веса — каталог ТК, а там встречаются опечатки (6882 = 9001 г).
        good, held, _ = self.screen(
            [draft()], [(1, "CT-1234", ["веса нет ни в прайсах, ни у карточек МС 1234 "
                                        "(внешний код) — взял 0.9 кг, сверить не с чем"])])
        self.assertEqual(good, [])

    def test_артикул_уже_есть_человеку(self):
        good, held, _ = self.screen(
            [draft()], [(1, "CT-1234", ["артикул уже есть в МС — импорт ПЕРЕПИШЕТ ту карточку"])])
        self.assertEqual(good, [])

    def test_безобидное_замечание_не_мешает(self):
        good, held, _ = self.screen(
            [draft()], [(1, "CT-1234", ["связь 1234 проставлена вручную — записал в «Связь»"])])
        self.assertEqual(len(good), 1)

    def test_черновик_не_собрался_не_теряется(self):
        good, held, _ = self.screen([], [(1, "CT-1234", ["нет живой карточки МС с внешним "
                                                         "кодом 1234 — проверить код"])])
        self.assertEqual(good, [])
        self.assertEqual(len(held), 1)          # строка ушла человеку, а не исчезла


class TestPick(unittest.TestCase):
    """Отбор на живой базе: у каждой возвращённой строки шесть галочек и один кандидат."""

    @classmethod
    def setUpClass(cls):
        try:
            from core.db import query
        except Exception as exc:                 # базы нет — тест не про неё
            raise unittest.SkipTest(f"нет базы: {exc}")
        cls.query = staticmethod(query)
        cls.rows = auto_card.pick("sakura", any_stock=True)
        if not cls.rows:
            raise unittest.SkipTest("сегодня нет строк со всеми шестью галочками")

    def test_все_шесть_галочек(self):
        ids = [r["id"] for r in self.rows]
        bad = self.query(
            f"""SELECT count(*) n FROM prc_novelty_candidate
                 WHERE rank = 1 AND novelty_id = ANY(%s)
                   AND NOT ({' AND '.join(f'{f} IS TRUE' for f in auto_card.SIX)})""", (ids,))
        self.assertEqual(bad[0]["n"], 0)

    def test_второго_кандидата_с_другим_кодом_нет(self):
        ids = [r["id"] for r in self.rows]
        six = " AND ".join(f"c2.{f} IS TRUE" for f in auto_card.SIX)
        rival = self.query(
            f"""SELECT count(*) n FROM prc_novelty_candidate c
                 JOIN prc_novelty_candidate c2 ON c2.novelty_id = c.novelty_id AND c2.rank <> 1
                WHERE c.rank = 1 AND c.novelty_id = ANY(%s) AND {six}
                  AND c2.external_code IS DISTINCT FROM c.external_code""", (ids,))
        self.assertEqual(rival[0]["n"], 0)

    def test_только_нерешённые_строки(self):
        ids = [r["id"] for r in self.rows]
        other = self.query("""SELECT count(*) n FROM prc_novelty
                               WHERE id = ANY(%s) AND decision <> 'pending'""", (ids,))
        self.assertEqual(other[0]["n"], 0)

    def test_боевой_режим_выключен(self):
        # Пока Сергей не сказал «включай», список поставщиков автозаведения пуст.
        self.assertEqual(auto_card.AUTO_APPLY, ())


if __name__ == "__main__":
    unittest.main()
