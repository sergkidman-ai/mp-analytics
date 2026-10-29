# поток: ret
"""Code recipients receive neither order reports nor weekend automatic deliveries."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import patch
from returns_bot import tg, bot, daily_push, weekly_received, codes_push, wb_qr, wb_qr_followup

FULL='fixture-full';LIST='fixture-list';CODES='fixture-codes'
NOW=datetime(2026,10,29,10,tzinfo=wb_qr.MSK)
ATTACHMENT=('fixture.png',b'fixture','Ozon - fixture','image/png')

class CodesOnlyTests(unittest.TestCase):
    def setUp(self):
        env=patch.dict(os.environ,{'TG_RETURNS_ALLOWED_IDS':FULL,'TG_RETURNS_NOTIFY_ID':FULL,
            'TG_RETURNS_LIST_ONLY_IDS':LIST,'TG_RETURNS_CODES_ONLY_IDS':CODES,
            'TG_RETURNS_NO_SATURDAY_IDS':LIST})
        env.start();self.addCleanup(env.stop)

    def test_role_does_not_join_daily_or_weekly_recipients(self):
        self.assertEqual(tg.allowed_ids(),{FULL,LIST,CODES})
        self.assertEqual(tg.notify_ids(),[FULL])
        self.assertEqual(tg.daily_notify_ids(NOW),[FULL,LIST])
        self.assertEqual(tg.codes_only_notify_ids(NOW),[CODES])
        with patch.dict(os.environ,{'TG_RETURNS_NOTIFY_ID':FULL+','+CODES}):
            self.assertEqual(tg.notify_ids(),[FULL])
        self.assertTrue(tg.can_receive_codes(CODES));self.assertFalse(tg.can_receive_lists(CODES))

    def test_weekend_and_moscow_boundary(self):
        self.assertEqual(tg.codes_only_notify_ids(NOW.replace(day=30)),[CODES])
        self.assertEqual(tg.codes_only_notify_ids(NOW.replace(day=31)),[])
        self.assertEqual(tg.codes_only_notify_ids(NOW.replace(month=11,day=1)),[])
        self.assertEqual(tg.codes_only_notify_ids(datetime(2026,10,30,22,tzinfo=timezone.utc)),[])

    def test_commands_and_old_refresh_callback_cannot_show_orders(self):
        with patch.object(tg,'send') as text,patch.object(bot,'send_summaries') as summaries,patch.object(bot,'refresh') as refresh,patch.object(tg,'answer_callback'):
            for cmd in ('/start','/help','/vozvraty','/pvz','/obnovit'):
                bot.handle(CODES,cmd)
            bot.handle_callback(CODES,{'id':'fixture','data':bot.CB_REFRESH})
        summaries.assert_not_called();refresh.assert_not_called()
        for call in text.call_args_list:self.assertEqual(call.args[1],bot.HELP_CODES_ONLY)

    def test_daily_and_weekly_explicit_target_is_filtered(self):
        with patch.object(daily_push.collector,'gather') as gather,patch.object(tg,'send') as send,patch.object(weekly_received,'build',return_value='fixture'):
            self.assertEqual(daily_push.run([CODES],report='full'),1)
            self.assertEqual(weekly_received.main(['--to',CODES]),1)
        gather.assert_not_called();send.assert_not_called()

    def test_ten_oclock_sends_both_platforms_only_to_code_user(self):
        with patch.object(codes_push.bot,'barcode_files',return_value=[ATTACHMENT]) as barcode,patch.object(codes_push.ozon,'CRED_ENV',{'oz_acc1':(), 'oz_acc2':()}),patch.object(codes_push,'send_ozon',return_value='sent') as ozsend,patch.object(wb_qr,'enabled_accounts',return_value=('wb_acc1','wb_acc2')),patch.object(wb_qr,'attachment',return_value=ATTACHMENT),patch.object(codes_push.wb_qr_delivery,'send',return_value='sent') as wbsend,patch.object(tg,'send') as text:
            self.assertEqual(codes_push.run(now=NOW),{'sent':4})
        self.assertEqual(barcode.call_count,2);self.assertEqual(ozsend.call_count,2)
        for call in ozsend.call_args_list+wbsend.call_args_list:self.assertEqual(call.args[1],CODES)
        text.assert_not_called()

    def test_no_fetch_or_delivery_before_ten_or_on_weekends(self):
        with patch.object(codes_push.bot,'barcode_files') as barcode,patch.object(wb_qr,'attachment') as qr:
            for dt in [NOW.replace(hour=9,minute=59),NOW.replace(day=31),NOW.replace(day=1,month=11)]:
                self.assertEqual(codes_push.run(now=dt),{})
        barcode.assert_not_called();qr.assert_not_called()

    def test_dry_run_never_fetches_or_sends_real_codes(self):
        with patch.object(codes_push.bot,'barcode_files') as barcode,patch.object(wb_qr,'attachment') as qr,patch.object(tg,'send_document') as send:
            result=codes_push.run(dry_run=True,now=NOW)
        self.assertEqual(result['recipients'],1);barcode.assert_not_called();qr.assert_not_called();send.assert_not_called()

    def test_late_qr_at_ten_fifteen_only_code_user_and_no_weekend(self):
        with patch.object(wb_qr,'enabled_accounts',return_value=('wb_acc2',)),patch.object(wb_qr,'attachment',return_value=ATTACHMENT) as qr,patch.object(wb_qr_followup.wb_qr_delivery,'send',return_value='sent') as send:
            self.assertEqual(wb_qr_followup.run(now=NOW.replace(minute=14)),{})
            self.assertEqual(wb_qr_followup.run(now=NOW.replace(minute=15)),{'sent':1})
            self.assertEqual(send.call_args.args[1],CODES)
            send.reset_mock()
            self.assertEqual(wb_qr_followup.run(now=NOW.replace(day=31,hour=10,minute=15)),{})
            send.assert_not_called()
            self.assertEqual(wb_qr_followup.run(now=NOW.replace(hour=14,minute=15)),{'sent':2})
            self.assertEqual({c.args[1] for c in send.call_args_list},{FULL,CODES})

    def test_missing_wb_does_not_generate_text_report(self):
        with patch.object(codes_push.ozon,'CRED_ENV',{}),patch.object(wb_qr,'enabled_accounts',return_value=('wb_acc2',)),patch.object(wb_qr,'attachment',return_value=None),patch.object(tg,'send') as text:
            self.assertEqual(codes_push.run(now=NOW),{'missing_wb':1})
        text.assert_not_called()

    def test_ozon_receipts_skip_confirmed_and_uncertain(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(codes_push,'DIRECTORY',Path(directory)),patch.object(wb_qr,'now_msk',return_value=NOW),patch.object(tg,'send_document',return_value={'message_id':123}) as send:
            self.assertEqual(codes_push.send_ozon('oz_acc1',CODES,ATTACHMENT,NOW),'sent')
            self.assertEqual(codes_push.send_ozon('oz_acc1',CODES,ATTACHMENT,NOW),'skipped')
            send.assert_called_once()
            send.side_effect=RuntimeError('transport unavailable')
            self.assertEqual(codes_push.send_ozon('oz_acc2',CODES,ATTACHMENT,NOW),'uncertain')
            self.assertEqual(codes_push.send_ozon('oz_acc2',CODES,ATTACHMENT,NOW),'uncertain')
            self.assertEqual(send.call_count,2)

    def test_ozon_rejected_is_retryable_and_wrong_role_cannot_send(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(codes_push,'DIRECTORY',Path(directory)),patch.object(wb_qr,'now_msk',return_value=NOW),patch.object(tg,'send_document',side_effect=tg.Rejected('rejected')) as send:
            self.assertEqual(codes_push.send_ozon('oz_acc1',CODES,ATTACHMENT,NOW),'failed')
            send.side_effect=None;send.return_value={'message_id':124}
            self.assertEqual(codes_push.send_ozon('oz_acc1',CODES,ATTACHMENT,NOW),'sent')
            self.assertEqual(codes_push.send_ozon('oz_acc1',LIST,ATTACHMENT,NOW),'skipped')
            self.assertEqual(send.call_count,2)

if __name__=='__main__':unittest.main()
