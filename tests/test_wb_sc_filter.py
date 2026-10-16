# поток: ev
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from ops import mp_news as news


class WbScFilterTests(unittest.TestCase):
    def test_unrelated_reception_pauses_do_not_become_alerts(self):
        for title in [
            'СЦ «Воронеж» и «Воронеж СГТ» временно не принимают товары, ожидаем возобновления работы',
            'СЦ «Рязань: Новосёлковская» временно не принимает товары, ожидаем возобновления работы',
            'СЦ\xa0«Чехов-1» возобновил работу',
            'Некоторые СЦ в Московской области временно не принимают товары']:
            with self.subTest(title=title):
                self.assertIs(news.wb_sc_relevance(title), False)
                self.assertEqual(news.classify(title, platform='wb'), ('info', 'СЦ не Вешки'))

    def test_veshki_is_relevant_with_nbsp_and_yo(self):
        for title in ['СЦ «Вешки» временно не принимает товары',
                      'СЦ\xa0«Вёшки» временно не\xa0принимает товары',
                      'СЦ «Воронеж» и «Вешки» временно не принимают товары']:
            with self.subTest(title=title):
                self.assertIs(news.wb_sc_relevance(title), True)
                self.assertEqual(news.classify(title, platform='wb')[0], 'alert')

    def test_redirect_to_veshki_does_not_make_another_sc_relevant(self):
        self.assertIs(news.wb_sc_relevance('СЦ «Воронеж» временно не принимает товары',
                                         'Перенаправьте поставки в СЦ «Вешки».'), False)

    def test_regional_headline_checks_affected_centres_in_body(self):
        title = 'Некоторые СЦ в Московской области временно не принимают товары'
        self.assertIs(news.wb_sc_relevance(title, 'СЦ «Вешки», СЦ «Чехов».'), True)
        self.assertIs(news.wb_sc_relevance(title, 'СЦ «Чехов» не принимает товары. Можно отгрузить в СЦ «Вешки».'), False)

    def test_general_services_tariffs_and_actual_warehouse_incidents_are_preserved(self):
        for title, body in [
            ('FBS: вам доступна услуга «Фулфилмент в СЦ»', ''),
            ('Новые тарифы для СЦ', ''),
            ('Работа склада «Тула» и СЦ «Тула КГТ+» приостановлена', 'Произошла нештатная ситуация, эвакуировали людей.')]:
            self.assertIsNone(news.wb_sc_relevance(title, body))
        self.assertEqual(news.classify('Новые тарифы для СЦ', platform='wb')[0], 'alert')
        self.assertEqual(news.classify('Склад «Котовск»: произошёл пожар', platform='wb')[0], 'alert')

    def test_filter_does_not_apply_to_other_platforms(self):
        title = 'СЦ «Воронеж» временно не принимает товары'
        self.assertEqual(news.classify(title, platform='ozon')[0], 'alert')

    def test_preclassified_irrelevant_wb_notice_is_not_published(self):
        row = dict(platform='wb', title='СЦ «Воронеж» временно не принимает товары', body='', importance='alert')
        with patch.object(news.db, 'query', return_value=[row]), \
             patch.object(news.biz_diary, 'add') as add:
            self.assertEqual(news.to_diary(dry=True, platform='wb'), 0)
            add.assert_not_called()

    def test_veshki_is_not_dropped_for_absence_of_stock_at_fbs_sc(self):
        pause = dict(platform='wb', title='СЦ «Вешки» временно не принимает товары', body='',
                     created_at=datetime.now(timezone.utc), event_id=None)
        with patch.object(news.db, 'query', side_effect=[[pause], []]), \
             patch.object(news, 'our_warehouses', return_value=set()), \
             patch.object(news, '_drop_pause') as drop:
            self.assertEqual(news.close_pauses(), (0, 0))
            drop.assert_not_called()

    def test_news_are_not_sent_to_telegram_even_without_quiet(self):
        fresh = [dict(account='wb_acc1', importance='alert', matched='склад/ЧП', body='', title=t)
                 for t in ['СЦ «Воронеж» временно не принимает товары',
                           'СЦ «Рязань» временно не принимает товары',
                           'СЦ «Вешки» временно не принимает товары', 'Изменение тарифов']]
        with patch.object(news, 'collect_wb', return_value=fresh), \
             patch.object(news, 'close_pauses'), \
             patch.object(news, 'to_diary', return_value=0), \
             patch.object(news, 'annotate_damage'), \
             patch.object(news, 'tg', return_value='mocked') as tg:
            news.main(['--platform', 'wb'])
            tg.assert_not_called()


if __name__ == '__main__':
    unittest.main()
