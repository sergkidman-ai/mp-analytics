# поток: ret
import unittest
from unittest.mock import patch
from datetime import datetime
from returns_bot import render, orgs, daily_push, tg, wb_qr, wb_qr_followup
NOW=datetime(2026,10,29,14,2,tzinfo=wb_qr.MSK)
ROWS=[dict(platform='wb',account='wb_acc1',return_id='r1',stage='pickup',campaign=None,pvz_name=None,pvz_address='улица <Тест>, 1',order_number='order-fixture',first_seen=None)]
class ScheduleTests(unittest.TestCase):
    def test_morning_counts_zero_platforms_without_details(self):
        with patch.object(render,'fetch',return_value=(ROWS,{})):
            letters=render.daily_summaries('morning',NOW)
        self.assertEqual([o for o,t in letters],orgs.ORDER)
        self.assertIn('Ozon — <b>0</b>',letters[0][1])
        self.assertIn('Wildberries — <b>1</b>',letters[0][1])
        self.assertIn('Яндекс Маркет — <b>0</b>',letters[0][1])
        self.assertNotIn('Яндекс Маркет',letters[1][1])
        self.assertIn('Итого — 1',letters[0][1]);self.assertIn('Итого — 0',letters[1][1])
        for _,text in letters:
            self.assertIn('14:00 МСК',text);self.assertNotIn('улица',text);self.assertNotIn('fixture',text)
    def test_full_bold_escaped_address_and_empty_company(self):
        with patch.object(render,'fetch',return_value=(ROWS,{})):
            letters=render.daily_summaries('full',NOW)
        self.assertIn('<b>улица &lt;Тест&gt;, 1</b>',letters[0][1])
        self.assertIn('order-fixture',letters[0][1]);self.assertIn('Забирать нечего',letters[1][1])
        for _,text in letters:self.assertIn('14:02 МСК',text)
    def test_failed_refresh_not_claimed_fresh(self):
        with patch.object(render,'fetch',return_value=(ROWS,{})):
            letters=render.daily_summaries('full',NOW,incomplete=True)
        for _,text in letters:
            self.assertIn('Часть данных может быть устаревшей',text);self.assertNotIn('Данные обновлены',text)
    def test_morning_collects_but_never_fetches_codes(self):
        with patch.object(daily_push.collector,'gather',return_value=([('row',)],[])) as gather, patch.object(daily_push.collector,'store') as store, patch.object(render,'daily_summaries',return_value=[(o,'counts') for o in orgs.ORDER]), patch.object(daily_push,'barcode_photos') as oz,patch.object(wb_qr,'attachment') as qr,patch.object(tg,'send') as send,patch.object(tg,'send_document') as document:
            self.assertEqual(daily_push.run(['full','list'],report='morning'),0)
        gather.assert_called_once();store.assert_called_once();self.assertEqual(send.call_count,4)
        oz.assert_not_called();qr.assert_not_called();document.assert_not_called()
    def test_full_refresh_errors_passed_to_report(self):
        with patch.object(daily_push.collector,'gather',return_value=([],['wb: unavailable'])) as gather,patch.object(render,'daily_summaries',return_value=[(o,'warning') for o in orgs.ORDER]) as summary,patch.object(tg,'send') as send:
            self.assertEqual(daily_push.run(['fixture'],report='full',with_codes=False),0)
        gather.assert_called_once();self.assertTrue(summary.call_args.kwargs['incomplete']);self.assertEqual(send.call_count,2)
    def test_upload_followup_does_not_send_before_full_report(self):
        with patch.object(wb_qr,'attachment') as attachment, patch.object(tg,'codes_only_notify_ids',return_value=[]):
            for hour,minute in [(9,15),(13,59),(14,0),(14,14)]:self.assertEqual(wb_qr_followup.run(now=NOW.replace(hour=hour,minute=minute)),{})
        attachment.assert_not_called()
if __name__=='__main__':unittest.main()
