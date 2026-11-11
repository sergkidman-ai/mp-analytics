# поток: mkt / ozon
"""Three-hour stop-only guard for exactly the four bound acc2 W1 campaigns."""
import argparse
import collections
import datetime as dt
from decimal import Decimal as D
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path('/opt/mp-analytics')
sys.path.insert(0, str(ROOT))
TZ = ZoneInfo('Europe/Moscow')


def utcnow():
    return dt.datetime.now(dt.timezone.utc)


def number(x):
    return D(str(x or 0).replace(' ', '').replace('\xa0', '').replace(',', '.'))


def save(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2, default=str) + '\n')
    temp.replace(path)


def decision(spend, own_ad_orders, model_ad_orders, own_general, halo_general, orders_complete, policy):
    """Risk decisions, not estimates of causal advertising benefit."""
    if own_ad_orders > 0 or model_ad_orders > 0:
        return None
    if own_general > 0 or halo_general > 0:
        return None
    if orders_complete and spend >= number(policy.get('automatic_stop_rub', 1000)):
        return 'ZERO_CONFIRMED_ORDERS_1000'
    if orders_complete and spend >= number(policy['zero_orders_spend_rub']):
        return 'ZERO_OWN_AND_HALO_ORDERS'
    if not orders_complete and spend >= number(policy['emergency_without_seller_orders_rub']):
        return 'PROVISIONAL_STOP_SELLER_ORDERS_UNAVAILABLE'
    return None


def record_signal(config, text):
    """Persist authorized risk signals without sending external messages."""
    path = Path(config['runtime_dir']) / 'signals.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        f.write(json.dumps({'account': 'oz_acc2', 'at_utc': utcnow().isoformat(), 'text': text,
                            'delivery': 'BI_AND_LOCAL_EVENT', 'external_message_sent': False}, ensure_ascii=False) + '\n')
        f.flush(); os.fsync(f.fileno())
    return {'ok': True, 'channel': 'BI_AND_LOCAL_EVENT', 'external_message_sent': False}


def reference_week(anchor, now):
    first = dt.datetime.fromisoformat(anchor).astimezone(TZ).date()
    current = now.astimezone(TZ).date()
    start = first + dt.timedelta(days=max(0, (current - first).days // 7) * 7)
    return start, start + dt.timedelta(days=6)


def weekly_budget_signal(api, binding, config, state, latest, execute):
    path = config.get('activation_events_path')
    events = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()] if path and Path(path).exists() else []
    starts = [e['at_utc'] for e in events if e.get('wave') == 1 and e.get('phase') == 'RUNNING_CONFIRMED']
    anchor_source = 'CONFIRMED_ACTIVATION_EVENT'
    if not starts:
        starts = [state.get('first_observed_running', {}).get(c['campaign_id']) for c in binding['campaigns'] if c['wave'] == 1]
        starts = [s for s in starts if s]
        anchor_source = 'FIRST_OBSERVED_RUNNING_NOT_EXACT_START'
    if not starts:
        latest['wave1_weekly_budget'] = {'reference_rub': 12500, 'status': 'NOT_STARTED'}
        return
    now = utcnow(); first, last = reference_week(min(starts), now)
    ids = [e['campaign_id'] for e in binding['campaigns'] if e['wave'] == 1]
    z = api.call('GET', '/api/client/statistics/daily/json', params={
        'campaignIds': ids, 'dateFrom': str(first), 'dateTo': str(min(last, now.astimezone(TZ).date()))})
    assert isinstance(z.get('rows'), list)
    assert all(str(r['id']) in ids for r in z['rows']), 'Weekly reference must contain only wave1 campaigns'
    spend = sum((number(r['moneySpent']) for r in z['rows']), D(0))
    reference = number(config['owner_weekly_reference_rub'])
    info = {'from_msk': str(first), 'through_msk': str(last), 'spent_rub': str(spend),
            'reference_rub': str(reference), 'status': 'EXCEEDED' if spend > reference else 'WITHIN_REFERENCE',
            'reference_is_hard_cap': False, 'automatic_budget_pause': False, 'anchor_source': anchor_source}
    latest['wave1_weekly_budget'] = info
    if spend <= reference:
        return
    week_key = str(first); previous = state.setdefault('budget_signals', {}).get(week_key, {})
    info['assessment'] = {'general_orders_window': latest.get('interpretation', 'current and previous Moscow calendar days'),
        'account_orders': latest.get('account_unique_orders'),
        'account_ordered_value_rub': latest.get('account_ordered_value_rub'),
        'account_CPC_by_date': latest.get('account_cpc_spend_by_date_rub'),
        'largest_SKU_expenses': sorted(latest.get('sku_metrics', []), key=lambda r: number(r['spend']), reverse=True)[:5],
        'risk_actions': latest.get('actions', []), 'causal_effect_claimed': False}
    save(Path(config['runtime_dir']) / 'budget_signals' / f'{week_key}.json', {'checked_at_utc': now.isoformat(), **info})
    # Signal first crossing, then a further 25% of the reference; no repeat after confirmed delivery.
    last_sent = number(previous.get('spent_at_last_delivery_rub', 0))
    if previous.get('delivery_confirmed') and spend < last_sent + reference * D('.25'):
        return
    account_orders = latest.get('account_unique_orders')
    account_value = latest.get('account_ordered_value_rub')
    text = (f'Ozon oz_acc2: первая волна превысила ориентир недели {first}–{last}.\n'
            f'Расход {spend:.2f} ₽ при ориентире {reference:.2f} ₽.\n'
            f'Общие заказы аккаунта за текущий/предыдущий дни: {account_orders if account_orders is not None else "неизвестно"}; '
            f'стоимость заказанного: {account_value if account_value is not None else "неизвестно"} ₽.\n'
            'Оценка SKU/склеек, крупнейшие расходы и решения защиты сохранены в BI. '
            'По одному превышению вывод об эффекте рекламы не делается.\n'
            'https://bi.beyindi.com/api/wb-bids/reports/ozon-acc2-monthly-experiment-2026-11-08')
    delivery = {'ok': False, 'error': 'dry_run'}
    if execute:
        from functools import partial
        send_sergey = partial(record_signal, config)
        delivery = send_sergey(text)
    info['delivery'] = delivery
    state['budget_signals'][week_key] = {'delivery_confirmed': bool(delivery.get('ok')),
        'spent_at_last_delivery_rub': str(spend) if delivery.get('ok') else str(last_sent),
        'last_attempt_at_utc': now.isoformat(), 'message_id': delivery.get('message_id')}
    save(Path(config['runtime_dir']) / 'budget_signals' / f'{week_key}.json', {'checked_at_utc': now.isoformat(), **info})


def validate_bindings(config):
    assert config['account'] == 'oz_acc2' and config['activation_allowed'] is False
    path = Path(config['bindings_path'])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == config['bindings_sha256'], 'Binding hash changed'
    b = json.loads(path.read_text())
    assert b['account'] == 'oz_acc2' and b.get('all_four_inactive_verified') is True
    assert b['activation_allowed'] is False
    mp = Path(config['family_maps_path'])
    assert hashlib.sha256(mp.read_bytes()).hexdigest() == config['family_maps_sha256'], 'Family map hash changed'
    assert json.loads(mp.read_text())['account'] == 'oz_acc2'
    forbidden = set(b['forbidden_existing_campaign_ids'])
    entries = b['campaigns']; ids = [str(c['campaign_id']) for c in entries]
    assert len(entries) == 4 and len(ids) == len(set(ids))
    assert all(cid.isdigit() and cid not in forbidden for cid in ids)
    return b


class Api:
    def __init__(self, binding, out):
        assert binding['account'] == 'oz_acc2'
        from collectors.ozon_ads import _token, PERF
        import requests
        self.requests = requests; self.base = PERF
        self.headers = {'Authorization': 'Bearer ' + _token('oz_acc2')}
        self.ids = {c['campaign_id'] for c in binding['campaigns']}
        self.out = out

    def call(self, method, path, body=None, params=None):
        assert '/activate' not in path, 'Guard can never activate advertising'
        allowed_read = method == 'GET' and path in ['/api/client/campaign', '/api/client/statistics/daily/json']
        allowed_read |= method == 'POST' and path == '/api/client/statistics/products/sku'
        allowed_stop = method == 'POST' and any(path == f'/api/client/campaign/{cid}/deactivate' for cid in self.ids)
        assert allowed_read or allowed_stop, 'Endpoint outside stop-only guard scope'
        for attempt in range(4):
            r = self.requests.request(method, self.base + path, headers=self.headers, json=body,
                                      params=params, timeout=60)
            if r.status_code != 429:
                break
            time.sleep(min(15, max(3, float(r.headers.get('Retry-After') or 5))))
        response = r.json() if r.content else {}
        raw_file = self.out / 'raw' / f'{utcnow():%Y%m%d_%H%M%S_%f}_performance.json'
        save(raw_file, {'at_utc': utcnow().isoformat(), 'method': method, 'path': path,
                        'request': body, 'params': params, 'http': r.status_code, 'response': response})
        with (self.out / 'api_operations.jsonl').open('a') as f:
            f.write(json.dumps({'at_utc': utcnow().isoformat(), 'method': method, 'path': path,
                                'request': body, 'params': params, 'http': r.status_code,
                                'raw_response_file': str(raw_file)}) + '\n')
        r.raise_for_status()
        return response

    def campaigns(self):
        return {str(c['id']): c for c in self.call('GET', '/api/client/campaign')['list']}


def account_cpc_profile(api, campaigns, day_from, day_to):
    ids = [cid for cid, c in campaigns.items() if c.get('advObjectType') == 'SKU']
    daily = collections.defaultdict(lambda: D(0))
    for i in range(0, len(ids), 10):
        result = api.call('GET', '/api/client/statistics/daily/json', params={
            'campaignIds': ids[i:i + 10], 'dateFrom': str(day_from), 'dateTo': str(day_to)})
        assert isinstance(result.get('rows'), list)
        for r in result['rows']:
            daily[r['date'][:10]] += number(r['moneySpent'])
    return {'account_cpc_spend_by_date_rub': {d: str(v) for d, v in daily.items()},
            'account_cpo_spend_rub': None, 'account_cpo_spend_status': 'NOT_MEASURED_NOT_ZERO'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args()
    config = json.loads(args.config.read_text())
    b = validate_bindings(config)
    out = Path(config['runtime_dir']); out.mkdir(parents=True, exist_ok=True)
    lock = (out / 'guard.lock').open('a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print('GUARD_ALREADY_RUNNING'); return 0
    state_path = out / 'state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'stopped_skus': [], 'first_observed_running': {}}
    now = utcnow(); api = Api(b, out); entries = {c['campaign_id']: c for c in b['campaigns']}
    latest = {'checked_at_utc': now.isoformat(), 'execute_enabled': args.execute,
              'activation_calls': 0, 'policy': config['risk_policy'], 'campaigns': [], 'actions': []}

    def publish_status():
        if config.get('public_status_path'):
            public = {k: v for k, v in latest.items() if k != 'sku_metrics'}
            public['checked_sku_rows'] = len(latest.get('sku_metrics', []))
            save(Path(config['public_status_path']), public)

    def event(record):
        with (out / 'stop_events.jsonl').open('a') as f:
            f.write(json.dumps({'at_utc': utcnow().isoformat(), **record}, ensure_ascii=False, default=str) + '\n')
            f.flush(); os.fsync(f.fileno())

    def stop_campaign(cid, reason):
        record = {'campaign_id': cid, 'title': entries[cid]['title'], 'reason': reason,
                  'action': 'DEACTIVATE_CAMPAIGN', 'executed': args.execute}
        event({**record, 'phase': 'INTENT'})
        if args.execute:
            api.call('POST', f'/api/client/campaign/{cid}/deactivate')
            current = api.campaigns()
            assert current[cid]['state'] == 'CAMPAIGN_STATE_INACTIVE', 'Stop not confirmed'
        event({**record, 'phase': 'CONFIRMED' if args.execute else 'DRY_RUN'})
        latest['actions'].append(record)

    try:
        current = api.campaigns()
        assert all(cid in current and current[cid]['title'] == e['title'] for cid, e in entries.items()), 'Bound identity mismatch'
        active = [cid for cid in entries if current[cid]['state'] == 'CAMPAIGN_STATE_RUNNING']
        latest['campaigns'] = [{'campaign_id': cid, 'title': e['title'], 'state': current[cid]['state']} for cid, e in entries.items()]
        state['last_active_campaigns'] = active
        if not active:
            latest['status'] = 'WAITING_FOR_ACTIVATION_ALL_BOUND_CAMPAIGNS_INACTIVE'
            weekly_budget_signal(api, b, config, state, latest, args.execute)
            state.pop('telemetry_error_since', None)
            save(state_path, state); save(out / 'latest.json', latest)
            publish_status()
            print('GUARD_OK: 4 bound acc2 campaigns inactive; activation not attempted')
            return 0
        for cid in active:
            state['first_observed_running'].setdefault(cid, now.isoformat())
        maps = json.loads(Path(config['family_maps_path']).read_text())
        aliases = maps['canonical_by_alias']; models = maps['model_by_sku_alias']; articles = maps['article_family_by_sku_alias']
        day_to = now.astimezone(TZ).date(); day_from = day_to - dt.timedelta(days=1)
        telemetry = []
        for i in range(0, len(active), 10):
            z = api.call('POST', '/api/client/statistics/products/sku',
                         {'campaignIds': active[i:i + 10], 'dateFrom': str(day_from), 'dateTo': str(day_to)})
            assert isinstance(z.get('rows'), list), 'No validated SKU telemetry rows'
            telemetry.extend(z['rows'])
        ad = collections.defaultdict(lambda: {'spend': D(0), 'orders': D(0), 'model_orders': D(0)})
        known = {}
        for cid, e in entries.items():
            rb = json.loads((Path(config['bindings_path']).parent / e['readback_file']).read_text())
            known[cid] = {str(p['sku']): p for p in rb['products']}
        for r in telemetry:
            cid = str(r['campaignId'])
            raw_sku = str(r['sku']); sku = aliases.get(raw_sku, raw_sku)
            assert cid in active, 'Telemetry contains an unrelated campaign'
            if sku not in known[cid]:
                raise AssertionError('Telemetry contains a SKU outside the fixed campaign pool')
            assert str(day_from) <= r['date'][:10] <= str(day_to)
            row = ad[(cid, sku)]
            row['spend'] += number(r['expense']); row['orders'] += number(r['orders']); row['model_orders'] += number(r['modelOrders'])
        # Campaign totals prevent a partial SKU response being mistaken for zero spend.
        reference = collections.defaultdict(lambda: D(0))
        for i in range(0, len(active), 10):
            z = api.call('GET', '/api/client/statistics/daily/json', params={
                 'campaignIds': active[i:i + 10], 'dateFrom': str(day_from), 'dateTo': str(day_to)})
            assert isinstance(z.get('rows'), list)
            for r in z['rows']:
                reference[str(r['id'])] += number(r['moneySpent'])
        for cid in active:
            sku_spend = sum((v['spend'] for (c, _), v in ad.items() if c == cid), D(0))
            if abs(sku_spend - reference[cid]) > max(D(50), reference[cid] * D('.2')) and reference[cid] >= number(config['risk_policy']['zero_orders_spend_rub']):
                latest.setdefault('risk_signals', []).append({'campaign_id': cid, 'reason': 'SPEND_WITH_INCOMPLETE_SKU_TELEMETRY', 'campaign_spend_rub': str(reference[cid])})
            assert abs(sku_spend - reference[cid]) <= max(D(50), reference[cid] * D('.2')), 'Campaign/SKU spend coverage mismatch'
        order_sets = collections.defaultdict(set); account_values = D(0); orders_complete = True
        actual_starts = {}
        event_path = config.get('activation_events_path')
        if event_path and Path(event_path).exists():
            for line in Path(event_path).read_text().splitlines():
                e = json.loads(line)
                if e.get('phase') == 'RUNNING_CONFIRMED':
                    cid = e['campaign_id']; at = dt.datetime.fromisoformat(e['at_utc'])
                    actual_starts[cid] = min(actual_starts.get(cid, at), at)
        risk_orders = {cid: collections.defaultdict(set) for cid in active}
        try:
            from collectors.ozon_postings import fetch_postings
            from core.ozon_posting import product_price
            for scheme in ['fbs', 'fbo']:
                posts = fetch_postings('oz_acc2', scheme, f'{day_from}T00:00:00+03:00',
                      now.astimezone(TZ).isoformat(), limit=100, window_budget=240)
                save(out / 'raw' / f'{now:%Y%m%d_%H%M%S}_postings_{scheme}.json', {
                     'from_msk': str(day_from), 'through_msk': now.astimezone(TZ).isoformat(),
                     'captured_at_utc': utcnow().isoformat(), 'complete': True, 'postings': posts})
                for post in posts:
                    if post['status'].lower().startswith('cancel'):
                        continue
                    order_time = dt.datetime.fromisoformat((post.get('in_process_at') or post['created_at']).replace('Z', '+00:00'))
                    oid = str(post['order_id']); order_sets[('account', 'oz_acc2')].add(oid)
                    for p in post['products']:
                        raw = str(p['sku']); sku = aliases.get(raw, raw)
                        order_sets[('sku', sku)].add(oid)
                        if models.get(raw): order_sets[('model', models[raw])].add(oid)
                        if articles.get(raw): order_sets[('article', articles[raw])].add(oid)
                        for cid in active:
                            # Pre-activation organic orders must not be counted as post-launch outcomes.
                            if cid not in actual_starts or order_time >= actual_starts[cid]:
                                risk_orders[cid][('sku', sku)].add(oid)
                                if models.get(raw): risk_orders[cid][('model', models[raw])].add(oid)
                                if articles.get(raw): risk_orders[cid][('article', articles[raw])].add(oid)
                        account_values += product_price(p) * number(p['quantity'])
        except Exception as exc:
            orders_complete = False; latest['seller_orders_error'] = type(exc).__name__
        latest.update(from_msk=str(day_from), through_msk=str(day_to), general_orders_complete=orders_complete,
              account_unique_orders=len(order_sets[('account', 'oz_acc2')]) if orders_complete else None,
              account_ordered_value_rub=str(account_values) if orders_complete else None,
              new_campaign_CPC_spend_rub=str(sum((v['spend'] for v in ad.values()), D(0))),
              interpretation='current and previous Moscow calendar days, not an exact rolling 24-hour window')
        latest.update(account_cpc_profile(api, current, day_from, day_to))
        stopped = {tuple(x) for x in state['stopped_skus']}; metrics = []
        pending_stops = collections.defaultdict(list)
        for (cid, sku), row in ad.items():
            scoped_orders = risk_orders[cid]
            halo = scoped_orders[('model', models.get(sku, ''))] | scoped_orders[('article', articles.get(sku, ''))]
            own_general = len(scoped_orders[('sku', aliases.get(sku, sku))]); halo_general = len(halo)
            own_orders_complete = orders_complete and bool(models.get(sku))
            reason = decision(row['spend'], row['orders'], row['model_orders'], own_general,
                              halo_general, own_orders_complete, config['risk_policy'])
            metrics.append({'campaign_id': cid, 'sku': sku, **{k: str(v) for k, v in row.items()},
                            'own_general_orders': own_general if orders_complete else None,
                            'halo_general_orders': halo_general if orders_complete else None, 'stop_reason': reason,
                            'general_orders_not_before_utc': actual_starts[cid].isoformat() if cid in actual_starts else None})
            if not reason or (cid, sku) in stopped:
                continue
            record = {'campaign_id': cid, 'sku': sku, 'bid_before_micro_rub': known[cid][sku]['bid'],
                      'reason': reason, 'spend_rub': str(row['spend']), 'action': 'SKU_RISK_SIGNAL_KEEP_COMPOSITION',
                      'executed': False, 'automatic_reenable': False}
            pending_stops[cid].append(record)
        for cid, records in pending_stops.items():
            # Owner forbids removal. No per-SKU pause was confirmed in CPC API.
            # Only reversible campaign pause is available; gated by the explicit policy choice.
            event({'campaign_id': cid, 'action': 'SKU_RISK_SIGNAL_KEEP_COMPOSITION',
                   'sku_reasons': records, 'phase': 'DETECTED'})
            stop_records = [r for r in records if r['reason'] == 'ZERO_CONFIRMED_ORDERS_1000']
            if stop_records and config.get('sku_risk_action') == 'PAUSE_CAMPAIGN_KEEP_COMPOSITION':
                stop_campaign(cid, 'ZERO_CONFIRMED_ORDERS_1000')
                for record in stop_records:
                    record['action'] = 'PAUSE_CAMPAIGN_KEEP_ALL_SKUS'
                    record['executed'] = args.execute
                    latest['actions'].append(record)
            latest.setdefault('sku_risk_signals', []).extend(r for r in records if r not in stop_records or config.get('sku_risk_action') != 'PAUSE_CAMPAIGN_KEEP_COMPOSITION')
            if args.execute and config.get('sku_risk_action') in ['PAUSE_CAMPAIGN_KEEP_COMPOSITION', 'SIGNAL_ONLY_SKU']:
                sent = state.setdefault('sku_risk_alerts', {})
                fresh = [r for r in records if f"{cid}:{r['sku']}" not in sent or
                         number(r['spend_rub']) >= number(sent[f"{cid}:{r['sku']}"]) + D(300)]
                if fresh:
                    from functools import partial
                    send_sergey = partial(record_signal, config)
                    action = 'РК поставлена на паузу: подтверждены 1000 ₽ без заказов; все SKU сохранены.' if stop_records and config['sku_risk_action'] == 'PAUSE_CAMPAIGN_KEEP_COMPOSITION' else 'Только сигнал: условия автоматической паузы от 1000 ₽ не подтверждены.'
                    lines = [f"SKU {r['sku']}: расход {r['spend_rub']} ₽, {r['reason']}" for r in fresh[:10]]
                    delivery = send_sergey('Ozon: риск расхода без заказов. РК ' + cid + '\n' + action + '\n' + '\n'.join(lines) + '\nhttps://bi.beyindi.com/api/wb-bids/reports/ozon-acc2-monthly-experiment-2026-11-08')
                    latest.setdefault('sku_risk_deliveries', []).append({'campaign_id': cid, **delivery})
                    if delivery.get('ok'):
                        for record in fresh:
                            sent[f"{cid}:{record['sku']}"] = record['spend_rub']
        state['stopped_skus'] = sorted([list(x) for x in stopped])
        state['last_successful_telemetry_at'] = now.isoformat(); state.pop('telemetry_error_since', None)
        latest['status'] = 'CHECKED'; latest['sku_metrics'] = metrics
        weekly_budget_signal(api, b, config, state, latest, args.execute)
        save(out / 'snapshots' / f'{now:%Y%m%d_%H%M%S}.json', latest)
        save(out / 'latest.json', latest); save(state_path, state)
        publish_status()
        print('GUARD_OK', len(active), 'active campaigns', len(metrics), 'SKU metrics, stops', len(latest['actions']))
        return 0
    except Exception as exc:
        latest['status'] = 'TELEMETRY_ERROR'; latest['error_type'] = type(exc).__name__; latest['error'] = str(exc)[:220]
        since = state.setdefault('telemetry_error_since', now.isoformat())
        elapsed = (now - dt.datetime.fromisoformat(since)).total_seconds()
        previous = state.get('last_telemetry_alert_at')
        if args.execute and (not previous or (now - dt.datetime.fromisoformat(previous)).total_seconds() >= 6 * 3600):
            from functools import partial
            send_sergey = partial(record_signal, config)
            delivery = send_sergey('Ozon: сигнал о потере данных контроля. Автоматической паузы по этому условию нет.\n'
                                  + latest['error_type'] + ': ' + latest['error'] + '\n'
                                  + f'Время от первой ошибки: {elapsed / 3600:.1f} ч.\n'
                                  + 'https://bi.beyindi.com/api/wb-bids/reports/ozon-acc2-monthly-experiment-2026-11-08')
            latest['telemetry_signal_delivery'] = delivery
            if delivery.get('ok'):
                state['last_telemetry_alert_at'] = now.isoformat()
        try:
            weekly_budget_signal(api, b, config, state, latest, args.execute)
        except Exception as budget_error:
            latest['budget_signal_error_type'] = type(budget_error).__name__
        save(out / 'latest.json', latest); save(state_path, state)
        publish_status()
        print('GUARD_TELEMETRY_ERROR', type(exc).__name__, 'new campaign pause actions', len(latest['actions']))
        return 1


if __name__ == '__main__':
    sys.exit(main())
