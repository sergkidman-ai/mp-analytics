# поток: inv
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
import json
import time

import requests
from ops import ms_market_status_live as live, ms_market_status_state as state
from shipment_guard.config import ORGS, AGENTS

NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
MSK = ZoneInfo('Europe/Moscow')


def document(uid='ms-1', name='123456-0010-1', status='В пути'):
    def ref(kind, value):
        return {'meta': {'href': live.ms_api.BASE + '/entity/' + kind + '/' + value}}
    org = next(k for k, v in ORGS.items() if str(v[0]) == '1')
    agent = next(k for k, v in AGENTS.items() if v == 'ozon')
    return {'id': uid, 'name': name, 'moment': '2026-11-01 10:00:00',
            'updated': '2026-11-03 10:00:00', 'organization': ref('organization', org),
            'agent': ref('counterparty', agent), 'store': dict(ref('store', 'store-1'), name='Звездный'),
            'state': {'name': status}, 'sum': 10000}


def proof(status='delivered', **changes):
    value = {'platform': 'ozon', 'account': 'oz_acc1', 'number': '123456-0010-1',
             'scheme': 'fbs', 'status': status, 'detail': status,
             'fetched_at': NOW.isoformat(), 'evidence': 'test_api'}
    value.update(changes)
    return value


class StatusLiveTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(state, 'RUNTIME', Path(self.temp.name)).start()
        patch.object(live, 'now', return_value=NOW).start()
        patch.object(live, 'status_ids', return_value={'В пути': 'in-transit', 'Доставлен': 'delivered',
                                                     'Отменен - сроки': 'cancelled'}).start()
        patch.object(state, 'reserve_slot', return_value=0).start()
        self.get = patch.object(live.ms_api, 'get').start()
        self.put = patch.object(live.requests, 'put', return_value=Mock(status_code=200, json=lambda: {})).start()
        self.doc = document(); state.index([live.row_for(self.doc)])

    def job(self, observation=None):
        state.enqueue(observation or proof(), 'fast', force=True)
        return state.claim()

    def test_rolling_window_and_moscow_midnight(self):
        start, end = live.window(NOW)
        self.assertEqual(start.isoformat(), '2026-08-05T00:00:00+03:00')
        self.assertEqual(end.isoformat(), '2026-11-04T00:00:00+03:00')
        for moment, expected in [('2026-08-05 00:00:00', True), ('2026-08-04 23:59:59', False),
                                 ('2026-11-03 23:59:59', True), ('2026-11-04 00:00:00', False)]:
            self.assertEqual(live.in_window(dict(self.doc, moment=moment)), expected)

    def test_night_window_stops_at_five_and_uses_one_night_id(self):
        self.assertEqual(live.night_id(datetime(2026, 11, 3, 23, tzinfo=MSK)), '2026-11-03')
        self.assertEqual(live.night_id(datetime(2026, 11, 4, 4, 59, tzinfo=MSK)), '2026-11-03')
        self.assertIsNone(live.night_id(datetime(2026, 11, 4, 5, tzinfo=MSK)))

    def test_only_state_is_written_after_fresh_source_and_ms_check(self):
        self.get.side_effect = [self.doc, document(status='Доставлен')]
        result = live.apply_job(self.job())
        self.assertEqual(result['result'], 'updated')
        self.put.assert_called_once()
        self.assertEqual(set(self.put.call_args.kwargs['json']), {'state'})
        self.assertEqual(self.get.call_count, 2)
        self.assertIn('/delivered', self.put.call_args.kwargs['json']['state']['meta']['href'])

    def test_cancel_uses_exact_existing_general_ms_state(self):
        self.get.side_effect = [self.doc, document(status='Отменен - сроки')]
        result = live.apply_job(self.job(proof('cancelled')))
        self.assertEqual(result['target'], 'Отменен - сроки')
        self.assertEqual(result['result'], 'updated')

    def test_assembling_staff_change_outside_scope_never_written(self):
        for status in ('Собирается', 'Неукомплектован', 'Подтвержден', 'Доставлен'):
            state.index([live.row_for(self.doc)])
            self.get.return_value = document(status=status)
            job = self.job(); result = live.apply_job(job)
            self.assertIn(result['result'], ('changed_by_other', 'already_updated'))
            state.finish(job, result)
        self.put.assert_not_called()

    def test_orders_older_than_ninety_days_are_never_written(self):
        self.get.return_value = dict(self.doc, moment='2026-08-04 10:00:00')
        self.assertEqual(live.apply_job(self.job())['result'], 'identity_or_window_changed')
        self.put.assert_not_called()

    def test_wrong_account_or_number_is_not_updated(self):
        self.get.return_value = dict(self.doc, name='different-order')
        self.assertEqual(live.apply_job(self.job())['result'], 'identity_or_window_changed')
        self.put.assert_not_called()

    def test_timeout_resolved_by_fact_without_second_put(self):
        self.put.side_effect = requests.ReadTimeout()
        self.get.side_effect = [self.doc, document(status='Доставлен')]
        result = live.apply_job(self.job())
        self.assertEqual(result['result'], 'updated')
        self.assertEqual(result['transport_error'], 'ReadTimeout')
        self.put.assert_called_once()

    def test_unconfirmed_timeout_stops_order_but_not_other_jobs(self):
        self.put.side_effect = requests.ReadTimeout(); self.get.return_value = self.doc
        job = self.job(); result = live.apply_job(job)
        self.assertEqual(result['result'], 'uncertain')
        state.finish(job, result, uncertain=True)
        state.enqueue(proof(), 'night', force=True)
        self.assertIsNone(state.claim('night'))
        self.put.assert_called_once()

    def test_restart_with_prior_intent_never_sends_another_put(self):
        state.begin_attempt(self.doc, 'Доставлен', proof(), None)
        self.get.return_value = self.doc
        self.assertEqual(live.apply_job(self.job())['result'], 'uncertain')
        self.put.assert_not_called()

    def test_duplicate_document_numbers_are_not_guessed(self):
        other = document('ms-2'); state.index([live.row_for(other)])
        self.get.return_value = {'meta': {'size': 2}, 'rows': [self.doc, other]}
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            live.apply_job(self.job())
        self.put.assert_not_called()

    def test_new_source_revision_cancels_claim_before_write(self):
        job = self.job()
        def changed(*args, **kwargs):
            state.enqueue(proof('cancelled', fetched_at=(NOW + timedelta(seconds=1)).isoformat()), 'fast')
            return self.doc
        self.get.side_effect = changed
        self.assertEqual(live.apply_job(job)['result'], 'scope_changed_or_night_ended')
        self.put.assert_not_called()

    def test_pult_baseline_is_not_historical_backfill_and_dbs_is_excluded(self):
        row = {'id': 1, 'platform': 'ozon', 'account': 'oz_acc1', 'posting_number': self.doc['name'],
               'mp_status': 'delivered', 'observed_at': NOW, 'scheme': None, 'program': None}
        with patch.object(live.db, 'query', return_value=[row]):
            self.assertEqual(live.poll_pult(True)['enqueued'], 0)
            self.assertIsNone(state.claim())
            row.update(mp_status='cancelled', observed_at=NOW + timedelta(seconds=1))
            self.assertEqual(live.poll_pult()['enqueued'], 1)
        job = state.claim(); self.assertEqual(job['proof']['status'], 'cancelled')
        row.update(program='DBS', observed_at=NOW + timedelta(seconds=2))
        with patch.object(live.db, 'query', return_value=[row]):
            self.assertEqual(live.poll_pult()['enqueued'], 0)

    def test_stale_terminal_proof_is_invalidated_when_market_returns_to_transit(self):
        job = self.job(proof(fetched_at=(NOW - timedelta(hours=1)).isoformat()))
        self.get.return_value = self.doc
        with patch.object(live, 'Provider') as provider:
            provider.return_value.fetch.return_value = {'ms-1': proof('transit', ms_id='ms-1')}
            result = live.apply_job(job)
        self.assertEqual(result['result'], 'refreshed')
        state.finish(job, result, retry_after=1)
        with patch.object(state.time, 'time', return_value=time.time() + 2):
            refreshed = state.claim()
        self.assertEqual(refreshed['proof']['status'], 'transit')
        self.assertEqual(live.apply_job(refreshed)['result'], 'unchanged')
        self.put.assert_not_called()

    def test_fast_cannot_claim_night_mass_work(self):
        state.enqueue(proof(), 'night')
        self.assertIsNone(state.claim('fast'))
        self.assertIsNotNone(state.claim('night'))

    def test_two_workers_cannot_claim_same_document(self):
        job = self.job()
        self.assertIsNone(state.claim('night'))
        self.assertTrue(state.current(job))

    def test_hourly_limit_counts_entities_not_batches(self):
        with state.database() as con:
            con.execute('BEGIN')
            con.executemany('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?)',
                [(str(n), 'other-' + str(n), 'Доставлен', 'v1', time.time(), None, 'confirmed', '{}', '{}') for n in range(3000)])
            con.commit()
        with self.assertRaisesRegex(RuntimeError, 'limit'):
            state.begin_attempt(self.doc, 'Доставлен', proof(), None)

    def test_canary_pause_is_persistent_and_checks_operational_health(self):
        with patch.object(state, 'night_confirmed', return_value=200), patch.object(live, 'operational_health') as health:
            self.assertFalse(live.canary('2026-11-03'))
            saved = state.setting('canary_2026-11-03'); saved['until'] = time.time() - 1
            state.set_setting('canary_2026-11-03', saved)
            self.assertTrue(live.canary('2026-11-03'))
            self.assertTrue(live.canary('2026-11-03'))
            health.assert_called_once()

    def test_nightly_cap_counts_entities_across_process_restarts(self):
        with state.database() as con:
            con.execute('BEGIN')
            con.executemany('INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?)',
                [(str(n), 'other-' + str(n), 'Доставлен', 'v1', time.time() - 7200,
                  '2026-11-03', 'confirmed', '{}', '{}') for n in range(10000)])
            con.commit()
        with self.assertRaisesRegex(RuntimeError, 'limit'):
            state.begin_attempt(self.doc, 'Доставлен', proof(), '2026-11-03')

    def test_night_command_during_day_has_no_api_calls(self):
        self.assertEqual(live.run_night(Path(self.temp.name))['result'], 'outside_night_window')
        self.get.assert_not_called(); self.put.assert_not_called()

    def test_ms_latency_increases_shared_spacing_and_recovers_gradually(self):
        state.latency(10)
        self.assertGreaterEqual(state.setting('write_interval'), 10)
        state.latency(0.1)
        self.assertGreaterEqual(state.setting('write_interval'), 1.5)
        self.assertLess(state.setting('write_interval'), 10)
