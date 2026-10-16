# поток: ev
from datetime import date, datetime, timezone
import unittest
from unittest.mock import patch

from ops import mp_news, ozon_promotion_news as promo


class OzonPromotionNewsTests(unittest.TestCase):
    def row(self, account='oz_acc1', title='Подключили вам «Сбор первых отзывов»', body='Текст услуги'):
        return dict(platform='ozon', account=account, chat_id=promo.CHANNELS[account],
                    chat_type='UNSPECIFIED', d=date(2026, 8, 31), title=title, body=body,
                    message_id='1', matched='', digest=None)

    def test_only_verified_promotion_channels(self):
        self.assertTrue(promo.is_channel('ozon', 'oz_acc1', promo.CHANNELS['oz_acc1']))
        self.assertFalse(promo.is_channel('ozon', 'oz_acc2', promo.CHANNELS['oz_acc1']))
        self.assertFalse(promo.is_channel('wb', 'oz_acc1', promo.CHANNELS['oz_acc1']))
        self.assertFalse(promo.is_channel('ozon', 'unknown', None))

    def test_brand_enrolment_is_selected_from_separate_channel_for_both_accounts(self):
        title = 'У вас появились бренды с продвижением'
        for account in promo.CHANNELS:
            self.assertTrue(promo.is_source('ozon', account, 'separate-brand-channel', title))
        self.assertFalse(promo.is_source('wb', 'oz_acc1', 'separate-brand-channel', title))
        self.assertFalse(promo.is_source('ozon', 'unknown', 'separate-brand-channel', title))
        self.assertFalse(promo.is_source('ozon', 'oz_acc1', 'separate-brand-channel', 'Как зарегистрировать бренд'))
        choice = promo.analyze(title, 'Через 3 дня начнём брать плату с продаж.')
        self.assertEqual(choice.reason, 'brand_enabled')
        self.assertTrue(choice.personal)
        self.assertIn('Бренды', choice.action)

    def test_brand_enrolment_flows_into_diary_without_promoting_entire_channel(self):
        row = self.row(title='У вас появились бренды с продвижением')
        row['chat_id'] = 'separate-brand-channel'
        row['importance'] = 'info'
        with patch.object(mp_news.db, 'query', return_value=[row]), \
             patch.object(promo, 'publish', return_value=True) as publish:
            self.assertEqual(mp_news.to_diary(dry=True, platform='ozon', section_only='promotion'), 1)
            self.assertEqual(publish.call_args.args[1].reason, 'brand_enabled')

    def test_june_announcement_reads_full_body_and_does_not_claim_personal_activation(self):
        body = 'Описание. ' * 600 + ('Запустим «Сбор первых отзывов» 15 июля 2026 года. '
               'В этот же день добавим в инструмент некоторое количество товаров, у которых мало или нет отзывов. '
               'Обязательно сообщим продавцам, чьи товары автоматически попадут в «Сбор первых отзывов».')
        choice = promo.analyze('Получайте видеоотзывы на 5 звёзд от топ-авторов', body)
        self.assertEqual(choice.reason, 'planned')
        self.assertFalse(choice.personal)

    def test_july_activation_is_personal(self):
        choice = promo.analyze('Подключили вам «Сбор первых отзывов»', '')
        self.assertEqual(choice.reason, 'enabled')
        self.assertTrue(choice.personal)

    def test_weekly_campaign_announcement_and_actual_launch_are_distinct(self):
        general = promo.analyze('Бонусы на продвижение для FBS-новинок',
                                'Каждую неделю будем начислять бонусы и создавать под них кампании в «Оплате за клик».')
        actual = promo.analyze('Начислили вам 4421.00 бонусов на продвижение и запустили кампанию', '')
        self.assertEqual(general.reason, 'planned')
        self.assertFalse(general.personal)
        self.assertEqual(actual.reason, 'enabled')
        self.assertTrue(actual.personal)

    def test_ruble_transition_is_a_warning_not_evidence_of_debit(self):
        choice = promo.analyze('Вы изменили бонусную кампанию — продолжим продвижение',
                               'Поэтому теперь, когда бонусы закончатся, начнём автоматически списывать рубли.')
        self.assertEqual(choice.reason, 'paid_transition')
        self.assertTrue(choice.personal)
        self.assertIn('Предупреждение', choice.label)

    def test_paused_campaign_does_not_get_classified_as_ruble_transition(self):
        choice = promo.analyze('Бонусы на продвижение закончились — проверьте кампанию',
                               'Кампания перешла в статус «Приостановлено». Можете продолжить за рубли.')
        self.assertEqual(choice.reason, 'bonus_ended')

    def test_automatic_bid_increase_and_review_price_change_are_selected(self):
        for title, body in [
            ('Про инструменты продвижения: обновим ставки с 20 октября', 'Ниже — автоматически увеличим её до нового минимума.'),
            ('С 26 октября «Сбор первых отзывов» станет выгоднее', 'Как изменится плата за «Сбор первых отзывов»')]:
            with self.subTest(title=title):
                self.assertEqual(promo.analyze(title, body).reason, 'terms')

    def test_new_paid_feature_is_not_reported_as_enrolment(self):
        choice = promo.analyze('Отправляйте баллы клиентам в чате',
                               'Анонсировали новую возможность. В момент отправки баллов спишем стоимость услуги.')
        self.assertEqual(choice.reason, 'new_service')
        self.assertFalse(choice.personal)

    def test_ordinary_marketing_and_optional_automation_are_excluded(self):
        for title, body in [
            ('Дайджест рекламной платформы', 'Автоматизировали подбор аудитории: система сама найдёт покупателей.'),
            ('Performance API: получайте отчёты', 'Автоматически подготовим отчёт.'),
            ('Новый канал продаж — Шопинг в Т-Банке', 'Оставьте заявку. Откройте доступ к интеграции. Всё запустится автоматически.'),
            ('Попробуйте «Целевой расход»', 'Запустите кампанию, а мы вернём бонусы.'),
            ('Подключите инструмент', 'После вашей заявки автоматически подключим услугу.'),
            ('Участвуйте в Марафоне', 'Подключите интеграцию и смотрите вебинары.')]:
            with self.subTest(title=title):
                self.assertIsNone(promo.analyze(title, body))

    def test_personal_notifications_keep_account_but_public_ones_share_key(self):
        with patch.object(promo, 'publish_scoped') as publish:
            personal = promo.analyze(self.row()['title'], '')
            for account in promo.CHANNELS:
                promo.publish(self.row(account), personal, dry=True)
            a, b = publish.call_args_list
            self.assertNotEqual(a.args[1], b.args[1])
            self.assertEqual(a.args[3], 'oz_acc1')
            general = promo.analyze('Обновим ставки', '')
            for account in promo.CHANNELS:
                promo.publish(self.row(account, title='Обновим ставки'), general, dry=True)
            a, b = publish.call_args_list[-2:]
            self.assertEqual(a.args[1], b.args[1])
            self.assertIsNone(a.args[3])

    def test_diary_filters_entire_promotion_channel_before_publishing(self):
        selected, noise = self.row(), self.row(title='Вебинар', body='Приходите на встречу')
        with patch.object(mp_news.db, 'query', return_value=[selected, noise]), \
             patch.object(promo, 'publish', return_value=True) as publish:
            self.assertEqual(mp_news.to_diary(dry=True, platform='ozon', section_only='promotion'), 1)
            publish.assert_called_once()
            self.assertEqual(publish.call_args.args[0], selected)

    def test_reclassify_preserves_selected_non_alert_promotion_event(self):
        row = self.row(title='Обновим ставки', body='Описание новых ставок')
        row['event_id'] = 123
        with patch.object(mp_news.db, 'query', side_effect=[[], [row]]), \
             patch.object(mp_news.db, 'execute') as execute, \
             patch.object(mp_news.biz_diary, 'delete') as delete:
            mp_news.reclassify(platform='ozon')
            execute.assert_not_called()
            delete.assert_not_called()

    def test_full_promotion_text_is_kept_without_telegram_promotion(self):
        text = '**Нейтральный заголовок**\n' + 'Описание. ' * 600
        msg = {'user': {'type': 'NotificationUser'}, 'message_id': '1',
               'created_at': datetime.now(timezone.utc).isoformat(), 'data': [text]}
        chat = {'chat': {'chat_id': promo.CHANNELS['oz_acc1'], 'chat_type': 'UNSPECIFIED'}}
        with patch.object(mp_news, 'seen_max', return_value={}), \
             patch.object(mp_news, 'chat_list', return_value=[chat]), \
             patch.object(mp_news, 'history', return_value=[msg]):
            rows = mp_news.collect('oz_acc1', dry=True)
        self.assertEqual(rows[0]['body'], text)
        self.assertEqual(rows[0]['importance'], 'info')

    def test_targeted_promotion_run_skips_other_jobs_and_telegram(self):
        with patch.object(mp_news, 'to_diary', return_value=0), \
             patch.object(mp_news, 'close_pauses') as pauses, \
             patch.object(mp_news, 'annotate_damage') as annotate, \
             patch.object(mp_news, 'tg') as tg:
            mp_news.main(['--diary-only', '--ozon-section', 'promotion', '--platform', 'ozon', '--quiet'])
            pauses.assert_not_called()
            annotate.assert_not_called()
            tg.assert_not_called()


if __name__ == '__main__':
    unittest.main()
