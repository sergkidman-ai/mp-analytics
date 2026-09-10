# поток: rev
"""Связи номенклатуры (`reports/sku_relations.py`) — разбор названий и выбор типа связи по вопросу.

Проверяется чистая часть, без БД: код расходника по нашей конвенции названий, состав комплекта и
решение «какую связь вообще уместно подставлять в этот вопрос». Последнее — главный предохранитель:
связь подставляется, только когда покупатель спросил именно про соседний лот; во всех прочих случаях
блок обязан быть пустым, иначе модель начнёт зазывать на другой товар посреди ответа о совместимости.

`./venv/bin/python -m unittest tests.test_sku_relations -v` — в БД и в сеть не ходит.
"""
import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from reports import sku_relations as sr   # noqa: E402


class TestCode(unittest.TestCase):
    def test_head_convention(self):
        # наша конвенция: «Картридж <КОД> для принтеров …», в т.ч. с приставкой DS у Дисквэра
        self.assertEqual(sr.code_of_title("Картридж W1360A для принтеров HP LaserJet M211"), "W1360A")
        self.assertEqual(sr.code_of_title("Фотобарабан DR-2085 для принтеров Brother DCP-L2620"), "DR-2085")
        self.assertEqual(sr.code_of_title("Картридж DS W2122X для принтеров HP Color LaserJet"), "W2122X")
        # у Canon код голый, без букв — общим шаблоном его не поймать, только по конвенции
        self.assertEqual(sr.code_of_title("Картриджи 067H для принтеров Canon i-SENSYS LBP631"), "067H")

    def test_no_code(self):
        self.assertIsNone(sr.code_of_title(""))
        self.assertIsNone(sr.code_of_title("Бумага офисная А4 500 листов"))

    def test_kit_components(self):
        codes = sr.codes_in_title("Комплект картриджей W1360A (10 шт.) для принтеров HP на 1150 страниц")
        self.assertEqual(codes[:1], ["W1360A"])
        # количество и ресурс — не коды: «10 шт.» и «1150 страниц» в состав попасть не должны
        self.assertNotIn("10", codes)
        self.assertNotIn("1150", codes)
        self.assertIn("CF401A", sr.codes_in_title("Картриджи CF400A + CF401A + CF402A комплект"))


class TestFamily(unittest.TestCase):
    def test_drum_toner_family(self):
        self.assertEqual(sr._family("DR-2085"), ("drum", "2085"))
        self.assertEqual(sr._family("TN-2085"), ("toner", "2085"))
        self.assertEqual(sr._family("DK-1150"), ("drum", "1150"))
        self.assertEqual(sr._family("TK-1150"), ("toner", "1150"))
        # чужие семейства в пару не сшиваются: у W1360A цифры ничего не значат
        self.assertEqual(sr._family("W1360A"), (None, None))


class TestQuestionTypes(unittest.TestCase):
    def test_chip_only_on_chipless(self):
        self.assertEqual(sr.types_for_question("а чип есть?", card_chip="none"), ["chip_pair"])
        # на лоте, где чип уже стоит, предлагать «версию с чипом» нечего
        self.assertEqual(sr.types_for_question("а чип есть?", card_chip="installed"), [])

    def test_kit_question(self):
        t = sr.types_for_question("что входит в комплект?", card_kind="drum")
        self.assertIn("kit_component", t)
        self.assertIn("drum_toner", t)

    def test_toner_to_drum_and_back(self):
        # стоим на барабане, спрашивают про тонер — и наоборот
        self.assertIn("drum_toner", sr.types_for_question("а тонер к нему есть?", card_kind="drum"))
        self.assertIn("drum_toner", sr.types_for_question("а барабан отдельно есть?", card_kind="toner"))
        # на барабане вопрос «это барабан?» связь не поднимает — он уже на нужной карточке
        self.assertEqual(sr.types_for_question("это фотобарабан?", card_kind="drum"), [])

    def test_silence_by_default(self):
        for q in ("подойдёт ли к HP M211?", "какой ресурс?", "когда доставка?", ""):
            self.assertEqual(sr.types_for_question(q, card_chip="none", card_kind="toner"), [],
                             f"лишняя связь на вопросе: {q!r}")


class ColorPair(unittest.TestCase):
    """Струйная пара чёрный ↔ цветной: сторона и ключ должны сойтись у обоих лотов пары."""

    def _v(self, title, brand=None, kind="ink"):
        return {"title": title, "code": sr.code_of_title(title), "brand": brand, "kind": kind}

    def test_canon_pg_cl(self):
        for bk, cl in (("Картридж PG-510BK для Canon Pixma IP2700 черный",
                        "Картридж CL-511 для Canon Pixma IP2700 цветной"),
                       ("Картридж PG-440 для Canon Pixma MG2140 черный",
                        "Картридж CL-441 для Canon Pixma MG2140 цветной"),
                       ("Картридж PG-445 для Canon Pixma MG2440 черный",
                        "Картридж CL-446 для Canon Pixma MG2440 цветной")):
            b, c = self._v(bk, "canon"), self._v(cl, "canon")
            self.assertEqual(sr._color_side(b), "black", bk)
            self.assertEqual(sr._color_side(c), "color", cl)
            self.assertEqual(sr._color_key(b), sr._color_key(c), f"{bk} ↔ {cl}")

    def test_hp_same_number_two_sides(self):
        b = self._v("Картридж 122 для принтеров HP Deskjet 1050 черный", "hp")
        c = self._v("Картридж 122 для принтеров HP Deskjet 1050 цветной", "hp")
        self.assertEqual((sr._color_side(b), sr._color_side(c)), ("black", "color"))
        self.assertEqual(sr._color_key(b), sr._color_key(c))

    def test_brand_in_key(self):
        """Голый номер 664 есть и у HP, и у Epson — в одну пару они слипаться не должны."""
        self.assertNotEqual(sr._color_key(self._v("Картридж 664 для принтеров HP черный", "hp")),
                            sr._color_key(self._v("Чернила 664 для принтеров Epson черные", "epson")))

    def test_kit_is_not_a_side(self):
        """Комплект «PG-510+CL-511» в паре не участвует — в нём уже обе стороны."""
        self.assertFalse(sr._is_single_ink(
            self._v("Картриджи PG-510+CL-511 для Canon Pixma MX320")))

    def test_color_question(self):
        for q in ("У вас есть цветной такой картридж 492 или 490?", "а чёрный отдельно есть?"):
            self.assertIn("color_pair", sr.types_for_question(q, card_kind="ink"), q)
        self.assertNotIn("color_pair", sr.types_for_question("какой ресурс?", card_kind="ink"))


class ChipLineScope(unittest.TestCase):
    """С 10.09.2026 строка про версию с чипом идёт на любой вопрос о ТОВАРЕ, но не о сделке."""

    def test_deal_questions_are_silent(self):
        for q in ("Когда будет доставка?", "есть в наличии?", "какая цена?", "сколько стоит?", ""):
            self.assertEqual(sr.chip_line("wb", "wb_acc1", "0", q), "", q)

    def test_chip_question_beats_deal(self):
        """«Есть в наличии с чипом?» — про чип спросили прямо, запрет по сделке не применяем."""
        self.assertTrue(sr._ASK_DEAL.search("Есть в наличии с чипом?"))
        self.assertTrue(sr._ASK_CHIP.search("Есть в наличии с чипом?"))

    def test_product_question_passes_filter(self):
        for q in ("Подойдёт ли к HP M211?", "какой ресурс?"):
            self.assertFalse(sr._ASK_DEAL.search(q), q)


if __name__ == "__main__":
    unittest.main()
