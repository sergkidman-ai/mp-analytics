# поток: rev
import json
import unittest
import uuid
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import psycopg2
from psycopg2 import sql
from psycopg2.extensions import parse_dsn, make_dsn
from core import db
from feedback_bot import recovery_runtime as runtime
from reports import recovery_budget
from reports import llm_batch


class DurableRuntimeTest(unittest.TestCase):
    def setUp(self):
        self.original_dsn = db.DATABASE_URL
        self.schema = 'rev_test_' + uuid.uuid4().hex[:12]
        conn = psycopg2.connect(self.original_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self.schema)))
        conn.close()
        opts = parse_dsn(self.original_dsn)
        opts['options'] = '-c search_path=' + self.schema
        self.patcher = patch.object(db, 'DATABASE_URL', make_dsn(**opts))
        self.patcher.start()
        db.execute('CREATE TABLE raw_feedback(id int)')
        db.execute('CREATE TABLE feedback_moderation(id int)')
        runtime.setup()
        recovery_budget.setup()

    def tearDown(self):
        self.patcher.stop()
        conn = psycopg2.connect(self.original_dsn)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(self.schema)))
        conn.close()

    def test_edit_survives_new_instance_and_read_until_saved(self):
        runtime.DurableEdits()['owner'] = 42
        self.assertEqual(runtime.DurableEdits().pop('owner'), 42)
        self.assertEqual(runtime.DurableEdits().pop('owner'), 42)
        runtime.DurableEdits().finish('owner')
        self.assertIsNone(runtime.DurableEdits().pop('owner'))

    def test_saved_edit_card_retries_after_failure_without_sending_or_changing_text(self):
        db.execute('''ALTER TABLE feedback_moderation ADD COLUMN state text,
            ADD COLUMN final_text text,ADD COLUMN tg_chat_id bigint,ADD COLUMN tg_msg_id bigint''')
        db.execute("INSERT INTO feedback_moderation(id,state,final_text,tg_chat_id,tg_msg_id) VALUES(7,'edited','Manual text',1,200)")
        failed=[]
        self.assertEqual(runtime.refresh_edit_cards(lambda *a,**k:failed.append(a),lambda *a,**k:{}),0)
        self.assertFalse(db.query('SELECT recovery_edit_card_ok FROM feedback_moderation')[0]['recovery_edit_card_ok'])
        successful=[]
        def edit(*args,**kwargs):
            successful.append(args)
            return {'ok':True}
        self.assertEqual(runtime.refresh_edit_cards(edit,lambda *a,**k:{}),1)
        self.assertEqual(runtime.refresh_edit_cards(edit,lambda *a,**k:{}),0)
        self.assertEqual(len(successful),1)
        row=db.query('SELECT state,final_text,recovery_edit_card_ok FROM feedback_moderation')[0]
        self.assertEqual(row['final_text'],'Manual text')
        self.assertEqual(row['state'],'edited')

    def test_failed_update_is_archived_and_not_acknowledged(self):
        update = {'update_id': 11, 'message': {'text': 'manual edit'}}
        requests = []
        def api(method, params, timeout):
            requests.append(dict(params))
            return {'ok': True, 'result': [update]}
        def fail(message):
            raise RuntimeError('simulated handler failure')
        with self.assertRaises(RuntimeError):
            runtime.poll_once(api, lambda cb: None, fail)
        self.assertNotIn('offset', requests[-1])
        self.assertEqual(db.query('SELECT state FROM rev_telegram_inbox')[0]['state'], 'pending')
        self.assertFalse(db.query('SELECT * FROM rev_telegram_checkpoint'))
        handled = []
        runtime.poll_once(api, lambda cb: None, handled.append)
        self.assertEqual(handled, [update['message']])
        runtime.poll_once(api, lambda cb: None, handled.append)
        self.assertEqual(len(handled), 1)
        self.assertEqual(requests[-1]['offset'], 12)

    def test_unknown_publication_survives_restart_and_cannot_be_claimed_twice(self):
        row = {'platform':'wb','account':'test','kind':'question','ext_id':'case'}
        self.assertTrue(runtime.claim_publication(row, 'approved text'))
        runtime.publication_result(row, 'unknown')
        self.assertFalse(runtime.claim_publication(row, 'approved text'))
        self.assertEqual(db.query('SELECT state FROM rev_publication_attempt')[0]['state'], 'unknown')

    def test_shared_budget_counts_both_providers_and_reservations(self):
        self.assertTrue(recovery_budget.reserve('search', 'deepseek', '0.75'))
        self.assertTrue(recovery_budget.reserve('batch', 'anthropic', '1.00'))
        with self.assertRaises(RuntimeError):
            recovery_budget.reserve('overflow', 'anthropic', '0.26')
        self.assertFalse(recovery_budget.reserve('batch', 'anthropic', '1.00'))
        with self.assertRaises(RuntimeError):
            recovery_budget.reserve('unknown-price', 'other', None)

    def test_batch_results_settle_shared_reservation_once_including_cache_cost(self):
        db.apply_sql_file(Path(__file__).resolve().parents[1]/'migrations/706_rev_llm_batch.sql')
        db.execute('''CREATE TABLE feedback_llm_cost_log(day date,model text,calls int,
            tokens_in bigint,tokens_out bigint,cost_usd numeric,PRIMARY KEY(day,model))''')
        key='test-request'
        reservation=hashlib.sha256(key.encode()).hexdigest()
        recovery_budget.reserve(reservation,'anthropic','1')
        db.execute("INSERT INTO feedback_llm_batch(batch_id,model,n_req) VALUES('b','claude-sonnet-4-6',1)")
        db.execute("""INSERT INTO feedback_llm_queue(req_key,model,system_text,content,batch_id)
            VALUES(%s,'claude-sonnet-4-6','system','content','b')""",(key,))
        message=SimpleNamespace(model='claude-sonnet-4-6',
            content=[SimpleNamespace(type='text',text='answer')],
            usage=SimpleNamespace(input_tokens=100,output_tokens=50,
                cache_creation_input_tokens=200,cache_read_input_tokens=1000))
        result=SimpleNamespace(custom_id=key,result=SimpleNamespace(type='succeeded',message=message))
        client=SimpleNamespace(messages=SimpleNamespace(batches=SimpleNamespace(
            retrieve=lambda bid:SimpleNamespace(processing_status='ended'),results=lambda bid:[result])))
        with patch('reports.llm_client.client_for',return_value=client):
            self.assertEqual(llm_batch.collect(verbose=False),1)
            self.assertEqual(llm_batch.collect(verbose=False),0)
        status=recovery_budget.status()
        self.assertAlmostEqual(float(status['spent_usd']),0.00105)
        self.assertEqual(float(status['reserved_usd']),0)
        self.assertEqual(db.query('SELECT calls FROM feedback_llm_cost_log')[0]['calls'],1)
        self.assertFalse(db.query('SELECT * FROM feedback_llm_queue'))


if __name__ == '__main__':
    unittest.main()
