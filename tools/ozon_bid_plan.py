# поток: mkt
# ozon_bid_plan.py — списки ставок Ozon: поднять / снизить / убрать из кампаний.
# Пишет docs/reports/ozon_bid_{raise,cut,drop}.csv, в чат — только сводка.
import sys, csv; sys.path.insert(0,'.')
from core import db
O=[]
def p(s=''): O.append(str(s))

PPO_NEW  = 0.05      # ставка оплаты за заказ после снижения 7 → 5 %
MARGIN_UP = 3.5      # п.п. маржи, которые вернутся после отключения Pro (2.5 %) и Звёздных (1.5 %), консервативно
KPI = 25.0
SHARE = 0.35         # какую долю потолка занимаем ставкой (запас на ошибку CR)
BUCKET_CR = {'от 0 до 1000':0.1149,'от 1000-2000':0.0860,'от 2000 до 5000':0.1042,
             'от 5000 до 10000':0.0871,'от 10000 до 500 тыс':0.0854,'Пустые РК':0.1435,
             'Эксперимент1':0.0366}

sales = {}
for r in db.query("""
  select account, (pr->>'sku') sku, sum((pr->>'quantity')::int) qty,
         sum((pr->>'price')::numeric*(pr->>'quantity')::int) rev
  from raw_ozon_posting, lateral jsonb_array_elements(payload->'products') pr
  where (payload->>'in_process_at')::timestamptz >= now() - interval '90 days'
    and payload->>'status' <> 'cancelled'
  group by 1,2"""):
    sales[(r['account'], r['sku'])] = (r['qty'], float(r['rev']))

cr_sku = {(r['account'], r['sku']): float(r['cr'])
          for r in db.query("""select account, sku::text sku, max(cr_bucket) cr from mkt_ozon_query_econ
                               where cr_bucket is not null group by 1,2""")}

mc = {(r['account'], r['sku']): r for r in db.query("""
  select account, sku::text sku, offer_id, name, our_price, margin_own_live, is_negative, cogs_source
  from mkt_ozon_margin_control where sku is not null""")}

bids = db.query("""select account, sku::text sku, campaign_title, adv_type, max(bid) bid
                   from ozon_bids group by 1,2,3,4""")

raise_rows, cut_rows, drop_rows = [], [], []
for b in bids:
    k = (b['account'], b['sku']); m = mc.get(k)
    qty, rev = sales.get(k, (0, 0.0))
    camp = b['campaign_title'] or ''
    if not m or m['our_price'] is None or m['margin_own_live'] is None:
        if qty == 0: drop_rows.append((b['account'], b['sku'], camp, b['bid'], 'нет маржи и нет продаж 90 дн'))
        continue
    price = float(m['our_price']); mg = float(m['margin_own_live']) + MARGIN_UP
    cr = cr_sku.get(k) or BUCKET_CR.get(camp, 0.05)
    ceil = (price * mg/100.0 - price * PPO_NEW) * cr
    cur = float(b['bid'])
    if mg < KPI or ceil <= cur:
        cut_rows.append((b['account'], b['sku'], m['offer_id'], (m['name'] or '')[:60], camp, cur,
                         round(ceil,1), round(mg,1), qty, round(rev),
                         'маржа ниже KPI' if mg < KPI else 'ставка выше потолка'))
        continue
    if qty == 0:
        drop_rows.append((b['account'], b['sku'], camp, cur, 'нет продаж 90 дн'))
        continue
    new = min(ceil*SHARE, cur*3, 60.0)
    if new >= cur + 2:
        raise_rows.append((b['account'], b['sku'], m['offer_id'], (m['name'] or '')[:60], camp, cur,
                           round(new), round(ceil,1), round(mg,1), qty, round(rev)))

for name, rows, hdr in (
    ('ozon_bid_raise.csv', raise_rows, ['account','sku','offer_id','name','campaign','bid_now','bid_new','ceiling','margin_pct','qty_90d','revenue_90d']),
    ('ozon_bid_cut.csv',   cut_rows,   ['account','sku','offer_id','name','campaign','bid_now','ceiling','margin_pct','qty_90d','revenue_90d','reason']),
    ('ozon_bid_drop.csv',  drop_rows,  ['account','sku','campaign','bid_now','reason'])):
    with open('docs/reports/'+name,'w',newline='') as f:
        w=csv.writer(f); w.writerow(hdr); w.writerows(sorted(rows, key=lambda x:-(x[-1] if isinstance(x[-1],(int,float)) else 0)))

p("=== ПОДНЯТЬ СТАВКУ ===")
for acc in ('oz_acc1','oz_acc2'):
    r=[x for x in raise_rows if x[0]==acc]
    if not r: continue
    p(f"{acc}: позиций {len(r)}, выручка 90 дн {sum(x[10] for x in r)/1e6:.1f} млн ₽, "
      f"ставка медиана {sorted(x[5] for x in r)[len(r)//2]:.0f} → {sorted(x[6] for x in r)[len(r)//2]:.0f} ₽, "
      f"медиана потолка {sorted(x[7] for x in r)[len(r)//2]:.0f} ₽")
    byc={}
    for x in r: byc.setdefault(x[4],[]).append(x)
    for c,v in sorted(byc.items(), key=lambda kv:-len(kv[1]))[:7]:
        p(f"   {c[:24]:24} {len(v):5} поз, выручка {sum(x[10] for x in v)/1e6:5.2f} млн, ставка {sorted(y[5] for y in v)[len(v)//2]:.0f} → {sorted(y[6] for y in v)[len(v)//2]:.0f} ₽")
p(); p("=== СНИЗИТЬ / УБРАТЬ ИЗ КАМПАНИЙ ===")
for acc in ('oz_acc1','oz_acc2'):
    r=[x for x in cut_rows if x[0]==acc]; d=[x for x in drop_rows if x[0]==acc]
    p(f"{acc}: снизить ставку {len(r)} (из них маржа ниже KPI {sum(1 for x in r if x[10]=='маржа ниже KPI')}), "
      f"выручка этих позиций 90 дн {sum(x[9] for x in r)/1e6:.2f} млн ₽")
    p(f"{acc}: убрать (нет продаж 90 дн) {len(d)} позиций")
    byc={}
    for x in d: byc[x[2]]=byc.get(x[2],0)+1
    p("   без продаж по кампаниям: "+", ".join(f"{c[:20]}={n}" for c,n in sorted(byc.items(), key=lambda kv:-kv[1])[:6]))
open('/tmp/claude-0/-opt-mp-analytics--claude-worktrees-mkt-ozon/1258757b-c5aa-4324-90f0-605097d04789/scratchpad/plan.txt','w').write("\n".join(O))
print("\n".join(O[:40]))
