# поток: mkt — разовая посадка SKU в кор ставок (дальше их ведёт ops/wb_bid_ladder.py).
#
# Зачем: в рекламе 6870 товаров, назначенный CPC есть у ~870. Остальные крутятся на полу 7.3 ₽
# без управления — ДРР по этой массе 11.2% против 3.0% по кору (замер 01–09.08). Посадить в кор
# значит отдать товар лестнице: она поднимает +10% за шаг и сама морозит по марже и ДРР.
#
# Когорты (--source):
#   top10   — стоят в топ-10 по модельному запросу Джема и УЖЕ дают заказы, но CPC не назначен.
#   clicked — из рекламного фона: получают клики, но CPC не назначен (реакция уже есть, ставки нет).
#   organic — есть органическое движение в воронке (открытия карточки), но CPC не назначен.
#             Замер 01–09.08 по 5426 неуправляемым товарам в наличии: у 528 с органикой CTR
#             рекламы 0.98% (у 85 с органическими корзинами — 2.9%, как в коре), у 4898 без
#             органики — 0.018% (27 900 показов → 5 кликов). Ставку имеет смысл давать только
#             там, где карточка уже кому-то нужна; остальным ставка покупает показы без кликов.
#
# Гейты те же, что у лестницы, плюс остаток: рекламировать то, чего нет на складе, — чистый слив.
#
#   ./venv/bin/python -m ops.wb_bid_seed_converting --source clicked          # dry-run
#   ./venv/bin/python -m ops.wb_bid_seed_converting --source clicked --apply  # живая запись
import sys, re, time, argparse
import requests
sys.path.insert(0, "/opt/mp-analytics")
from core import db
from reports.bid_policy import raise_allowed
from ops.wb_bid_ladder import apply_step, FLOOR

ACC = "wb_acc1"

ap = argparse.ArgumentParser()
ap.add_argument("--account", default="wb_acc1", choices=["wb_acc1", "wb_acc2"])
ap.add_argument("--source", default="top10", choices=["top10", "clicked", "organic", "file"])
ap.add_argument("--file", help="для --source file: CSV со столбцом nm_id (напр. пул ширины из ops/wb_breadth)")
ap.add_argument("--min-opens", type=int, default=1, help="порог открытий карточки для --source organic")
ap.add_argument("--cpc", type=float, default=10.90, help="стартовая ставка, ₽ (уровень кора)")
ap.add_argument("--apply", action="store_true", help="живая запись в ВБ (иначе dry-run)")
A = ap.parse_args()
ACC = A.account

if A.source == "file":
    # Готовый список номенклатур (пул ширины из ops/wb_breadth: спрос есть, показов нет).
    # Берём только те, у кого ставка ещё не назначена, — чужие правки лестницы не трогаем.
    import csv as _csv
    with open(A.file, encoding="utf-8-sig") as fh:
        want = sorted({int(r["nm_id"]) for r in _csv.DictReader(fh, delimiter=";") if r.get("nm_id")})
    cand = db.query("""
      select d.nm_id, sum(d.clicks) cl, sum(d.orders) o, round(sum(d.spend)::numeric,2) sp,
             c.vendor_code vc, c.title
        from wb_ad_nm_daily d
        left join wb_cards c on c.nm_id=d.nm_id and c.account=d.account
        left join wb_bid_override b on b.nm_id=d.nm_id and b.account=d.account
       where d.account=%s and b.nm_id is null and d.nm_id = any(%s::bigint[])
       group by d.nm_id, c.vendor_code, c.title
    """, (ACC, want))
    print(f"из файла {len(want)} nm, в рекламе аккаунта и без назначенной ставки {len(cand)}")
elif A.source == "top10":
    cand = db.query("""
      select t.nm_id, t.text, t.orders o, c.vendor_code vc, c.title
        from wb_search_text t
        left join wb_cards c on c.nm_id=t.nm_id and c.account=t.account
        left join wb_bid_override b on b.nm_id=t.nm_id and b.account=t.account
       where t.account=%s and t.period_start='2026-08-01'
         and t.avg_position between 1 and 10 and t.orders > 0 and b.nm_id is null
    """, (ACC,))
    cand = [r for r in cand if re.search(r"\d", r["text"] or "")]   # категорийные ключи не в счёт
elif A.source == "organic":
    # Органика — из воронки за текущий месяц; реклама уже крутится (nm есть в wb_ad_nm_daily),
    # но ставка не назначена. Порог открытий задаётся --min-opens.
    cand = db.query("""
      select f.nm_id, f.open_count oc, f.cart_count cc, f.order_count o,
             c.vendor_code vc, c.title
        from wb_funnel f
        join (select distinct nm_id from wb_ad_nm_daily where account=%s) d on d.nm_id=f.nm_id
        left join wb_cards c on c.nm_id=f.nm_id and c.account=f.account
        left join wb_bid_override b on b.nm_id=f.nm_id and b.account=f.account
       where f.account=%s and f.period=date_trunc('month', current_date)::date
         and f.open_count >= %s and b.nm_id is null
    """, (ACC, ACC, A.min_opens))
else:
    cand = db.query("""
      select d.nm_id, sum(d.clicks) cl, sum(d.orders) o, round(sum(d.spend)::numeric,2) sp,
             c.vendor_code vc, c.title
        from wb_ad_nm_daily d
        left join wb_cards c on c.nm_id=d.nm_id and c.account=d.account
        left join wb_bid_override b on b.nm_id=d.nm_id and b.account=d.account
       where d.account=%s and b.nm_id is null
       group by d.nm_id, c.vendor_code, c.title
      having sum(d.clicks) > 0
    """, (ACC,))
nms = sorted({r["nm_id"] for r in cand})

adv = {r["nm_id"]: r["advert_id"] for r in db.query("""
  select distinct on (nm_id) nm_id, advert_id from wb_ad_nm_daily
   where account=%s and nm_id = any(%s::bigint[]) and advert_id is not null
   order by nm_id, dt desc""", (ACC, nms))}
day = db.query("select max(captured_date) d from mkt_margin_control where account=%s", (ACC,))[0]["d"]
marg = {r["nm_id"]: (float(r["net_live"] or 0), r["margin_own_live"]) for r in db.query("""
  select nm_id, net_live, margin_own_live from mkt_margin_control
   where account=%s and captured_date=%s and nm_id = any(%s::bigint[])""", (ACC, day, nms))}
# Остаток берём живьём с карточек: wb_stocks — это только FBS-склад (в снимке ~590 nm),
# отсутствие товара там НЕ значит «нет в наличии». totalQuantity в card.wb.ru — реальный
# покупаемый остаток по всем складам.
def _live_stock(ids):
    out = {}
    for i in range(0, len(ids), 100):
        chunk = ids[i:i + 100]
        try:
            r = requests.get("https://card.wb.ru/cards/v4/detail",
                             params={"appType": 1, "curr": "rub", "dest": -1257786,
                                     "spp": 30, "nm": ";".join(str(x) for x in chunk)}, timeout=45)
            for pr in (r.json().get("products") or []):
                out[pr["id"]] = pr.get("totalQuantity", 0)
        except Exception as e:
            print(f"  [остатки] батч {i//100}: {type(e).__name__} — пропуск", flush=True)
        time.sleep(0.3)
    return out

stock = _live_stock(nms)

rows, skip = [], {"нет кампании": 0, "нет маржи": 0, "маржа ниже пола": 0, "убыток": 0, "нет остатка": 0}
for nm in nms:
    if nm not in adv:
        skip["нет кампании"] += 1; continue
    if not stock.get(nm):
        skip["нет остатка"] += 1; continue
    if nm not in marg:
        skip["нет маржи"] += 1; continue
    net, m = marg[nm]
    if net <= 0:
        skip["убыток"] += 1; continue
    ok, _why, _below = raise_allowed(float(m) if m is not None else None)
    if not ok:
        skip["маржа ниже пола"] += 1; continue
    rows.append({"nm_id": nm, "advert_id": adv[nm], "old_cpc": FLOOR, "new_cpc": A.cpc,
                 "margin": float(m) if m is not None else None, "net": net})

print(f"когорта «{A.source}»: {len(nms)} товаров (снимок маржи {day})")
print("снято:", ", ".join(f"{k} {v}" for k, v in skip.items() if v) or "нет")
print(f"К ПОСАДКЕ: {len(rows)} товаров, {FLOOR} → {A.cpc} ₽ (кампаний {len({r['advert_id'] for r in rows})})")
for r in sorted(rows, key=lambda x: -x["net"])[:8]:
    t = next(c for c in cand if c["nm_id"] == r["nm_id"])
    print(f"  {r['nm_id']}  чистая {r['net']:6.0f} ₽  маржа {r['margin']:5.1f}%  {(t['title'] or '')[:44]}")
if not A.apply:
    print("\n[dry-run] живой записи не было")
elif rows:
    ok, bad = apply_step(ACC, rows, note=f"посадка в кор «{A.source}» {FLOOR}→{A.cpc}")
    print(f"\nЗАПИСЬ В ВБ: успешно {ok}, ошибок {bad}")
