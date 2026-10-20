# поток: fin
"""Shared API lock: bounded, session-owned, reentrant; optional PG checks in test namespace."""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch
import psycopg2
from core import ozon_posting_lock as lock


def connection(acquired=True):
    conn=MagicMock();conn.closed=0
    cur=conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value=(acquired,)
    return conn,cur


class LockTests(unittest.TestCase):
    def setUp(self):
        token=lock._HELD.set({});self.addCleanup(lock._HELD.reset,token)
        self.conn,self.cur=connection()
        p=patch.object(lock.psycopg2,'connect',return_value=self.conn)
        self.connect=p.start();self.addCleanup(p.stop)

    def test_session_lock_autocommit_release_and_namespace(self):
        with lock.account_lock('oz_acc2') as lease:
            self.assertTrue(self.conn.autocommit);lease.check()
            self.conn.close.assert_not_called()
        self.assertEqual(self.cur.execute.call_args_list[0].args,
                         ('SELECT pg_try_advisory_lock(%s, %s)',(lock.LOCK_NAMESPACE,2)))
        self.assertEqual(self.cur.execute.call_args_list[-1].args,
                         ('SELECT pg_advisory_unlock(%s, %s)',(lock.LOCK_NAMESPACE,2)))
        self.conn.close.assert_called_once()
        with self.assertRaises(lock.OzonLockLost):lease.check()

    def test_nested_same_account_reuses_session_without_unlocking_early(self):
        with lock.account_lock('oz_acc1') as outer:
            with lock.account_lock('oz_acc1') as inner:self.assertIs(inner,outer)
            self.conn.close.assert_not_called()
            self.assertFalse(any('pg_advisory_unlock' in c.args[0] for c in self.cur.execute.call_args_list))
        self.connect.assert_called_once();self.conn.close.assert_called_once()

    def test_different_accounts_use_different_keys_and_sessions(self):
        conn2,cur2=connection();self.connect.side_effect=[self.conn,conn2]
        with lock.account_lock('oz_acc1'):
            with lock.account_lock('oz_acc2'):pass
            self.conn.close.assert_not_called()
        self.assertEqual(self.cur.execute.call_args_list[0].args[1],(lock.LOCK_NAMESPACE,1))
        self.assertEqual(cur2.execute.call_args_list[0].args[1],(lock.LOCK_NAMESPACE,2))

    def test_busy_nonblocking_fails_without_unlocking_another_owner(self):
        self.cur.fetchone.return_value=(False,)
        with patch.object(lock.time,'sleep') as sleep,self.assertRaises(lock.OzonLockBusy):
            with lock.account_lock('oz_acc1',timeout=0):self.fail('must not enter')
        sleep.assert_not_called();self.conn.close.assert_called_once()
        self.assertFalse(any('pg_advisory_unlock' in c.args[0] for c in self.cur.execute.call_args_list))

    def test_busy_wait_stops_on_deadline(self):
        self.cur.fetchone.return_value=(False,)
        with patch.object(lock.time,'monotonic',side_effect=[0,0,3]),patch.object(lock.time,'sleep') as sleep, self.assertRaises(lock.OzonLockBusy):
            with lock.account_lock('oz_acc1',timeout=2):self.fail('must not enter')
        sleep.assert_called_once_with(0.1);self.conn.close.assert_called_once()

    def test_late_acquisition_releases_lock_without_entering(self):
        with patch.object(lock.time,'monotonic',side_effect=[0,6]),self.assertRaises(lock.OzonLockBusy):
            with lock.account_lock('oz_acc1',timeout=5):self.fail('must not enter')
        self.assertIn('pg_advisory_unlock',self.cur.execute.call_args.args[0]);self.conn.close.assert_called_once()

    def test_body_exception_releases_and_does_not_leak_context(self):
        with self.assertRaisesRegex(RuntimeError,'body failure'):
            with lock.account_lock('oz_acc1'):raise RuntimeError('body failure')
        self.assertNotIn('oz_acc1',lock._HELD.get());self.conn.close.assert_called_once()

    def test_connection_loss_fails_closed(self):
        with lock.account_lock('oz_acc1') as lease:
            self.cur.execute.side_effect=psycopg2.OperationalError('raw connection detail')
            with self.assertRaisesRegex(lock.OzonLockLost,'connection lost'):lease.check()
        self.conn.close.assert_called_once()

    def test_connect_error_does_not_expose_connection_details(self):
        self.connect.side_effect=psycopg2.OperationalError('sensitive DSN')
        with self.assertRaises(lock.OzonLockLost) as exc:
            with lock.account_lock('oz_acc1'):pass
        self.assertNotIn('sensitive',str(exc.exception))

    def test_acquire_query_error_closes_session(self):
        self.cur.execute.side_effect=psycopg2.OperationalError('query failed')
        with self.assertRaises(lock.OzonLockLost):
            with lock.account_lock('oz_acc1'):pass
        self.conn.close.assert_called_once()

    def test_invalid_inputs_do_not_connect(self):
        for account,timeout in [('unknown',1),('oz_acc1',True),('oz_acc1',-1),('oz_acc1',float('inf'))]:
            with self.subTest(account=account,timeout=timeout),self.assertRaises(ValueError):
                with lock.account_lock(account,timeout=timeout):pass
        self.connect.assert_not_called()

    def test_inherited_context_cannot_bypass_other_thread_owner(self):
        self.cur.fetchone.side_effect=[(True,),(False,)]
        with lock.account_lock('oz_acc1') as lease:
            with patch.object(lock,'_owner',return_value=('other owner',)),self.assertRaises(lock.OzonLockBusy):
                with lock.account_lock('oz_acc1',timeout=0):pass
            # The fake factory reuses one mock connection, but acquisition was attempted twice.
        self.assertEqual(self.connect.call_count,2)


@unittest.skipUnless(os.getenv('OZON_POSTING_SQL_TEST')=='1','opt-in PostgreSQL advisory lock checks')
class PostgresLockTests(unittest.TestCase):
    def setUp(self):
        # Never compete with real Ozon account locks or the shipment/MS bigint lock.
        p=patch.object(lock,'LOCK_NAMESPACE',0x6F5A4F4E);p.start();self.addCleanup(p.stop)

    @staticmethod
    def attempt(account):
        try:
            with lock.account_lock(account,timeout=0) as lease:lease.check();return True
        except lock.OzonLockBusy:return False

    def test_two_db_sessions_exclude_same_account_but_allow_other_account(self):
        with ThreadPoolExecutor(max_workers=1) as worker:
            with lock.account_lock('oz_acc1',timeout=2):
                self.assertFalse(worker.submit(self.attempt,'oz_acc1').result(timeout=5))
                self.assertTrue(worker.submit(self.attempt,'oz_acc2').result(timeout=5))
            self.assertTrue(worker.submit(self.attempt,'oz_acc1').result(timeout=5))

    def test_nested_context_does_not_release_outer_session_lock(self):
        with ThreadPoolExecutor(max_workers=1) as worker:
            with lock.account_lock('oz_acc1',timeout=2) as outer:
                with lock.account_lock('oz_acc1',timeout=0) as inner:self.assertIs(inner,outer)
                self.assertFalse(worker.submit(self.attempt,'oz_acc1').result(timeout=5))
            self.assertTrue(worker.submit(self.attempt,'oz_acc1').result(timeout=5))

    def test_release_after_exception(self):
        with self.assertRaisesRegex(RuntimeError,'test body'):
            with lock.account_lock('oz_acc1',timeout=2):raise RuntimeError('test body')
        self.assertTrue(self.attempt('oz_acc1'))


if __name__=='__main__':unittest.main()
