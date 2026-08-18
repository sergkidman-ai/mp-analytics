# поток: mkt
# ozon_bid_ramp.py — плавный разгон ставок Ozon (+10 % в день до цели) с ежедневными снимками.
#
#   plan      — залить цели из docs/reports/ozon_acc1_raise.csv в mkt_ozon_bid_ramp
#   snapshot  — снять статистику по позициям за день и текущие ставки (наблюдение)
#   step      — посчитать шаг дня (+10 %); БЕЗ --apply только пишет план в журнал
#   step --apply  — отправить ставки в Ozon. ТОЛЬКО по прямой команде Сергея.
#   rollback  — вернуть доразгонную ставку тем, кто за окно не дал ни одного заказа
#               (конвертеры не трогаем); --apply отправляет, без него сухой прогон.
#
# Снимок обязателен ДО первого шага: без базовой линии рост не с чем сравнивать.
import sys, csv, time, json, argparse, datetime as dt
sys.path.insert(0, '/opt/mp-analytics')
import requests
from core import db
from collectors.ozon_ads import _token, PERF

ACC = 'oz_acc1'
RAISE_CSV = '/opt/mp-analytics/docs/reports/ozon_acc1_raise.csv'
STEP_PCT = 10.0
rub = lambda s: float(str(s or '0').replace('\xa0', '').replace(' ', '').replace(',', '.') or 0)


def _camp_ids(acc):
    """(campaign_title, sku) -> campaign_id по последнему снимку ставок."""
    rows = db.query("""SELECT campaign_id::text cid, campaign_title, sku::text sku, bid FROM ozon_bids
        WHERE account=%s AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%s)""", (acc, acc))
    return {(r['campaign_title'] or '', r['sku']): (r['cid'], float(r['bid'])) for r in rows}


def cmd_plan(acc):
    idx, n = _camp_ids(acc), 0
    for r in csv.DictReader(open(RAISE_CSV)):
        key = (r['campaign'], r['sku'])
        if key not in idx:
            continue
        cid, cur = idx[key]
        db.execute("""INSERT INTO mkt_ozon_bid_ramp
              (account,campaign_id,sku,offer_id,bid_start,bid_target,bid_current,step_pct,grp,status)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'planned')
            ON CONFLICT (account,campaign_id,sku) DO UPDATE SET
              bid_target=EXCLUDED.bid_target, bid_current=EXCLUDED.bid_current,
              grp=EXCLUDED.grp, updated_at=now()""",
            (acc, cid, r['sku'], r['offer_id'], cur, float(r['bid_new']), cur, STEP_PCT, r.get('group')))
        n += 1
    print(f'план разгона: {n} позиций в mkt_ozon_bid_ramp (status=planned)')


def _report(acc, d_from, d_to, patience=300, tries=2):
    """Асинхронный отчёт Performance API в разрезе SKU: {campaign_id: rows}.

    NOT_STARTED — это очередь на стороне Ozon, а не ошибка: отчёт просто ещё не начали
    считать. 13.08 и 16.08 потерялись именно так — ждали 5 минут и молча писали 0 строк.
    Поэтому: ждём `patience` секунд и при NOT_STARTED заказываем отчёт заново (`tries`)."""
    H = {'Authorization': f'Bearer {_token(acc)}', 'Content-Type': 'application/json'}
    ids = [str(r['campaign_id']) for r in db.query("""SELECT DISTINCT campaign_id FROM ozon_bids
        WHERE account=%s AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%s)""", (acc, acc))]
    out = {}
    for i in range(0, len(ids), 10):                       # не больше 10 кампаний за запрос
        body = {'campaigns': ids[i:i + 10], 'dateFrom': d_from, 'dateTo': d_to, 'groupBy': 'NO_GROUP_BY'}
        for attempt in range(1, tries + 1):
            r = requests.post(f'{PERF}/api/client/statistics/json', headers=H, json=body, timeout=120)
            r.raise_for_status()
            uuid = r.json().get('UUID') or r.json().get('uuid')
            st = None
            for _ in range(max(1, patience // 5)):
                time.sleep(5)
                st = requests.get(f'{PERF}/api/client/statistics/{uuid}', headers=H, timeout=60).json().get('state')
                if st in ('OK', 'ERROR', 'CANCELLED'):
                    break
            if st == 'OK':
                rep = requests.get(f'{PERF}/api/client/statistics/report', headers=H,
                                   params={'UUID': uuid}, timeout=180)
                out.update(rep.json())
                break
            print(f'  отчёт {uuid}: {st} (попытка {attempt}/{tries})', flush=True)
    return out


def cmd_gaps(acc, days, patience):
    """Добрать дни, за которые в витрине пусто. Вызывается ежедневно — дыры сами зарастают."""
    today = dt.date.today()
    have = {r['stat_date'] for r in db.query(
        """SELECT DISTINCT stat_date FROM mkt_ozon_ads_sku_daily
           WHERE account=%s AND stat_date >= %s""", (acc, today - dt.timedelta(days=days)))}
    miss = [today - dt.timedelta(days=i) for i in range(1, days + 1)]
    miss = [d for d in sorted(miss) if d not in have]
    if not miss:
        print(f'{acc}: дыр за последние {days} дн нет'); return
    print(f'{acc}: добираю {len(miss)} дн: ' + ', '.join(d.strftime("%d.%m") for d in miss), flush=True)
    for d in miss:
        cmd_snapshot(acc, str(d), patience=patience)


def cmd_snapshot(acc, day, patience=300):
    bids = {(r['cid'], r['sku']): r for r in
            [{'cid': str(x['campaign_id']), 'sku': str(x['sku']), 'bid': float(x['bid'])} for x in
             db.query("""SELECT campaign_id, sku, max(bid) bid FROM ozon_bids WHERE account=%s
                 AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%s)
                 GROUP BY 1,2""", (acc, acc))]}
    bridge = {r['sku']: r['offer_id'] for r in
              db.query('SELECT sku, offer_id FROM ozon_product WHERE account=%s', (acc,))}
    agg, n = {}, 0
    for cid, blk in _report(acc, day, day, patience=patience).items():
        for row in blk.get('report', {}).get('rows', []):
            if not row.get('sku'):
                continue
            a = agg.setdefault((cid, row['sku']), [0, 0, 0.0, 0.0, 0.0])
            a[0] += int(rub(row.get('views'))); a[1] += int(rub(row.get('clicks')))
            a[2] += rub(row.get('moneySpent')); a[3] += rub(row.get('orders'))
            a[4] += rub(row.get('ordersMoney'))
    for (cid, sku), a in agg.items():
        drr = round(100 * a[2] / a[4], 2) if a[4] else None
        db.execute("""INSERT INTO mkt_ozon_ads_sku_daily
              (stat_date,account,campaign_id,sku,offer_id,bid,views,clicks,money_spent,orders_qty,orders_money,drr)
              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (stat_date,account,campaign_id,sku) DO UPDATE SET
              bid=EXCLUDED.bid, views=EXCLUDED.views, clicks=EXCLUDED.clicks,
              money_spent=EXCLUDED.money_spent, orders_qty=EXCLUDED.orders_qty,
              orders_money=EXCLUDED.orders_money, drr=EXCLUDED.drr, collected_at=now()""",
            (day, acc, cid, sku, bridge.get(sku),
             (bids.get((cid, sku)) or {}).get('bid'), a[0], a[1], a[2], a[3], a[4], drr))
        n += 1
    print(f'снимок за {day}: {n} позиций в mkt_ozon_ads_sku_daily')


def _apply_bids(acc, cid, items):
    """Запись ставок в кампанию. Проверено боем 08.08.2026 на sku 1510947813 (23.0 -> 25.3 ₽):
    PUT /api/client/campaign/{id}/products {"bids":[{"sku","bid"}]}, ставка — строка в микрорублях.
    POST по тому же пути отвечает 200, но ставку НЕ меняет (это добавление товаров);
    /v2/products на любую запись отвечает 405 (Allow: GET)."""
    H = {'Authorization': f'Bearer {_token(acc)}', 'Content-Type': 'application/json'}
    body = {'bids': [{'sku': str(s), 'bid': str(int(round(b * 1_000_000)))} for s, b in items]}
    r = requests.put(f'{PERF}/api/client/campaign/{cid}/products', headers=H, json=body, timeout=120)
    return r.status_code, r.text[:300]


def _spend_guard(acc, cap):
    """Стоп-кран разгона: если вчерашний расход перевалил потолок, шаг не делаем.
    База 07.08.2026 — 3 238 ₽/день по всему acc1."""
    r = db.query("""SELECT stat_date, sum(money_spent) s FROM mkt_ozon_ads_sku_daily
        WHERE account=%s GROUP BY 1 ORDER BY 1 DESC LIMIT 1""", (acc,))
    if not r:
        return True, 'снимков нет'
    spent = float(r[0]['s'] or 0)
    return spent <= cap, f"расход за {r[0]['stat_date']}: {spent:.0f} ₽ при потолке {cap:.0f} ₽"


def cmd_step(acc, day, apply_, limit, cap=8000.0):
    ok, msg = _spend_guard(acc, cap)
    print(f'стоп-кран: {msg}')
    if not ok:
        print('ПОТОЛОК РАСХОДА ПРЕВЫШЕН — шаг не делаю, нужно решение Сергея'); return
    rows = db.query("""SELECT campaign_id, sku, bid_current, bid_target, step_pct FROM mkt_ozon_bid_ramp
        WHERE account=%s AND status IN ('planned','running') AND bid_current < bid_target
        ORDER BY priority, bid_target-bid_current DESC""" + (' LIMIT %s' if limit else ''),
        (acc, limit) if limit else (acc,))
    plan = {}
    for r in rows:
        cur, tgt = float(r['bid_current']), float(r['bid_target'])
        nxt = min(round(cur * (1 + float(r['step_pct']) / 100.0), 2), tgt)
        if nxt <= cur:
            continue
        plan.setdefault(str(r['campaign_id']), []).append((str(r['sku']), cur, nxt, tgt))
    total = sum(len(v) for v in plan.values())
    print(f'шаг {day}: {total} позиций в {len(plan)} кампаниях, +{STEP_PCT:.0f} % к текущей ставке'
          + ('' if apply_ else '  [DRY-RUN, на площадку ничего не уходит]'))
    for cid, items in plan.items():
        code, resp = _apply_bids(acc, cid, [(s, n) for s, _, n, _ in items]) if apply_ else (None, None)
        for sku, cur, nxt, tgt in items:
            db.execute("""INSERT INTO mkt_ozon_bid_step_log
                  (step_date,account,campaign_id,sku,bid_before,bid_after,bid_target,applied,api_response)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (step_date,account,campaign_id,sku) DO UPDATE SET
                  bid_after=EXCLUDED.bid_after, applied=EXCLUDED.applied, api_response=EXCLUDED.api_response""",
                (day, acc, cid, sku, cur, nxt, tgt, bool(apply_ and code in (200, 201)),
                 None if code is None else f'{code} {resp}'))
            if apply_ and code in (200, 201):
                db.execute("""UPDATE mkt_ozon_bid_ramp SET bid_current=%s, status=
                      CASE WHEN %s >= bid_target THEN 'done' ELSE 'running' END, updated_at=now()
                    WHERE account=%s AND campaign_id=%s AND sku=%s""", (nxt, nxt, acc, cid, sku))
        if apply_:
            print(f'  кампания {cid}: {len(items)} ставок -> {code} {resp[:80]}')


ROLLBACK_SQL = """
WITH first_step AS (
  SELECT DISTINCT ON (campaign_id, sku) campaign_id, sku::text sku, bid_before
  FROM mkt_ozon_bid_step_log WHERE account=%(acc)s AND applied
  ORDER BY campaign_id, sku, step_date),
last_bid AS (
  SELECT campaign_id, sku::text sku, max(bid) bid FROM ozon_bids
  WHERE account=%(acc)s AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%(acc)s)
  GROUP BY 1,2),
perf AS (
  SELECT campaign_id, sku::text sku, sum(money_spent) spend, sum(orders_qty) ord,
         sum(orders_money) rev, sum(clicks) clicks, sum(views) views
  FROM mkt_ozon_ads_sku_daily WHERE account=%(acc)s AND stat_date >= %(since)s
  GROUP BY 1,2)
SELECT f.campaign_id::text campaign_id, f.sku, f.bid_before::float pre_bid, b.bid::float cur_bid,
       coalesce(p.spend,0)::float spend, coalesce(p.ord,0)::float ord, coalesce(p.rev,0)::float rev,
       coalesce(p.clicks,0)::int clicks, coalesce(p.views,0)::int views
FROM first_step f JOIN last_bid b USING (campaign_id, sku)
LEFT JOIN perf p ON p.campaign_id=f.campaign_id AND p.sku=f.sku
"""


def cmd_rollback(acc, apply_, since='2026-08-08', week=None, batch=100):
    """Откат разгона: позициям, которые за окно наблюдения не дали НИ ОДНОГО заказа,
    возвращаем доразгонную ставку (bid_before первого применённого шага).
    Конвертеры (заказы > 0) не трогаем. Без --apply на площадку ничего не уходит."""
    from tools.ozon_weekly_bids import ensure_journal
    ensure_journal()
    today = dt.date.today()
    week = week or str(today - dt.timedelta(days=today.weekday()))
    rows = db.query(ROLLBACK_SQL, {'acc': acc, 'since': since})
    zero = [r for r in rows if float(r['ord'] or 0) == 0]
    conv = [r for r in rows if float(r['ord'] or 0) > 0]
    plan = {}
    for r in zero:
        cur, pre = float(r['cur_bid']), float(r['pre_bid'])
        if cur - pre < 0.01:
            continue
        plan.setdefault(r['campaign_id'], []).append((r['sku'], cur, pre, r))
    total = sum(len(v) for v in plan.values())
    s_cur = sum(c for v in plan.values() for _, c, _, _ in v)
    s_pre = sum(p for v in plan.values() for _, _, p, _ in v)
    spend = sum(float(r['spend'] or 0) for r in zero)
    print(f'откат разгона {acc}: когорта {len(rows)} пар, без заказов с {since} — {len(zero)}, '
          f'конвертеров {len(conv)} (не трогаем)')
    print(f'к отправке {total} пар в {len(plan)} кампаниях: сумма ставок {s_cur:,.0f} → {s_pre:,.0f} ₽ '
          f'(−{s_cur - s_pre:,.0f}); их расход за окно {spend:,.0f} ₽'
          + ('' if apply_ else '   [DRY-RUN, на площадку ничего не уходит]'))
    if not apply_:
        return
    sent = failed = 0
    for cid, items in plan.items():
        for i in range(0, len(items), batch):
            chunk = items[i:i + batch]
            code, resp = _apply_bids(acc, cid, [(s, p) for s, _, p, _ in chunk])
            ok = code in (200, 201)
            sent += len(chunk) if ok else 0
            failed += 0 if ok else len(chunk)
            for sku, cur, pre, r in chunk:
                db.execute("""INSERT INTO mkt_ozon_bid_journal
                      (decided_on,week_start,account,campaign_id,sku,tier,action,bid_before,bid_after,
                       reason,m_before,applied,applied_at,api_response,review_on)
                      VALUES (%s,%s,%s,%s,%s,'red','rollback',%s,%s,%s,%s,%s,now(),%s,%s)
                    ON CONFLICT (week_start,account,campaign_id,sku) DO UPDATE SET
                      decided_on=EXCLUDED.decided_on, action=EXCLUDED.action,
                      bid_before=EXCLUDED.bid_before, bid_after=EXCLUDED.bid_after,
                      reason=EXCLUDED.reason, m_before=EXCLUDED.m_before, applied=EXCLUDED.applied,
                      applied_at=EXCLUDED.applied_at, api_response=EXCLUDED.api_response,
                      review_on=EXCLUDED.review_on""",
                    (today, week, acc, cid, sku, cur, pre,
                     f"откат разгона: с {since} ноль заказов при расходе {float(r['spend'] or 0):.0f} ₽ "
                     f"и {r['clicks']} кликах; ставка {cur:.2f} → доразгонная {pre:.2f} ₽",
                     json.dumps({'spend': float(r['spend'] or 0), 'orders': 0, 'clicks': r['clicks'],
                                 'views': r['views'], 'window_from': since}), ok,
                     f'{code} {resp[:120]}', today + dt.timedelta(days=7)))
            print(f'  кампания {cid}: {len(chunk)} ставок -> {code} {resp[:60]}')
            time.sleep(1.0)
    print(f'итог: применено {sent}, ошибок {failed}; решения записаны в mkt_ozon_bid_journal '
          f'(week_start={week}, action=rollback)')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['plan', 'snapshot', 'step', 'gaps', 'rollback'])
    ap.add_argument('--account', default=ACC)
    ap.add_argument('--date', default=str(dt.date.today() - dt.timedelta(days=1)))
    ap.add_argument('--apply', action='store_true', help='ОТПРАВИТЬ ставки в Ozon (только по команде Сергея)')
    ap.add_argument('--limit', type=int, default=0, help='ограничить число позиций шага (обкатка)')
    ap.add_argument('--days', type=int, default=10, help='gaps: глубина проверки дыр в витрине')
    ap.add_argument('--since', default='2026-08-08',
                    help='rollback: с какой даты считаем заказы разогнанных позиций')
    ap.add_argument('--patience', type=int, default=1800,
                    help='gaps/snapshot: сколько секунд ждать отчёт Ozon (NOT_STARTED = очередь)')
    ap.add_argument('--max-spend', type=float, default=8000.0,
                    help='потолок дневного расхода acc1, выше которого разгон останавливается')
    a = ap.parse_args()
    if a.cmd == 'plan':
        cmd_plan(a.account)
    elif a.cmd == 'snapshot':
        cmd_snapshot(a.account, a.date, patience=a.patience)
    elif a.cmd == 'gaps':
        cmd_gaps(a.account, a.days, a.patience)
    elif a.cmd == 'rollback':
        cmd_rollback(a.account, a.apply, since=a.since)
    else:
        cmd_step(a.account, str(dt.date.today()), a.apply, a.limit, a.max_spend)
