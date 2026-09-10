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


if __name__ == "__main__":
    unittest.main()
