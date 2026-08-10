# поток: mkt
# ozon_acc1_plan.py — план по рекламе Ozon acc1: поднять ставку / снизить / убрать (две волны).
# Ключ связки — offer_id (sku у ставок, витрины и отчётов площадки живут в разных пространствах).
# Пишет docs/reports/ozon_acc1_{raise,cut,wave1,wave2}.csv + сводку ozon_acc1_plan.md.
import sys, csv, json
sys.path.insert(0, '/opt/mp-analytics')
from core import db

ACC = 'oz_acc1'
PPO_NEW, MARGIN_UP, KPI, SHARE = 0.05, 2.4, 25.0, 0.35   # см. docs/reports/ozon_bid_plan.md §0
BUCKET_CR = {'от 0 до 1000': .1149, 'от 1000-2000': .0860, 'от 2000 до 5000': .1042,
             'от 5000 до 10000': .0871, 'от 10000 до 500 тыс': .0854, 'Пустые РК': .1435,
             'Эксперимент1': .0366}
REP = '/tmp/claude-0/-opt-mp-analytics/81d9d6b2-467b-43ba-a7be-598f803a8a33/scratchpad/rep.bin'

O = []
p = lambda s='': (O.append(str(s)), print(s))

# --- справочники ------------------------------------------------------------
bridge = {r['sku']: r['offer_id'] for r in
          db.query("SELECT sku, offer_id FROM ozon_product WHERE account=%s", (ACC,))}

mc = {r['offer_id']: r for r in db.query("""
    SELECT offer_id, name, our_price, margin_own_live, cogs_source
    FROM mkt_ozon_margin_control
    WHERE account=%s AND captured_date=(SELECT max(captured_date) FROM mkt_ozon_margin_control WHERE account=%s)
""", (ACC, ACC))}

# продажи 90 дней (отправления, есть с 01.05.2026) и 180 дней (транзакции, с 01.01.2026)
s90 = {r['offer_id']: (r['qty'], float(r['rev'])) for r in db.query("""
    SELECT pr->>'offer_id' offer_id, sum((pr->>'quantity')::int) qty,
           sum((pr->>'price')::numeric*(pr->>'quantity')::int) rev
    FROM raw_ozon_posting t CROSS JOIN LATERAL jsonb_array_elements(t.payload->'products') pr
    WHERE t.account=%s AND (t.payload->>'in_process_at')::timestamptz >= now()-interval '90 days'
      AND t.payload->>'status' <> 'cancelled' GROUP BY 1""", (ACC,))}
s180 = {r['offer_id'] for r in db.query("""
    SELECT DISTINCT p.offer_id FROM raw_ozon_transaction t
      CROSS JOIN LATERAL jsonb_array_elements(t.payload->'items') i
      JOIN ozon_product p ON p.account=t.account AND p.sku=(i->>'sku')
    WHERE t.account=%s AND (t.payload->>'operation_date')::date >= current_date-180
      AND t.payload->>'operation_type' LIKE '%%DeliveredToCustomer%%'""", (ACC,))}

cr_sku = {r['sku']: float(r['cr']) for r in db.query("""
    SELECT sku::text sku, max(cr_bucket) cr FROM mkt_ozon_query_econ
    WHERE account=%s AND cr_bucket IS NOT NULL GROUP BY 1""", (ACC,))}

# факт июля по каждой позиции: расход и выручка от рекламы (отчёт Performance API).
# Ключ — (кампания, sku): один и тот же товар крутится сразу в нескольких кампаниях.
TITLES = {str(r['campaign_id']): r['campaign_title'] for r in
          db.query("SELECT DISTINCT campaign_id, campaign_title FROM ozon_bids WHERE account=%s", (ACC,))}
ads = {}
rub = lambda s: float((s or '0').replace('\xa0', '').replace(' ', '').replace(',', '.') or 0)
for cid, blk in json.load(open(REP)).items():
    for row in blk['report']['rows']:
        if not row.get('sku'):
            continue
        a = ads.setdefault((TITLES.get(cid, cid), row['sku']), {'spend': 0.0, 'rev': 0.0, 'views': 0})
        a['spend'] += rub(row.get('moneySpent'))
        a['rev'] += rub(row.get('ordersMoney'))
        a['views'] += int(rub(row.get('views')))

bids = db.query("""SELECT sku::text sku, campaign_title camp, max(bid) bid FROM ozon_bids
    WHERE account=%s AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%s)
    GROUP BY 1,2""", (ACC, ACC))

# --- разбор -----------------------------------------------------------------
up, cut, w1, w2 = [], [], [], []
for b in bids:
    sku, camp, cur = b['sku'], b['camp'] or '', float(b['bid'])
    off = bridge.get(sku)
    m = mc.get(off) if off else None
    qty, rev = s90.get(off, (0, 0.0))
    a = ads.get((camp, sku), {'spend': 0.0, 'rev': 0.0, 'views': 0})
    name = (m['name'] if m and m['name'] else '')[:60]
    base = [sku, off or '', name, camp, cur, qty, round(rev), round(a['spend']), round(a['rev'])]

    if qty == 0:                                   # не продавалось 90 дней
        (w2 if off in s180 else w1).append(base + ['продажи 90-180 дн назад' if off in s180
                                                   else 'нет продаж 180 дн'])
        continue
    if not m or m['our_price'] is None or m['margin_own_live'] is None:
        cut.append(base + [None, None, 'продаётся, но маржа не посчитана'])
        continue

    price, mg = float(m['our_price']), float(m['margin_own_live']) + MARGIN_UP
    cr = cr_sku.get(sku) or BUCKET_CR.get(camp, 0.05)
    ceil = (price * mg / 100.0 - price * PPO_NEW) * cr          # CPC, где реклама съедает всю прибыль
    if mg < KPI:
        cut.append(base + [round(ceil, 1), round(mg, 1), 'маржа ниже KPI 25 %'])
    elif ceil <= cur:
        cut.append(base + [round(ceil, 1), round(mg, 1), 'ставка выше потолка'])
    else:
        new = min(ceil * SHARE, cur * 3, 60.0)                  # 35 % потолка, рост не больше ×3
        if new >= cur + 2:
            up.append(base + [round(ceil, 1), round(mg, 1), round(new)])

H = ['sku', 'offer_id', 'name', 'campaign', 'bid_now', 'qty_90d', 'revenue_90d', 'ad_spend_july', 'ad_revenue_july']
for fn, rows, hdr in (('ozon_acc1_raise.csv', up,  H + ['ceiling', 'margin_pct', 'bid_new']),
                      ('ozon_acc1_cut.csv',   cut, H + ['ceiling', 'margin_pct', 'reason']),
                      ('ozon_acc1_wave1.csv', w1,  H + ['reason']),
                      ('ozon_acc1_wave2.csv', w2,  H + ['reason'])):
    with open('/opt/mp-analytics/docs/reports/' + fn, 'w', newline='') as f:
        w = csv.writer(f); w.writerow(hdr); w.writerows(sorted(rows, key=lambda x: -x[6]))

med = lambda v: sorted(v)[len(v) // 2] if v else 0
uniq = lambda rows: len({x[1] for x in rows})
p(f"Ozon acc1 — записей «товар × кампания»: {len(bids)}, уникальных товаров: {len({b['sku'] for b in bids})}")
p(f"поднять {len(up)} ({uniq(up)} товаров) | снизить/чинить {len(cut)} ({uniq(cut)}) | "
  f"волна 1 {len(w1)} ({uniq(w1)}) | волна 2 {len(w2)} ({uniq(w2)})")
p("")
p("=== ПОДНЯТЬ СТАВКУ ===")
p(f"выручка 90 дн {sum(x[6] for x in up)/1e6:.1f} млн ₽, расход июля {sum(x[7] for x in up):,.0f} ₽, "
  f"выручка от рекламы {sum(x[8] for x in up)/1e6:.2f} млн ₽")
p(f"ставка медиана {med([x[4] for x in up]):.0f} → {med([x[11] for x in up]):.0f} ₽ при потолке {med([x[9] for x in up]):.0f} ₽")
byc = {}
for x in up:
    byc.setdefault(x[3], []).append(x)
for c, v in sorted(byc.items(), key=lambda kv: -sum(y[6] for y in kv[1])):
    p(f"  {c[:24]:26} {len(v):4} поз  выручка {sum(y[6] for y in v)/1e6:5.2f} млн  "
      f"расход июля {sum(y[7] for y in v):>7,.0f} ₽  ставка {med([y[4] for y in v]):.0f} → {med([y[11] for y in v]):.0f} ₽")
p("")
p("=== СНИЗИТЬ / ЧИНИТЬ ===")
for reason in ('маржа ниже KPI 25 %', 'ставка выше потолка', 'продаётся, но маржа не посчитана'):
    v = [x for x in cut if x[-1] == reason]
    if v:
        p(f"  {reason:36} {len(v):5} поз  выручка 90 дн {sum(y[6] for y in v)/1e6:5.2f} млн  расход июля {sum(y[7] for y in v):>7,.0f} ₽")
p("")
p("=== УБРАТЬ ИЗ КАМПАНИЙ ===")
for tag, rows in (('волна 1 (мертвы 180 дн)', w1), ('волна 2 (жили 90-180 дн)', w2)):
    p(f"  {tag:26} {len(rows):5} записей / {uniq(rows):5} товаров  расход июля {sum(y[7] for y in rows):>8,.0f} ₽")
    byc = {}
    for x in rows:
        c = byc.setdefault(x[3], [0, 0.0]); c[0] += 1; c[1] += x[7]
    for c, (n, sp) in sorted(byc.items(), key=lambda kv: -kv[1][1])[:7]:
        p(f"      {c[:24]:26} {n:5} поз  {sp:>8,.0f} ₽")

open('/opt/mp-analytics/docs/reports/ozon_acc1_plan.md', 'w').write(
    "# Ozon acc1 — план по рекламе\n\n```\n" + "\n".join(O) + "\n```\n")
