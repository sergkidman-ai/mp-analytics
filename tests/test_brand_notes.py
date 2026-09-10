# поток: rev
"""Справочник знаний по брендам (миграция 707, задача «заметки по брендам» 10.09.2026).

Смысл проверок. Справочник — это входные данные наравне с карточкой, поэтому цена ошибки здесь
такая же: подставили чужую серию — модель уверенно расскажет покупателю про не тот картридж.
Проверяем, что запись цепляется по коду серии, а не по слову «Pro», что общие правила идут всегда,
и что `covers()` гасит веб-поиск только там, где своё знание по теме действительно есть.

Тест ходит в БД (таблица brand_notes) и за собой прибирает: бренд-пустышка `тест-бренд`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import db                                   # noqa: E402
from reports import brand_notes as bn                 # noqa: E402

B = "тест-бренд"


class LookupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.execute("DELETE FROM brand_notes WHERE brand=%s", (B,))
        bn.upsert(B, "M454/M479 (415A), 953XL", "чип", "415A требует чип.")
        bn.upsert(B, "", "заправка", "Гарантия на заправку не распространяется.")

    @classmethod
    def tearDownClass(cls):
        db.execute("DELETE FROM brand_notes WHERE brand=%s", (B,))

    def test_серия_ловится_по_коду_из_карточки(self):
        got = bn.lookup(B, "А чип стоит?", "CARD_DATA: картридж 415A для M454dn")
        self.assertTrue(any(n["brand"] == B and n["topic"] == "чип" for n in got))

    def test_чужая_серия_не_подставляется(self):
        got = bn.lookup(B, "А чип стоит?", "CARD_DATA: картридж TK-1150 для Kyocera M2135")
        self.assertFalse(any(n["brand"] == B and n["topic"] == "чип" for n in got),
                         "запись про 415A не имеет права уехать в промпт по чужому лоту")

    def test_запись_без_серии_берётся_по_теме(self):
        self.assertTrue(any(n["brand"] == B and n["topic"] == "заправка" for n in bn.lookup(B, "Можно заправлять?", "")))
        self.assertFalse(any(n["brand"] == B and n["topic"] == "заправка" for n in bn.lookup(B, "Когда доставка?", "")))

    def test_общие_правила_идут_всегда(self):
        # brand='*' загружены из файла Сергея; они не зависят ни от бренда, ни от темы
        got = bn.lookup(B, "Когда доставка?", "")
        self.assertTrue(any(n["brand"] == bn.ANY_BRAND for n in got))

    def test_неизвестный_бренд_даёт_только_общие(self):
        got = bn.lookup("бренда-нет-такого", "Чип есть?", "")
        self.assertTrue(all(n["brand"] == bn.ANY_BRAND for n in got))


class CoversTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        db.execute("DELETE FROM brand_notes WHERE brand=%s", (B,))
        bn.upsert(B, "", "чип", "Чип одноразовый.")

    @classmethod
    def tearDownClass(cls):
        db.execute("DELETE FROM brand_notes WHERE brand=%s", (B,))

    def test_своё_знание_гасит_веб(self):
        self.assertTrue(bn.covers(B, "Чип одноразовый или сбрасывается?"))

    def test_чужая_тема_веб_не_гасит(self):
        # про ресурс записи нет — за фактом по-прежнему идём в веб, а не молчим
        self.assertFalse(bn.covers(B, "На сколько страниц хватит?"))

    def test_без_бренда_не_гасим(self):
        self.assertFalse(bn.covers(None, "Чип есть?"))


class BlockTest(unittest.TestCase):
    def test_пустой_справочник_не_даёт_блока(self):
        self.assertEqual(bn.block([]), "")

    def test_блок_называет_приоритет_карточки(self):
        txt = bn.block([{"brand": "canon", "series_pattern": "067H", "topic": "чип",
                         "text": "Чип одноразовый."}])
        self.assertIn("ЗНАНИЯ ПО БРЕНДУ", txt)
        self.assertIn("прав CARD_DATA", txt)
        self.assertIn("067H", txt)


class ParseTest(unittest.TestCase):
    def test_нормализация_бренда(self):
        self.assertEqual(bn._norm_brand("Konica Minolta"), "konica")
        self.assertEqual(bn._norm_brand("Общие правила (все бренды)"), bn.ANY_BRAND)

    def test_коды_серии_без_слов(self):
        codes = bn._codes("OfficeJet Pro 8710/8720 (953, 963)")
        self.assertIn("8710", codes)
        self.assertIn("953", codes)
        self.assertNotIn("pro", codes)


if __name__ == "__main__":
    unittest.main(verbosity=2)
