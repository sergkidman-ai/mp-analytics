# поток: mkt / ozon
"""Owner-authorized, scoped, resumable Seller unarchive queue for oz_acc2 only."""
import argparse
import csv
import datetime as dt
import fcntl
import hashlib
import html
import json
import os
import re
import sys
import time
from decimal import Decimal as D
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path('/opt/mp-analytics')
sys.path.insert(0, str(ROOT))
import requests
from collectors.ozon import _headers, PRODUCT_INFO_URL
from collectors.ozon_attributes import ATTR_URL

O = ROOT / 'docs/reports/ozon_dsprint_restore_20261108'
TZ = ZoneInfo('Europe/Moscow')
SELLER = 'https://api-seller.ozon.ru'
PINNED_MANIFEST_SHA256 = '0d1646fe523375832050ca6e3b2f2551af1d7e1c6e7dfbb6f62008c6742077f1'
NOW = lambda: dt.datetime.now(dt.timezone.utc).isoformat()


def read(p):
    return json.loads(p.read_text())


def save(p, value):
    tmp = p.with_suffix(p.suffix + '.tmp')
    with tmp.open('w') as f:
        json.dump(value, f, ensure_ascii=False, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
    tmp.replace(p)


def quota_day():
    return (dt.datetime.now(TZ) - dt.timedelta(hours=3)).date().isoformat()


def eligible(info, attrs):
    brands = [v.get('value') for a in attrs.get('attributes', []) if a['id'] == 85 for v in a.get('values') or []]
    stocks = info.get('stocks') or {}
    free = sum((max(D(0), D(str(s.get('present') or 0)) - D(str(s.get('reserved') or 0))) for s in stocks.get('stocks') or []), D(0))
    created = dt.datetime.fromisoformat(info['created_at'].replace('Z', '+00:00')).astimezone(TZ)
    reasons = []
    if brands != ['ДС принт']: reasons.append('BRAND_CHANGED')
    if not re.match(r'^[0-9]{5}', info['offer_id']): reasons.append('NOT_CHILD')
    if created.year != 2026: reasons.append('NOT_CREATED_2026')
    if stocks.get('has_stock') is not True or free <= 0: reasons.append('NO_FREE_STOCK')
    return reasons


class Queue:
    def __init__(self, execute):
        self.execute = execute
        self.manifest = read(O / 'restore_manifest.json')
        assert hashlib.sha256((O / 'restore_manifest.json').read_bytes()).hexdigest() == PINNED_MANIFEST_SHA256
        self.policy = read(O / 'restore_policy.json')
        assert self.policy['account'] == self.manifest['account'] == 'oz_acc2'
        assert self.policy['manifest_sha256'] == PINNED_MANIFEST_SHA256
        assert self.policy['max_auto_restored_per_quota_day'] == 100
        assert self.policy['max_batch_size'] == 10 and self.policy['first_batch_size'] == 1
        assert hashlib.sha256((O / 'owner_authorization.json').read_bytes()).hexdigest() == self.manifest['owner_authorization_sha256']
        self.products = {r['product_id']: r for r in self.manifest['products']}
        self.ids = set(self.products)
        self.state = read(O / 'queue_state.json') if (O / 'queue_state.json').exists() else {
            'account': 'oz_acc2', 'manifest_sha256': PINNED_MANIFEST_SHA256,
            'products': {p: {'state': 'PENDING'} for p in self.ids}, 'quota_blocked_day': None,
            'request_counter': 0, 'runs': 0}
        assert self.state['manifest_sha256'] == PINNED_MANIFEST_SHA256 and set(self.state['products']) == self.ids
        self.day = quota_day()
        self.H = _headers('oz_acc2')
        self.raw = O / 'runtime_raw'; self.raw.mkdir(exist_ok=True)
        self.verified_this_run = 0
        self.stop_reason = None

    def persist(self):
        self.state['updated_at_utc'] = NOW()
        save(O / 'queue_state.json', self.state)

    def event(self, value):
        with (O / 'operations.jsonl').open('a') as f:
            f.write(json.dumps({'at_utc': NOW(), 'account': 'oz_acc2', **value}, ensure_ascii=False) + '\n')
            f.flush(); os.fsync(f.fileno())

    def request(self, path, body, mutation=False):
        allowed = ['/v3/product/info/list', '/v4/product/info/attributes', '/v1/roles']
        if mutation:
            assert self.execute and path == '/v1/product/unarchive'
            assert set(body) == {'product_id'} and 1 <= len(body['product_id']) <= 10
            assert len(set(body['product_id'])) == len(body['product_id'])
            assert {str(p) for p in body['product_id']} <= self.ids
        else:
            assert path in allowed
        self.state['request_counter'] += 1
        seq = self.state['request_counter']
        req_id = f'{self.day}-{seq:06d}'
        # Persist an intent before a mutation. Restart always reads actual card flags first.
        if mutation:
            self.event({'event': 'UNARCHIVE_INTENT', 'request_id': req_id, 'body': body})
            for pid in body['product_id']:
                self.state['products'][str(pid)]['last_intent_id'] = req_id
            self.persist()
        try:
            response = requests.post(SELLER + path, headers=self.H, json=body, timeout=90)
            try: payload = response.json()
            except ValueError: payload = {'non_json_response': response.text[:500]}
            result = {'request_id': req_id, 'account': 'oz_acc2', 'at_utc': NOW(), 'method': 'POST',
                      'path': path, 'body': body, 'http': response.status_code, 'response': payload}
        except requests.RequestException as exc:
            result = {'request_id': req_id, 'account': 'oz_acc2', 'at_utc': NOW(), 'method': 'POST',
                      'path': path, 'body': body, 'http': None, 'error_type': type(exc).__name__}
        save(self.raw / (req_id + '.json'), result)
        if mutation:
            self.event({'event': 'UNARCHIVE_RESPONSE', 'request_id': req_id, 'http': result['http'],
                        'response_file': str((self.raw / (req_id + '.json')).relative_to(O))})
        self.persist()
        return result

    def get_info(self, ids):
        z = self.request('/v3/product/info/list', {'product_id': ids})
        if z['http'] != 200: raise RuntimeError('Product info read failed; no new writes permitted')
        rows = {str(r['id']): r for r in z['response'].get('items', [])}
        if set(rows) != set(ids): raise RuntimeError('Incomplete info coverage; no new writes permitted')
        return rows

    def get_attrs(self, ids):
        z = self.request('/v4/product/info/attributes', {'filter': {'product_id': ids, 'visibility': 'ALL'},
                                                       'limit': 1000, 'sort_dir': 'ASC'})
        if z['http'] != 200: raise RuntimeError('Attribute read failed; no new writes permitted')
        rows = {str(r['id']): r for r in z['response'].get('result', [])}
        if set(rows) != set(ids): raise RuntimeError('Incomplete brand coverage; no new writes permitted')
        return rows

    def count_day(self, all_archives=False):
        return sum(r.get('restore_quota_day') == self.day and
                   (all_archives or r.get('restored_from_autoarchive', self.products[pid]['autoarchived']))
                   for pid,r in self.state['products'].items())

    def active(self, pid, via_response=False, was_autoarchived=None):
        st = self.state['products'][pid]
        st['state'] = 'RESTORED_VERIFIED' if via_response else 'ALREADY_ACTIVE_VERIFIED'
        st['verified_at_utc'] = NOW()
        if via_response:
            st['restore_quota_day'] = self.day
            st['restored_from_autoarchive'] = self.products[pid]['autoarchived'] if was_autoarchived is None else bool(was_autoarchived)
            self.verified_this_run += 1
        elif st.get('last_intent_id'):
            # Conservatively account for an interrupted mutation once its flags
            # are observed active, even if the response was lost before saving.
            st['restore_quota_day'] = st['last_intent_id'][:10]
            st['restored_from_autoarchive'] = st.get('restored_from_autoarchive', self.products[pid]['autoarchived'])
            st['active_after_interrupted_intent'] = True
        self.event({'event': st['state'], 'product_id': pid, 'offer_id': self.products[pid]['offer_id']})

    def apply(self, ids):
        if not ids or self.stop_reason: return
        # Final strict read immediately before each mutation, including split retries.
        infos = self.get_info(ids); attrs = self.get_attrs(ids); valid = []
        for pid in ids:
            if not (infos[pid].get('is_archived') or infos[pid].get('is_autoarchived')):
                self.active(pid); continue
            reasons = eligible(infos[pid], attrs[pid])
            if reasons:
                self.state['products'][pid].update(state='DEFERRED_FILTER', filter_reasons=reasons)
            else:
                valid.append(pid)
        self.persist()
        if not valid: return
        if quota_day() != self.day:
            self.stop_reason = 'QUOTA_DAY_CHANGED'; return
        limit = self.policy['max_auto_restored_per_quota_day'] - self.count_day()
        filtered = []
        for pid in valid:
            if infos[pid].get('is_autoarchived'):
                if limit <= 0: continue
                limit -= 1
            filtered.append(pid)
        valid = filtered
        if not valid:
            self.stop_reason = 'LOCAL_AUTO_DAILY_CAP'; return
        z = self.request('/v1/product/unarchive', {'product_id': [int(p) for p in valid]}, mutation=True)
        # A 200 or true result is never treated as restoration without actual flag readback.
        after = self.get_info(valid)
        unresolved = [pid for pid in valid if after[pid].get('is_archived') or after[pid].get('is_autoarchived')]
        for pid in set(valid) - set(unresolved): self.active(pid, via_response=True, was_autoarchived=infos[pid].get('is_autoarchived'))
        for _ in range(2):
            if not unresolved or z['http'] != 200: break
            time.sleep(4)
            after = self.get_info(unresolved)
            still = [pid for pid in unresolved if after[pid].get('is_archived') or after[pid].get('is_autoarchived')]
            for pid in set(unresolved) - set(still): self.active(pid, via_response=True, was_autoarchived=infos[pid].get('is_autoarchived'))
            unresolved = still
        self.persist()
        if not unresolved: return
        text = json.dumps(z.get('response', {}), ensure_ascii=False).casefold()
        quota = (any(k in text for k in ['autoarchive_restore_failed', 'превышен лимит', 'суточн', 'daily limit', 'quota'])
                 or ('limit' in text and ('archiv' in text or 'restore' in text))
                 or ('лимит' in text and ('архив' in text or 'восстанов' in text)))
        if not quota and (z['http'] is None or z['http'] in [429, 500, 502, 503, 504] or z['http'] == 200):
            self.stop_reason = 'UNCONFIRMED_OR_TRANSIENT_RESPONSE'
            self.event({'event': self.stop_reason, 'product_ids': unresolved, 'response_request_id': z['request_id']})
            return
        if z['http'] in [401, 403]:
            self.stop_reason = 'AUTHORIZATION_ERROR'; return
        if len(unresolved) > 1:
            mid = len(unresolved) // 2
            self.apply(unresolved[:mid]); self.apply(unresolved[mid:]); return
        pid = unresolved[0]
        if quota:
            self.state['quota_blocked_day'] = self.day
            self.stop_reason = 'OZON_DAILY_QUOTA_EXHAUSTED'
        else:
            self.state['products'][pid].update(state='ERROR_REVIEW', last_error_request_id=z['request_id'])
        self.persist()

    def publish(self):
        counts = {}
        for r in self.state['products'].values(): counts[r['state']] = counts.get(r['state'], 0) + 1
        pending = sum(v for k,v in counts.items() if k not in ['RESTORED_VERIFIED', 'ALREADY_ACTIVE_VERIFIED'])
        summary = {'account': 'oz_acc2', 'asof_utc': NOW(), 'manifest_sha256': PINNED_MANIFEST_SHA256,
                   'selected': len(self.products), 'counts': counts, 'remaining': pending,
                   'restored_current_quota_day': self.count_day(all_archives=True),
                   'auto_restored_current_quota_day': self.count_day(), 'restored_this_run': self.verified_this_run,
                   'quota_day_msk_reset03': self.day, 'stop_reason': self.stop_reason,
                   'queue_complete': pending == 0, 'ad_stock_price_brand_writes': 0,
                   'next_daily_run_msk': '03:10', 'timer_installed': False}
        # Read actual timer state rather than infer scheduling from a unit file.
        import subprocess
        check = subprocess.run(['systemctl', 'is-enabled', 'ozon-dsprint-restore.timer'], capture_output=True, text=True)
        active = subprocess.run(['systemctl', 'is-active', 'ozon-dsprint-restore.timer'], capture_output=True, text=True)
        summary['timer_installed'] = check.stdout.strip() == 'enabled' and active.stdout.strip() == 'active'
        if pending == 0 and summary['timer_installed']:
            done = subprocess.run(['systemctl', 'disable', '--now', 'ozon-dsprint-restore.timer'], capture_output=True, text=True)
            summary['timer_disabled_after_completion'] = done.returncode == 0
            if done.returncode == 0: summary['timer_installed'] = False
        save(O / 'execution_status.json', summary)
        out = []
        for pid, product in self.products.items(): out.append({**product, **self.state['products'][pid]})
        fields = list(dict.fromkeys(k for r in out for k in r))
        with (O / 'queue_status.csv.tmp').open('w', encoding='utf-8-sig', newline='') as f:
            w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(out)
        (O / 'queue_status.csv.tmp').replace(O / 'queue_status.csv')
        body = '<h1>Восстановление дочерних ДС принт · акк2</h1><p>Прямое поручение владельца: брендДСпринт, дочерние, созданы2026, положительный свободный остаток. Условия перепроверяются перед записью; родители не требуются.</p>'
        body += '<p>Выбрано '+str(len(self.products))+' карточек:3519автоархивных и7изпрочегоархива. Реклама, ставки, цены, остатки, бренд не изменяются.</p>'
        labels = {'PENDING':'Ожидают восстановления', 'RESTORED_VERIFIED':'Восстановлены, результат проверен',
                  'ALREADY_ACTIVE_VERIFIED':'Уже были активны при проверке', 'DEFERRED_FILTER':'Отложены: изменились условия отбора',
                  'ERROR_REVIEW':'Требуется разбор ошибки'}
        stops = {'LOCAL_AUTO_DAILY_CAP':'Достигнут дневной предел очереди: 100 автоархивных',
                 'OZON_DAILY_QUOTA_EXHAUSTED':'Ozon сообщил об исчерпании суточной квоты',
                 'RUN_FINISHED':'Текущий проход завершён'}
        body += '<table><tr><th>Статус</th><th>Карточек</th></tr>'+''.join('<tr><td>'+html.escape(labels.get(k,k))+'</td><td>'+str(v)+'</td></tr>' for k,v in sorted(counts.items()))+'</table>'
        body += '<p>Восстановлено и проверено '+str(counts.get('RESTORED_VERIFIED',0))+'. Уже были активны '+str(counts.get('ALREADY_ACTIVE_VERIFIED',0))+'. Осталось '+str(pending)+'. '+html.escape(stops.get(self.stop_reason,str(self.stop_reason)))+'.</p>'
        body += '<p>Последний срез '+html.escape(summary['asof_utc'])+' UTC. Ежедневное продолжение '+('включено' if summary['timer_installed'] else 'не включено')+'. Запуск03:10МСК; максимум100успешных восстановлений из автоархива за квотный день, прекращение по фактической квотеOzon. Прочие7архивных не входят в лимит автоархива. Текущая свободная внешняя квота заранее неизвестна.</p>'
        body += '<p><a href="/api/wb-bids/reports/ozon-dsprint-restore-2026-11-08-skus">Очередь и результат по каждому SKU · CSV</a></p>'
        (O / 'index.html').write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ДС принт: восстановление</title><style>body{font:16px/1.55 system-ui;max-width:1100px;margin:25px auto;padding:0 16px}td,th{border:1px solid #ddd;padding:9px}table{border-collapse:collapse}</style>'+body+'</html>')
        print(json.dumps(summary, ensure_ascii=False), flush=True)

    def run(self):
        self.state['runs'] += 1; self.persist()
        role = self.request('/v1/roles', {})
        assert role['http'] == 200 and '/v1/product/unarchive' in {m for r in role['response']['roles'] for m in r['methods']}
        if not self.execute:
            self.stop_reason = 'READ_ONLY_PREVIEW'; return
        if self.state.get('quota_blocked_day') == self.day:
            self.stop_reason = 'OZON_DAILY_QUOTA_EXHAUSTED'; return
        todo = [p for p,r in self.state['products'].items() if r['state'] not in ['RESTORED_VERIFIED','ALREADY_ACTIVE_VERIFIED','ERROR_REVIEW']]
        todo.sort(key=lambda p: (self.products[p]['autoarchived'], int(p)))
        # Reconcile after any interrupted request before considering another write.
        for i in range(0, len(todo), 1000):
            batch = todo[i:i+1000]; infos = self.get_info(batch)
            for pid in batch:
                if not (infos[pid].get('is_archived') or infos[pid].get('is_autoarchived')):
                    self.active(pid)
            self.persist()
        todo = [p for p in todo if self.state['products'][p]['state'] not in ['RESTORED_VERIFIED','ALREADY_ACTIVE_VERIFIED']]
        first = self.count_day() == 0
        while todo and not self.stop_reason:
            if self.count_day() >= self.policy['max_auto_restored_per_quota_day'] and all(self.products[p]['autoarchived'] for p in todo):
                self.stop_reason = 'LOCAL_AUTO_DAILY_CAP'; break
            n = 1 if first else self.policy['max_batch_size']; first = False
            batch, todo = todo[:n], todo[n:]
            self.apply(batch)
        if not self.stop_reason:
            self.stop_reason = 'LOCAL_AUTO_DAILY_CAP' if self.count_day() >= self.policy['max_auto_restored_per_quota_day'] else 'RUN_FINISHED'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--publish-only', action='store_true')
    args = parser.parse_args()
    with (O / 'restore.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        q = Queue(args.execute)
        try:
            if args.publish_only:
                q.stop_reason = read(O / 'execution_status.json').get('stop_reason') if (O / 'execution_status.json').exists() else 'PUBLISH_ONLY'
            else:
                q.run()
        except Exception as exc:
            q.stop_reason = 'ERROR_' + type(exc).__name__
            q.event({'event':'RUN_ERROR','error_type':type(exc).__name__,'message':str(exc)[:300]})
            raise
        finally:
            q.state['last_stop_reason'] = q.stop_reason
            q.persist(); q.publish()


if __name__ == '__main__':
    main()
