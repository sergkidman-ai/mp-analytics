# поток: rev
"""Пакет правок 21.09.2026 по неделе модерации — по тесту на каждый пункт, где это возможно без БД.

Пункты, которым нужна живая база (п.8 поиск в кэше, п.10 склейка, п.12 brand_notes), проверяет
tools/rev_regression.py, раздел 12, на реальных строках недели.

    PYTHONPATH=/opt/mp-analytics ./venv/bin/python -m unittest tests.test_rev_package_0921 -v
"""
import inspect
import unittest

from reports import answer_cache as ac
from reports import feedback_draft_run as dr
from reports import feedback_llm as fl
from reports import feedback_today as ft
from reports import publish_gate as gate
from reports import request_class as rc


class P1PositiveAuto(unittest.TestCase):
    def rv(self, text, rating=5):
        return {"kind": "review", "rating": rating, "body": text, "pros": "", "cons": ""}

    def test_длинный_позитив_без_дефекта_авто(self):
        self.assertTrue(ft._positive_clean(self.rv("Отличный картридж, " * 20)))

    def test_4_звезды_тоже(self):
        self.assertTrue(ft._positive_clean(self.rv("Нормально печатает, доставка быстрая", 4)))

    def test_дефект_на_вычитку(self):
        self.assertFalse(ft._positive_clean(self.rv("Хороший, но полосит", 5)))

    def test_3_звезды_на_вычитку(self):
        self.assertFalse(ft._positive_clean(self.rv("Нормально", 3)))


class P2FalseClaim(unittest.TestCase):
    def test_провалы_в_прошлом_не_претензия(self):
        self.assertFalse(rc.is_claim_text("Приобрела с надеждой, что подойдёт (были провалы уже). Печать отличная",
                                          "review", 5))

    def test_заменила_сама_не_претензия(self):
        self.assertFalse(rc.is_claim_text("Хорошее решение. Заменила картридж сама", "review", 5))

    def test_дефект_на_позитиве_остаётся_претензией(self):
        self.assertTrue(rc.is_claim_text("Хорошие, но полосит, хочу замену", "review", 4))

    def test_на_4_звёздах_кнопка_как_есть_есть(self):
        row = {"kind": "review", "rating": 4, "platform": "wb", "body": "Хорошие, но полосит, хочу замену",
               "draft_text": "Спасибо за отзыв!", "draft_route": "review",
               "draft_grounding": {"template_id": "llm", "source": "карточка"}}
        _ok, why, _ = gate.verdict_full(row, row["draft_text"])
        self.assertTrue(any("≥4★" in w for w in why), why)
        self.assertTrue(gate.can_override(why))

    def test_на_2_звёздах_без_кнопки(self):
        row = {"kind": "review", "rating": 2, "platform": "wb", "body": "Полосит, хочу замену",
               "draft_text": "Спасибо за отзыв!", "draft_route": "review",
               "draft_grounding": {"template_id": "llm", "source": "карточка"}}
        _ok, why, _ = gate.verdict_full(row, row["draft_text"])
        self.assertFalse(gate.can_override(why))


class P3Defect(unittest.TestCase):
    def test_новые_маркеры(self):
        for t in ("не пропечатывает до конца", "что-то плохо работает", "три подошли, один не тот",
                  "пришёл не тот цвет"):
            self.assertTrue(dr.DEFECT_RX.search(t), t)

    def test_похвала_не_дефект(self):
        for t in ("хватило до конца месяца", "всё то, что нужно"):
            self.assertFalse(dr.DEFECT_RX.search(t), t)


class P4Jargon(unittest.TestCase):
    def test_правило_в_промпте_генерации(self):
        self.assertIn("БЕЗ ЖАРГОНА", fl.SYSTEM)
        self.assertIn("вариант серии", fl.SYSTEM)

    def test_детерминированный_ответ_без_жаргона(self):
        src = inspect.getsource(ft._answer)
        # сам текст ответа (f-строка), а не комментарий, который объясняет правку
        self.assertNotIn('f"совместимости карточки."', src)
        self.assertIn('fam_reply = (f"Здравствуйте! Да, подойдёт для {\', \'.join(mm)}."', src)


class P5Fallback(unittest.TestCase):
    def test_нет_серийного_фолбэка_на_многосоставный(self):
        src = inspect.getsource(ft._answer)
        self.assertNotIn("fallback на shortcut", src)
        self.assertIn("вопрос многосоставный", src)


class P6SetClaim(unittest.TestCase):
    def test_один_не_тот(self):
        self.assertTrue(rc.is_claim_text("3 цвета подошли, один не тот", "question"))

    def test_один_короче(self):
        self.assertTrue(rc.set_claim("Все подошли, а один короче остальных"))

    def test_цвет_короче_остальных(self):
        self.assertTrue(rc.set_claim("Дело в том, что жёлтый картридж короче, чем остальные три"))

    def test_обычный_вопрос_о_наборе_не_претензия(self):
        self.assertFalse(rc.set_claim("Сколько картриджей в наборе?"))
        self.assertFalse(rc.set_claim("Чёрный больше остальных по объёму?"))
        self.assertFalse(rc.set_claim("Один из лучших наборов, цвета яркие"))


class P7Series(unittest.TestCase):
    CARD = ["i-SENSYS MF651Cw", "MF655Cdw", "MF657Cdw", "LBP631Cw"]

    def test_серия_перечисляется(self):
        self.assertEqual(ft._series_members("Подойдёт на MF650?", self.CARD),
                         ["i-SENSYS MF651Cw", "MF655Cdw", "MF657Cdw"])

    def test_точная_модель_не_серия(self):
        self.assertEqual(ft._series_members("На MF655Cdw подойдёт?", self.CARD), [])

    def test_x_как_маска(self):
        self.assertEqual(ft._series_members("Epson C5x90?", ["WF-C5290DW", "WF-C5790DWF", "WF-C579R"]),
                         ["WF-C5290DW", "WF-C5790DWF"])

    def test_формулировка(self):
        self.assertEqual(ft._or_list(["A", "B", "C"]), "A, B или C")


class P8Family(unittest.TestCase):
    def test_код_из_названия(self):
        f = {"code": None, "name": "Картриджи №963XL для принтеров HP OfficeJet Pro 9010",
             "annot": "", "models": ["OfficeJet Pro 9010"]}
        self.assertEqual(ac.family_code(f), "963XL")

    def test_модель_принтера_не_код(self):
        f = {"code": None, "name": "Картридж для HP OfficeJet Pro 9019",
             "annot": "Совместим с 9019, 9010. Аналог 963XL.", "models": ["OfficeJet Pro 9019", "OfficeJet Pro 9010"]}
        self.assertEqual(ac.family_code(f), "963XL")

    def test_нормализация(self):
        self.assertEqual(ac.norm_code("963 xl"), "963XL")


class P9AutoCardQ(unittest.TestCase):
    R = {"kind": "question", "platform": "wb", "account": "wb_acc1", "ext_id": "T", "rating": None,
         "body": "Какой ресурс картриджа?", "payload": {}}

    def g(self, **kw):
        base = {"request_class": "характеристики", "source": "карточка", "verify": "PASS",
                "grounded": True, "llm": True}
        base.update(kw)
        return base

    def test_карточный_класс_уходит_авто(self):
        route, g = ft._auto_card_q(self.R, "Ресурс 1500 страниц.", "review", self.g())
        self.assertEqual(route, "auto")
        self.assertIn("auto_policy", g)

    def test_без_pass_контролёра_нет(self):
        self.assertEqual(ft._auto_card_q(self.R, "Ресурс 1500 страниц.", "review",
                                         self.g(verify="skip"))[0], "review")

    def test_источник_модель_нет(self):
        self.assertEqual(ft._auto_card_q(self.R, "Ресурс 1500 страниц.", "review",
                                         self.g(source="модель"))[0], "review")

    def test_обещание_нет(self):
        self.assertEqual(ft._auto_card_q(self.R, "Ресурс 1500 страниц, заменим если что.", "review",
                                         self.g())[0], "review")

    def test_совместимость_только_точная(self):
        q = dict(self.R, body="Подойдёт на MF655Cdw?")
        loose = self.g(request_class="совместимость",
                       compat={"asked": ["MF655Cdw"], "matched": ["MF655Cdw"], "exact": False})
        exact = dict(loose, compat={"asked": ["MF655Cdw"], "matched": ["MF655Cdw"], "exact": True})
        self.assertEqual(ft._auto_card_q(q, "Да, подойдёт для MF655Cdw.", "review", loose)[0], "review")
        self.assertEqual(ft._auto_card_q(q, "Да, подойдёт для MF655Cdw.", "review", exact)[0], "auto")

    def test_первая_подстановка_кэша_нет(self):
        self.assertEqual(ft._auto_card_q(self.R, "Ресурс 1500 страниц.", "review",
                                         self.g(source="кэш: утверждено", cache={"confirm": True}))[0],
                         "review")

    def test_отзыв_не_трогаем(self):
        self.assertEqual(ft._auto_card_q(dict(self.R, kind="review"), "Спасибо", "review", self.g())[0],
                         "review")


class P11Source(unittest.TestCase):
    def test_оговорка_модели_главнее(self):
        self.assertEqual(ft._llm_source({"source": "card", "note": "не из CARD_DATA, по знанию серии"}),
                         "модель")

    def test_поле_source(self):
        self.assertEqual(ft._llm_source({"source": "knowledge"}), "модель")
        self.assertEqual(ft._llm_source({"source": "brand_notes"}), "справочник бренда")
        self.assertEqual(ft._llm_source({"source": "card", "note": "по CARD_DATA"}), "карточка")

    def test_без_поля_по_grounded(self):
        self.assertEqual(ft._llm_source({"grounded": False}), "модель")


if __name__ == "__main__":
    unittest.main(verbosity=2)
