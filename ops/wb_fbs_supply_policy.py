# поток: inv
"""Dated WB FBS supplies, shared by the regular and receipt adapters."""
from contextlib import contextmanager
from datetime import date, datetime
import fcntl
import json
import os
from pathlib import Path
import re
import time

RUNTIME = Path(__file__).resolve().parents[1] / '.private/ms_supply_fulfillment/wb_supplies'
BUSINESSES = {'wb_acc1': 'Цифровой', 'wb_acc2': 'Дисквер'}
LOCK_WAIT_SECONDS = 5


class SupplyError(ValueError):
    pass


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


@contextmanager
def account_lock(account):
    if account not in BUSINESSES:
        raise SupplyError('wb_supply_account_unknown')
    root = RUNTIME / account
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    fd = os.open(root / 'lock', os.O_CREAT | os.O_RDWR, 0o600)
    try:
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SupplyError('wb_supply_account_busy') from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


def dated(info, target):
    name = str(info.get('name', ''))
    dates = re.findall(r'(?<!\d)(\d{4}-\d{2}-\d{2}|\d{1,2}[.-]\d{1,2}(?:[.-](?:\d{4}|\d{2}))?)(?![.\d])', name)
    if len(dates) != 1:
        return False
    value = dates[0]
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            actual = date.fromisoformat(value)
        else:
            parts = re.split(r'[.-]', value)
            if len(parts) == 2:
                parts.append(str(datetime.fromisoformat(str(info['createdAt']).replace('Z', '+00:00')).year))
            year = int(parts[2]) + (2000 if len(parts[2]) == 2 else 0)
            actual = date(year, int(parts[1]), int(parts[0]))
        return actual == date.fromisoformat(target)
    except (ValueError, KeyError):
        return False


def opened(info):
    return info.get('done') is False and not info.get('closedAt')


def b2b_flag(raw):
    for value in (raw, raw.get('options')):
        if isinstance(value, dict):
            for key in ('isB2b', 'isB2B'):
                if type(value.get(key)) is bool:
                    return value[key]
    return None


def compatible(info, raw):
    if not raw:
        return True
    if info.get('cargoType') not in (None, 0, raw.get('cargoType')):
        return False
    if info.get('crossBorderType') not in (None, raw.get('crossBorderType')):
        return False
    flag, supply_flag = b2b_flag(raw), b2b_flag(info)
    if flag is True:
        return supply_flag is True
    if supply_flag is True:
        return False
    return not (info.get('cargoType') and flag is not None
                and supply_flag is not None and flag != supply_flag)


def read_supply(session, folder, account, sid, label):
    http, info = session._call('wb', account, folder, label, 'GET', '/api/v3/supplies/' + sid)
    if http == 404:
        return None
    if http != 200 or not isinstance(info, dict):
        raise SupplyError('read_api_http_' + str(http))
    if info.get('id') != sid:
        raise SupplyError('wb_saved_supply_identity_mismatch')
    return info


def catalog(session, folder, base, save, *, refresh=False):
    path = RUNTIME / base['account'] / 'catalog.json'
    cached = load(path, {})
    if not refresh and 0 <= time.time() - cached.get('at', 0) < session.cache_ttl:
        return cached['supplies'], False
    result, cursor, seen = [], 0, set()
    for page in range(50):
        data = session._read('wb', base['account'], folder, 'wb_supply_list_' + str(page),
                             'GET', '/api/v3/supplies', params={'limit': 100, 'next': cursor})
        rows, following = data.get('supplies'), data.get('next')
        if not isinstance(rows, list) or type(following) is not int:
            raise SupplyError('wb_supply_list_incomplete')
        for item in rows:
            sid = item.get('id')
            if not isinstance(sid, str) or sid in seen:
                raise SupplyError('wb_supply_list_identity_mismatch')
            seen.add(sid)
        result.extend(rows)
        if following == 0:
            save(path, {'at': time.time(), 'supplies': result})
            return result, True
        if following == cursor or not rows:
            raise SupplyError('wb_supply_list_pagination_stalled')
        cursor = following
    raise SupplyError('wb_supply_list_page_limit')


def remember(account, info, save):
    path = RUNTIME / account / 'catalog.json'
    data = load(path, {'at': 0, 'supplies': []})
    data['supplies'] = [item for item in data['supplies'] if item['id'] != info['id']] + [info]
    save(path, data)


def resolve(session, folder, base, target, raw, save, *, allow_create):
    """Caller holds account_lock through selection and the first assignment."""
    date.fromisoformat(target)
    account = base['account']
    b2b = bool(raw and b2b_flag(raw) is True)
    if b2b:
        allow_create = False
    root = RUNTIME / account / target
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    selected_path = root / ('selected_b2b.json' if raw and b2b_flag(raw) is True else 'selected.json')
    selected = load(selected_path)
    if selected:
        if selected.get('account') != account or selected.get('day') != target:
            raise SupplyError('wb_saved_supply_scope_mismatch')
        info = read_supply(session, folder, account, selected['id'], 'wb_bound_dated_supply')
        if info and opened(info) and dated(info, target) and compatible(info, raw):
            return info
    supplies, fresh = catalog(session, folder, base, save, refresh=bool(selected) or b2b)

    def matches(items):
        return [item for item in items if opened(item) and dated(item, target) and compatible(item, raw)]

    candidates = matches(supplies)
    if not candidates and allow_create and not fresh:
        supplies, _ = catalog(session, folder, base, save, refresh=True)
        candidates = matches(supplies)
    if not candidates and allow_create:
        # TK has its own writer and cannot take our process lock. Check once
        # more immediately before considering a new supply.
        supplies, _ = catalog(session, folder, base, save, refresh=True)
        candidates = matches(supplies)
    if len(candidates) > 1:
        raise SupplyError('wb_multiple_open_supplies_for_planned_day')
    if candidates:
        sid = candidates[0]['id']
    elif not allow_create:
        raise SupplyError('wb_b2b_supply_not_found_wait_for_tk' if b2b else 'wb_confirmed_order_has_no_dated_supply')
    else:
        attempts = sorted(root.glob('attempt_*'))
        for attempt in attempts:
            intent = load(attempt / 'mut_create_supply_intent.json')
            if not intent:
                continue
            binding = load(attempt / 'binding.json')
            if not binding:
                same = [item for item in supplies if item.get('name') == intent['body']['name']
                        and opened(item) and compatible(item, raw)]
                if len(same) != 1:
                    raise SupplyError('wb_supply_create_unconfirmed_no_retry')
                binding = {'id': same[0]['id'], 'name': same[0]['name']}
                save(attempt / 'binding.json', binding)
            known = next((item for item in supplies if item['id'] == binding['id']), None)
            if known is None:
                known = read_supply(session, folder, account, binding['id'], 'wb_previous_created_supply')
            if known and opened(known) and compatible(known, raw):
                raise SupplyError('wb_supply_registry_requires_reconciliation')
        attempt = root / ('attempt_' + str(len(attempts) + 1).zfill(4))
        attempt.mkdir(mode=0o700)
        name = date.fromisoformat(target).strftime('%d.%m.%Y') + ' ' + BUSINESSES[account] + ' FBS'
        if attempts:
            name += ' #' + str(len(attempts) + 1)
        try:
            http, body = session._call('wb', account, attempt, 'create_supply', 'POST',
                                        '/api/v3/supplies', body={'name': name}, mutation=True)
        except Exception as exc:
            save(attempt / 'unconfirmed.json', {'error': type(exc).__name__})
            http, body = 0, {}
        sid = body.get('id') if http == 201 else None
        if not isinstance(sid, str) or not re.fullmatch(r'WB-GI-\d+', sid):
            actual, _ = catalog(session, folder, base, save, refresh=True)
            same = [item for item in actual if item.get('name') == name and opened(item)
                    and compatible(item, raw)]
            if len(same) != 1:
                raise SupplyError('wb_supply_create_unconfirmed_no_retry')
            sid = same[0]['id']
        save(attempt / 'binding.json', {'id': sid, 'name': name, 'day': target, 'account': account})
    info = session._read('wb', account, folder, 'wb_dated_supply', 'GET', '/api/v3/supplies/' + sid)
    if info.get('id') != sid or not opened(info) or not dated(info, target) or not compatible(info, raw):
        raise SupplyError('wb_dated_supply_not_verified')
    save(selected_path, {'id': sid, 'name': info['name'], 'day': target, 'account': account})
    remember(account, info, save)
    return info
