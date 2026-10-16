# поток: ev
import unittest
from unittest.mock import patch

from ops import mp_news as news, wb_news_policy as policy


class WbNewsPolicyTests(unittest.TestCase):
    def test_requested_rubrics_are_unconditional_including_api_and_education(self):
        for category in ['Важно', 'Условия сотрудничества', 'API', 'АПИ',
                         'Персональная новость, Условия сотрудничества', 'Важно, Обучение']:
            with self.subTest(category=category):
                imp, reason = news.classify('Подкаст: новости недели', 'Обычный текст', 'wb', chat_type=category)
                self.assertEqual(imp, 'alert')
                self.assertTrue(reason.startswith('рубрика WB:'))

    def test_other_sc_is_excluded_even_if_tagged_important_or_api(self):
        self.assertEqual(news.classify('СЦ «Рязань» временно не принимает товары', '', 'wb',
                                       chat_type='Важно, API'), ('info', 'СЦ не Вешки'))
        self.assertEqual(news.classify('Открываем новый СЦ для FBS в Ленинградской области', '', 'wb',
                                       chat_type='Важно')[0], 'info')
        self.assertEqual(news.classify('СЦ «Вешки» временно не принимает товары', '', 'wb',
                                       chat_type='Важно')[0], 'alert')

    def test_changes_outside_rubrics_are_found_in_full_text(self):
        cases = [
            ('Обновления карточек', 'Справка. ' * 700 + 'Передача серийных номеров станет обязательной.'),
            ('Отгрузка в ПВЗ по модели FBS', 'Вес короба не должен превышать 25 кг.'),
            ('Новые возможности', 'Справка. ' * 700 + 'Запускаем новую услугу обработки товаров.'),
            ('Изменения инструмента', 'После окончания пробного периода сервис станет платным.'),
            ('Ваши товары', 'Справка. ' * 700 + 'Автоматически подключим услугу продвижения.'),
            ('FBS: обновили список предметов для автовозврата', ''),
            ('FBS: перенесите заказ в новую поставку', ''),
            ('Скоро изменения', 'Стоимость обработки изменится с 1 ноября.'),
            ('Упаковка', 'Смягчили требования к упаковке товаров.')]
        for title, body in cases:
            with self.subTest(title=title):
                self.assertEqual(news.classify(title, body, 'wb')[0], 'alert')

    def test_ui_news_and_invitation_are_not_selected_by_incidental_terms(self):
        for title, body in [
            ('Обновили фильтры на странице подменных артикулов',
             'Это обновление уже доступно независимо от наличия опций в конструкторе тарифов.'),
            ('FBW: добавили лист подбора', 'Лист поможет собрать поставки. Новые листы доступны всем.'),
            ('Приглашаем на эфир по FBS', 'Обсудим новые требования и стоимость услуг.'),
            ('Новые статьи платформы', 'Изменим требования к упаковке.'),
            ('Обновили дизайн отчёта', 'Виджет показывает стоимость ранее созданной поставки.')]:
            with self.subTest(title=title):
                self.assertIsNone(policy.reason(title, body))

    def test_collect_passes_real_category_names_into_classifier(self):
        item = {'id': 1, 'date': '2026-10-15T12:00:00Z', 'header': 'Новая информация',
                'content': 'Обычный текст', 'types': [{'id': 1, 'name': 'API'}]}
        with patch.object(news, 'wb_news', return_value=[item]), \
             patch.object(news.db, 'query', return_value=[]):
            rows = news.collect_wb(dry=True)
        self.assertEqual(rows[0]['importance'], 'alert')
        self.assertEqual(rows[0]['chat_type'], 'API')

    def test_ozon_is_not_affected_by_wb_category_policy(self):
        self.assertEqual(news.classify('Обычная информация', '', 'ozon', chat_type='Важно')[0], 'info')

    def test_wb_full_body_is_published_and_existing_key_is_reused(self):
        row = dict(platform='wb', account='wb_acc1', message_id='1', d='2026-10-15',
                   title='Новые требования', body='Полный текст. ' * 500, matched='новые требования',
                   digest=None, chat_id=None, chat_type='Важно', importance='alert')
        with patch.object(news.db, 'query', side_effect=[[row], [{'id': 123}]]), \
             patch.object(news.biz_diary, 'add', return_value=None) as add, \
             patch.object(news.db, 'execute') as execute:
            news.to_diary(platform='wb', message_ids=['1'])
            self.assertIn(row['body'], add.call_args.kwargs['details'])
            self.assertEqual(execute.call_args.args[1][0], 123)


if __name__ == '__main__':
    unittest.main()
