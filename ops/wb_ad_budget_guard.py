#!/usr/bin/env python3
# поток: mkt
"""ops/wb_ad_budget_guard.py — сторож бюджетов рекламных кампаний ВБ.

ЗАЧЕМ. 18.09.2026 нашли 14 рабочих cpc-кампаний (заказ по 34–166 ₽, в сумме 90 заказов на 8 269 ₽
за 28 дней), которые стояли на нуле: автопополнения у ВБ нет, и никто этого не видел. Сторож каждый
день читает баланс всех cpc-кампаний и пишет Сергею в Dropbox-бот, какие ВЫГОДНЫЕ кампании кончатся или уже
кончились, и на сколько их пополнить.

ТОЛЬКО ЧТЕНИЕ. В ВБ сторож ничего не пишет: пополнение — деньги, решение за Сергеем (инвариант 8).
  GET /adv/v1/promotion/count, GET /api/advert/v2/adverts — состав и тип оплаты
  POST /api/advert/v2/budget                              — чтение остатков (по 50 ID)

ПРАВИЛО.
  выгодная   = за 28 дней ≥ 2 заказов и цена заказа ≤ CPO_OK (200 ₽);
  тревога    = баланс < 50 ₽ ИЛИ денег меньше, чем на RUNWAY_DAYS (3) дня при расходе последних 7 дней;
  сумма      = расход за 28 дней, округлённый вверх до 100, но не меньше 1000 ₽ (минимум ВБ);
  прочие пустые кампании (дорогой заказ или нет заказов) перечисляются одной строкой без суммы.

  ./venv/bin/python -m ops.wb_ad_budget_guard --dry     # печать и файл, без телеграма
  ./venv/bin/python -m ops.wb_ad_budget_guard           # + сообщение Сергею в Dropbox-бот
"""
import sys
import json
import time
import math
import argparse
import pathlib
import datetime as dt
from zoneinfo import ZoneInfo

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from ops.wb_ad_enroll import composition  # noqa: E402
from collectors.wb_ads import B, _token  # noqa: E402
from ops.ad_alert_delivery import send_sergey  # noqa: E402

CPO_OK = 200
RUNWAY_DAYS = 3
MIN_DEPOSIT = 1000
REPORT = BASE_DIR / "docs" / "reports" / "mkt_ad_budget_guard_latest.txt"
STATUS = REPORT.with_suffix('.json')


def read_budgets(account, ids):
    """Read-only POST; never deposit, retry only transient read failures."""
    out = {}
    for start in range(0, len(ids), 50):
        chunk = ids[start:start + 50]
        for attempt in range(3):
            r = requests.post(B + '/api/advert/v2/budget',
                              headers={'Authorization': _token(account)},
                              json={'advertIds': chunk}, timeout=45)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(5)
                continue
            r.raise_for_status()
            break
        response = r.json()
        raw_dir = REPORT.parent / 'wb_ad_budget_guard_raw' / account
        raw_dir.mkdir(parents=True, exist_ok=True)
        observed = dt.datetime.now(dt.timezone.utc)
        raw_path = raw_dir / (observed.strftime('%Y%m%dT%H%M%S%fZ') + f'_{start // 50:02}.json')
        raw_path.write_text(json.dumps({'at_utc': observed.isoformat(), 'account': account,
                                       'method': 'POST', 'path': '/api/advert/v2/budget',
                                       'request': {'advertIds': chunk}, 'http': r.status_code,
                                       'response': response}, ensure_ascii=False, indent=2) + '\n')
        for row in response.get('adverts', []):
            aid = int(row['advertId'])
            if aid not in chunk or row.get('currency') != 'RUB' or aid in out:
                raise ValueError('Unexpected budget identity, currency or duplicate')
            value = row.get('total')
            if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
                raise ValueError('Invalid campaign budget')
            out[aid] = value
        if start + 50 < len(ids):
            time.sleep(3.2)
    return out


def scan(account):
    comp = composition(account)
    ids = [a for a, c in comp.items() if c["payment"] == "cpc"]
    today = dt.datetime.now(ZoneInfo('Europe/Moscow')).date()
    stats = {r["advert_id"]: r for r in db.query("""
        select advert_id,
               coalesce(sum(spend) filter (where dt >= %s), 0) sp7,
               coalesce(sum(spend), 0) sp28, coalesce(sum(orders), 0) o28,
               max(dt) last_day, count(*) source_rows
          from wb_ad_nm_daily where account=%s and dt >= %s and dt < %s and advert_id=any(%s)
         group by 1""", (today - dt.timedelta(days=7), account,
                          today - dt.timedelta(days=28), today, ids))}
    balances = read_budgets(account, ids)
    rows = []
    for aid in ids:
        st = stats.get(aid)
        state = ('MISSING' if not st else
                 'STALE' if st['last_day'] < today - dt.timedelta(days=1) else 'CURRENT')
        # Missing data is unknown, never proof of zero orders or unprofitable ads.
        sp7, sp28, o28 = ((float(st['sp7']), float(st['sp28']), int(st['o28']))
                           if state == 'CURRENT' else (None, None, None))
        bal = balances.get(aid)
        cpo = sp28 / o28 if o28 else None
        per_day = sp7 / 7 if sp7 is not None else None
        runway = (bal / per_day) if (bal is not None and per_day is not None and per_day > 0) else None
        good = state == 'CURRENT' and o28 >= 2 and cpo is not None and cpo <= CPO_OK
        low = bal is not None and (bal < 50 or (runway is not None and runway < RUNWAY_DAYS))
        rows.append({"id": aid, "status": comp[aid]["status"], "bal": bal, "sp7": sp7, "sp28": sp28,
                     "o28": o28, "cpo": cpo, "runway": runway, "good": good, "low": low,
                     "statistics_state": state,
                     "statistics_last_day": str(st['last_day']) if st else None,
                     "topup": (max(MIN_DEPOSIT, int(math.ceil(sp28 / 100.0)) * 100)
                               if state == 'CURRENT' else None)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="без телеграма")
    a = ap.parse_args()
    lines, total, snapshots, errors = [], 0, {}, {}
    for acc in ("wb_acc1", "wb_acc2"):
        try:
            rows = scan(acc)
        except Exception as exc:
            errors[acc] = type(exc).__name__
            lines.append(f'{acc}: ОШИБКА ЧТЕНИЯ {type(exc).__name__}; остатки/оценка не подтверждены')
            continue
        snapshots[acc] = rows
        act = sorted((r for r in rows if r["good"] and r["low"]), key=lambda r: r["cpo"])
        unknown = [r for r in rows if r.get('statistics_state', 'CURRENT') != 'CURRENT']
        weak = [r for r in rows if not r["good"] and r not in unknown and r["bal"] is not None and r["bal"] < 50]
        unread = [r["id"] for r in rows if r["bal"] is None]
        lines.append(f"{acc}: cpc-кампаний {len(rows)}; выгодных на исходе {len(act)}")
        if unknown:
            lines.append(f'  НЕТ ПОЛНОЙ АКТУАЛЬНОЙ СТАТИСТИКИ у {len(unknown)} РК; оценка CPO и сумм пополнения недоступна')
            low_unknown = [r for r in unknown if r['status'] == 9 and r['bal'] is not None and r['bal'] < 50]
            if low_unknown:
                lines.append('  работающие с остатком <50 ₽, без оценки окупаемости: '
                             + ', '.join(f"{r['id']}={r['bal']} ₽" for r in low_unknown))
        for r in act:
            left = "0 ₽" if r["bal"] < 50 else f"{round(r['bal'])} ₽ ≈ {r['runway']:.1f} дн"
            lines.append(f"  {r['id']}: осталось {left}; заказ {round(r['cpo'])} ₽ ({r['o28']} за 28 дн) "
                         f"→ пополнить {r['topup']} ₽")
            total += r["topup"]
        if weak:
            lines.append(f"  пустые, но невыгодные (не пополнять без решения): "
                         + ", ".join(str(r["id"]) for r in weak))
        if unread:
            lines.append(f"  баланс не прочитан: {unread}")
    degraded = bool(errors or any(r.get('statistics_state', 'CURRENT') != 'CURRENT' or r['bal'] is None
                                 for rows in snapshots.values() for r in rows))
    head = ("ВБ реклама: бюджеты. НЕПОЛНЫЕ ДАННЫЕ — оценка требует проверки" if degraded else
            f"ВБ реклама: бюджеты. К пополнению выгодных — {total} ₽" if total
            else "ВБ реклама: бюджеты. Выгодные кампании при деньгах")
    text = head + "\n" + "\n".join(lines)
    REPORT.write_text(text + "\n", encoding="utf-8")
    payload = {'at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
               'state': 'DEGRADED' if degraded else 'CURRENT', 'accounts': snapshots, 'errors': errors,
               'recommended_topup_total_RUB': total if not degraded else None,
               'automatic_deposits_enabled': False, 'dry_no_telegram': a.dry}
    tmp = STATUS.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    tmp.replace(STATUS)
    print(text)
    if not a.dry and (total or degraded):
        result = send_sergey(text + "\n\nПополнение — только по вашему «да» с суммой.")
        print("телеграм Сергею в Dropbox-бот:",
              "ok" if result.get("ok") else result.get("error", "delivery_not_confirmed"))


if __name__ == "__main__":
    main()
