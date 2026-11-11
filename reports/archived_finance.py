# поток: fin
"""Read preserved monthly reports without pretending to restore SKU-level history."""
import calendar
import math

from reports import mp_numbers
from reports import wb_mp_report, ozon_mp_report, yandex_mp_report

MODULES = {'wb': wb_mp_report, 'ozon': ozon_mp_report, 'yandex': yandex_mp_report}


def periods():
    return sorted({p for module in MODULES.values()
                   for p in module._hist().get('period_keys', [])})


def account(platform, account_name, period):
    module = MODULES[platform]
    history = module._hist()
    month = (period or '')[:7]
    keys = history.get('period_keys', [])
    record = history.get('accounts', {}).get(account_name)
    if month not in keys or not record:
        return None
    index = keys.index(month)

    def cell(values):
        value = values[index]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Unknown or invalid archived amount')
        return value

    try:
        # These optional zero lines match the historical report renderer's
        # backward compatibility; required financial totals are never invented.
        lines = {k: cell(record['lines'].get(k, [0]*len(keys))) for k in module._BAL_KEYS}
        cogs, net = cell(record['cogs']), cell(record['net'])
        derived = module._derive(lines, cell(record['orders']), cell(record['returns_cnt']), cogs)
        rounding_delta = round(derived['net']-net, 2)
        # The saved net is the report cell the user saw. Independently rounded
        # archived lines can differ by one or two rubles when recomputed.
        derived['net'] = net
        result = mp_numbers._pack(platform, derived)
    except (KeyError, IndexError, TypeError, ValueError):
        return None
    return {**result, 'source': 'saved_monthly_report', 'details_restored': False,
            'provisional': month in history.get('provisional', []),
            'rounding_delta': rounding_delta}


def business(period, org=''):
    if (period or '')[:7] not in periods():
        return None
    owned = mp_numbers.ORG_ACCOUNTS.get(org)
    platforms, accounts, missing = {}, [], []
    for platform, names in mp_numbers.ACCOUNTS.items():
        wanted = ([owned[platform]] if owned.get(platform) else []) if owned else names
        rows = []
        for name in wanted:
            value = account(platform, name, period)
            if value is None:
                missing.append(platform+':'+name)
                continue
            rows.append(value)
            accounts.append({'account': name, 'platform': platform, **value})
        if not rows:
            platforms[platform] = None
            continue
        sums = {k: round(sum(v[k] for v in rows), 2)
                for k in ('oborot', 'net', 'cogs', 'payout', 'hold')}
        revenue = sums['oborot']
        platforms[platform] = {**sums, 'revenue': revenue,
                              'margin_pct': round(sums['net']/revenue*100, 1) if revenue else None,
                              'hold_pct': round(sums['hold']/revenue*100, 1) if revenue else None}
    if not accounts:
        return None
    available = [v for v in platforms.values() if v is not None]
    total = {k: round(sum(v[k] for v in available), 2)
             for k in ('revenue', 'net', 'cogs', 'hold')}
    total['margin_pct'] = round(total['net']/total['revenue']*100, 1) if total['revenue'] else None
    total['cogs_pct'] = round(total['cogs']/total['revenue']*100, 1) if total['revenue'] else None
    total['hold_pct'] = round(total['hold']/total['revenue']*100, 1) if total['revenue'] else None
    provisional = any(v['provisional'] for v in accounts)
    return {'period': period, 'data_available': True, 'data_source': 'saved_monthly_reports',
            'details_restored': False, 'provisional': provisional, 'missing_accounts': missing,
            'total': total, 'accounts': accounts, **platforms,
            'opex': None, 'net_after_opex': None, 'opex_org_split': False,
            'purchases': {'net': None, 'docs': None}, 'prev_period': None, 'delta': None,
            'recovery_note': 'Сохранённые месячные итоги. Детализация и операционные расходы ещё восстанавливаются.'}


def slices():
    result = []
    for platform, module in MODULES.items():
        for month in module._hist().get('period_keys', []):
            y, m = map(int, month.split('-'))
            for name in mp_numbers.ACCOUNTS[platform]:
                if account(platform, name, month):
                    result.append({'platform': platform, 'account': name,
                                   'period_from': month+'-01',
                                   'period_to': f'{month}-{calendar.monthrange(y,m)[1]:02d}',
                                   'source': 'saved_monthly_report', 'details_restored': False})
    return result
