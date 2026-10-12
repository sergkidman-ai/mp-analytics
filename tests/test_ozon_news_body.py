"""Body rules for Ozon news that change seller economics."""

import unittest
from unittest.mock import patch

from ops import mp_news

classify = mp_news.classify


class OzonNewsBodyTests(unittest.TestCase):
    def test_fbs_change_after_fbo_intro(self):
        body = (
            "**Новые акции для FBO-продавцов**\n"
            + "Условия FBO. " * 70
            + "\n**№3. Бонусы на продвижение для FBS-новинок** "
            + "С 31 августа будем начислять бонусы на продвижение новых карточек."
        )
        self.assertEqual(classify("Новые акции для FBO-продавцов", body, "ozon")[0], "alert")

    def test_actual_bonus_and_fbs_money_changes(self):
        cases = [
            ("Начислили вам бонусы", "Начислили вам 26492.00 бонусов на продвижение"),
            ("Отсрочка выплат", "Отсрочка выплат. Скидка на тариф за продажу станет меньше."),
            ("Об индексе ошибок", "Штрафы за просрочки и отмены вернутся."),
            ("Скоро изменения", "С 6 октября начнём отменять рек. слот и скидку по нему."),
            ("Обновили цены", "Ваша скидка с продвижением теперь суммируется со скидками от Ozon."),
            ("Объявленная стоимость", "Объявленная стоимость товара повлияет на возмещения."),
            ("Меры поддержки", "С 30 августа просто снизим тариф за продажу на 3 п.п. без условий."),
        ]
        for title, body in cases:
            with self.subTest(title=title):
                self.assertEqual(classify(title, body, "ozon")[0], "alert")

    def test_routine_notice_and_fbo_only_are_not_alerts(self):
        self.assertEqual(classify(
            "У вас есть возвраты в точке выдачи",
            "У вас есть возвраты. Тариф хранения приведён для справки.", "ozon"
        )[0], "info")
        self.assertEqual(classify(
            "Бонус для fashion-продавцов",
            "Бонус для fashion-продавцов. " + "Условия FBO. " * 45
            + "Также объявленная стоимость влияет на возмещения.", "ozon"
        )[0], "info")
        self.assertEqual(classify(
            "Об индексе ошибок", "Штрафы за просрочки и отмены вернутся.", "wb"
        )[0], "info")

    def test_reclassify_can_limit_to_ozon(self):
        with patch.object(mp_news.db, "query", side_effect=[[], []]) as query:
            mp_news.reclassify(platform="ozon")
        self.assertEqual(query.call_count, 2)
        for call in query.call_args_list:
            self.assertIn("platform = %s", call.args[0])
            self.assertEqual(call.args[1], ("ozon",))


if __name__ == "__main__":
    unittest.main()
