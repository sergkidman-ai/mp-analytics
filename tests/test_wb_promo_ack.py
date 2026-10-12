# поток: ev
import os
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, Mock
import psycopg2
from psycopg2 import sql
from ops import wb_promo_ack as ack
from ops import wb_promo_watch as watch
from ops.prc_promo_callbacks import process_updates


class PureTests(unittest.TestCase):
    def test_dedup_different_ids_accounts_and_auto_suffix(self):
        self.assertEqual(ack.group_key('Новая акция (автоматические скидки)','2026-10-13','2026-10-20'),
                         ack.group_key('новая  акция','2026-10-13','2026-10-20'))
        self.assertNotEqual(ack.group_key('акция','2026-10-13','2026-10-20'),
                            ack.group_key('акция','2026-10-14','2026-10-20'))

    def test_dates_use_moscow(self):
        self.assertEqual(str(ack.local_date('2026-10-12T21:00:00Z')),'2026-10-13')

    def test_reminder_after_start_does_not_say_tomorrow(self):
        start=datetime.now(ack.MSK).date()-timedelta(days=3)
        self.assertIn('Напоминание',ack.notification(dict(starts_on=start,ends_on=None,
            accounts=['wb_acc1'],name='Акция')))

    def test_telegram_transport_error_does_not_expose_token(self):
        with patch.dict(os.environ,{'TG_PRC_BOT_TOKEN':'secret'}), \
             patch.object(ack.requests,'post',side_effect=ack.requests.RequestException('URL contains secret')):
            with self.assertRaisesRegex(RuntimeError,'transport failure') as error:
                ack.api('sendMessage',{})
        self.assertNotIn('secret',str(error.exception))

    def test_http_200_api_rejection_is_failure(self):
        response=Mock(ok=True,status_code=200)
        response.json.return_value={'ok':False,'error_code':400}
        with patch.dict(os.environ,{'TG_PRC_BOT_TOKEN':'secret'}),patch.object(ack.requests,'post',return_value=response):
            with self.assertRaises(RuntimeError):ack.api('sendMessage',{})


@unittest.skipUnless(os.environ.get('WB_PROMO_ACK_DB_TEST')=='1','temporary-schema PostgreSQL integration')
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conn=watch.db();cls.schema='test_promo_ack_'+uuid.uuid4().hex
        with cls.conn.cursor() as cur:
            cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(cls.schema)))
            cur.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(cls.schema)))
            root=Path(__file__).resolve().parents[1]
            cur.execute((root/'migrations/511_wb_promo_notice.sql').read_text())
            cur.execute((root/'migrations/514_wb_promo_ack.sql').read_text())
        cls.conn.commit()

    @classmethod
    def tearDownClass(cls):
        cls.conn.rollback()
        with cls.conn.cursor() as cur:
            cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(cls.schema)))
        cls.conn.commit();cls.conn.close()

    def setUp(self):
        with self.conn.cursor() as cur:
            cur.execute('TRUNCATE wb_promo_task,wb_promo_notice RESTART IDENTITY')
            cur.execute('UPDATE prc_promo_callback_cursor SET next_update_id=0')
        self.conn.commit()
        self.target=datetime.now(ack.MSK).date()+timedelta(days=1)
        self.natalia=ack.recipient()

    def queue(self,name='Новая акция',pid=123,account='wb_acc1'):
        p=dict(id=pid,type='auto',name=name,
            startDateTime=f'{self.target}T00:00:00+03:00',endDateTime=f'{self.target}T23:59:00+03:00')
        ack.discover(self.conn,[(account,p)],self.target)
        self.conn.commit()

    def value(self,query):
        with self.conn.cursor() as cur:
            cur.execute(query);return cur.fetchone()[0]

    def click(self,task=1,user=None):
        return dict(id='callback1',data=f'{ack.PREFIX}{task}',
                    message=dict(message_id=1,chat=dict(id=self.natalia)),
                    **{'from':dict(id=self.natalia if user is None else user)})

    def test_each_action_has_own_button_and_hourly_repeat(self):
        self.queue();self.queue('Другая акция',124)
        with patch.object(ack,'api',return_value={'message_id':42}) as api:
            self.assertEqual(ack.send_due(self.conn),2);self.conn.commit()
            buttons=[x.args[1]['reply_markup']['inline_keyboard'][0][0] for x in api.call_args_list]
            self.assertEqual([b['text'] for b in buttons],['Сделано','Сделано'])
            self.assertNotEqual(buttons[0]['callback_data'],buttons[1]['callback_data'])
            self.assertEqual(ack.send_due(self.conn),0)
            with self.conn.cursor() as cur:
                cur.execute("UPDATE wb_promo_task SET last_sent_at=now()-interval '1 hour'")
            self.conn.commit()
            self.assertEqual(ack.send_due(self.conn),2);self.conn.commit()
        self.assertEqual(self.value('SELECT min(send_count) FROM wb_promo_task'),2)

    def test_done_stops_only_one_action_and_survives_replay(self):
        self.queue();self.queue('Другая акция',124)
        with patch.object(ack,'api',return_value={'message_id':42}):
            self.assertTrue(ack.handle_callback(self.conn,self.click()))
            done=self.value('SELECT done_at FROM wb_promo_task WHERE id=1')
            self.assertTrue(ack.handle_callback(self.conn,self.click()))
            self.assertEqual(done,self.value('SELECT done_at FROM wb_promo_task WHERE id=1'))
            self.assertEqual(ack.send_due(self.conn),1);self.conn.commit()
        self.assertEqual(self.value('SELECT send_count FROM wb_promo_task WHERE id=1'),0)

    def test_foreign_user_cannot_acknowledge(self):
        self.queue()
        with patch.object(ack,'api'):
            self.assertFalse(ack.handle_callback(self.conn,self.click(user=7)))
        self.assertIsNone(self.value('SELECT done_at FROM wb_promo_task'))

    def test_delivery_failure_is_not_marked_sent(self):
        self.queue()
        with patch.object(ack,'api',side_effect=RuntimeError('rejected')):
            self.assertEqual(ack.send_due(self.conn),0);self.conn.commit()
        self.assertIsNone(self.value('SELECT last_sent_at FROM wb_promo_task'))
        self.assertIsNone(self.value('SELECT notified_at FROM wb_promo_notice'))

    def test_ack_persists_when_telegram_ui_fails_and_cursor_advances(self):
        self.queue()
        with patch.object(ack,'api',side_effect=RuntimeError('UI error')):
            process_updates(self.conn,[dict(update_id=20,callback_query=self.click())])
        self.assertIsNotNone(self.value('SELECT done_at FROM wb_promo_task'))
        self.assertEqual(self.value('SELECT next_update_id FROM prc_promo_callback_cursor'),21)

    def test_same_action_in_two_accounts_is_one_task(self):
        self.queue();self.queue(pid=999,account='wb_acc2')
        self.assertEqual(self.value('SELECT count(*) FROM wb_promo_task'),1)
        self.assertEqual(set(self.value('SELECT accounts FROM wb_promo_task')),{'wb_acc1','wb_acc2'})
        self.assertEqual(len(self.value('SELECT promo_refs FROM wb_promo_task')),2)

    def test_old_notices_are_not_turned_into_tasks(self):
        self.queue()
        with self.conn.cursor() as cur:
            cur.execute('DELETE FROM wb_promo_task')
            cur.execute('UPDATE wb_promo_notice SET notified_at=now()')
        self.conn.commit();self.queue()
        self.assertEqual(self.value('SELECT count(*) FROM wb_promo_task'),0)

    def test_explicit_adoption_adds_button_task_for_upcoming_notice(self):
        self.queue()
        with self.conn.cursor() as cur:
            cur.execute('DELETE FROM wb_promo_task')
            cur.execute('UPDATE wb_promo_notice SET notified_at=now()')
            cur.execute('SELECT account,promo_id,name,starts_at,ends_at FROM wb_promo_notice')
            rows=[(a,dict(id=pid,name=name,type='auto',startDateTime=start.isoformat(),
                         endDateTime=end.isoformat())) for a,pid,name,start,end in cur.fetchall()]
        ack.discover(self.conn,rows,self.target,include_notified=True)
        self.conn.commit()
        self.assertEqual(self.value('SELECT count(*) FROM wb_promo_task'),1)

    def test_dry_does_not_send_and_registration_can_roll_back(self):
        self.queue()
        with patch.object(ack,'api') as api:
            self.assertEqual(ack.send_due(self.conn,dry=True),0)
            api.assert_not_called()
        self.assertIsNone(self.value('SELECT last_sent_at FROM wb_promo_task'))

    def test_concurrent_sender_skips_locked_task(self):
        self.queue()
        with self.conn.cursor() as cur:cur.execute('SELECT id FROM wb_promo_task FOR UPDATE')
        other=watch.db()
        try:
            with other.cursor() as cur:
                cur.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(self.schema)))
            with patch.object(ack,'api') as api:
                self.assertEqual(ack.send_due(other),0);api.assert_not_called()
            other.commit()
        finally:other.close();self.conn.rollback()
