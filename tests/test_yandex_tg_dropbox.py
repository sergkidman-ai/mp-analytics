import unittest

from ops.mp_news import classify
from ops.yandex_tg_dropbox import parse_posts


class YandexTelegramCollectorTests(unittest.TestCase):
    def test_parse_public_channel_card_and_full_text(self):
        html = '''<div class="tgme_widget_message_wrap">
          <div class="tgme_widget_message" data-post="market_marketplace/123">
            <div class="tgme_widget_message_text">🌩<br/><b>Обновляем тарифы</b>
              <a href="https://example.test">Стоимость станет ниже</a></div>
            <div><time datetime="2026-08-26T10:00:00+00:00"></time></div>
          </div></div>'''
        rows = parse_posts(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "123")
        self.assertIn("Стоимость станет ниже", rows[0]["text"])
        self.assertEqual(rows[0]["date"], "2026-08-26T10:00:00+00:00")

    def test_market_body_detects_change_with_neutral_headline(self):
        body = "С 1 сентября обновим договор и логику начисления тарифов."
        self.assertEqual(classify("🌩", body, platform="yandex"),
                         ("alert", "изменение тарифа/стоимости услуги (в тексте)"))

    def test_market_body_ignores_static_contract_reference(self):
        body = "ЭТрН нужна, если заключён договор на грузоперевозку."
        self.assertEqual(classify("Оформите электронную накладную", body, platform="yandex"),
                         ("info", ""))

    def test_market_body_ignores_fee_mention_without_change(self):
        body = "Пользоваться опцией можно без комиссии и переплат."
        self.assertEqual(classify("Покупайте товары для бизнеса сейчас", body,
                                  platform="yandex"), ("info", ""))

    def test_market_body_does_not_join_unrelated_sections(self):
        body = "Тарифы в файле для новых категорий.\n"
        body += "Заранее предупредим о переносе товаров."
        self.assertEqual(classify("Обновляем каталог", body, platform="yandex"),
                         ("info", ""))


if __name__ == "__main__":
    unittest.main()
