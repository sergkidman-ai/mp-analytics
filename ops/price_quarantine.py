#!/usr/bin/env python3
# поток: mkt
"""Карантин цен на площадках → проверка «не уйдём ли в минус» перед разблокировкой.

Цены на карточки шлёт ТК несколько раз в день; при изменении в разы площадка сажает товар
в карантин цен (WB — цена со скидкой втрое ниже прежней, Маркет — вердикты PRICE_CHANGE /
LOW_PRICE, Ozon — свыше ~50 %). Пока товар в карантине, он не продаётся по новой цене.

Скрипт читает карантин по API, подставляет себестоимость ТЕКУЩЕГО остатка из МойСклада и
считает, остаётся ли после удержаний площадки прибыль выше пола. Ничего не пишет на площадки:
`--apply` только для тех площадок, где выпуск возможен по API (пока Маркет).

Правила (решения Сергея 23.08.2026):
  * удержания площадки — ОДНИМ числом из витрин «Отчёты МП» за последний закрытый месяц;
  * себест — из остатка: Звездный → Цифровой, Дисквер → Дисквэр, Удаленный склад → общий;
  * остатка нет нигде → товар остаётся в карантине (цена из ТК по нему и не считалась);
  * пол прибыли = max(10 % от цены продажи, 300 ₽).

Запуск:  ./venv/bin/python ops/price_quarantine.py [--apply]
"""
import os
import re
import sys
import csv
import json
import pathlib
import datetime
import argparse
import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                                    # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                                               # noqa: E402
from collectors.wb_prices import _token as wb_token               # noqa: E402

WB_API = "https://discounts-prices-api.wildberries.ru"
YA_API = "https://api.partner.market.yandex.ru"
STORE_OF_ACC = {"acc1": "Звездный", "acc2": "Дисквер"}
COMMON_STORE = "Удаленный склад"
FLOOR_PCT, FLOOR_ABS = 0.10, 300.0
HIST = BASE_DIR / "reports" / "data"


# ─── удержания площадки: одно число из витрин «Отчёты МП» ────────────────────────────────
def _last_closed(d):
    """Индекс последнего месяца, который уже сверен (не provisional)."""
    prov = set(d.get("provisional") or [])
    keys = d.get("period_keys") or []
    for i in range(len(d["months"]) - 1, -1, -1):
        if not keys or keys[i] not in prov:
            return i
    return len(d["months"]) - 1


def retention():
    """{account: доля удержаний площадки от оборота} — как в строке «Итого удержания»."""
    out, month = {}, None
    d = json.loads((HIST / "mp_ozon_hist.json").read_text())
    i = _last_closed(d); month = d["months"][i]
    exp = ["commission", "delivery", "partners", "fbo", "promo", "penalty", "unclassified"]
    for acc, a in d["accounts"].items():
        L = a["lines"]
        out[acc] = sum(L[k][i] for k in exp) / L["sales"][i]

    d = json.loads((HIST / "mp_wb_hist.json").read_text())
    i = _last_closed(d)
    exp = ["delivery", "storage", "acceptance", "ads", "points", "penalty", "other"]
    for acc, a in d["accounts"].items():
        L = a["lines"]; N = len(d["months"])
        ob = L["own_price"][i]
        comp = (L.get("compensation") or [0] * N)[i]
        retp = (L.get("returns_pay") or [0] * N)[i]
        itog = L["to_pay"][i] - sum(L[k][i] for k in exp) + comp
        out[acc] = (ob - itog - retp) / ob

    d = json.loads((HIST / "mp_yandex_hist.json").read_text())
    i = _last_closed(d)
    # promotion — РОДИТЕЛЬСКАЯ строка (бусты/полка/отзывы/реклама уже внутри неё): берём её,
    # компоненты отдельно не складываем, иначе продвижение считается дважды.
    exp = ["fee", "delivery", "transfer", "promotion", "agency", "other_fee", "subscription_cost"]
    for acc, a in d["accounts"].items():
        L = a["lines"]
        out[acc] = sum(L[k][i] for k in exp) / (L["revenue"][i] + L["netting"][i])
    return out, month


# ─── себестоимость текущего остатка ─────────────────────────────────────────────────────
def cost_map():
    """{external_code: {store: себест/шт}} по свежему срезу остатков МойСклада.

    Внутри склада может лежать товар от разных поставщиков — берём средневзвешенную по штукам.
    """
    rows = db.query("""
        SELECT external_code, store,
               SUM(cost_seb * stock) AS s, SUM(stock) AS q
          FROM supplier_stock
         WHERE captured_at = (SELECT MAX(captured_at) FROM supplier_stock)
           AND stock > 0 AND cost_seb > 0 AND external_code IS NOT NULL
         GROUP BY 1, 2""")
    m = {}
    for r in rows:
        m.setdefault(r["external_code"], {})[r["store"]] = float(r["s"]) / float(r["q"])
    return m


def cost_for(code, acc, cm):
    """Себест единицы по правилу «свой склад → общий склад поставщика»."""
    stores = cm.get(code)
    if not stores:
        return None, None
    own = STORE_OF_ACC["acc2" if acc.endswith("acc2") else "acc1"]
    if own in stores:
        return stores[own], own
    if COMMON_STORE in stores:
        return stores[COMMON_STORE], COMMON_STORE
    return None, None


# ─── карантин по площадкам ──────────────────────────────────────────────────────────────
def wb_quarantine(acc):
    h, off, out = {"Authorization": wb_token(acc)}, 0, []
    while True:
        r = requests.get(f"{WB_API}/api/v2/quarantine/goods", headers=h,
                         params={"limit": 1000, "offset": off}, timeout=60)
        r.raise_for_status()
        g = (r.json().get("data") or {}).get("quarantineGoods") or []
        out += g
        if len(g) < 1000:
            return out
        off += 1000


def ya_quarantine():
    key, biz = os.getenv("YANDEX_API_KEY_ACC1"), os.getenv("YANDEX_BUSINESS_ID_ACC1")
    H = {"Api-Key": key, "Content-Type": "application/json"}
    tok, out = None, []
    while True:
        p = {"limit": 200}
        if tok:
            p["page_token"] = tok
        r = requests.post(f"{YA_API}/businesses/{biz}/price-quarantine", headers=H,
                          json={}, params=p, timeout=60)
        r.raise_for_status()
        res = r.json().get("result") or {}
        out += res.get("offers") or []
        tok = (res.get("paging") or {}).get("nextPageToken")
        if not tok:
            return out


YA_OFFER = re.compile(r"^(\d+)(?:X(\d+))?$")


def ya_code(offer_id):
    """offerId Маркета → (внешний код МС, штук в комплекте). '0123X2' → ('123', 2)."""
    m = YA_OFFER.match(offer_id or "")
    if not m:
        return None, 1
    return str(int(m.group(1))), int(m.group(2) or 1)


# ─── экономика и вердикт ────────────────────────────────────────────────────────────────
def verdict(price, cogs, ret):
    """(вердикт, прибыль ₽, пол ₽) для карантинной цены."""
    if not price:
        return "НЕТ ЦЕНЫ", None, None      # площадка держит карточку без цены — считать нечего
    if cogs is None:
        return "НЕТ ОСТАТКА", None, None
    net = price * (1 - ret) - cogs
    floor = max(price * FLOOR_PCT, FLOOR_ABS)
    return ("ОК" if net >= floor else "СТОП"), net, floor


def build():
    ret, month = retention()
    cm = cost_map()
    wb_vc = {r["nm_id"]: r["vendor_code"] for r in db.query("SELECT nm_id, vendor_code FROM wb_price")}
    rows = []

    for acc in ("wb_acc1", "wb_acc2"):
        for g in wb_quarantine(acc):
            price = float(g["newPrice"]) * (1 - float(g.get("newDiscount") or 0) / 100)
            old = float(g["oldPrice"]) * (1 - float(g.get("oldDiscount") or 0) / 100)
            code = wb_vc.get(g["nmID"])
            cogs, store = cost_for(code, acc, cm) if code else (None, None)
            v, net, floor = verdict(price, cogs, ret[acc])
            rows.append(dict(platform="wb", account=acc, id=g["nmID"], code=code or "",
                             pack=1, price_new=round(price, 2), price_old=round(old, 2),
                             cogs=round(cogs, 2) if cogs else None, store=store or "",
                             retention_pct=round(ret[acc] * 100, 1),
                             net=round(net, 2) if net is not None else None,
                             floor=round(floor, 2) if floor is not None else None,
                             verdict=v, reason=""))

    for o in ya_quarantine():
        acc = "ya_acc1"
        price = float((o.get("currentPrice") or {}).get("value") or 0)
        old = float((o.get("lastValidPrice") or {}).get("value") or 0)
        code, pack = ya_code(o.get("offerId"))
        unit, store = cost_for(code, acc, cm) if code else (None, None)
        cogs = unit * pack if unit is not None else None
        v, net, floor = verdict(price, cogs, ret[acc])
        rows.append(dict(platform="ya", account=acc, id=o.get("offerId"), code=code or "",
                         pack=pack, price_new=round(price, 2), price_old=round(old, 2),
                         cogs=round(cogs, 2) if cogs else None, store=store or "",
                         retention_pct=round(ret[acc] * 100, 1),
                         net=round(net, 2) if net is not None else None,
                         floor=round(floor, 2) if floor is not None else None,
                         verdict=v,
                         reason=",".join(sorted({x.get("type") for x in (o.get("verdicts") or [])}))))
    return rows, month


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="выпустить из карантина «ОК» (пока только Маркет)")
    a = ap.parse_args()

    rows, month = build()
    day = datetime.date.today().isoformat()
    out = BASE_DIR / "docs" / "reports" / f"quarantine_check_{day}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"Карантин цен на {day} (удержания — по месяцу «{month}»)")
    print(f"{'площадка':10} {'всего':>6} {'ОК':>5} {'СТОП':>6} {'нет остатка':>12}")
    for acc in sorted({r["account"] for r in rows}):
        s = [r for r in rows if r["account"] == acc]
        c = lambda v: sum(1 for r in s if r["verdict"] == v)          # noqa: E731
        print(f"{acc:10} {len(s):6} {c('ОК'):5} {c('СТОП'):6} {c('НЕТ ОСТАТКА') + c('НЕТ ЦЕНЫ'):12}")
    stop = [r for r in rows if r["verdict"] == "СТОП"]
    if stop:
        loss = sum(r["floor"] - r["net"] for r in stop)
        print(f"СТОП суммарно недобирают до пола {loss:,.0f} ₽ на единицу товара")
    print(f"файл: {out.relative_to(BASE_DIR)}")

    if a.apply:
        print("--apply: выпуск пока не подключён (см. отчёт разведки), запусти без флага")


if __name__ == "__main__":
    main()
