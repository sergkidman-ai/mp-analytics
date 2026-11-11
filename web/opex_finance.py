# поток: inv — read-only adapter to the existing MP report snapshots.
"""Use the same saved profit as the MP reports; live months use mp_numbers."""
import datetime as dt
import math
from zoneinfo import ZoneInfo
from reports import mp_numbers, wb_mp_report, ozon_mp_report, yandex_mp_report

MODULES={'wb':wb_mp_report,'ozon':ozon_mp_report,'yandex':yandex_mp_report}


def business(period,org,today=None):
    today=today or dt.datetime.now(ZoneInfo('Europe/Moscow')).date()
    key=str(period)[:7]
    expected=mp_numbers.ORG_ACCOUNTS[org]
    parts={};sources={};provisional=[];live=None
    for platform,acc in expected.items():
        if not acc:
            parts[platform]=None
            continue
        net=None
        if key < today.strftime('%Y-%m'):
            h=MODULES[platform]._hist()
            keys=h.get('period_keys') or []
            if key in keys:
                i=keys.index(key);values=(h.get('accounts',{}).get(acc) or {}).get('net') or []
                if i<len(values) and isinstance(values[i],(int,float)) and not isinstance(values[i],bool) and math.isfinite(values[i]):
                    net=float(values[i]);sources[platform]='saved_report'
                    if key in (h.get('provisional') or []):provisional.append(platform)
        if net is None:
            if live is None:live=mp_numbers.business(period,org)
            net=((live.get('platforms') or {}).get(platform) or {}).get('net')
            sources[platform]='live_report' if net is not None else 'missing'
            if net is not None:provisional.append(platform)
        parts[platform]={'net':net}
    required=[parts[p] for p,acc in expected.items() if acc]
    total=mp_numbers._sum_known(required,'net')
    return {'platforms':parts,'total':{'net':total},'sources':sources,'provisional':provisional}
