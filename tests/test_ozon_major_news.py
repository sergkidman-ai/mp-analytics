# поток: ev
from datetime import date, datetime, timezone
import unittest
from unittest.mock import patch

from ops import mp_news, ozon_major_news as major


class OzonMajorNewsTests(unittest.TestCase):
    def row(self, account='oz_acc1'):
        return dict(platform='ozon', account=account, chat_id=major.CHANNELS[account],
                    d=date(2026, 10, 14), title='FBO: откроем кросс-докинг в ПВЗ',
                    body='Обновление без изменения тарифов.', message_id='1')

    def test_only_verified_channels_qualify(self):
        self.assertTrue(major.is_major('ozon', 'oz_acc1', major.CHANNELS['oz_acc1']))
        self.assertTrue(major.is_major('ozon', 'oz_acc2', major.CHANNELS['oz_acc2']))
        self.assertFalse(major.is_major('ozon', 'oz_acc2', major.CHANNELS['oz_acc1']))
        self.assertFalse(major.is_major('ozon', 'oz_acc1', 'ordinary-operational-chat'))
        self.assertFalse(major.is_major('wb', 'oz_acc1', major.CHANNELS['oz_acc1']))
        self.assertFalse(major.is_major('ozon', 'unknown', None))

    def test_identical_publications_share_key_but_distinct_texts_do_not(self):
        self.assertEqual(major.publication_key(self.row()), major.publication_key(self.row('oz_acc2')))
        changed = self.row('oz_acc2')
        changed['body'] += ' Условия для другого кабинета.'
        self.assertNotEqual(major.publication_key(self.row()), major.publication_key(changed))

    def test_requested_sections_are_selected_without_operational_notifications(self):
        for section, channels in major.EXTRA_CHANNELS.items():
            for account, chat in channels.items():
                self.assertEqual(major.section_for('ozon', account, chat), section)
                self.assertTrue(major.is_selected('ozon', account, chat))
                self.assertFalse(major.is_selected('wb', account, chat))
        for chat_type in major.SELLER_API_TYPES:
            self.assertEqual(major.section_for('ozon', 'oz_acc1', 'api-chat', chat_type), 'seller-api')
        self.assertFalse(major.is_selected('ozon', 'oz_acc1', 'ordinary-operational-chat', 'UNSPECIFIED'))
        self.assertFalse(major.is_selected('ozon', 'unknown', 'api-chat', 'SELLER_API_UPDATES'))

    def test_account_specific_notifications_keep_separate_keys(self):
        rows = []
        for account in ('oz_acc1', 'oz_acc2'):
            row = self.row(account)
            row.update(chat_id='api-chat', chat_type='SELLER_API_NOTIFICATIONS')
            rows.append(row)
        self.assertNotEqual(major.publication_key(rows[0]), major.publication_key(rows[1]))
        for row in rows:
            self.assertIn("account = '" + row['account'] + "'", major.publication_where(row))
            row['chat_type'] = 'SELLER_API_UPDATES'
        self.assertEqual(major.publication_key(rows[0]), major.publication_key(rows[1]))

    def test_finance_notices_keep_account_attribution(self):
        rows = []
        for account in ('oz_acc1', 'oz_acc2'):
            row = self.row(account)
            row['chat_id'] = major.EXTRA_CHANNELS['finance'][account]
            rows.append(row)
            self.assertTrue(major.account_specific(row))
        self.assertNotEqual(major.publication_key(rows[0]), major.publication_key(rows[1]))

    def test_targeted_section_backfill_skips_other_platform_jobs(self):
        for section in ('seller-api', 'content', 'fbs', 'finance'):
            with self.subTest(section=section), \
                 patch.object(mp_news, 'to_diary', return_value=0) as publish, \
                 patch.object(mp_news, 'close_pauses') as pauses, \
                 patch.object(mp_news, 'annotate_damage') as annotate, \
                 patch.object(mp_news, 'tg') as tg:
                mp_news.main(['--diary-only', '--ozon-section', section, '--platform', 'ozon', '--quiet'])
                self.assertEqual(publish.call_args.kwargs['section_only'], section)
                pauses.assert_not_called()
                annotate.assert_not_called()
                tg.assert_not_called()

    def test_dry_preview_counts_duplicate_once_and_never_writes(self):
        with patch.object(major.db, 'query', return_value=[]), \
             patch.object(major.db, 'execute') as execute, \
             patch.object(major.biz_diary, 'add') as add:
            planned = set()
            self.assertTrue(major.publish(self.row(), dry=True, planned=planned))
            self.assertFalse(major.publish(self.row('oz_acc2'), dry=True, planned=planned))
            execute.assert_not_called()
            add.assert_not_called()

    def test_existing_event_is_linked_without_overwriting_manual_text(self):
        with patch.object(major.db, 'query', return_value=[{'id': 89}]), \
             patch.object(major.db, 'execute') as execute, \
             patch.object(major.biz_diary, 'add') as add:
            self.assertFalse(major.publish(self.row()))
            add.assert_not_called()
            self.assertEqual(execute.call_args.args[1][0], 89)
            self.assertIn('UPDATE mp_notices', execute.call_args.args[0])

    def test_concurrent_insert_reuses_event_instead_of_clearing_link(self):
        with patch.object(major.db, 'query', side_effect=[[], [{'id': 123}]]), \
             patch.object(major.db, 'execute') as execute, \
             patch.object(major.biz_diary, 'add', return_value=None):
            major.publish(self.row())
            self.assertEqual(execute.call_args.args[1][0], 123)

    def test_full_text_is_preserved_without_promoting_telegram_importance(self):
        text = '**Новый инструмент**\n' + 'Описание инструмента. ' * 400
        msg = {'user': {'type': 'NotificationUser'}, 'message_id': '1',
               'created_at': datetime.now(timezone.utc).isoformat(), 'data': [text]}
        chat = {'chat': {'chat_id': major.CHANNELS['oz_acc1'], 'chat_type': 'UNSPECIFIED'}}
        with patch.object(mp_news, 'seen_max', return_value={}), \
             patch.object(mp_news, 'chat_list', return_value=[chat]), \
             patch.object(mp_news, 'history', return_value=[msg]):
            rows = mp_news.collect('oz_acc1', dry=True)
        self.assertGreater(len(rows[0]['body']), 4000)
        self.assertEqual(rows[0]['body'], text)
        self.assertNotEqual(rows[0]['importance'], 'alert')

    def test_targeted_backfill_does_not_touch_wb_or_send_telegram(self):
        with patch.object(mp_news, 'to_diary', return_value=1) as publish, \
             patch.object(mp_news, 'close_pauses') as pauses, \
             patch.object(mp_news, 'annotate_damage') as annotate, \
             patch.object(mp_news, 'tg') as tg:
            mp_news.main(['--diary-only', '--major-updates-only', '--platform', 'ozon', '--quiet'])
            self.assertTrue(publish.call_args.kwargs['major_only'])
            pauses.assert_not_called()
            annotate.assert_not_called()
            tg.assert_not_called()


if __name__ == '__main__':
    unittest.main()
