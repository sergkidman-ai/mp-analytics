# поток: mkt — разовая посадка 20 конвертящих SKU из топ-10 в кор ставок
import sys, re, os, json, argparse
sys.path.insert(0, "/opt/mp-analytics")
from core import db
from reports.bid_policy import raise_allowed
from ops.wb_bid_ladder import apply_step, FLOOR

ACC, TARGET = "wb_acc1", 10.90
ap = argparse.ArgumentParser(); ap.add_argument("--apply", action="store_true")
A = ap.parse_args()

cand = db.query("""
  select t.nm_id, t.text, t.frequency f, t.avg_position p, t.orders o, c.vendor_code vc, c.title
    from wb_search_text t
    left join wb_cards c on c.nm_id=t.nm_id and c.account=t.account
    left join wb_bid_override b on b.nm_id=t.nm_id and b.account=t.account
   where t.account=%s and t.period_start='2026-08-01'
     and t.avg_position between 1 and 10 and t.orders > 0 and b.nm_id is null
""", (ACC,))
cand = [r for r in cand if re.search(r"\d", r["text"] or "")]
nms = sorted({r["nm_id"] for r in cand})

adv = {r["nm_id"]: r["advert_id"] for r in db.query("""
  select distinct on (nm_id) nm_id, advert_id from wb_ad_nm_daily
   where account=%s and nm_id = any(%s::bigint[]) and advert_id is not null
   order by nm_id, dt desc""", (ACC, nms))}
d = db.query("select max(captured_date) d from mkt_margin_control where account=%s", (ACC,))[0]["d"]
marg = {r["nm_id"]: (float(r["net_live"] or 0), r["margin_own_live"]) for r in db.query("""
  select nm_id, net_live, margin_own_live from mkt_margin_control
   where account=%s and captured_date=%s and nm_id = any(%s::bigint[])""", (ACC, d, nms))}

rows, skip = [], {"нет кампании": 0, "нет маржи": 0, "маржа ниже пола": 0, "убыток": 0}
for nm in nms:
    if nm not in adv: skip["нет кампании"] += 1; continue
    if nm not in marg: skip["нет маржи"] += 1; continue
    net, m = marg[nm]
    if net <= 0: skip["убыток"] += 1; continue
    ok, why, below = raise_allowed(float(m) if m is not None else None)
    if not ok: skip["маржа ниже пола"] += 1; continue
    rows.append({"nm_id": nm, "advert_id": adv[nm], "old_cpc": FLOOR, "new_cpc": TARGET,
                 "margin": float(m) if m is not None else None, "net": net})

o = sum(r["o"] for r in cand); ordn = {r["nm_id"] for r in cand}
print(f"кандидатов {len(nms)} товаров ({len(cand)} ключей, {o} заказов/нед, снимок маржи {d})")
print("снято:", ", ".join(f"{k} {v}" for k, v in skip.items() if v) or "нет")
print(f"К ПОСАДКЕ: {len(rows)} товаров, {FLOOR} → {TARGET} ₽ (кампаний {len({r['advert_id'] for r in rows})})")
for r in sorted(rows, key=lambda x: -x["net"])[:8]:
    t = next(c for c in cand if c["nm_id"] == r["nm_id"])
    print(f"  {r['nm_id']}  чистая {r['net']:6.0f} ₽  маржа {r['margin']:5.1f}%  «{t['text'][:28]}»  {(t['title'] or '')[:34]}")
if not A.apply:
    print("\n[dry-run] живой записи не было")
else:
    ok, bad = apply_step(ACC, rows, note=f"посадка топ-10 конвертящих в кор {FLOOR}→{TARGET}")
    print(f"\nЗАПИСЬ В ВБ: успешно {ok}, ошибок {bad}")
