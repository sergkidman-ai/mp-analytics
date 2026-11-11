# поток: fin
"""Read-only presentation of recovered facts while financial validation continues."""
import calendar
import json
from pathlib import Path

from core import db
from reports import mp_numbers

STATE_FILE = Path(__file__).with_name('data') / 'bi_recovery.json'


def state():
    if not STATE_FILE.exists():
        return None
    return json.loads(STATE_FILE.read_text())


def slices():
    status = state()
    if not status:
        return []
    if 'slices' in status:
        return status['slices']
    rows = db.query("""SELECT DISTINCT 'wb' platform, account,
            date_trunc('month',(payload->>'create_dt')::date)::date::text period_from
        FROM raw_wb_report WHERE payload->>'create_dt' IS NOT NULL
        UNION SELECT DISTINCT 'ozon', account,
            date_trunc('month',(payload->>'operation_date')::date)::date::text
        FROM raw_ozon_transaction
        UNION SELECT 'yandex', account, month::text FROM yandex_finance_monthly""")
    for row in rows:
        y, m = map(int, row['period_from'][:7].split('-'))
        row['period_to'] = f'{y}-{m:02d}-{calendar.monthrange(y,m)[1]:02d}'
        row['source'] = 'recovered_api_reports'
    return rows


def business(period, org=''):
    status = state()
    if not status:
        return None
    available = {(r['platform'], r['account']) for r in slices()
                 if r['period_from'][:7] == period[:7]}
    owned = mp_numbers.ORG_ACCOUNTS.get(org)
    platforms, accounts, missing = {}, [], []
    for platform, names in mp_numbers.ACCOUNTS.items():
        wanted = ([owned[platform]] if owned.get(platform) else []) if owned else names
        rows = []
        for name in wanted:
            if (platform, name) not in available:
                missing.append(platform + ':' + name)
                continue
            value = mp_numbers.account(platform, name, period)
            if value is None:
                missing.append(platform + ':' + name)
                continue
            rows.append(value)
            accounts.append({'account': name, 'platform': platform, **value})
        if not rows:
            platforms[platform] = None
            continue
        sums = {k: mp_numbers._sum_known(rows, k)
                for k in ('oborot', 'net', 'cogs', 'payout', 'hold')}
        rev, net = sums['oborot'], sums['net']
        platforms[platform] = {**sums, 'revenue': rev,
                              'margin_pct': round(net/rev*100, 1) if rev and net is not None else None,
                              'hold_pct': round(sums['hold']/rev*100, 1) if rev else None}
    present = [r for r in platforms.values() if r]
    total = {k: mp_numbers._sum_known(present, k) for k in ('revenue', 'net', 'cogs', 'hold')}
    # A missing account means the total cost/profit is still incomplete.
    if missing:
        total['net'] = total['cogs'] = None
    rev, net, cogs = total['revenue'], total['net'], total['cogs']
    total['margin_pct'] = round(net/rev*100, 1) if rev and net is not None else None
    total['cogs_pct'] = round(cogs/rev*100, 1) if rev and cogs is not None else None
    total['hold_pct'] = round(total['hold']/rev*100, 1) if rev else None
    return {'period': period, 'data_available': bool(present), 'data_source': 'recovered_api_reports',
            'total': total, 'accounts': accounts, **platforms, 'provisional': True,
            'missing_accounts': missing, 'details_restored': 'partial',
            'opex': None, 'net_after_opex': None, 'opex_org_split': False,
            'purchases': {'net': None, 'docs': None}, 'prev_period': None, 'delta': None,
            'recovery_note': status.get('report_note') or ('Отчёты площадок восстановлены по API по 3 ноября. '
                'Себестоимость и детализация проверяются; неизвестные суммы показаны прочерком. '
                'Банковские и операционные расходы ещё не восстановлены.'),
            'recovery': status}
