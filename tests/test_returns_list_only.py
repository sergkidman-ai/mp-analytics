# поток: ret
"""A daily list recipient must not receive pickup codes through any bot route."""
import os
import unittest
from datetime import datetime
from unittest.mock import patch
from returns_bot import tg,bot,daily_push,weekly_received,wb_qr,wb_qr_followup,wb_qr_delivery

FULL='fixture-full'
LIST='fixture-list'


class ListOnlyTests(unittest.TestCase):
    def setUp(self):
        p=patch.dict(os.environ,{'TG_RETURNS_ALLOWED_IDS':FULL,'TG_RETURNS_NOTIFY_ID':FULL,'TG_RETURNS_LIST_ONLY_IDS':LIST,'TG_RETURNS_CODES_ONLY_IDS':''})
        p.start();self.addCleanup(p.stop)

    def test_daily_recipient_added_without_weekly_or_code_notifications(self):
        self.assertEqual(tg.allowed_ids(),{FULL,LIST})
        self.assertEqual(tg.notify_ids(),[FULL])
        self.assertEqual(tg.daily_notify_ids(),[FULL,LIST])
        self.assertFalse(tg.can_receive_codes(LIST));self.assertTrue(tg.can_receive_codes(FULL))

    def test_restriction_overrides_accidental_full_notify_entry(self):
        with patch.dict(os.environ,{'TG_RETURNS_NOTIFY_ID':FULL+','+LIST}):
            self.assertEqual(tg.notify_ids(),[FULL])
            self.assertEqual(tg.daily_notify_ids(),[FULL,LIST])
        with patch.dict(os.environ,{'TG_RETURNS_NOTIFY_ID':''}):
            self.assertEqual(tg.notify_ids(),[FULL])

    def test_code_command_does_not_fetch_or_send_barcode(self):
        with patch.object(bot,'barcode_files') as code,patch.object(tg,'send') as text,patch.object(tg,'send_document') as document:
            bot.handle(LIST,'/shtrihkod@ds_vozvrat_bot')
        code.assert_not_called();document.assert_not_called();text.assert_called_once()

    def test_help_and_unknown_command_do_not_advertise_codes(self):
        with patch.object(tg,'send') as send:
            for command in ['/start','/help','/unknown','']:
                bot.handle(LIST,command)
        for call in send.call_args_list:self.assertNotIn('/shtrihkod',call.args[1])

    def test_media_and_forwarding_blocked_at_transport_before_network(self):
        with patch.object(tg.requests,'post') as network:
            for method in ['sendDocument','sendPhoto','sendMediaGroup','copyMessage','forwardMessage']:
                with self.subTest(method=method),self.assertRaises(PermissionError):
                    tg._api(method,{'chat_id':LIST})
        network.assert_not_called()

    def test_list_summary_and_refresh_button_still_available(self):
        with patch.object(bot.render,'summaries',return_value=[('Дисквэр','list text')]),patch.object(tg,'send') as text,patch.object(tg,'send_document') as document:
            bot.handle(LIST,'/vozvraty')
        self.assertEqual(text.call_args.args,(LIST,'list text'))
        self.assertEqual(text.call_args.kwargs['reply_markup'],bot.KB_REFRESH)
        document.assert_not_called()

    def test_mixed_daily_send_uses_clean_summaries_and_only_full_user_gets_attachments(self):
        qr=('wb.png',b'fixture','QR','image/png')
        oz=('ozon.png',b'fixture','barcode','image/png')
        with patch.object(daily_push.render,'summaries',return_value=[('Дисквэр','daily list')]), \
             patch.object(daily_push,'barcode_photos',return_value=[oz]), \
             patch.object(wb_qr,'enabled_accounts',return_value=('wb_acc2',)), \
             patch.object(wb_qr,'now_msk',return_value=datetime(2026,10,29,10,tzinfo=wb_qr.MSK)), \
             patch.object(wb_qr,'attachment',return_value=qr), \
             patch.object(tg,'send') as text,patch.object(tg,'send_document') as document, \
             patch.object(wb_qr_delivery,'send',return_value='sent') as wb:
            self.assertEqual(daily_push.run([FULL,LIST],skip_collect=True),0)
        self.assertEqual([c.args[0] for c in text.call_args_list],[FULL,LIST])
        self.assertEqual([c.args[0] for c in document.call_args_list],[FULL])
        self.assertEqual(wb.call_args.args[1],FULL)

    def test_missing_qr_warning_is_not_shown_to_list_only_user(self):
        with patch.object(daily_push.render,'summaries',return_value=[('Дисквэр','daily list')]), \
             patch.object(daily_push,'barcode_photos',return_value=[]), \
             patch.object(wb_qr,'enabled_accounts',return_value=('wb_acc2',)), \
             patch.object(wb_qr,'attachment',return_value=None),patch.object(tg,'send') as text:
            daily_push.run([FULL,LIST],skip_collect=True)
        full=next(c.args[1] for c in text.call_args_list if c.args[0]==FULL)
        limited=next(c.args[1] for c in text.call_args_list if c.args[0]==LIST)
        self.assertIn('Свежий QR',full);self.assertEqual(limited,'daily list')

    def test_only_list_targets_never_fetch_code_images(self):
        with patch.object(daily_push.render,'summaries',return_value=[('Дисквэр','daily list')]), \
             patch.object(daily_push,'barcode_photos') as barcode,patch.object(wb_qr,'attachment') as qr, \
             patch.object(tg,'send') as text,patch.object(tg,'send_document') as document:
            self.assertEqual(daily_push.run([LIST],skip_collect=True),0)
        barcode.assert_not_called();qr.assert_not_called();document.assert_not_called();text.assert_called_once()

    def test_late_qr_delivery_filters_even_explicit_notify_list(self):
        with patch.object(tg,'notify_ids',return_value=[FULL,LIST]), \
             patch.object(wb_qr,'enabled_accounts',return_value=('wb_acc2',)), \
             patch.object(wb_qr,'attachment',return_value=('fixture',b'fixture','fixture','image/png')), \
             patch.object(wb_qr_delivery,'send',return_value='sent') as send:
            result=wb_qr_followup.run(now=datetime(2026,10,29,15,tzinfo=wb_qr.MSK))
        self.assertEqual(result,{'sent':1});send.assert_called_once();self.assertEqual(send.call_args.args[1],FULL)

    def test_direct_qr_ledger_send_cannot_create_intent_for_restricted_user(self):
        with patch.object(wb_qr_delivery,'locked') as lock,patch.object(tg,'send_document') as send:
            self.assertEqual(wb_qr_delivery.send('wb_acc2',LIST,('fixture',b'fixture','fixture','image/png')),'skipped')
        lock.assert_not_called();send.assert_not_called()

    def test_weekly_report_is_not_sent_even_with_explicit_target(self):
        with patch.object(weekly_received,'build',return_value='weekly report'),patch.object(tg,'send') as send:
            self.assertEqual(weekly_received.main(['--to',LIST]),1)
        send.assert_not_called()


if __name__=='__main__':unittest.main()
