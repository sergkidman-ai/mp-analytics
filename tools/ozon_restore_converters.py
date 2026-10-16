# поток: mkt
"""tools/ozon_restore_converters.py — вернуть разогнанные ставки тем SKU, которых откат 18.08
снял по ошибке.

Почему инструмент нужен. Откат разгона (tools/ozon_bid_ramp.py cmd_rollback) считал заказы по
паре «кампания × SKU». Товар стоит в нескольких кампаниях сразу, заказ достаётся одной из них,
и в «молчащей» кампании тот же товар выглядит как ноль заказов. Проверка 19.08.2026: из откаченных
987 SKU 21 давали заказы — 409 325 ₽ выручки при расходе 10 721 ₽ (ДРР 2,6 %). У 17 из них заказ
пришёл в другой кампании, у 4 — 18.08, в день отката, когда статистика его ещё не отдала.

Что делает: возвращает таким SKU ставку, которая стояла ДО отката (bid_before строки rollback
в mkt_ozon_bid_journal). Ничего не поднимает выше доотрокатной ставки и не трогает никого,
кроме этих SKU.

Дисциплина: без --apply на площадку не уходит ничего; план ДО отправки пишется в
docs/reports/ozon_restore_converters_<дата>.csv (урок волны 1 — список снятого не сохранили);
решения пишутся в mkt_ozon_bid_journal с action='restore_converter'.

Запуск:
  ./venv/bin/python tools/ozon_restore_converters.py --account oz_acc1            # dry-run
  ./venv/bin/python tools/ozon_restore_converters.py --account oz_acc1 --apply    # по команде Сергея
"""
import argparse
import csv
import datetime as dt
import json
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from core import db  # noqa: E402
from tools.ozon_bid_ramp import _apply_bids  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'reports'

# SKU, которые откатили, хотя они давали заказы: заказы считаем по SKU целиком (все кампании),
# текущую ставку — по последнему снимку ozon_bids.
PLAN_SQL = """
WITH rb AS (
  SELECT campaign_id::text campaign_id, sku, offer_id, name,
         max(bid_before) pre_rollback_bid, max(bid_after) rolled_to, max(decided_on) decided_on
    FROM mkt_ozon_bid_journal
   WHERE account=%(acc)s AND week_start=%(week)s AND action='rollback' AND applied
   GROUP BY 1,2,3,4),
perf_sku AS (
  SELECT sku::text sku, sum(orders_qty) ord, sum(orders_money) rev, sum(money_spent) spend,
         sum(clicks) clicks, sum(views) views
    FROM mkt_ozon_ads_sku_daily
   WHERE account=%(acc)s AND stat_date >= %(since)s GROUP BY 1),
perf_link AS (
  SELECT campaign_id::text campaign_id, sku::text sku, sum(orders_qty) ord
    FROM mkt_ozon_ads_sku_daily
   WHERE account=%(acc)s AND stat_date >= %(since)s GROUP BY 1,2),
cur AS (
  SELECT campaign_id::text campaign_id, sku::text sku, max(bid) bid FROM ozon_bids
   WHERE account=%(acc)s AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%(acc)s)
   GROUP BY 1,2)
SELECT rb.campaign_id, rb.sku, rb.offer_id, rb.name,
       rb.pre_rollback_bid::float pre_bid, rb.rolled_to::float rolled_to,
       c.bid::float cur_bid,
       ps.ord::float ord, ps.rev::float rev, ps.spend::float spend,
       ps.clicks::int clicks, ps.views::int views,
       coalesce(pl.ord,0)::float ord_link
  FROM rb
  JOIN perf_sku ps ON ps.sku = rb.sku AND ps.ord > 0
  LEFT JOIN perf_link pl ON pl.campaign_id = rb.campaign_id AND pl.sku = rb.sku
  LEFT JOIN cur c ON c.campaign_id = rb.campaign_id AND c.sku = rb.sku
 ORDER BY ps.rev DESC
"""


def main(acc, week, since, apply_, batch=100):
    today = dt.date.today()
    rows = db.query(PLAN_SQL, {'acc': acc, 'week': week, 'since': since})
    plan = {}
    skipped = []
    for r in rows:
        cur = float(r['cur_bid'] or 0)
        tgt = float(r['pre_bid'] or 0)
        if tgt <= 0 or cur <= 0 or tgt - cur < 0.01:      # ставка уже не ниже доотрокатной
            skipped.append(r)
            continue
        plan.setdefault(r['campaign_id'], []).append((r['sku'], cur, round(tgt, 2), r))

    total = sum(len(v) for v in plan.values())
    skus = {s for v in plan.values() for s, _, _, _ in v}
    s_cur = sum(c for v in plan.values() for _, c, _, _ in v)
    s_tgt = sum(t for v in plan.values() for _, _, t, _ in v)
    rev = sum(float(r['rev'] or 0) for r in rows)
    spend = sum(float(r['spend'] or 0) for r in rows)
    cross = sum(1 for r in rows if float(r['ord_link'] or 0) == 0)

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f'ozon_restore_converters_{acc}_{today}.csv'
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['campaign_id', 'sku', 'offer_id', 'name', 'bid_now', 'bid_target',
                    'rolled_to', 'orders_sku', 'revenue_sku', 'spend_sku', 'orders_in_this_campaign'])
        for cid, items in plan.items():
            for sku, cur, tgt, r in items:
                w.writerow([cid, sku, r['offer_id'], r['name'], f'{cur:.2f}', f'{tgt:.2f}',
                            r['rolled_to'], r['ord'], round(float(r['rev'] or 0)),
                            round(float(r['spend'] or 0)), r['ord_link']])

    print(f'возврат конвертеров {acc}: откачено по ошибке {len(rows)} связок / {len({r["sku"] for r in rows})} SKU; '
          f'у {cross} связок заказ пришёл в другой кампании')
    print(f'  их факт с {since}: {rev:,.0f} ₽ выручки при расходе {spend:,.0f} ₽ '
          f'(ДРР {100 * spend / rev if rev else 0:.1f} %)')
    print(f'  к отправке {total} связок / {len(skus)} SKU в {len(plan)} кампаниях: '
          f'сумма ставок {s_cur:,.0f} → {s_tgt:,.0f} ₽ (+{s_tgt - s_cur:,.0f}); пропущено {len(skipped)}')
    print(f'  план сохранён: {path}' + ('' if apply_ else '   [DRY-RUN, на площадку ничего не уходит]'))
    if not apply_:
        return

    sent = failed = 0
    for cid, items in plan.items():
        for i in range(0, len(items), batch):
            chunk = items[i:i + batch]
            code, resp = _apply_bids(acc, cid, [(s, t) for s, _, t, _ in chunk])
            ok = code in (200, 201)
            sent += len(chunk) if ok else 0
            failed += 0 if ok else len(chunk)
            for sku, cur, tgt, r in chunk:
                db.execute("""INSERT INTO mkt_ozon_bid_journal
                      (decided_on,week_start,account,campaign_id,sku,offer_id,name,tier,action,
                       bid_before,bid_after,reason,applied,applied_at,api_response,review_on)
                      VALUES (%s,%s,%s,%s,%s,%s,%s,'green','restore_converter',%s,%s,%s,%s,now(),%s,%s)
                    ON CONFLICT (week_start,account,campaign_id,sku) DO UPDATE SET
                      decided_on=EXCLUDED.decided_on, action=EXCLUDED.action,
                      bid_before=EXCLUDED.bid_before, bid_after=EXCLUDED.bid_after,
                      reason=EXCLUDED.reason, applied=EXCLUDED.applied,
                      applied_at=EXCLUDED.applied_at, api_response=EXCLUDED.api_response,
                      review_on=EXCLUDED.review_on""",
                    (today, week, acc, cid, sku, r['offer_id'], r['name'], cur, tgt,
                     f"откат 18.08 был ошибочным: SKU дал {r['ord']:.0f} заказов на "
                     f"{float(r['rev'] or 0):,.0f} ₽ с {since} "
                     f"({'заказ в другой кампании' if float(r['ord_link'] or 0) == 0 else 'заказ в этой же кампании, статистика опоздала'}); "
                     f"ставка {cur:.2f} → доотрокатная {tgt:.2f} ₽",
                     ok, f'{code} {resp[:120]}', today + dt.timedelta(days=14)))
            print(f'  кампания {cid}: {len(chunk)} ставок -> {code} {resp[:60]}')
            time.sleep(1.0)
    print(f'итог: применено {sent}, ошибок {failed}; решения записаны в mkt_ozon_bid_journal '
          f"(week_start={week}, action=restore_converter)")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', default='oz_acc1')
    ap.add_argument('--week', default='2026-08-17', help='неделя журнала, в которой лежит откат')
    ap.add_argument('--since', default='2026-08-08', help='с какой даты считаем заказы SKU')
    ap.add_argument('--apply', action='store_true', help='ОТПРАВИТЬ ставки в Ozon (только по команде Сергея)')
    a = ap.parse_args()
    main(a.account, a.week, a.since, a.apply)
