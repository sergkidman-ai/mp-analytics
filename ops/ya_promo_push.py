"""ops/ya_promo_push.py — поток: mkt
Маркет: заведение товаров в акцию «Бестселлеры» по самой глубокой ступени, какую позволяет пол.

Это НЕ сторож (ops/ya_promo_guard.py защищает от убытка), а рычаг продаж: у акции четыре
ступени цены — light / best / superBest / topBest, и чем глубже ступень, тем заметнее товар
в выдаче. `maxPromoPrice` всегда равен ступени light, то есть самой мягкой.

Решения Сергея 17.09.2026:
  ступень — САМАЯ ГЛУБОКАЯ, чья цена не ниже пола (topBest → superBest → best → light);
  запас над полом не требуется: годится цена ≥ пол;
  заводим ВСЕ товары акции, проходящие по полу (себестоимость есть = товар в наличии).
Пол и себестоимость — общие с обоими сторожами: `ozon_stock_action.floor_price` и
`ozon_promo_guard.cost_map` (наличие СЕЙЧАС).

Зачёркнутая цена (`price`) берётся из каталога — POST /v2/businesses/{b}/offer-prices.
Маркет требует promoPrice в пределах 1–95 % от неё; товар без цены каталога пропускаем.

Изменения Маркет применяет через 4–6 часов. Отправленные строки пишутся в тот же журнал,
что и у сторожа (`ya_promo_guard_log`, mode='add'), и их сверяет следующий прогон сторожа.

Запуск (по умолчанию расчёт, в кабинет ничего не уходит):
    ./venv/bin/python -m ops.ya_promo_push                       # план
    ./venv/bin/python -m ops.ya_promo_push --csv /tmp/push.csv   # + построчно
    ./venv/bin/python -m ops.ya_promo_push --apply --limit 100   # завести (по прямой команде)
"""
import argparse
import math
import pathlib
import re
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from ops import ozon_promo_guard as g                            # noqa: E402
from ops import ozon_stock_action as oz                          # noqa: E402
from ops import price_quarantine as q                            # noqa: E402
from ops import ya_promo_guard as y                              # noqa: E402

LEVELS = [("topBestLevel", "топбест"), ("superBestLevel", "супербест"),
          ("bestLevel", "бест"), ("lightBestLevel", "лайт")]     # от глубокой к мягкой
# Статусы, из которых товар можно завести. MINIMUM_FOR_PROMOS не трогаем: у товара стоит
# минимальная цена для акций, ниже которой Маркет его не пускает, — это чужое решение.
ADDABLE = ("NOT_PARTICIPATING", "RENEW_FAILED")
MAX_DISCOUNT_SHARE = 0.95
MIN_DISCOUNT_SHARE = 0.01
PRICE_BATCH = 200                    # offerIds в /offer-prices: 1–200, иначе 400


def catalog_prices(offer_ids):
    """{offerId: цена каталога} — зачёркнутая цена для акции.

    Спрашиваем ТОЛЬКО нужные SKU (`offerIds` в теле, до 200 — жёсткий лимит Маркета): полный обход каталога — это
    ~70 страниц, и на них Маркет начинает отдавать 420 (лимит ресурса 5000/ч)."""
    out = {}
    ids = sorted(offer_ids)
    for k in range(0, len(ids), PRICE_BATCH):
        res = y._req(f"/v2/businesses/{y._biz()}/offer-prices?limit={PRICE_BATCH}",
                     {"offerIds": ids[k:k + PRICE_BATCH]}).get("result") or {}
        for o in res.get("offers", []) or []:
            v = (o.get("price") or {}).get("value")
            if v:
                out[o.get("offerId")] = float(v)
    return out


def plan():
    """Строки решения по каждому товару-кандидату акций «Бестселлеры»."""
    ret, month = q.retention()
    keep = 1 - float(ret[y.ACCOUNT])
    rows, acts = [], []
    for a in y.promos():
        mi = a.get("mechanicsInfo") or {}
        mech = [m.get("type") for m in mi] if isinstance(mi, list) else [mi.get("type")]
        if not any(m in y.EDITABLE for m in mech if m):
            continue
        acts.append(a)
        token = None
        offers = []
        while True:
            path = f"/v2/businesses/{y._biz()}/promos/offers?limit={y.PAGE}" + (f"&page_token={token}" if token else "")
            res = y._req(path, {"promoId": a["id"]}).get("result") or {}
            offers += [o for o in (res.get("offers", []) or []) if o.get("status") in ADDABLE]
            token = (res.get("paging") or {}).get("nextPageToken")
            if not token:
                break
        for o in offers:
            d = (o.get("params") or {}).get("discountParams") or {}
            rows.append({"account": y.ACCOUNT, "promo_id": a["id"], "promo_name": a.get("name"),
                         "mechanics": ",".join(m for m in mech if m), "offer_id": o.get("offerId"),
                         "status": o.get("status"), "levels": d.get("bestPriceLevels") or {},
                         "price": 0.0,
                         "promo_price": 0.0,
                         "max_promo_price": float(d.get("maxPromoPrice") or 0),
                         "keep_ratio": keep})
    prices = catalog_prices({r["offer_id"] for r in rows if r["offer_id"]})
    cogs = g.cost_map(sorted({r["offer_id"] for r in rows if r["offer_id"]}))
    for r in rows:
        r["price"] = prices.get(r["offer_id"]) or 0.0
        r["cogs"], r["cogs_source"] = cogs.get(r["offer_id"], (None, "НЕТ"))
        decide(r)
    return rows, acts, keep, month


def decide(r):
    """mode: add (со ступенью в note) | skip."""
    r["floor_price"] = r["new_price"] = r["level"] = None
    r["status_row"], r["note"] = "dry", None
    if r["cogs"] is None:
        r["mode"], r["reason"] = "skip", f"себестоимости нет ({r['cogs_source']})"
        return r
    floor = oz.floor_price(float(r["cogs"]), r["keep_ratio"])
    r["floor_price"] = round(floor, 2)
    if not r["price"]:
        r["mode"], r["reason"] = "skip", "нет цены каталога"
        return r
    # Потолок — И коридор скидки Маркета (1–95 % цены), И maxPromoPrice акции: 18.09 ступень
    # «лайт» у 6077 оказалась НА РУБЛЬ ВЫШЕ maxPromoPrice, и Маркет ответил PROMO_PRICE_BIGGER_THAN_MAX.
    lo, hi = r["price"] * MIN_DISCOUNT_SHARE, math.floor(r["price"] * MAX_DISCOUNT_SHARE)
    if r["max_promo_price"]:
        hi = min(hi, r["max_promo_price"])
    for key, name in LEVELS:
        lvl = float(r["levels"].get(key) or 0)
        if lvl <= 0 or lvl < floor - y.PRICE_EPS:
            continue
        if lvl > hi or lvl < lo:              # вне коридора скидки Маркета (1–95 % цены)
            continue
        r["mode"], r["new_price"], r["level"] = "add", lvl, name
        r["reason"] = f"ступень {name}: {lvl:.0f} ≥ пол {floor:.0f}"
        return r
    best = max((float(r["levels"].get(k) or 0) for k, _ in LEVELS), default=0)
    r["mode"] = "skip"
    r["reason"] = (f"все ступени ниже пола {floor:.0f} (лучшая {best:.0f})" if best
                   else "у товара нет ступеней")
    return r


def push(rows):
    """Заведение = тот же update, что и правка цены: promoPrice + зачёркнутая цена."""
    todo = [r for r in rows if r["mode"] == "add"]
    for pid in sorted({r["promo_id"] for r in todo}):
        part = [r for r in todo if r["promo_id"] == pid]
        for k in range(0, len(part), y.BATCH):
            chunk = part[k:k + y.BATCH]
            res = y._req(f"/v2/businesses/{y._biz()}/promos/offers/update",
                         {"promoId": pid,
                          "offers": [{"offerId": r["offer_id"],
                                      "params": {"discountParams": {"price": int(r["price"]),
                                                                    "promoPrice": int(r["new_price"])}}}
                                     for r in chunk]}).get("result") or {}
            rej = {x.get("offerId"): x.get("reason") for x in (res.get("rejectedOffers") or [])}
            warn = {x.get("offerId"): ",".join(w.get("code", "") for w in (x.get("warnings") or []))
                    for x in (res.get("warningOffers") or [])}
            for r in chunk:
                if r["offer_id"] in rej:
                    r["status_row"], r["note"] = "error", f"отказ: {rej[r['offer_id']]}"
                else:
                    r["status_row"] = "sent"
                    r["note"] = " · ".join(x for x in (f"ступень {r['level']}", warn.get(r["offer_id"])) if x)
            print(f"  {pid}: отправлено {len(chunk) - len(rej)}, отказов {len(rej)}", flush=True)


def summary(rows, acts, keep, month, apply):
    add = [r for r in rows if r["mode"] == "add"]
    by = {}
    for r in add:
        by[r["level"]] = by.get(r["level"], 0) + 1
    lines = [f"*Маркет* · заведение в акции · {'исполнение' if apply else 'план'}",
             f"акций {len(acts)}, кандидатов {len(rows)}, остаётся нам {keep*100:.1f}% ({month})",
             f"  · завести: {len(add)}  · мимо: {len(rows) - len(add)}"]
    lines += [f"  – ступень {name}: {by[name]}" for _, name in LEVELS if by.get(name)]
    why = {}
    for r in rows:
        if r["mode"] == "skip":
            k = re.sub(r"\d+", "N", r["reason"].split(" (")[0].split(":")[0])
            why[k] = why.get(k, 0) + 1
    for k, n in sorted(why.items(), key=lambda x: -x[1])[:3]:
        lines.append(f"  – мимо, {k}: {n}")
    if apply:
        st = {}
        for r in add:
            st[r["status_row"]] = st.get(r["status_row"], 0) + 1
        lines.append("отправлено: " + ", ".join(f"{k} {v}" for k, v in st.items()))
        lines.append("Маркет применит через 4–6 ч; сверит следующий прогон сторожа")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="Маркет: завести товары в акцию по глубокой ступени")
    ap.add_argument("--apply", action="store_true", help="отправить в кабинет (по умолчанию план)")
    ap.add_argument("--wave", default="push", help="метка прогона в журнале")
    ap.add_argument("--limit", type=int, help="не больше N заведений за прогон")
    ap.add_argument("--csv", help="построчный план в файл")
    ap.add_argument("--no-log", action="store_true", help="не писать журнал")
    ap.add_argument("--no-tg", action="store_true", help="не слать сводку в бот")
    a = ap.parse_args()
    rows, acts, keep, month = plan()
    if a.limit is not None:
        # заводим сперва те, где запас над полом больше: у них риск уйти в минус меньше
        add = sorted([r for r in rows if r["mode"] == "add"],
                     key=lambda r: -(r["new_price"] - r["floor_price"]))
        for r in add[a.limit:]:
            r["mode"], r["reason"] = "skip", r["reason"] + f" · отложено --limit {a.limit}"
    if a.apply:
        push(rows)
    if not a.no_log:
        y.save([r for r in rows if r["mode"] == "add"], a.wave)
    txt = summary(rows, acts, keep, month, a.apply)
    print(txt.replace("*", ""), flush=True)
    if a.apply and not a.no_tg:
        oz_send = __import__("ops.ozon_stock_action", fromlist=["send"]).send
        oz_send(txt)
    if a.csv and rows:
        head = ["promo_name", "offer_id", "status", "price", "max_promo_price", "cogs",
                "cogs_source", "floor_price", "new_price", "level", "mode", "status_row",
                "reason", "note"]
        pathlib.Path(a.csv).write_text(
            "\n".join([";".join(head)] + [";".join("" if r.get(c) is None else str(r.get(c)) for c in head)
                                          for r in rows]), encoding="utf-8")
        print(f"\nпострочно → {a.csv}", flush=True)


if __name__ == "__main__":
    main()
