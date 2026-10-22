# поток: fin
from datetime import datetime, timezone
from decimal import Decimal
import json
from unittest.mock import Mock

import pytest

from core.profit_floor import minimum_profit
from core.wb_auto_entry import choose, number, protection, sale
from core.wb_auto_journal import Journal
from ops import wb_auto_entry as runner

NOW = datetime(2026, 10, 21, 21, tzinfo=timezone.utc)


def offer(pid, cap, start='2026-10-21T21:00:00Z', end='2026-11-15T20:59:59Z'):
    return {'ids': [pid], 'start': start, 'end': end, 'cap': str(cap), 'source': 'test'}


def calendar(offers):
    return {o['ids'][0]: {'id': o['ids'][0], 'type': 'auto', 'startDateTime': o['start'], 'endDateTime': o['end']} for o in offers}


@pytest.mark.parametrize('cost,units,target', [(150, 1, 200), ('150.001', 1, 300), (450, 3, 600), ('450.001', 3, 900), (10000, 1, 1000)])
def test_common_floor(cost, units, target):
    assert minimum_profit(cost, units) == target


def test_highest_active_cap_and_integer_discount():
    offers = [offer(1, 700), offer(2, 805), offer(3, 999, start='2026-10-22T21:00:00Z'),
              offer(4, 990, end='2026-10-21T20:59:59Z')]
    result = choose(1000, 750, offers, calendar(offers), NOW)
    assert result['offer']['ids'] == [2]
    assert result['discount'] == 20
    assert result['target'] == '800.00'


def test_rounding_cannot_breach_floor():
    o = [offer(1, 805)]
    assert choose(1000, 801, o, calendar(o), NOW)['reason'] == 'no_integer_discount'


def test_no_raise_base_for_entry_and_zero_discount_allowed():
    o = [offer(1, 1001)]
    assert choose(1000, 999, o, calendar(o), NOW)['discount'] == 0
    assert choose(1000, 1000.01, o, calendar(o), NOW)['reason'] == 'no_integer_discount'


def test_changed_calendar_generation_fails_closed():
    o = [offer(1, 999)]
    cal = calendar(o)
    cal[1]['startDateTime'] = '2026-10-20T21:00:00Z'
    assert choose(1000, 500, o, cal, NOW)['reason'] == 'no_active_verified_offer'


@pytest.mark.parametrize('base,discount,floor', [(1000, 90, '950.01'), (1000, 90, '1000.01'), (100, 50, '333.3333333333333')])
def test_protective_raise(base, discount, floor):
    b, d = protection(base, discount, floor)
    assert sale(b, d) >= number(floor)
    assert sale(b, d) > sale(base, discount)


def row(nm=1):
    return {'nm_id': nm, 'base': 1000, 'discount': 20, 'target': '800.00', 'vendor': '0001'}


@pytest.fixture
def journal(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'barrier', lambda account: tmp_path / (account + '.pending'))
    return Journal(tmp_path / 'journal.sqlite3')


def test_network_ambiguity_is_durable_and_never_blindly_reposted(journal):
    client = Mock()
    client.request.side_effect = runner.requests.Timeout()
    assert not runner.submit(client, journal, 'wb_acc1', [row()])
    assert journal.pending('wb_acc1')[0]['state'] == 'uncertain'
    assert not runner.recover(client, journal, 'wb_acc1', {1: (1000, 20, 800, '0001')})
    assert client.request.call_count == 1
    client.get.assert_not_called()


def test_crash_after_intent_cannot_be_assumed_unsent(journal):
    journal.intent('wb_acc1', [row()])
    client = Mock()
    assert not runner.recover(client, journal, 'wb_acc1', {})
    assert journal.pending('wb_acc1')[0]['state'] == 'uncertain'
    client.request.assert_not_called()


def test_partial_success_requires_item_prices(journal):
    uid = journal.intent('wb_acc1', [row(), row(2)])
    journal.update(uid, 'accepted', upload_id=123)
    client = Mock()
    client.get.side_effect = [{'data': {'status': 3, 'successGoodsNumber': 2}}, {'data': {'historyGoods': []}}]
    assert runner.recover(client, journal, 'wb_acc1', {1: (1000, 20, 800, '0001'), 2: (1000, 75, 250, '0001')})
    result = journal.conn.execute('SELECT * FROM uploads').fetchone()
    assert result['state'] == 'partial'
    event = journal.conn.execute('SELECT detail FROM events ORDER BY id DESC LIMIT 1').fetchone()
    assert json.loads(event[0])['unconfirmed'] == [2]


def test_pending_task_blocks_new_writes_even_if_current_price_matches(journal):
    uid = journal.intent('wb_acc1', [row()])
    journal.update(uid, 'accepted', upload_id=123)
    client = Mock()
    client.get.return_value = {'data': {'status': 1}}
    assert not runner.recover(client, journal, 'wb_acc1', {1: (1000, 20, 800, '0001')})


def test_status_six_is_completed_rejection_not_pending(journal):
    uid = journal.intent('wb_acc2', [row()])
    journal.update(uid, 'accepted', upload_id=123)
    client = Mock()
    client.get.side_effect = [
        {'data': {'status': 6, 'overAllGoodsNumber': 1, 'successGoodsNumber': 0}},
        {'data': {'historyGoods': [{'nmID': 1, 'errorText': 'New price is several times lower than the current price. Item has been moved to Price Quarantine'}]}}]
    assert runner.recover(client, journal, 'wb_acc2', {1: (1000, 0, 1000, '0001')})
    assert not journal.pending('wb_acc2')
    state = journal.conn.execute('SELECT state,terminal FROM uploads WHERE id=?', (uid,)).fetchone()
    assert state['state'] == 'partial' and state['terminal'] == 1
    client.request.assert_not_called()


def test_explicit_rate_limit_can_retry_without_pending_barrier(journal):
    client = Mock()
    client.request.return_value = Mock(status_code=429)
    assert not runner.submit(client, journal, 'wb_acc1', [row()])
    assert not journal.pending('wb_acc1')


def test_proposals_no_stock_means_no_price_decrease_but_protect_floor():
    o = [offer(1, 800)]
    manifest = {'offers': {'wb_acc1': {'1': o}}}
    costs = {1: {'cogs': 100, 'branch': 'remote', 'bundle': 1}}
    # Cheap group floor=(100+200)/.431=696.05; target 800.
    result = runner.proposals('wb_acc1', manifest, {1: (1000, 0, 1000, '0001')}, {}, costs, {}, calendar(o), NOW)[0]
    assert result['reason'] == 'stock_not_confirmed_for_price_decrease'
    result = runner.proposals('wb_acc1', manifest, {1: (1000, 75, 250, '0001')}, {}, costs, {}, calendar(o), NOW)[0]
    assert result['action']
    assert number(result['target']) >= number(result['floor'])


def test_dry_run_cannot_submit(tmp_path, monkeypatch):
    client = Mock()
    client.deadline = runner.time.monotonic() + 120
    client.goods.return_value = ({nm: (1000, 0, 1000, '0001') for nm in range(1, 5001)}, {})
    client.calendar.return_value = calendar([offer(1, 800)])
    client.stocks.return_value = {1: 4}
    monkeypatch.setattr(runner, 'Client', lambda *args: client)
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner.guard, 'COGS_MIN_KNOWN', 1)
    monkeypatch.setattr(runner.guard, 'cogs_map', lambda *args: {1: {'cogs': 100, 'branch': 'remote'}})
    args = Mock(budget=120, apply=False, at=NOW.isoformat(), out=str(tmp_path), limit=0)
    runner.run_account(args, {'offers': {'wb_acc1': {'1': [offer(1, 800)]}}, 'sha256': 'test'}, 'wb_acc1')
    client.request.assert_not_called()


def test_no_optimization_before_new_action_start():
    old = offer(3012, 900, start='2026-10-07T21:00:00Z')
    new = offer(3038, 800)
    manifest = {'offers': {'wb_acc1': {'1': [old, new]}}}
    costs = {1: {'cogs': 100, 'branch': 'remote'}}
    before = datetime(2026, 10, 21, 20, 59, tzinfo=timezone.utc)
    result = runner.proposals('wb_acc1', manifest, {1: (1000, 0, 1000, '0001')}, {}, costs, {1: 4}, calendar([old, new]), before)[0]
    assert result['reason'] == 'waiting_new_action_start'
    assert not result.get('action')


def test_known_zero_stock_cannot_be_optimized():
    o = [offer(1, 800)]
    result = runner.proposals('wb_acc1', {'offers': {'wb_acc1': {'1': o}}},
                              {1: (1000, 25, 750, '0001')}, {},
                              {1: {'cogs': 100, 'branch': 'remote'}}, {1: 0}, calendar(o), NOW)[0]
    assert result['reason'] == 'no_mp_stock'
    assert not result.get('action')


@pytest.mark.parametrize('stocks', [{1: 4}, {1: 0}, {}])
def test_unknown_cost_is_raised_above_all_known_active_caps(stocks):
    offers = [offer(1, 1100), offer(2, 1200)]
    result = runner.proposals('wb_acc1', {'offers': {'wb_acc1': {'1': offers}}},
                              {1: (1000, 50, 500, '0001')}, {}, {}, stocks, calendar(offers), NOW)[0]
    assert result['reason'] == 'exclude_unknown_cost'
    assert number(result['target']) > 1200
    assert 'cost' not in result and 'floor' not in result


def test_writer_lock_excludes_second_writer(tmp_path, monkeypatch):
    from core import wb_price_writer as writer
    monkeypatch.setattr(writer, 'ROOT', tmp_path)
    with writer.writer_lock():
        with pytest.raises(BlockingIOError):
            with writer.writer_lock():
                pass


def test_journal_survives_reopen_and_recovers_without_post(journal):
    uid = journal.intent('wb_acc1', [row()])
    journal.update(uid, 'accepted', upload_id=123)
    path = journal.conn.execute('PRAGMA database_list').fetchone()[2]
    reopened = Journal(path)
    client = Mock()
    client.get.side_effect = [{'data': {'status': 3, 'successGoodsNumber': 1}}, {'data': {'historyGoods': []}}]
    assert runner.recover(client, reopened, 'wb_acc1', {1: (1000, 20, 800, '0001')})
    client.request.assert_not_called()


@pytest.mark.parametrize('status', [408, 409, 500, 503])
def test_ambiguous_http_does_not_become_retriable_rejection(journal, status):
    client = Mock()
    client.request.return_value = Mock(status_code=status)
    assert not runner.submit(client, journal, 'wb_acc1', [row()])
    assert journal.pending('wb_acc1')[0]['state'] == 'uncertain'


def test_barrier_exists_before_intent_and_network(tmp_path, monkeypatch):
    path = tmp_path / 'wb_acc1.pending'
    monkeypatch.setattr(runner, 'barrier', lambda account: path)
    journal = Mock()
    journal.intent.side_effect = OSError('disk full')
    client = Mock()
    with pytest.raises(OSError):
        runner.submit(client, journal, 'wb_acc1', [row()])
    assert path.exists()
    client.request.assert_not_called()


def test_stale_legacy_plan_never_lowers_current_price(tmp_path, monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'writer_lock', nullcontext)
    monkeypatch.setattr(runner, 'barrier', lambda account: tmp_path / (account + '.pending'))
    monkeypatch.setattr(runner.guard, 'cogs_map', lambda *args: {1: {'cogs': 100}})
    client = Mock()
    client.goods.return_value = ({1: (1000, 20, 800, '0001')}, {})
    monkeypatch.setattr(runner, 'Client', lambda *args: client)
    ok, error = runner.legacy_push('wb_acc1', [{'nm_id': 1, 'send_price': 1000, 'send_disc': 30,
                                               'target_price': 700, 'price_before': 1000}])
    assert ok and not error
    client.request.assert_not_called()


def test_quarantine_cannot_lower_below_new_common_floor(tmp_path, monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'writer_lock', nullcontext)
    monkeypatch.setattr(runner.guard, 'cogs_map', lambda *args: {1: {'cogs': 100}})
    client = Mock()
    client.goods.return_value = ({1: (1000, 0, 1000, '0001')}, {})
    monkeypatch.setattr(runner, 'Client', lambda *args: client)
    ok, error = runner.quarantine_push('wb_acc1', 1, 600)
    assert not ok and 'floor' in error
    client.request.assert_not_called()


def test_retries_count_only_unconfirmed_items(journal):
    uid = journal.intent('wb_acc1', [row(), row(2)])
    journal.update(uid, 'partial', terminal=True, detail={'unconfirmed': [2], 'matched': [1]})
    assert journal.recent_failures('wb_acc1') == {2: 1}


def test_price_get_retry_is_bounded(monkeypatch):
    response = Mock(status_code=429, headers={'X-Ratelimit-Retry': '1'})
    response.raise_for_status.side_effect = runner.requests.HTTPError(response=response)
    request = Mock(return_value=response)
    monkeypatch.setattr(runner.requests, 'request', request)
    monkeypatch.setattr(runner.time, 'sleep', lambda seconds: None)
    with pytest.raises(runner.requests.HTTPError):
        runner.Client('wb_acc1', 60).get('/test')
    assert request.call_count == 4


def test_failed_account_does_not_cancel_other_account(tmp_path, monkeypatch):
    from contextlib import nullcontext
    import hashlib
    manifest = {'offers': {a: {'1': [offer(3038, 800)]} for a in ('wb_acc1', 'wb_acc2')}}
    manifest['sha256'] = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(runner, 'datetime', Clock)
    monkeypatch.setattr(runner, 'writer_lock', nullcontext)
    calls = []
    def fake_run(args, manifest, account):
        calls.append(account)
        if account == 'wb_acc1':
            raise TimeoutError('network unavailable')
        return {'account': account}
    monkeypatch.setattr(runner, 'run_account', fake_run)
    monkeypatch.setattr(runner.sys, 'argv', ['wb_auto_entry', '--manifest', str(path), '--out', str(tmp_path)])
    assert runner.main() == 1
    assert sorted(calls) == ['wb_acc1', 'wb_acc2']


def test_floor_repair_survives_calendar_outage(tmp_path, monkeypatch):
    client = Mock()
    client.deadline = runner.time.monotonic() + 120
    client.goods.return_value = ({nm: (1000, 90, 100, '0001') for nm in range(1, 5001)}, {})
    client.calendar.side_effect = RuntimeError('calendar unavailable')
    monkeypatch.setattr(runner, 'Client', lambda *args: client)
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner.guard, 'COGS_MIN_KNOWN', 1)
    monkeypatch.setattr(runner.guard, 'cogs_map', lambda *args: {1: {'cogs': 100, 'branch': 'remote'}})
    args = Mock(budget=120, apply=False, at=NOW.isoformat(), out=str(tmp_path), limit=0)
    result = runner.run_account(args, {'offers': {'wb_acc1': {'1': [offer(3038, 800)]}}, 'sha256': 'test'}, 'wb_acc1')
    assert result['selected'] == 1
    assert result['reasons'] == {'protect_floor': 1}
    assert result['calendar_warning']
    client.request.assert_not_called()


def test_quarantine_accepts_cheap_group_price_with_200_profit(tmp_path, monkeypatch):
    from contextlib import nullcontext
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'writer_lock', nullcontext)
    monkeypatch.setattr(runner, 'barrier', lambda account: tmp_path / (account + '.pending'))
    monkeypatch.setattr(runner.guard, 'cogs_map', lambda *args: {1: {'cogs': 100}})
    monkeypatch.setattr(runner.time, 'sleep', lambda seconds: None)
    client = Mock()
    client.goods.side_effect = [({1: (1000, 0, 1000, '0001')}, {}), ({1: (800, 0, 800, '0001')}, {})]
    client.request.return_value = Mock(status_code=200, json=lambda: {'data': {'id': 123}})
    client.get.side_effect = [{'data': {'status': 3}}, {'data': {'historyGoods': []}}]
    client.remaining.return_value = 60
    monkeypatch.setattr(runner, 'Client', lambda *args: client)
    ok, error = runner.quarantine_push('wb_acc1', 1, 800)
    assert ok and not error
    assert client.request.call_count == 1


def test_recover_only_does_not_plan_or_send_prices(tmp_path, monkeypatch):
    client = Mock()
    client.goods.return_value = ({nm: (1000, 20, 800, '0001') for nm in range(1, 5001)}, {})
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'Client', lambda *args: client)
    args = Mock(apply=False, recover_only=True, budget=60)
    result = runner.run_account(args, {}, 'wb_acc1')
    assert result['price_posts'] == 0
    client.calendar.assert_not_called()
    client.request.assert_not_called()


def test_discount_drop_during_preparation_is_replanned_in_same_run(tmp_path, monkeypatch):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(runner, 'datetime', Clock)
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'barrier', lambda account: tmp_path / (account + '.pending'))
    monkeypatch.setattr(runner.time, 'sleep', lambda seconds: None)
    monkeypatch.setattr(runner.guard, 'COGS_MIN_KNOWN', 1)
    monkeypatch.setattr(runner.guard, 'cogs_map', lambda *args: {1: {'cogs': 100, 'branch': 'remote'}})
    (tmp_path/'wb_acc1_stocks.json').write_text(json.dumps({'captured': runner.time.time(), 'stocks': {'1': 4}}))
    client = Mock()
    client.deadline = runner.time.monotonic() + 120
    before = {nm: (1000, 0, 1000, '0001') for nm in range(1, 5001)}
    changed = dict(before, **{})
    changed[1] = (1000, 50, 500, '0001')
    after = dict(before, **{})
    after[1] = (1000, 20, 800, '0001')
    client.goods.side_effect = [(before, {}), (changed, {}), (after, {})]
    client.calendar.return_value = calendar([offer(3038, 800)])
    client.remaining.return_value = 90
    client.request.return_value = Mock(status_code=200, json=lambda: {'data': {'id': 123}})
    client.get.side_effect = [{'data': {'status': 3}}, {'data': {'historyGoods': []}}]
    monkeypatch.setattr(runner, 'Client', lambda *args: client)
    args = Mock(apply=True, recover_only=False, pilot_active=False, budget=120, at=None, limit=0, out=str(tmp_path))
    result = runner.run_account(args, {'offers': {'wb_acc1': {'1': [offer(3038, 800)]}}, 'sha256': 'test'}, 'wb_acc1')
    assert result['cas_skipped'] == 1
    assert result['urgent_replanned'] == 1
    assert result['verified_products'] == 1
    assert client.request.call_args.kwargs['json']['data'][0]['discount'] == 20


@pytest.mark.parametrize('complete', [False, True])
def test_stock_refresh_keeps_valid_snapshot_until_replacement_complete(tmp_path, monkeypatch, complete):
    from contextlib import nullcontext
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'writer_lock', lambda *args: nullcontext())
    old = {'captured': runner.time.time()-1260, 'stocks': {'1': 5}, 'complete': True, 'next_offset': 1000}
    path = tmp_path/'wb_acc1_stocks.json'
    path.write_text(json.dumps(old))
    client = Mock()
    def scan(start, initial, checkpoint):
        checkpoint({2: 4}, 1000, complete)
        if not complete:
            raise TimeoutError('bounded progress')
    client.stocks.side_effect = scan
    monkeypatch.setattr(runner, 'Client', lambda *args: client)
    assert runner.warm_stocks(Mock(account=['wb_acc1'], budget=60)) == 0
    published = json.loads(path.read_text())
    assert published['stocks'] == ({'2': 4} if complete else old['stocks'])
    assert json.loads((tmp_path/'wb_acc1_stocks_build.json').read_text())['next_offset'] == 1000


@pytest.mark.parametrize('state,exit_code', [({'pending_batches': 1}, 1), ({'deferred': 1}, 1), ({}, 0)])
def test_only_unfinished_wave_requests_systemd_retry(tmp_path, monkeypatch, state, exit_code):
    from contextlib import nullcontext
    import hashlib
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    monkeypatch.setattr(runner, 'datetime', Clock)
    manifest = {'offers': {a: {'1': [offer(3038, 800)]} for a in ('wb_acc1', 'wb_acc2')}}
    manifest['sha256'] = hashlib.sha256(json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    path = tmp_path/'manifest.json'
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(runner, 'ROOT', tmp_path)
    monkeypatch.setattr(runner, 'writer_lock', nullcontext)
    monkeypatch.setattr(runner, 'run_account', lambda args, manifest, acc: {'account': acc, **(state if acc == 'wb_acc1' else {})})
    monkeypatch.setattr(runner.sys, 'argv', ['wb_auto_entry', '--apply', '--manifest', str(path), '--out', str(tmp_path)])
    assert runner.main() == exit_code
