# поток: rev
"""Фильтры-ситуации в шаблонах отзывов (`reports/neg_templates.py`).

Два фильтра от 10.09.2026 стоят ПЕРЕД разбором типа дефекта и перебивают его: «после заправки
полосы» — это про заправку, а не про полосы; «не подошёл, выкинул» — помогать уже некому.
Здесь же проверяется, что фильтры не хватают лишнего: «перезаправляемый?» — свойство товара,
«сломался» — отказ товара, а не действие покупателя.

`./venv/bin/python -m unittest tests.test_neg_templates -v` — в БД и в сеть не ходит.
"""
import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from reports import neg_templates as nt              # noqa: E402
from reports.feedback_draft_run import _neg_draft, CLOSED_MARKER   # noqa: E402


class Refill(unittest.TestCase):
    def test_refill_beats_defect(self):
        for t in ("После заправки пошли полосы", "Заправил — печатает бледно",
                  "заправляли в сервисе, теперь не видит картридж", "сдал на заправку, стал мазать"):
            self.assertEqual(nt.classify(t), "refill", t)

    def test_template_says_where_the_warranty_ends(self):
        k, tpl = nt.pick("После заправки полосы")
        self.assertEqual(k, "refill")
        self.assertIn("заводское состояние", tpl)

    def test_refillable_is_not_refilled(self):
        # «перезаправляемый?» — вопрос о свойстве товара, заправки ещё не было
        for t in ("Картриджи перезаправляемые?", "он заправляемый или нет"):
            self.assertIsNone(nt.classify(t), t)


class Closed(unittest.TestCase):
    def test_closed_situations(self):
        for t in ("Не подошёл, выкинул", "выбросил на помойку", "уронил и сломал",
                  "вернул товар и купил другой", "разбил при установке"):
            self.assertEqual(nt.classify(t), "closed", t)

    def test_no_template_for_closed(self):
        k, tpl = nt.pick("Не подошёл, выкинул")
        self.assertEqual(k, "closed")
        self.assertIsNone(tpl, "по закрытой ситуации шаблона быть не должно")

    def test_draft_is_a_marker_for_a_human(self):
        draft, kind = _neg_draft({"platform": "wb", "body": "не подошёл, выкинул",
                                  "pros": None, "cons": None}, "Иван", "Картридж")
        self.assertEqual(kind, "closed")
        self.assertEqual(draft, CLOSED_MARKER)
        self.assertTrue(draft.startswith("\u26a0\ufe0f"), "маркер обязан начинаться с ⚠️ — его режет publish_gate")
        self.assertIn(nt.CLOSED_NOTE, draft)

    def test_broken_on_arrival_is_an_ordinary_complaint(self):
        # «сломался»/«разбилась» — отказ товара, а не действие покупателя: ситуация не закрыта
        for t in ("Картридж сломался через неделю", "крышка разбилась при доставке"):
            self.assertNotEqual(nt.classify(t), "closed", t)


if __name__ == "__main__":
    unittest.main()
