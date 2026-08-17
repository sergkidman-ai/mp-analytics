# поток: rev
"""Offline-тесты запрета на закрытые данные в промптах внешних моделей.

Ни один тест не ходит в сеть, в БД и в LLM. Проверяем ровно правило 13.08.2026: наружу уходят
только опубликованные данные витрин + текст покупателя; складские пометки МС и финансовая/
закупочная фактура вырезаются на СБОРКЕ промпта.
"""

import sys
import pathlib
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from reports.prompt_privacy import (          # noqa: E402
    PrivateDataInPrompt, public_name, assert_public_facts, scrub_finance)


class PublicName(unittest.TestCase):
    def test_c822_incident(self):
        """Реальное имя МС из инцидента 12.08.2026 — пометки не должны уйти модели."""
        got = public_name("*ВНИМАНИЕ* Картридж для OKI C831/С841 44844408 30K DRUM Black "
                          "White Box (Совместимый) Не идёт в аппарат C822")
        self.assertNotIn("ВНИМАНИЕ", got)
        self.assertNotIn("White Box", got)
        self.assertNotIn("C822", got)
        self.assertIn("44844408", got)          # публичная фактура остаётся
        self.assertIn("OKI C831", got)

    def test_ds_mark(self):
        self.assertNotIn(" DS ", " " + public_name("Картриджи DS KC-36IP для Canon CP510") + " ")

    def test_model_code_with_ds_survives(self):
        self.assertIn("DS-620", public_name("Картридж для Brother DS-620"))

    def test_dimensions_with_asterisks_survive(self):
        self.assertIn("54*86", public_name("Картриджи KC-36IP (бумага 54*86мм х 36л.)"))

    def test_clean_name_untouched(self):
        n = "Картридж лазерный CF259A для HP LaserJet Pro M304/M404 (3000 стр.)"
        self.assertEqual(public_name(n), n)


class Finance(unittest.TestCase):
    def test_sentence_with_cogs_dropped(self):
        out = scrub_finance("Ресурс 3000 стр. Закупочная цена 480 руб. Цвет чёрный.", "тест")
        self.assertNotIn("480", out)
        self.assertIn("Ресурс 3000", out)
        self.assertIn("Цвет чёрный", out)

    def test_bullet_list_survives(self):
        out = scrub_finance("- ресурс, стр: 3000\n- поставщик: ООО Ромашка\n- цвет: чёрный", "тест")
        self.assertNotIn("Ромашка", out)
        self.assertEqual(out.count("\n"), 1)      # вырезан пункт целиком, пустой маркер не остался

    def test_public_price_question_untouched(self):
        """Слово «цена» само по себе не запрещено — витринная цена опубликована."""
        self.assertIn("цена", scrub_finance("Цена указана за 1 шт, цена с НДС.", "тест"))


class Facts(unittest.TestCase):
    def test_forbidden_key_raises(self):
        with self.assertRaises(PrivateDataInPrompt):
            assert_public_facts({"code": "CF259A", "buyPrice": 48000}, "CARD_DATA")

    def test_stock_key_raises(self):
        with self.assertRaises(PrivateDataInPrompt):
            assert_public_facts({"models": ["M404"], "stock": 12}, "CARD_DATA")

    def test_card_facts_shape_ok(self):
        assert_public_facts({"name": "Картридж", "article": "0157", "code": "CF259A", "kind": "тонер",
                             "chip": "installed", "resource": "3000", "color": "чёрный",
                             "models": ["M404"], "annot": "описание", "weight_pkg": "0.9 кг"})


if __name__ == "__main__":
    unittest.main()
