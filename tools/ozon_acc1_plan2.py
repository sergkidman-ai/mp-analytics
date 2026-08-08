# поток: mkt
# ozon_acc1_plan2.py — план рекламы Ozon acc1, редакция 2 (правила Сергея 08.08.2026):
#   KPI маржи снижен 25 % -> 17 % (больше позиций допускаем в рекламу);
#   окно продаж 180 дней (транзакции), 90 дней — только как признак «живой сейчас»;
#   + оценка органики (сколько позиция продаёт БЕЗ рекламы), + каннибализация,
#   + кандидаты вне РК. Ключ связки — offer_id.
import sys, csv, json, re
sys.path.insert(0, '/opt/mp-analytics')
from core import db

ACC = 'oz_acc1'
PPO_NEW, MARGIN_UP, KPI, SHARE = 0.05, 3.5, 17.0, 0.35
BUCKET_CR = {'от 0 до 1000': .1149, 'от 1000-2000': .0860, 'от 2000 до 5000': .1042,
             'от 5000 до 10000': .0871, 'от 10000 до 500 тыс': .0854, 'Пустые РК': .1435,
             'Эксперимент1': .0366}
REP = '/tmp/claude-0/-opt-mp-analytics/81d9d6b2-467b-43ba-a7be-598f803a8a33/scratchpad/rep.bin'
D = '/opt/mp-analytics/docs/reports/'
# 0002 — материнская карточка картриджа, 00021/000213 — карточки принтеров на тот же картридж
base_code = lambda off: off[:4] if off and off[:4].isdigit() else (off or '')

O = []
p = lambda s='': (O.append(str(s)), print(s))

# --- справочники ------------------------------------------------------------
prods = db.query("SELECT sku, offer_id, name, is_archived FROM ozon_product WHERE account=%s", (ACC,))
bridge = {r['sku']: r['offer_id'] for r in prods}
pname = {r['offer_id']: r['name'] for r in prods}
alive = {r['offer_id'] for r in prods if not r['is_archived']}

mc = {r['offer_id']: r for r in db.query("""
    SELECT offer_id, name, our_price, margin_own_live, cogs_source FROM mkt_ozon_margin_control
    WHERE account=%s AND captured_date=(SELECT max(captured_date) FROM mkt_ozon_margin_control WHERE account=%s)
""", (ACC, ACC))}

# продажи 90 дней — отправления (цена в payload, есть с 01.05.2026)
s90 = {r['offer_id']: (r['qty'], float(r['rev'])) for r in db.query("""
    SELECT pr->>'offer_id' offer_id, sum((pr->>'quantity')::int) qty,
           sum((pr->>'price')::numeric*(pr->>'quantity')::int) rev
    FROM raw_ozon_posting t CROSS JOIN LATERAL jsonb_array_elements(t.payload->'products') pr
    WHERE t.account=%s AND (t.payload->>'in_process_at')::timestamptz >= now()-interval '90 days'
      AND t.payload->>'status' <> 'cancelled' GROUP BY 1""", (ACC,))}

# продажи 180 дней — транзакции: accruals_for_sale делим поровну на позиции чека
s180 = {r['offer_id']: (r['qty'], float(r['rev'])) for r in db.query("""
    SELECT p.offer_id, count(*) qty,
           sum((t.payload->>'accruals_for_sale')::numeric / jsonb_array_length(t.payload->'items')) rev
    FROM raw_ozon_transaction t
      CROSS JOIN LATERAL jsonb_array_elements(t.payload->'items') i
      JOIN ozon_product p ON p.account=t.account AND p.sku=(i->>'sku')
    WHERE t.account=%s AND (t.payload->>'operation_date')::date >= current_date-180
      AND t.payload->>'operation_type' LIKE '%%DeliveredToCustomer%%' GROUP BY 1""", (ACC,))}

# выручка июля — для доли рекламы в продажах (органика)
jul = {r['offer_id']: float(r['rev']) for r in db.query("""
    SELECT pr->>'offer_id' offer_id, sum((pr->>'price')::numeric*(pr->>'quantity')::int) rev
    FROM raw_ozon_posting t CROSS JOIN LATERAL jsonb_array_elements(t.payload->'products') pr
    WHERE t.account=%s AND (t.payload->>'in_process_at')::timestamptz >= '2026-07-01'
      AND (t.payload->>'in_process_at')::timestamptz < '2026-08-01'
      AND t.payload->>'status' <> 'cancelled' GROUP BY 1""", (ACC,))}

cr_sku = {r['sku']: float(r['cr']) for r in db.query("""
    SELECT sku::text sku, max(cr_bucket) cr FROM mkt_ozon_query_econ
    WHERE account=%s AND cr_bucket IS NOT NULL GROUP BY 1""", (ACC,))}

TITLES = {str(r['campaign_id']): r['campaign_title'] for r in
          db.query("SELECT DISTINCT campaign_id, campaign_title FROM ozon_bids WHERE account=%s", (ACC,))}
ads = {}
rub = lambda s: float((s or '0').replace('\xa0', '').replace(' ', '').replace(',', '.') or 0)
for cid, blk in json.load(open(REP)).items():
    for row in blk['report']['rows']:
        if row.get('sku'):
            a = ads.setdefault((TITLES.get(cid, cid), row['sku']), {'spend': 0.0, 'rev': 0.0, 'views': 0})
            a['spend'] += rub(row.get('moneySpent')); a['rev'] += rub(row.get('ordersMoney'))
            a['views'] += int(rub(row.get('views')))
ad_by_off = {}                                        # суммарно по товару (для органики)
for (c, sku), a in ads.items():
    o = bridge.get(sku)
    if o:
        x = ad_by_off.setdefault(o, [0.0, 0.0]); x[0] += a['spend']; x[1] += a['rev']

bids = db.query("""SELECT sku::text sku, campaign_title camp, max(bid) bid FROM ozon_bids
    WHERE account=%s AND captured_at=(SELECT max(captured_at) FROM ozon_bids WHERE account=%s)
    GROUP BY 1,2""", (ACC, ACC))
in_camp = {bridge.get(b['sku']) for b in bids} - {None}

# --- разбор ставок ----------------------------------------------------------
up, cut, w1, w2 = [], [], [], []
for b in bids:
    sku, camp, cur = b['sku'], b['camp'] or '', float(b['bid'])
    off = bridge.get(sku)
    m = mc.get(off) if off else None
    q90, r90 = s90.get(off, (0, 0.0))
    q180, r180 = s180.get(off, (0, 0.0))
    a = ads.get((camp, sku), {'spend': 0.0, 'rev': 0.0, 'views': 0})
    # органика = какая доля выручки июля пришла НЕ с рекламы (None — в июле продаж не было)
    jr = jul.get(off, 0.0); adrev = ad_by_off.get(off, [0, 0])[1]
    org = round(100 * (1 - min(adrev, jr) / jr), 0) if jr > 0 else None
    name = (m['name'] if m and m['name'] else pname.get(off) or '')[:60]
    base = [sku, off or '', name, camp, cur, q180, round(r180), q90, round(a['spend']), round(a['rev']), org]

    if q180 == 0 and q90 == 0:                       # мертва и за полгода, и за 90 дней
        w1.append(base + ['нет продаж 180 дн']); continue
    if q90 == 0:                                     # жила, но за 90 дней тишина
        w2.append(base + ['продажи 90-180 дн назад']); continue
    if q180 == 0:                                    # продаётся сейчас, транзакций 180 дн нет
        q180, r180 = q90, r90
        base[5], base[6] = q90, round(r90)
    if not m or m['our_price'] is None or m['margin_own_live'] is None:
        cut.append(base + [None, None, 'продаётся, но маржа не посчитана']); continue

    price, mg = float(m['our_price']), float(m['margin_own_live']) + MARGIN_UP
    cr = cr_sku.get(sku) or BUCKET_CR.get(camp, 0.05)
    ceil = (price * mg / 100.0 - price * PPO_NEW) * cr
    if mg < KPI:
        cut.append(base + [round(ceil, 1), round(mg, 1), 'маржа ниже KPI 17 %'])
    elif ceil <= cur:
        cut.append(base + [round(ceil, 1), round(mg, 1), 'ставка выше потолка'])
    else:
        new = min(ceil * SHARE, cur * 3, 60.0)
        if new >= cur + 2:
            grp = ('C. в июле не продавалась' if org is None else
                   'A. реклама тянет' if org < 60 else 'B. продаёт сама')
            up.append(base + [round(ceil, 1), round(mg, 1), round(new), grp])

H = ['sku', 'offer_id', 'name', 'campaign', 'bid_now', 'qty_180d', 'revenue_180d', 'qty_90d',
     'ad_spend_july', 'ad_revenue_july', 'organic_pct']
for fn, rows, hdr in (('ozon_acc1_raise.csv', up,  H + ['ceiling', 'margin_pct', 'bid_new', 'group']),
                      ('ozon_acc1_cut.csv',   cut, H + ['ceiling', 'margin_pct', 'reason']),
                      ('ozon_acc1_wave1.csv', w1,  H + ['reason']),
                      ('ozon_acc1_wave2.csv', w2,  H + ['reason'])):
    with open(D + fn, 'w', newline='') as f:
        w = csv.writer(f); w.writerow(hdr); w.writerows(sorted(rows, key=lambda x: -x[6]))

# --- каннибализация ---------------------------------------------------------
# (1) один товар в нескольких кампаниях; (2) разные фасовки одного картриджа в рекламе
multi = {}
for b in bids:
    multi.setdefault(b['sku'], []).append(b['camp'])
dup_camp = {k: v for k, v in multi.items() if len(v) > 1}
dup_spend = sum(a['spend'] for (c, s), a in ads.items() if s in dup_camp)

fam = {}
for off in in_camp:
    fam.setdefault(base_code(off), set()).add(off)
fam = {k: v for k, v in fam.items() if len(v) > 1 and k}
fam_rows = []
for code, offs in fam.items():
    sp = sum(ad_by_off.get(o, [0, 0])[0] for o in offs)
    rv = sum(s180.get(o, (0, 0.0))[1] for o in offs)
    sold = [o for o in offs if s180.get(o, (0, 0))[0] > 0 or s90.get(o, (0, 0))[0] > 0]
    fam_rows.append([code, len(offs), len(sold), round(sp), round(rv),
                     '; '.join(sorted(offs))[:120], (pname.get(sorted(offs)[0]) or '')[:60]])
fam_rows.sort(key=lambda x: -x[3])
with open(D + 'ozon_acc1_cannibal.csv', 'w', newline='') as f:
    w = csv.writer(f); w.writerow(['code', 'offers_in_ads', 'offers_with_sales', 'ad_spend_july',
                                   'revenue_180d', 'offer_ids', 'name']); w.writerows(fam_rows)

# --- кандидаты ВНЕ рекламы --------------------------------------------------
outs = []
for off, (q, rv) in s180.items():
    if off in in_camp or off not in alive:
        continue
    m = mc.get(off)
    mg = (float(m['margin_own_live']) + MARGIN_UP) if m and m['margin_own_live'] is not None else None
    price = float(m['our_price']) if m and m['our_price'] is not None else None
    q90 = s90.get(off, (0, 0.0))[0]
    ceil = (price * mg / 100.0 - price * PPO_NEW) * 0.085 if (price and mg) else None
    outs.append([off, (pname.get(off) or '')[:60], q, round(rv), q90,
                 round(mg, 1) if mg else None, round(ceil, 1) if ceil else None,
                 round(min(ceil * SHARE, 60.0)) if ceil and ceil > 0 else None])
ok = [x for x in outs if x[5] is not None and x[5] >= KPI and x[7]]
ok.sort(key=lambda x: -x[3])
with open(D + 'ozon_acc1_add.csv', 'w', newline='') as f:
    w = csv.writer(f); w.writerow(['offer_id', 'name', 'qty_180d', 'revenue_180d', 'qty_90d',
                                   'margin_pct', 'ceiling', 'bid_start']); w.writerows(ok)

# --- сводка -----------------------------------------------------------------
med = lambda v: sorted(v)[len(v) // 2] if v else 0
uniq = lambda rows: len({x[1] for x in rows})
p(f"Ozon acc1 (KPI {KPI:.0f} %, окно продаж 180 дн) — записей «товар × кампания» {len(bids)}, товаров {len({b['sku'] for b in bids})}")
p(f"поднять {len(up)} ({uniq(up)} тов.) | снизить/чинить {len(cut)} ({uniq(cut)}) | волна1 {len(w1)} ({uniq(w1)}) | волна2 {len(w2)} ({uniq(w2)})")
p("")
p("=== ПОДНЯТЬ ===")
for g in ('A. реклама тянет', 'B. продаёт сама', 'C. в июле не продавалась'):
    v = [x for x in up if x[-1] == g]
    if not v: continue
    orgs = [y[10] for y in v if y[10] is not None]
    p(f"  {g:26} {len(v):5} поз / {uniq(v):5} тов.  выручка 180 дн {sum(y[6] for y in v)/1e6:5.2f} млн  "
      f"расход июля {sum(y[8] for y in v):>7,.0f} ₽  ставка {med([y[4] for y in v]):.0f} → {med([y[13] for y in v]):.0f} ₽"
      f"  органика мед. {med(orgs):.0f} %" if orgs else
      f"  {g:26} {len(v):5} поз / {uniq(v):5} тов.  выручка 180 дн {sum(y[6] for y in v)/1e6:5.2f} млн  "
      f"расход июля {sum(y[8] for y in v):>7,.0f} ₽  ставка {med([y[4] for y in v]):.0f} → {med([y[13] for y in v]):.0f} ₽")
p(f"  всего: выручка 180 дн {sum(x[6] for x in up)/1e6:.1f} млн ₽, расход июля {sum(x[8] for x in up):,.0f} ₽, "
  f"выручка от рекламы {sum(x[9] for x in up)/1e6:.2f} млн ₽")
byc = {}
for x in up: byc.setdefault(x[3], []).append(x)
for c, v in sorted(byc.items(), key=lambda kv: -sum(y[6] for y in kv[1])):
    p(f"    {c[:24]:26} {len(v):4} поз  выручка {sum(y[6] for y in v)/1e6:5.2f} млн  "
      f"расход {sum(y[8] for y in v):>7,.0f} ₽  ставка {med([y[4] for y in v]):.0f} → {med([y[13] for y in v]):.0f} ₽")
p("")
p("=== СНИЗИТЬ / ЧИНИТЬ ===")
for reason in ('маржа ниже KPI 17 %', 'ставка выше потолка', 'продаётся, но маржа не посчитана'):
    v = [x for x in cut if x[-1] == reason]
    if v: p(f"  {reason:36} {len(v):5} поз  выручка 180 дн {sum(y[6] for y in v)/1e6:5.2f} млн  расход июля {sum(y[8] for y in v):>7,.0f} ₽")
p("")
p("=== УБРАТЬ ===")
for tag, rows in (('волна 1 (мертвы 180 дн)', w1), ('волна 2 (жили 90-180 дн)', w2)):
    p(f"  {tag:26} {len(rows):5} записей / {uniq(rows):5} товаров  расход июля {sum(y[8] for y in rows):>8,.0f} ₽")
p("")
p("=== КАННИБАЛИЗАЦИЯ ===")
p(f"  один товар в нескольких кампаниях: {len(dup_camp)} товаров, расход июля {dup_spend:,.0f} ₽")
p(f"  семей «один картридж — разные фасовки» в рекламе: {len(fam)}, "
  f"расход июля {sum(x[3] for x in fam_rows):,.0f} ₽; из них где продаёт только одна карточка: "
  f"{len([x for x in fam_rows if x[2] <= 1])} (расход {sum(x[3] for x in fam_rows if x[2] <= 1):,.0f} ₽)")
p("")
p("=== ВНЕ РЕКЛАМЫ, НО ПРОДАЮТСЯ ===")
p(f"  всего вне кампаний с продажами 180 дн: {len(outs)}; из них маржа >= {KPI:.0f} % и есть расчёт: {len(ok)}")
p(f"  их выручка 180 дн {sum(x[3] for x in ok)/1e6:.2f} млн ₽, продавались за 90 дн: {len([x for x in ok if x[4] > 0])}")
p(f"  стартовая ставка медиана {med([x[7] for x in ok]):.0f} ₽ при потолке {med([x[6] for x in ok]):.0f} ₽")

open(D + 'ozon_acc1_plan.md', 'w').write("# Ozon acc1 — план по рекламе (ред. 2)\n\n```\n" + "\n".join(O) + "\n```\n")
