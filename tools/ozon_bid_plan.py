# поток: mkt
# ozon_bid_plan.py — списки ставок Ozon: поднять / снизить / убрать из кампаний.
# Пишет docs/reports/ozon_bid_{raise,cut,drop}.csv и таблицу mkt_ozon_bid_plan
# (её показывает витрина «Ставки Ozon»). В чат — только сводка.
import sys, csv; sys.path.insert(0,'.')
from core import db

PPO_NEW  = 0.05      # ставка оплаты за заказ после снижения 7 → 5 %
MARGIN_UP = 3.5      # п.п. маржи, которые вернутся после отключения Pro (2.5 %) и Звёздных (1.5 %), консервативно
KPI = 25.0
SHARE = 0.35         # какую долю потолка занимаем ставкой (запас на ошибку CR)
BUCKET_CR = {'от 0 до 1000':0.1149,'от 1000-2000':0.0860,'от 2000 до 5000':0.1042,
             'от 5000 до 10000':0.0871,'от 10000 до 500 тыс':0.0854,'Пустые РК':0.1435,
             'Эксперимент1':0.0366}
O=[]
def p(s=''): O.append(str(s))

sales = {}
for r in db.query("""
  select account, (pr->>'sku') sku, sum((pr->>'quantity')::int) qty,
         sum((pr->>'price')::numeric*(pr->>'quantity')::int) rev
  from raw_ozon_posting, lateral jsonb_array_elements(payload->'products') pr
  where in_process_at >= now() - interval '90 days' and status <> 'cancelled'
  group by 1,2"""):
    sales[(r['account'], r['sku'])] = (r['qty'], float(r['rev']))

cr_sku = {(r['account'], r['sku']): float(r['cr'])
          for r in db.query("""select account, sku::text sku, max(cr_bucket) cr from mkt_ozon_query_econ
                               where cr_bucket is not null group by 1,2""")}

mc = {(r['account'], r['sku']): r for r in db.query("""
  select account, sku::text sku, offer_id, name, our_price, margin_own_live, is_negative, cogs_source
  from mkt_ozon_margin_control where sku is not null""")}

# доказательная база: где позиция в поиске и как показ конвертится в карточку (последние 4 недели)
srch = {(r['account'], r['sku']): r for r in db.query("""
  select account, sku::text sku, round(avg(position)::numeric,1) pos,
         round(avg(view_conversion)::numeric,4) vc
  from ozon_search_product
  where period_start >= (select max(period_start) from ozon_search_product) - interval '28 days'
  group by 1,2""")}

bids = db.query("""select account, sku::text sku, campaign_id, campaign_title, adv_type, max(bid) bid
                   from ozon_bids where captured_at=(select max(captured_at) from ozon_bids b2
                                                     where b2.account=ozon_bids.account)
                   group by 1,2,3,4,5""")

raise_rows, cut_rows, drop_rows, plan = [], [], [], []
for b in bids:
    k = (b['account'], b['sku']); m = mc.get(k); s = srch.get(k) or {}
    qty, rev = sales.get(k, (0, 0.0))
    camp = b['campaign_title'] or ''
    cur = float(b['bid'] or 0)
    base = dict(account=b['account'], sku=b['sku'], campaign_id=b['campaign_id'] or 0,
                campaign_title=camp, bid_at_plan=cur, qty90=qty, revenue90=round(rev),
                search_pos=s.get('pos'), view_conv=s.get('vc'))
    if not m or m['our_price'] is None or m['margin_own_live'] is None:
        if qty == 0:
            drop_rows.append((b['account'], b['sku'], camp, cur, 'нет маржи и нет продаж 90 дн'))
            plan.append(dict(base, offer_id=None, name=None, action='drop', bid_target=None,
                             bid_ceiling=None, our_price=None, margin_pct=None, cr=None,
                             reason='нет маржи и нет продаж 90 дн'))
        continue
    price = float(m['our_price']); mg = float(m['margin_own_live']) + MARGIN_UP
    cr = cr_sku.get(k) or BUCKET_CR.get(camp, 0.05)
    ceil = (price * mg/100.0 - price * PPO_NEW) * cr
    com = dict(base, offer_id=m['offer_id'], name=(m['name'] or '')[:120],
               bid_ceiling=round(ceil,1), our_price=price, margin_pct=round(mg,1), cr=round(cr,4))
    if mg < KPI or ceil <= cur:
        why = 'маржа ниже KPI' if mg < KPI else 'ставка выше потолка'
        cut_rows.append((b['account'], b['sku'], m['offer_id'], (m['name'] or '')[:60], camp, cur,
                         round(ceil,1), round(mg,1), qty, round(rev), why))
        plan.append(dict(com, action='cut', bid_target=round(ceil*SHARE,1), reason=why))
        continue
    if qty == 0:
        drop_rows.append((b['account'], b['sku'], camp, cur, 'нет продаж 90 дн'))
        plan.append(dict(com, action='drop', bid_target=None, reason='нет продаж 90 дн'))
        continue
    new = min(ceil*SHARE, cur*3, 60.0)
    if new >= cur + 2:
        raise_rows.append((b['account'], b['sku'], m['offer_id'], (m['name'] or '')[:60], camp, cur,
                           round(new), round(ceil,1), round(mg,1), qty, round(rev)))
        plan.append(dict(com, action='raise', bid_target=round(new),
                         reason='продажи есть, маржа выше KPI, ставка ниже потолка'))

for name, rows, hdr in (
    ('ozon_bid_raise.csv', raise_rows, ['account','sku','offer_id','name','campaign','bid_now','bid_new','ceiling','margin_pct','qty_90d','revenue_90d']),
    ('ozon_bid_cut.csv',   cut_rows,   ['account','sku','offer_id','name','campaign','bid_now','ceiling','margin_pct','qty_90d','revenue_90d','reason']),
    ('ozon_bid_drop.csv',  drop_rows,  ['account','sku','campaign','bid_now','reason'])):
    with open('docs/reports/'+name,'w',newline='') as f:
        w=csv.writer(f); w.writerow(hdr); w.writerows(sorted(rows, key=lambda x:-(x[-1] if isinstance(x[-1],(int,float)) else 0)))

COLS = ['account','sku','campaign_id','campaign_title','offer_id','name','action','bid_at_plan',
        'bid_target','bid_ceiling','our_price','margin_pct','cr','qty90','revenue90','search_pos',
        'view_conv','reason']
db.execute("DELETE FROM mkt_ozon_bid_plan WHERE built_at = current_date")
seen=set(); batch=[]
for r in plan:
    key=(r['account'], r['sku'], r['campaign_id'])
    if key in seen: continue
    seen.add(key); batch.append(tuple(r.get(c) for c in COLS))
from psycopg2.extras import execute_values
with db.get_conn() as _c:
    with _c.cursor() as _cur:
        execute_values(_cur, f"INSERT INTO mkt_ozon_bid_plan (built_at,{','.join(COLS)}) VALUES %s",
                       [(__import__('datetime').date.today(),) + b for b in batch])
p(f"в mkt_ozon_bid_plan записано {len(batch)} строк")

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
print("\n".join(O[:40]))
