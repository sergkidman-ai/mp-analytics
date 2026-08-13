# поток: rev
"""Офлайн-тесты нормализации омоглифов и её эффекта на разбор фактов карточки.

Инцидент 6806 (13.08.2026): «оснащен c чипом без счетчика» с ЛАТИНСКОЙ c → чип не распознан.
"""
import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from core.text_norm import fix_homoglyphs, fix_deep          # noqa: E402
from reports.card_facts import _classify_chip                # noqa: E402


class TestHomoglyphs(unittest.TestCase):
    def test_lone_latin_letter_between_russian_words(self):
        self.assertEqual(fix_homoglyphs("оснащен c чипом"), "оснащен с чипом")

    def test_mixed_token(self):
        self.assertEqual(fix_homoglyphs("Cовместимый картридж"), "Совместимый картридж")
        self.assertEqual(fix_homoglyphs("оснaщен чипом"), "оснащен чипом")

    def test_latin_words_untouched(self):
        for s in ("Картридж W1510A для HP LJ Pro MFP 4103dw черный",
                  "Не идёт в аппарат OKI C822",
                  "принтер P 1102 подойдёт"):
            self.assertEqual(fix_homoglyphs(s), s)

    def test_non_russian_and_non_str(self):
        self.assertEqual(fix_homoglyphs("HP LaserJet Pro"), "HP LaserJet Pro")
        self.assertIsNone(fix_homoglyphs(None))
        self.assertEqual(fix_homoglyphs(""), "")

    def test_deep(self):
        self.assertEqual(fix_deep({"a": ["оснащен c чипом", 5], "b": None}),
                         {"a": ["оснащен с чипом", 5], "b": None})


class TestChipClassification(unittest.TestCase):
    def test_latin_c_still_recognised(self):
        self.assertEqual(_classify_chip("Картридж оснащен c чипом без счетчика"), "installed")
        self.assertEqual(_classify_chip("Картридж оснащен с чипом без счетчика"), "installed")

    def test_no_chip_and_unknown(self):
        self.assertEqual(_classify_chip("Картридж оснащен без чипа"), "none")
        self.assertIsNone(_classify_chip("Картридж чёрный, ресурс 3050 страниц"))


if __name__ == "__main__":
    unittest.main()
