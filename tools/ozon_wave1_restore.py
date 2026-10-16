# поток: mkt
"""tools/ozon_wave1_restore.py — вернуть в кампании половину когорты, снятой волной 1 (09.08.2026),
вторую половину оставить снятой как контроль.

Зачем. Волна 1 сняла с рекламы 11 943 связки товар×кампания (8 834 SKU) acc1. Неделя поиска
10–17.08 показала: у снятой когорты показы упали на 57 % при неизменной позиции — то есть реклама
тянула за собой органический показ (эффект ореола), а не только платный трафик. Одновременно
видно встречный факт: часть когорты продолжает продавать и без рекламы. Различить «ореол» и
«сезон» на всей когорте нельзя — нет контроля. Поэтому возвращаем ровно половину.

Метод. Делим КОГОРТУ ПО SKU (не по связкам, чтобы товар целиком попал в одну группу) устойчивым
хэшем md5(SEED:sku) — при повторном запуске деление то же самое. Группа A возвращается в свою
кампанию на ту же ставку, что стояла 08.08 (это пол соответствующей кампании, 6–17 ₽), группа B
не трогается вообще. Сверка через 3 недели по показам (ozon_search_product) и заказам
(mkt_ozon_ads_sku_daily + raw_ozon_posting): разница A−B и есть цена снятия.

Риск, который надо держать в голове. Бюджет у кампаний общий. Возврат хвоста может отъедать
показы у ядра — ровно так же, как в июле разгон ядра погасил хвост. Смотреть ежедневный расход
и показы ядра первые дни после возврата.

Дисциплина: без --apply на площадку не уходит ничего; полный список когорты с группами пишется
в docs/reports/ozon_wave1_restore_<acc>_<дата>.csv ДО отправки; решения обеих групп пишутся
в mkt_ozon_bid_journal (action='wave1_restore' и 'wave1_control').

Запуск:
  ./venv/bin/python tools/ozon_wave1_restore.py --account oz_acc1           # dry-run
  ./venv/bin/python tools/ozon_wave1_restore.py --account oz_acc1 --apply   # по команде Сергея
"""
import argparse
import csv
import datetime as dt
import hashlib
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from core import db  # noqa: E402
from tools.ozon_core_push import _add_products  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parent.parent / 'docs' / 'reports'
SEED = '20260819'          # фиксированное зерно деления: повторный запуск даёт те же группы
BATCH = 100

# Когорта = связки, которые были в снимке ставок ДО волны и исчезли ПОСЛЕ неё,
# и которых нет в кампании сейчас (иначе товар уже вернулся другим путём).
COHORT_SQL = """
WITH before AS (
  SELECT campaign_id::text campaign_id, max(campaign_title) campaign_title, sku::text sku,
         max(title) title, max(bid) bid
    FROM ozon_bids WHERE account=%(acc)s AND captured_at::date=%(d_before)s GROUP BY 1,3),
after AS (
  SELECT campaign_id::text campaign_id, sku::text sku
    FROM ozon_bids WHERE account=%(acc)s AND captured_at::date=%(d_after)s GROUP BY 1,2),
now_in AS (
  SELECT campaign_id::text campaign_id, sku::text sku, max(state) state
    FROM ozon_bids WHERE account=%(acc)s
     AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%(acc)s) GROUP BY 1,2),
live_camp AS (
  SELECT campaign_id::text campaign_id FROM ozon_bids WHERE account=%(acc)s
     AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%(acc)s)
     AND state='CAMPAIGN_STATE_RUNNING' GROUP BY 1),
booked AS (
  SELECT campaign_id::text campaign_id, sku::text sku FROM mkt_ozon_bid_journal
   WHERE account=%(acc)s AND week_start=%(week)s GROUP BY 1,2)
SELECT b.campaign_id, b.campaign_title, b.sku, b.title, b.bid::float bid
  FROM before b
  JOIN live_camp lc ON lc.campaign_id = b.campaign_id
  LEFT JOIN after a  ON a.campaign_id = b.campaign_id AND a.sku = b.sku
  LEFT JOIN now_in n ON n.campaign_id = b.campaign_id AND n.sku = b.sku
  LEFT JOIN booked k ON k.campaign_id = b.campaign_id AND k.sku = b.sku
 WHERE a.sku IS NULL AND n.sku IS NULL AND k.sku IS NULL
 ORDER BY b.campaign_id, b.sku
"""


def group_of(sku):
    """A — возвращаем, B — контроль. Деление устойчивое: зависит только от SEED и sku."""
    return 'A' if hashlib.md5(f'{SEED}:{sku}'.encode()).digest()[0] % 2 == 0 else 'B'


def journal(acc, week, today, cid, sku, title, bid_before, bid_after, action, applied, resp, review):
    db.execute("""INSERT INTO mkt_ozon_bid_journal
          (decided_on,week_start,account,campaign_id,sku,offer_id,name,tier,action,
           bid_before,bid_after,reason,applied,applied_at,api_response,review_on)
          VALUES (%s,%s,%s,%s,%s,NULL,%s,'brown',%s,%s,%s,%s,%s,now(),%s,%s)
        ON CONFLICT (week_start,account,campaign_id,sku) DO UPDATE SET
          decided_on=EXCLUDED.decided_on, action=EXCLUDED.action,
          bid_before=EXCLUDED.bid_before, bid_after=EXCLUDED.bid_after,
          reason=EXCLUDED.reason, applied=EXCLUDED.applied, applied_at=EXCLUDED.applied_at,
          api_response=EXCLUDED.api_response, review_on=EXCLUDED.review_on""",
        (today, week, acc, cid, sku, title, action, bid_before, bid_after,
         ('возврат половины когорты волны 1: проверяем эффект ореола, ставка = пол кампании'
          if action == 'wave1_restore' else
          'контрольная половина когорты волны 1: остаётся снятой до сверки через 3 недели'),
         applied, resp, review))


def main(acc, week, d_before, d_after, apply_):
    today = dt.date.today()
    rows = db.query(COHORT_SQL, {'acc': acc, 'week': week, 'd_before': d_before, 'd_after': d_after})
    for r in rows:
        r['grp'] = group_of(r['sku'])
    a_rows = [r for r in rows if r['grp'] == 'A']
    b_rows = [r for r in rows if r['grp'] == 'B']
    a_sku = {r['sku'] for r in a_rows}
    b_sku = {r['sku'] for r in b_rows}

    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f'ozon_wave1_restore_{acc}_{today}.csv'
    with open(path, 'w', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        w.writerow(['group', 'campaign_id', 'campaign_title', 'sku', 'title', 'bid_target'])
        for r in rows:
            w.writerow([r['grp'], r['campaign_id'], r['campaign_title'], r['sku'],
                        r['title'], f"{float(r['bid'] or 0):.2f}"])

    plan = {}
    for r in a_rows:
        plan.setdefault(r['campaign_id'], []).append(r)
    day_cost = sum(float(r['bid'] or 0) for r in a_rows)
    print(f'когорта волны 1 {acc}: {len(rows)} связок / {len(a_sku | b_sku)} SKU '
          f'(снято {d_after}, в кампаниях сейчас нет)')
    print(f'  A — возвращаем: {len(a_rows)} связок / {len(a_sku)} SKU в {len(plan)} кампаниях; '
          f'сумма ставок {day_cost:,.0f} ₽ (это цена одного клика по каждой, не дневной расход)')
    print(f'  B — контроль:   {len(b_rows)} связок / {len(b_sku)} SKU, не трогаем')
    print(f'  список сохранён: {path}' + ('' if apply_ else '   [DRY-RUN, на площадку ничего не уходит]'))
    for cid, items in sorted(plan.items(), key=lambda kv: -len(kv[1])):
        print(f'    кампания {cid} {items[0]["campaign_title"]}: {len(items)}')
    if not apply_:
        return

    sent = failed = 0
    for cid, items in plan.items():
        for i in range(0, len(items), BATCH):
            chunk = items[i:i + BATCH]
            code, resp = _add_products(acc, cid, [(r['sku'], float(r['bid'] or 0)) for r in chunk])
            ok = code in (200, 201)
            sent += len(chunk) if ok else 0
            failed += 0 if ok else len(chunk)
            for r in chunk:
                journal(acc, week, today, cid, r['sku'], r['title'], 0, float(r['bid'] or 0),
                        'wave1_restore', ok, f'{code} {resp[:120]}', today + dt.timedelta(days=21))
            print(f'  кампания {cid}: +{len(chunk)} товаров -> {code} {resp[:60]}')
            time.sleep(1.0)
    for r in b_rows:
        journal(acc, week, today, r['campaign_id'], r['sku'], r['title'], 0, 0,
                'wave1_control', False, None, today + dt.timedelta(days=21))
    print(f'итог: возвращено {sent}, ошибок {failed}; контроль {len(b_rows)} связок записан '
          f'без изменений (mkt_ozon_bid_journal, week_start={week})')


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', default='oz_acc1')
    ap.add_argument('--week', default='2026-08-17')
    ap.add_argument('--before', default='2026-08-08', help='снимок ставок ДО волны')
    ap.add_argument('--after', default='2026-08-09', help='снимок ставок ПОСЛЕ волны')
    ap.add_argument('--apply', action='store_true', help='ОТПРАВИТЬ в Ozon (только по команде Сергея)')
    a = ap.parse_args()
    main(a.account, a.week, a.before, a.after, a.apply)
