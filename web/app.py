"""web/app.py — дашборд «Пульт бизнеса» (BI маркетплейсов).

FastAPI: читает Postgres (данные обновляются run_daily.py 2×/день), отдаёт JSON-API + фронт.
Drill-down: большие цифры → SKU → (позже категории/заказы). Фильтры: площадка/аккаунт/период.
За хостовым nginx с basic-auth (bi.metaverseworld.ru). Локально: 127.0.0.1:8090.

Запуск:  ./venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8090
"""
import os
import re
import sys
import pathlib

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

app = FastAPI(title="Пульт бизнеса")
STATIC = BASE_DIR / "web" / "static"


@app.get("/", response_class=HTMLResponse)
def home():
    return (STATIC / "home.html").read_text(encoding="utf-8")


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/api/meta")
def meta():
    """Доступные срезы для фильтров (площадка/аккаунт/период)."""
    rows = db.query("""SELECT DISTINCT platform, account,
        period_from::text period_from, period_to::text period_to
        FROM margin_by_sku ORDER BY 1,2,3""")
    return {"slices": rows}


def _where(platform, account, period, extra=None):
    w, p = [], []
    if platform:
        w.append("platform=%s"); p.append(platform)
    if account:
        w.append("account=%s"); p.append(account)
    if period:
        w.append("period_from=%s"); p.append(period)
    if extra:
        w.append(extra)
    return (" WHERE " + " AND ".join(w)) if w else "", p


def _summary_one(platform, account, period):
    """Сводные цифры по одному срезу (используется и для текущего, и для прошлого периода)."""
    w, p = _where(platform, account, period)
    r = db.query(f"""SELECT count(*) n_sku,
        coalesce(sum(qty),0)::float qty,
        coalesce(sum(revenue_buyer),0)::float revenue,
        coalesce(sum(cogs),0)::float cogs,
        coalesce(sum(commission),0)::float commission,
        coalesce(sum(logistics),0)::float logistics,
        coalesce(sum(net_profit),0)::float net,
        coalesce(sum(net_profit) FILTER (WHERE (qty>0 OR revenue_buyer>0) AND article<>'0'),0)::float net_activity,
        coalesce(sum(CASE WHEN net_profit<0 AND qty>0 AND article<>'0' THEN 1 ELSE 0 END),0) loss_count,
        coalesce(sum(net_profit) FILTER (WHERE net_profit<0 AND qty>0 AND article<>'0'),0)::float loss_sum,
        coalesce(sum(qty) FILTER (WHERE net_profit<0 AND qty>0 AND article<>'0'),0)::float loss_qty,
        coalesce(sum(returns_sum),0)::float returns_sum
        FROM margin_by_sku{w}""", p)[0]
    r["margin_pct"] = round(r["net"] / r["revenue"] * 100, 1) if r["revenue"] else None
    r["commission_pct"] = round(r["commission"] / r["revenue"] * 100, 1) if r["revenue"] else None
    r["net_other"] = round(r["net"] - r["net_activity"], 2)
    w2, p2 = _where(platform, account, period)
    own = db.query(f"""SELECT coalesce(sum(our_price*qty),0)::float own_revenue,
        coalesce(sum(revenue_wb),0)::float revenue_wb
        FROM sales{w2 + (' AND ' if w2 else ' WHERE ')}qty>0 AND our_price IS NOT NULL""", p2)[0]
    r["own_revenue"] = own["own_revenue"]
    r["revenue_wb"] = round(own["revenue_wb"], 2) or None   # «Вайлдберриз реализовал Товар» (после СПП, цена ВБ)
    r["margin_wb"] = round(r["net"] / own["revenue_wb"] * 100, 1) if own["revenue_wb"] else None  # маржа от цены ВБ
    r["margin_own"] = round(r["net"] / own["own_revenue"] * 100, 1) if own["own_revenue"] else None
    # СПП (эффективная) = (наша цена − цена ВБ/реализовал) / наша цена
    r["spp_pct"] = round((r["revenue"] - own["revenue_wb"]) / r["revenue"] * 100, 1) \
        if r["revenue"] and own["revenue_wb"] else None
    # расходы из сырого отчёта WB по типу удержания: реклама / отзывы / хранение / прочее.
    # Это «нераспределённые» (без привязки к nm_id) — уже вычтены из net, показываем отдельно.
    # Пустой account = агрегат обоих ВБ-юрлиц.
    rw, rp = (["account=%s"], [account]) if account else (["account=ANY(%s)"], [["wb_acc1", "wb_acc2"]])
    if period:
        rw.append("period_from=%s"); rp.append(period)
    adv = db.query(f"""SELECT
        coalesce(sum((payload->>'deduction')::numeric)
            FILTER (WHERE payload->>'bonus_type_name' ILIKE '%%WB Продвижение%%'),0)::float adv_spend,
        coalesce(sum((payload->>'deduction')::numeric)
            FILTER (WHERE payload->>'bonus_type_name' ILIKE 'Списание за отзыв%%'),0)::float reviews_spend,
        coalesce(sum((payload->>'storage_fee')::numeric),0)::float storage_all,
        coalesce(sum((payload->>'deduction')::numeric),0)::float ded_all,
        coalesce(sum((payload->>'quantity')::numeric)
            FILTER (WHERE payload->>'supplier_oper_name'='Возврат'),0)::float ret_qty
        FROM raw_wb_report WHERE {' AND '.join(rw)}""", rp)[0]
    r["adv_spend"] = adv["adv_spend"]
    r["reviews_spend"] = adv["reviews_spend"]
    r["storage_all"] = adv["storage_all"]
    r["other_ded"] = round(adv["ded_all"] - adv["adv_spend"] - adv["reviews_spend"], 2)
    r["adv_pct"] = round(adv["adv_spend"] / r["revenue"] * 100, 1) if r["revenue"] else None
    r["returns_qty"] = adv["ret_qty"]
    # нераспределённые = удержания WB без привязки к товару (хранение + реклама + отзывы + прочее)
    r["unalloc"] = round(adv["storage_all"] + adv["ded_all"], 2)
    return r


def _prev_period(platform, account, period):
    """Предыдущий доступный период (max period_from < выбранного) по тому же срезу."""
    if not period:
        return None
    w, p = ["period_from < %s"], [period]
    if platform:
        w.append("platform=%s"); p.append(platform)
    if account:
        w.append("account=%s"); p.append(account)
    row = db.query(f"""SELECT max(period_from)::text pf FROM margin_by_sku
        WHERE {' AND '.join(w)}""", p)
    return row[0]["pf"] if row and row[0]["pf"] else None


@app.get("/api/summary")
def summary(platform: str = "", account: str = "", period: str = ""):
    """Большие цифры + сравнение с прошлым периодом (рост/падение маржи, COGS, прибыли)."""
    r = _summary_one(platform, account, period)
    prev_p = _prev_period(platform, account, period)
    if prev_p:
        pr = _summary_one(platform, account, prev_p)
        r["prev_period"] = prev_p
        # абсолютная и относительная динамика по ключевым метрикам
        r["delta"] = {}
        for k in ("net", "revenue", "revenue_wb", "cogs", "own_revenue", "qty", "margin_pct",
                  "margin_wb", "margin_own", "adv_spend", "commission_pct", "returns_sum",
                  "logistics", "spp_pct"):
            cur, old = r.get(k), pr.get(k)
            if cur is None or old is None:
                r["delta"][k] = None
                continue
            d = {"abs": round(cur - old, 2)}
            # %-изменение: для маржи (уже в %) даём разницу в п.п., для денег — относит. рост
            d["pct"] = None if not old else round((cur - old) / abs(old) * 100, 1)
            r["delta"][k] = d
    else:
        r["prev_period"] = None
        r["delta"] = None
    return r


# Колонка → SQL-выражение для сортировки (любая колонка, asc/desc).
SKU_SORT = {
    "nm_id": "m.article", "vendor_code": "c.vendor_code", "title": "c.title",
    "qty": "m.qty", "revenue_buyer": "m.revenue_buyer", "cogs": "m.cogs",
    "net_profit": "m.net_profit", "margin_pct": "m.margin_pct", "margin_own": "margin_own",
}


def _wb_pricing(account, period):
    """{nm_id: {price_before, price_after, discount_pct, spp_pct}} из raw_wb_report (по продажам).
    price_before = retail_price (до скидки), price_after = retail_price_withdisc (после нашей скидки)."""
    accts = [account] if account else ["wb_acc1", "wb_acc2"]
    rows = db.query("""SELECT payload->>'nm_id' nm,
        avg(nullif((payload->>'retail_price')::numeric,0)) before,
        avg(nullif((payload->>'retail_price_withdisc_rub')::numeric,0)) after,
        avg(nullif((payload->>'ppvz_spp_prc')::numeric,0)) spp
        FROM raw_wb_report WHERE account=ANY(%s) AND period_from=%s
          AND payload->>'supplier_oper_name'='Продажа' GROUP BY 1""", (accts, period))
    out = {}
    for r in rows:
        if not r["nm"]:
            continue
        b, a = float(r["before"] or 0), float(r["after"] or 0)
        out[r["nm"]] = {"price_before": round(b, 2), "price_after": round(a, 2),
                        "discount_pct": round((b - a) / b * 100, 1) if b else 0,
                        "spp_pct": round(float(r["spp"] or 0), 1)}
    return out


@app.get("/api/sku")
def sku(platform: str = "", account: str = "", period: str = "",
        problem: bool = False, sort: str = "revenue_buyer", order: str = "desc",
        q: str = "", limit: int = 300):
    """SKU-уровень: артикул WB (nm_id) + наш артикул (vendorCode) + полное название.

    Артефакты (nm=0, строки без продаж) убраны. revenue_buyer = цена ПОКУПАТЕЛЯ (после СПП).
    Две маржи: margin_pct = от цены продажи ВБ; margin_own = от НАШЕЙ цены (до СПП).
    problem=true → убыточные + price_up_pct: на сколько % поднять НАШУ цену, чтобы выйти
    на +10% маржи (от цены ВБ). keep-ratio 0.61: СПП ~29% + комиссия ~14%."""
    conds, p = [], []
    if platform:
        conds.append("m.platform=%s"); p.append(platform)
    if account:
        conds.append("m.account=%s"); p.append(account)
    if period:
        conds.append("m.period_from=%s"); p.append(period)
    conds.append("m.article<>'0'")
    conds.append("m.net_profit<0 AND m.qty>0" if problem else "(m.qty>0 OR m.revenue_buyer>0)")
    if q:
        conds.append("(m.article ILIKE %s OR c.vendor_code ILIKE %s OR c.title ILIKE %s)")
        p += [f"%{q}%", f"%{q}%", f"%{q}%"]
    where = " WHERE " + " AND ".join(conds)
    sort_sql = SKU_SORT.get(sort, "m.revenue_buyer")
    order = "DESC" if order.lower() == "desc" else "ASC"
    rows = db.query(f"""
        SELECT m.article nm_id, c.vendor_code, c.title,
            m.qty::float, s.our_price::float,
            m.revenue_buyer::float, m.cogs::float, m.logistics::float,
            m.net_profit::float, round(m.margin_pct,1)::float margin_pct,
            CASE WHEN s.our_price>0 AND m.qty>0
                 THEN round(m.net_profit/(s.our_price*m.qty)*100, 1) END::float margin_own,
            -- на сколько поднять НАШУ цену (доля), чтобы маржа от нашей цены достигла 0.25:
            -- x = (0.25 − net/own_rev) / 0.36  (0.36 = keep-ratio 0.61 − цель 0.25)
            CASE WHEN m.qty>0 AND s.our_price>0
                  AND m.net_profit/(s.our_price*m.qty) < 0.25
                 THEN ceil((0.25 - m.net_profit/(s.our_price*m.qty)) / 0.36 * 100)
                 ELSE NULL END::float price_up_pct,
            round(c.volume_l,2)::float volume_l,
            round((c.weight_kg/NULLIF(c.volume_l,0))::numeric,3)::float density,
            CASE WHEN c.dims_valid=false OR c.volume_l IS NULL OR c.weight_kg IS NULL THEN 'невалидные'
                 WHEN c.weight_kg/NULLIF(c.volume_l,0) < {DENS_LOW} THEN 'крупн./лёгкий'
                 WHEN c.weight_kg/NULLIF(c.volume_l,0) > {DENS_HIGH} THEN 'тяжёлый/мелкий'
                 ELSE NULL END dims_flag
        FROM margin_by_sku m
        LEFT JOIN wb_cards c ON c.account=m.account AND c.nm_id::text=m.article
        LEFT JOIN sales s ON s.platform=m.platform AND s.account=m.account
             AND s.period_from=m.period_from AND s.article=m.article
        {where} ORDER BY {sort_sql} {order} NULLS LAST LIMIT %s""", p + [limit])
    if problem and rows:
        streak = _loss_streak(platform, account, period, [r["nm_id"] for r in rows])
        for r in rows:
            r["loss_months"] = streak.get(r["nm_id"], 1)
    if (platform == "wb" or not platform) and rows:        # цены/скидки/СПП по ВБ
        pr = _wb_pricing(account, period)
        for r in rows:
            x = pr.get(r["nm_id"])
            if x:
                r["price_before"], r["discount_pct"], r["spp_pct"] = (
                    x["price_before"], x["discount_pct"], x["spp_pct"])
    return {"rows": rows, "count": len(rows)}


@app.get("/api/opex")
def opex(period: str = ""):
    """Постоянные/общебизнесовые расходы (ФОТ + аренда + налоги) — весь бизнес (WB + Ozon).
    Снапшот действует с effective_from. Чистая бизнеса = WB (из margin) + Ozon
    (к перечислению − COGS) за месяц минус эти расходы. Редактируется помесячно в UI."""
    if not period:
        return {"applies": False, "items": [], "total": 0}
    # Активный снапшот = последний effective_from ≤ месяца (НЕ сумма всех — иначе двойной счёт
    # при помесячном редактировании). Месяц без своего снапшота наследует предыдущий.
    snap = db.query("SELECT max(effective_from)::text e FROM opex WHERE effective_from<=%s", (period,))[0]["e"]
    if not snap:
        return {"applies": False, "items": [], "total": 0, "period": period,
                "snapshot": None, "own": False}
    items = db.query("""SELECT id, name, role, category, base::float, tax_pct::float, amount::float
        FROM opex WHERE effective_from=%s ORDER BY category, amount DESC""", (snap,))
    fot = sum(i["amount"] for i in items if i["category"] == "salary")
    rent = sum(i["amount"] for i in items if i["category"] == "rent")
    tax = sum(i["amount"] for i in items if i["category"] == "tax")
    total = sum(i["amount"] for i in items)          # ФОТ + аренда + налоги + прочее
    wb_net = db.query("""SELECT coalesce(sum(net_profit),0)::float n FROM margin_by_sku
        WHERE period_from=%s AND platform='wb'""", (period,))[0]["n"]
    oz_net = _oz_summary("", period)["net"]   # к перечислению − COGS (включает оверхед)
    biz = wb_net + oz_net
    return {"applies": True, "period": period, "snapshot": snap,
            "own": snap == period[:8] + "01", "items": items,
            "fot": round(fot, 2), "rent": round(rent, 2), "tax": round(tax, 2), "total": round(total, 2),
            "wb_net": round(wb_net, 2), "oz_net": round(oz_net, 2),
            "biz_net": round(biz, 2), "net_after": round(biz - total, 2),
            "headcount": sum(1 for i in items if i["category"] == "salary")}


class OpexItem(BaseModel):
    name: str = ""
    role: str = ""
    category: str = "salary"          # salary | rent | tax
    base: float = 0
    tax_pct: float = 0


class OpexSave(BaseModel):
    period: str
    items: list[OpexItem] = []


@app.post("/api/opex/save")
def opex_save(payload: OpexSave):
    """Заменить снапшот общебизнесовых расходов за месяц (effective_from = 1-е число месяца).
    Месяц получает свой снапшот (перестаёт наследовать прошлый); следующие месяцы наследуют его.
    amount = base*(1+tax_pct). Дубли по имени схлопываем (последний выигрывает)."""
    eff = payload.period[:8] + "01"
    seen = {}
    for it in payload.items:
        nm = (it.name or "").strip()
        if nm:
            seen[nm] = it
    rows = [(eff, (it.category or "salary"), nm, (it.role or ""),
             float(it.base or 0), float(it.tax_pct or 0),
             round(float(it.base or 0) * (1 + float(it.tax_pct or 0)), 2))
            for nm, it in seen.items()]
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM opex WHERE effective_from=%s", (eff,))
            for r in rows:
                cur.execute("""INSERT INTO opex(effective_from,category,name,role,base,tax_pct,amount)
                    VALUES(%s,%s,%s,%s,%s,%s,%s)""", r)
    return {"ok": True, "period": eff, "count": len(rows)}


def _biz_for(period):
    """Агрегат по ВСЕМ аккаунтам за период (через _summary_one на аккаунт) + разбивка."""
    # Главная/бизнес-экран — только WB (его метрики WB-специфичны: СПП, наша цена, raw_wb_report).
    # Ozon живёт на отдельной странице /ozon и в бизнес-агрегат WB не подмешивается.
    accts = [r["account"] for r in db.query(
        "SELECT DISTINCT account FROM margin_by_sku WHERE period_from=%s AND platform='wb' ORDER BY 1", (period,))]
    per = [(a, _summary_one("", a, period)) for a in accts]
    agg = {}
    for k in ("revenue", "net", "cogs", "commission", "logistics", "qty", "own_revenue",
              "revenue_wb", "adv_spend", "reviews_spend", "storage_all", "returns_sum",
              "loss_count", "loss_sum", "loss_qty", "unalloc"):
        agg[k] = sum((p.get(k) or 0) for _, p in per)
    rev, own, wb_rev = agg["revenue"], agg["own_revenue"], agg["revenue_wb"]
    agg["margin_pct"] = round(agg["net"] / rev * 100, 1) if rev else None
    agg["margin_own"] = round(agg["net"] / own * 100, 1) if own else None
    # СПП = (наша цена − «ВБ реализовал») / наша цена; revenue витрины = наша цена
    agg["spp_pct"] = round((rev - wb_rev) / rev * 100, 1) if rev and wb_rev else None
    agg["margin_wb"] = round(agg["net"] / wb_rev * 100, 1) if wb_rev else None
    agg["cogs_pct"] = round(agg["cogs"] / rev * 100, 1) if rev else None
    agg["commission_pct"] = round(agg["commission"] / rev * 100, 1) if rev else None
    agg["logi_pct"] = round(agg["logistics"] / rev * 100, 1) if rev else None
    agg["adv_pct"] = round(agg["adv_spend"] / rev * 100, 1) if rev else None
    return agg, per


@app.get("/api/business")
def business(period: str = ""):
    """Главный экран: агрегат по всему бизнесу (оба ВБ) + расходы + динамика к прошлому месяцу."""
    if not period:
        period = db.query("SELECT max(period_from)::text p FROM margin_by_sku")[0]["p"]
    cur, per = _biz_for(period)            # cur — WB-часть
    oz = _oz_summary("", period)           # Ozon-часть (к перечислению − COGS)
    # ИТОГ по всему бизнесу = WB + Ozon. ВАЖНО: ВБ берём по НАШЕЙ цене (own_revenue) и нашей
    # марже (margin_own), а НЕ по цене покупателя после СПП — для единой базы с Ozon.
    wb_rev = cur.get("own_revenue") or 0
    t_rev, t_net = wb_rev + oz["revenue"], cur["net"] + oz["net"]
    t_cogs = (cur.get("cogs") or 0) + oz["cogs"]
    cur["wb"] = {"revenue": round(wb_rev, 2), "net": cur["net"], "cogs": cur.get("cogs"),
                 "margin_pct": cur.get("margin_own")}
    cur["ozon"] = {"revenue": oz["revenue"], "net": oz["net"], "margin_pct": oz["margin_pct"],
                   "cogs": oz["cogs"]}
    ya = _ya_business(period)              # Маркет за месяц: выручка/расходы/COGS — входит в ИТОГ
    if ya:
        cur["yandex"] = ya
        t_rev += ya["revenue"]; t_net += ya["net"]; t_cogs += ya["cogs"]
    # Учётная себестоимость (из таблицы расходов, Цифровой) за период — для сверки рядом
    ca = db.query("SELECT coalesce(sum(cogs),0)::float c FROM cogs_actual WHERE month=%s", (period,))[0]["c"]
    cur["total"] = {"revenue": round(t_rev, 2), "net": round(t_net, 2), "cogs": round(t_cogs, 2),
                    "margin_pct": round(t_net / t_rev * 100, 1) if t_rev else None,
                    "cogs_pct": round(t_cogs / t_rev * 100, 1) if t_rev else None,
                    "cogs_actual": round(ca, 2) if ca else None,
                    "with_market": bool(ya)}
    _snap = db.query("SELECT max(effective_from)::date e FROM opex WHERE effective_from<=%s", (period,))[0]["e"]
    op = db.query("SELECT coalesce(sum(amount),0)::float t FROM opex WHERE effective_from=%s", (_snap,))[0]["t"] if _snap else 0.0
    cur["opex"] = round(op, 2)
    cur["net_after_opex"] = round(t_net - op, 2)       # после ФОТ — от ИТОГА бизнеса
    prev_p = _prev_period("", "", period)
    if prev_p:
        prv, _ = _biz_for(prev_p)
        oz_prev = _oz_summary("", prev_p)
        cur["prev_period"] = prev_p
        cur["delta"] = {}
        for k in ("net", "revenue", "cogs", "margin_pct", "margin_own", "spp_pct",
                  "commission_pct", "adv_spend", "loss_count"):
            c, o = cur.get(k), prv.get(k)
            cur["delta"][k] = None if c is None or o is None else {
                "abs": round(c - o, 2), "pct": (None if not o else round((c - o) / abs(o) * 100, 1))}
        # дельты по ИТОГУ (WB по нашей цене + Ozon + Маркет) — Маркет теперь помесячный
        ya_prev = _ya_business(prev_p) if ya else None
        p_rev = (prv.get("own_revenue") or 0) + oz_prev["revenue"] + (ya_prev["revenue"] if ya_prev else 0)
        p_net = prv["net"] + oz_prev["net"] + (ya_prev["net"] if ya_prev else 0)
        for k, c, o in (("total_revenue", t_rev, p_rev),
                        ("total_net", t_net, p_net),
                        ("total_margin", cur["total"]["margin_pct"],
                         round(p_net / p_rev * 100, 1) if p_rev else None)):
            cur["delta"][k] = None if c is None or o is None else {
                "abs": round(c - o, 2), "pct": (None if not o else round((c - o) / abs(o) * 100, 1))}
    else:
        cur["prev_period"] = None
        cur["delta"] = None
    cur["accounts"] = [{"account": a, "platform": "ВБ",
                        "name": {"wb_acc1": "Цифровой квадрат", "wb_acc2": "Дисквэр"}.get(a, a),
                        "revenue": p["revenue"], "net": p["net"],
                        "margin_pct": p["margin_pct"], "margin_own": p["margin_own"]} for a, p in per]
    for a in ("oz_acc1", "oz_acc2"):                # Ozon-аккаунты в ту же разбивку
        s = _oz_summary(a, period)
        if s["revenue"]:
            cur["accounts"].append({"account": a, "platform": "Ozon", "name": OZ_NAMES.get(a, a),
                "revenue": s["revenue"], "net": s["net"], "margin_pct": s["margin_pct"], "margin_own": None})
    cur["period"] = period
    return cur


@app.get("/api/trend")
def trend():
    """Помесячная динамика по всему бизнесу (оба ВБ) для графиков на главной + YTD-итоги."""
    periods = [r["p"] for r in db.query(
        "SELECT DISTINCT period_from::text p FROM margin_by_sku ORDER BY 1")]
    months = []
    for p in periods:
        b, _ = _biz_for(p)
        months.append({
            "month": p, "revenue": b["revenue"], "net": b["net"], "qty": b["qty"],
            "margin_pct": b["margin_pct"], "margin_own": b["margin_own"],
            "cogs_pct": b["cogs_pct"], "commission_pct": b["commission_pct"],
            "logi_pct": b["logi_pct"], "spp_pct": b["spp_pct"], "adv_pct": b["adv_pct"],
        })
    ytd = {k: round(sum(m[k] or 0 for m in months), 2) for k in ("revenue", "net", "qty")}
    ytd["margin_pct"] = round(ytd["net"] / ytd["revenue"] * 100, 1) if ytd["revenue"] else None
    return {"months": months, "ytd": ytd}


@app.get("/api/advice")
def advice(period: str = ""):
    """Аналитический слой: приоритизированные советы из цифр (детерминированно, без ИИ-вызова).
    Поверх этого можно подключить LLM для свободных вопросов — данные те же."""
    if not period:
        period = db.query("SELECT max(period_from)::text p FROM margin_by_sku")[0]["p"]
    b, _ = _biz_for(period)
    prev_p = _prev_period("", "", period)
    prv = _biz_for(prev_p)[0] if prev_p else None
    tips = []

    def tip(sev, title, text):
        tips.append({"sev": sev, "title": title, "text": text})

    # 1. Маржа от нашей цены vs цель 25%
    if b["margin_own"] is not None and b["margin_own"] < 25:
        need = db.query(f"""SELECT coalesce(sum(0.25*s.our_price*m.qty - m.net_profit),0)::float gap
            FROM margin_by_sku m JOIN sales s ON s.platform=m.platform AND s.account=m.account
              AND s.period_from=m.period_from AND s.article=m.article
            WHERE m.period_from=%s AND m.qty>0 AND s.our_price>0
              AND m.net_profit/(s.our_price*m.qty) < 0.25""", (period,))[0]["gap"]
        tip("high", f"Маржа от нашей цены {b['margin_own']}% < цели 25%",
            f"До цели не хватает ≈{need:,.0f} ₽ чистой. Поднять цены по блоку «🎯 Поднять цену первыми» "
            f"(там позиции отсортированы по вкладу в недобор).")
    # 2. СПП растёт
    if prv and b["spp_pct"] and prv["spp_pct"] and b["spp_pct"] > prv["spp_pct"] + 0.5:
        tip("warn", f"СПП выросла до {b['spp_pct']}% (было {prv['spp_pct']}%)",
            "СПП несёт продавец: каждый +1₽ СПП = −0.84₽ нам. Поднять базовые цены примерно на размер "
            "роста СПП, чтобы удержать маржу.")
    # 3. Постоянные расходы / чистая после них
    op = db.query("SELECT coalesce(sum(amount),0)::float t FROM opex WHERE effective_from<=%s", (period,))[0]["t"]
    if op > 0:
        after = b["net"] - op
        burden = round(op / b["revenue"] * 100, 1) if b["revenue"] else None
        if after < 0:
            tip("high", f"Чистая после ФОТ+аренды отрицательна ({after:,.0f} ₽)",
                f"Постоянные расходы {op:,.0f} ₽ ({burden}% выручки) превышают чистую с ВБ. "
                f"Срочно: поднять маржу (цены) и/или нарастить оборот; проверить раздутый штат к обороту.")
        else:
            tip("info", f"Постоянные расходы {op:,.0f} ₽/мес ({burden}% выручки)",
                f"Чистая бизнеса после ФОТ+аренды ≈{after:,.0f} ₽.")
    # 4. Рост COGS (микс)
    if prv and b["cogs_pct"] and prv["cogs_pct"] and b["cogs_pct"] > prv["cogs_pct"] + 3:
        tip("warn", f"COGS вырос до {b['cogs_pct']}% выручки (было {prv['cogs_pct']}%)",
            "Скорее всего дорогой микс (новые дорогие позиции). Проверить наценку на новинки — "
            "цель 25% от нашей цены должна закладываться сразу при заводе карточки.")
    # 5. Хронические убыточные (≥3 мес подряд)
    chronic = db.query("""
        WITH p AS (SELECT DISTINCT period_from FROM margin_by_sku WHERE period_from<=%s ORDER BY 1 DESC LIMIT 3)
        SELECT count(*) n, coalesce(sum(last_net),0)::float s FROM (
          SELECT account, article, count(*) k, max(net_profit) FILTER (WHERE period_from=%s)*1.0 last_net
          FROM margin_by_sku WHERE period_from IN (SELECT period_from FROM p) AND net_profit<0 AND qty>0 AND article<>'0'
          GROUP BY 1,2 HAVING count(*)>=3) t""", (period, period))[0]
    if chronic["n"] and chronic["n"] > 0:
        tip("warn", f"{chronic['n']} позиций убыточны ≥3 мес подряд",
            "Это хронический убыточный хвост. Решение: вывести из ассортимента или поднять цену/исправить "
            "габариты карточки (см. блоки «Убыточные» и «Подозрительные габариты»).")
    # 6. Габариты среди убыточных
    dims = db.query(f"""SELECT count(*) n FROM margin_by_sku m JOIN wb_cards c
        ON c.account=m.account AND c.nm_id::text=m.article
        WHERE m.period_from=%s AND m.net_profit<0 AND m.qty>0
          AND (c.dims_valid=false OR c.volume_l IS NULL
               OR c.weight_kg/NULLIF(c.volume_l,0) < {DENS_LOW}
               OR c.weight_kg/NULLIF(c.volume_l,0) > {DENS_HIGH})""", (period,))[0]["n"]
    if dims and dims > 0:
        tip("info", f"{dims} убыточных позиций — из-за габаритов, а не цены",
            "WB считает логистику по литрам. Перемерить/исправить Д×Ш×В в карточках — дешевле, чем поднимать цену.")
    if not tips:
        tip("info", "Ключевых проблем не видно", "Маржа у цели, расходы покрыты. Держать курс.")
    return {"period": period, "tips": tips}


@app.get("/api/uplift")
def uplift(platform: str = "", account: str = "", period: str = "", target: float = 0.25, limit: int = 20):
    """Какие позиции поднять в цене ПЕРВЫМИ, чтобы держать целевую маржу (по умолч. 25% от НАШЕЙ цены).
    Ранжируем по ₽-вкладу (net_gap = цель·наша_выручка − net): сверху — кто сильнее всего тянет
    месяц от цели (большая выручка × недобор маржи). Для подозрительных габаритов — пометка
    (там сначала чинить карточку, а не цену)."""
    conds, p = [], []
    if platform:
        conds.append("m.platform=%s"); p.append(platform)
    if account:
        conds.append("m.account=%s"); p.append(account)
    if period:
        conds.append("m.period_from=%s"); p.append(period)
    conds += ["m.article<>'0'", "m.qty>0", "s.our_price>0"]
    where = " WHERE " + " AND ".join(conds)
    rows = db.query(f"""
        SELECT m.article nm_id, c.vendor_code, c.title,
            (s.our_price*m.qty)::float own_rev, m.qty::float,
            round((m.net_profit/(s.our_price*m.qty)*100)::numeric,1)::float margin_own,
            ({target}*s.our_price*m.qty - m.net_profit)::float net_gap,
            ceil(({target} - m.net_profit/(s.our_price*m.qty)) / (0.61-{target}) * 100)::float price_up_pct,
            CASE WHEN c.dims_valid=false OR c.volume_l IS NULL THEN 'невалидные'
                 WHEN c.weight_kg/NULLIF(c.volume_l,0) < {DENS_LOW} THEN 'крупн./лёгкий'
                 WHEN c.weight_kg/NULLIF(c.volume_l,0) > {DENS_HIGH} THEN 'тяжёлый/мелкий'
                 ELSE NULL END dims_flag
        FROM margin_by_sku m
        LEFT JOIN wb_cards c ON c.account=m.account AND c.nm_id::text=m.article
        LEFT JOIN sales s ON s.platform=m.platform AND s.account=m.account
             AND s.period_from=m.period_from AND s.article=m.article
        {where} AND m.net_profit/(s.our_price*m.qty) < {target}
        ORDER BY net_gap DESC NULLS LAST LIMIT %s""", p + [limit])
    return {"rows": rows, "target": target, "count": len(rows)}


@app.get("/api/weekly")
def weekly(platform: str = "", account: str = "", period: str = "", rolling: int = 0):
    """Разбивка по неделям (по дате реализации rr_dt). COGS на неделю — из себест/ед × недельные
    количества. opmargin = (к перечислению − логистика − COGS)/выр (без накладных: реклама/хранение
    лумпи); net% — с накладными. rolling>0 — скользящие последние N недель через все месяцы
    (себест/ед — глобальная, т.к. replacement-cost стабилен); иначе — недели выбранного месяца."""
    accts = [account] if account else ["wb_acc1", "wb_acc2"]   # пусто = оба ВБ
    if rolling and rolling > 0:
        # глобальная себест/ед по nm (стабильна) + все периоды; берём последние N недель
        rows = db.query("""
            WITH cpu AS (SELECT article, sum(cogs)/sum(qty) u FROM margin_by_sku
                WHERE account=ANY(%s) AND qty>0 AND cogs>0 GROUP BY article),
            r AS (SELECT date_trunc('week',(payload->>'rr_dt')::date)::date wk,
                payload->>'nm_id' nm, payload->>'supplier_oper_name' op,
                coalesce((payload->>'quantity')::numeric,0) q, coalesce((payload->>'retail_amount')::numeric,0) ra,
                coalesce((payload->>'ppvz_for_pay')::numeric,0) pay, coalesce((payload->>'delivery_rub')::numeric,0) del,
                coalesce((payload->>'storage_fee')::numeric,0) stor, coalesce((payload->>'acceptance')::numeric,0) acc,
                coalesce((payload->>'deduction')::numeric,0) ded, coalesce((payload->>'penalty')::numeric,0) pen
                FROM raw_wb_report WHERE account=ANY(%s))
            SELECT wk,
                sum(CASE WHEN op='Продажа' THEN ra WHEN op='Возврат' THEN -ra ELSE 0 END)::float rev,
                sum(pay)::float topay, sum(del)::float logi,
                (sum(stor)+sum(acc)+sum(ded)+sum(pen))::float overhead,
                sum(CASE WHEN op='Продажа' THEN q WHEN op='Возврат' THEN -q ELSE 0 END)::float qty,
                sum(CASE WHEN op='Продажа' THEN q*coalesce(c.u,0)
                         WHEN op='Возврат' THEN -q*coalesce(c.u,0) ELSE 0 END)::float cogs
            FROM r LEFT JOIN cpu c ON c.article=r.nm GROUP BY wk ORDER BY wk DESC LIMIT %s""",
            (accts, accts, rolling))
        rows = list(reversed(rows))
    elif not period:
        return {"rows": []}
    else:
        rows = db.query("""
        WITH cpu AS (SELECT article, sum(cogs)/sum(qty) u FROM margin_by_sku
            WHERE account=ANY(%s) AND period_from=%s AND qty>0 AND cogs>0 GROUP BY article),
        r AS (SELECT date_trunc('week',(payload->>'rr_dt')::date)::date wk,
            payload->>'nm_id' nm, payload->>'supplier_oper_name' op,
            coalesce((payload->>'quantity')::numeric,0) q, coalesce((payload->>'retail_amount')::numeric,0) ra,
            coalesce((payload->>'ppvz_for_pay')::numeric,0) pay, coalesce((payload->>'delivery_rub')::numeric,0) del,
            coalesce((payload->>'storage_fee')::numeric,0) stor, coalesce((payload->>'acceptance')::numeric,0) acc,
            coalesce((payload->>'deduction')::numeric,0) ded, coalesce((payload->>'penalty')::numeric,0) pen
            FROM raw_wb_report WHERE account=ANY(%s) AND period_from=%s)
        SELECT wk,
            sum(CASE WHEN op='Продажа' THEN ra WHEN op='Возврат' THEN -ra ELSE 0 END)::float rev,
            sum(pay)::float topay, sum(del)::float logi,
            (sum(stor)+sum(acc)+sum(ded)+sum(pen))::float overhead,
            sum(CASE WHEN op='Продажа' THEN q WHEN op='Возврат' THEN -q ELSE 0 END)::float qty,
            sum(CASE WHEN op='Продажа' THEN q*coalesce(c.u,0)
                     WHEN op='Возврат' THEN -q*coalesce(c.u,0) ELSE 0 END)::float cogs
        FROM r LEFT JOIN cpu c ON c.article=r.nm GROUP BY wk ORDER BY wk""",
        (accts, period, accts, period))
    ad = _weekly_adspend("wb", accts, period if not rolling else None)
    out = []
    for x in rows:
        rev = x["rev"] or 0
        if rev < 1000:
            continue
        adv = round(ad.get(x["wk"], 0))
        out.append({
            "wk": x["wk"], "rev": rev, "qty": x["qty"],
            "cogs_pct": round(x["cogs"] / rev * 100, 1),
            "logi_pct": round(x["logi"] / rev * 100, 1),
            "ad": adv, "ad_pct": round(adv / rev * 100, 1),
            "opmargin": round((x["topay"] - x["logi"] - x["cogs"]) / rev * 100, 1),
            "net_pct": round((x["topay"] - x["logi"] - x["cogs"] - x["overhead"]) / rev * 100, 1),
        })
    return {"rows": out}


def _weekly_adspend(platform, accts, period=None):
    """{wk(date): расход рекламы за неделю} из ad_spend_daily (неделя date_trunc как в отчётах)."""
    cond = "platform=%s AND account=ANY(%s)"
    params = [platform, accts]
    if period:
        cond += " AND date >= %s AND date < (%s::date + interval '1 month')"
        params += [period, period]
    rows = db.query(f"""SELECT date_trunc('week',date)::date wk, sum(spend)::float ad
        FROM ad_spend_daily WHERE {cond} GROUP BY 1""", params)
    return {r["wk"]: r["ad"] for r in rows}


def _loss_streak(platform, account, period, articles):
    """Сколько периодов подряд (заканчивая выбранным) позиция убыточна. Нужны загруженные
    прошлые месяцы; считаем число ведущих убыточных периодов в порядке от текущего назад."""
    if not period or not articles:
        return {}
    w, p = ["period_from <= %s", "article = ANY(%s)"], [period, list(articles)]
    if platform:
        w.append("platform=%s"); p.append(platform)
    if account:
        w.append("account=%s"); p.append(account)
    hist = db.query(f"""SELECT article, period_from::text pf,
        (net_profit<0 AND qty>0) loss FROM margin_by_sku
        WHERE {' AND '.join(w)} ORDER BY article, period_from DESC""", p)
    by = {}
    for h in hist:
        by.setdefault(h["article"], []).append(h["loss"])
    out = {}
    for art, flags in by.items():
        n = 0
        for f in flags:          # от текущего периода назад, считаем ведущие True
            if f:
                n += 1
            else:
                break
        out[art] = n
    return out


@app.get("/api/stocks")
def stocks(account: str = ""):
    """Проблемные точки по остаткам WB (FBO): что лежит + возвраты в пути. Пусто = оба ВБ."""
    accts = [account] if account else ["wb_acc1", "wb_acc2"]
    cap = db.query("SELECT max(captured_at)::text m FROM wb_stocks")[0]["m"]
    if not cap:
        return {"captured_at": None, "total": {}, "by_subject": []}
    by = db.query("""SELECT subject, sum(quantity)::float qty,
        sum(in_way_from_client)::float returns
        FROM wb_stocks WHERE account=ANY(%s) AND captured_at=%s GROUP BY 1 ORDER BY 2 DESC""",
                  (accts, cap))
    tot = db.query("""SELECT sum(quantity)::float qty, sum(quantity_full)::float full,
        sum(in_way_from_client)::float returns, count(DISTINCT nm_id) nm
        FROM wb_stocks WHERE account=ANY(%s) AND captured_at=%s""", (accts, cap))[0]
    # стоимость остатков на ФБО по себестоимости: остаток × себест/ед (из margin, последний период)
    val = db.query("""
        WITH cost AS (
            SELECT DISTINCT ON (article) article, cogs/qty AS unit_cost
            FROM margin_by_sku WHERE account=ANY(%s) AND qty>0 AND cogs>0
            ORDER BY article, period_from DESC)
        SELECT coalesce(sum(st.quantity*c.unit_cost),0)::float fbo_value,
               count(DISTINCT st.nm_id) FILTER (WHERE c.unit_cost IS NOT NULL) nm_valued,
               count(DISTINCT st.nm_id) nm_total
        FROM wb_stocks st LEFT JOIN cost c ON c.article=st.nm_id::text
        WHERE st.account=ANY(%s) AND st.captured_at=%s AND st.quantity>0""",
                   (accts, accts, cap))[0]
    return {"captured_at": cap, "total": tot, "by_subject": by,
            "fbo_value": val["fbo_value"], "fbo_nm_valued": val["nm_valued"],
            "fbo_nm_total": val["nm_total"]}


# Пороги плотности (кг/л) для подозрительных карточек. Медиана ≈0.14, p05≈0.04, p95≈0.61.
DENS_LOW, DENS_HIGH = 0.05, 0.7


@app.get("/api/anomalies")
def anomalies(account: str = "", limit: int = 50):
    """Подозрительные габариты карточек WB. WB считает логистику по объёму (литры),
    поэтому ошибки в Д×Ш×В бьют по марже. Флаги:
      • «крупн./лёгкий» (плотность < 0.05 кг/л) — раздут объём, лишние литры логистики;
      • «тяжёлый/мелкий» (> 0.7 кг/л) — занижены габариты (часто опечатка, напр. длина 1 см);
      • «невалидные» — WB пометил dims как невалидные.
    Показываем только продаваемые позиции, сортируем по объёму продаж (где больнее)."""
    rows = db.query("""
        SELECT c.nm_id::text nm_id, c.vendor_code, c.title,
            c.length_cm::float, c.width_cm::float, c.height_cm::float,
            c.weight_kg::float, round(c.volume_l,2)::float volume_l,
            round((c.weight_kg/NULLIF(c.volume_l,0))::numeric,3)::float density,
            c.dims_valid, m.qty::float qty_sold, m.logistics::float logistics,
            m.net_profit::float net_profit,
            CASE WHEN c.dims_valid=false OR c.volume_l IS NULL OR c.weight_kg IS NULL THEN 'невалидные'
                 WHEN c.weight_kg/NULLIF(c.volume_l,0) < %s THEN 'крупн./лёгкий'
                 WHEN c.weight_kg/NULLIF(c.volume_l,0) > %s THEN 'тяжёлый/мелкий'
                 ELSE NULL END flag
        FROM wb_cards c
        JOIN margin_by_sku m ON m.account=c.account AND m.article=c.nm_id::text AND m.qty>0
        WHERE c.account=ANY(%s)
          AND (c.dims_valid=false OR c.volume_l IS NULL OR c.weight_kg IS NULL
               OR c.weight_kg/NULLIF(c.volume_l,0) < %s
               OR c.weight_kg/NULLIF(c.volume_l,0) > %s)
        ORDER BY m.qty DESC NULLS LAST, m.logistics DESC LIMIT %s""",
        (DENS_LOW, DENS_HIGH, ([account] if account else ["wb_acc1", "wb_acc2"]), DENS_LOW, DENS_HIGH, limit))
    return {"rows": rows, "count": len(rows), "median_density": 0.137}


# =========================================================================
# OZON — отдельный контур. У Ozon нет понятий WB (СПП/наша цена/габариты),
# а расходы раскладываются в Python (categorize_operation), не в SQL.
# Данные: raw_ozon_transaction (расходы по статьям) + margin_by_sku
# (platform='ozon') для COGS/маржи по SKU. WB-экраны это не затрагивает.
# =========================================================================
import datetime as _dt  # noqa: E402
from collections import defaultdict as _dd  # noqa: E402
from collectors.ozon import categorize_operation, CATEGORIES  # noqa: E402
from collectors.ozon_realization import sales_split as _oz_realization_split  # noqa: E402
import reports.ozon_mp_report as _ozmp  # noqa: E402
import reports.wb_mp_report as _wbmp  # noqa: E402
import reports.yandex_mp_report as _yamp  # noqa: E402

OZ_NAMES = {"oz_acc1": "Цифровой квадрат", "oz_acc2": "Дисквэр"}
OZ_RU = {"revenue": "Выручка", "commission": "Комиссия", "advertising": "Реклама/продвиж.",
         "logistics": "Логистика", "returns": "Возвраты", "penalties": "Штрафы",
         "acquiring": "Эквайринг", "storage": "Склад/обработка", "subscription": "Подписка",
         "partners": "Партнёрские", "points": "Баллы/Звёздные", "compensation": "Компенсации",
         "fbo": "FBO склад", "other": "Прочее"}
# управляемость статьи: green — режется решением, yellow — частично, red — фикс площадки
OZ_CTRL = {"advertising": "green", "penalties": "green", "subscription": "green",
           "points": "green", "logistics": "yellow", "returns": "yellow", "storage": "yellow",
           "partners": "yellow", "fbo": "yellow", "other": "yellow",
           "commission": "red", "acquiring": "red", "compensation": "red"}


def _oz_last_period():
    r = db.query("SELECT max(period_from)::text p FROM margin_by_sku WHERE platform='ozon'")
    return r[0]["p"] if r and r[0]["p"] else None


def _oz_month(period):
    """period 'YYYY-MM-01' → (first, last) ISO месяца."""
    f = _dt.date.fromisoformat(period)
    nxt = (f.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)
    return f.isoformat(), (nxt - _dt.timedelta(days=1)).isoformat()


def _oz_ops(account, df, dt):
    """payload-строки raw_ozon_transaction за период по operation_date (account опц.)."""
    cond = "(payload->>'operation_date')::date BETWEEN %s AND %s"
    p = [df, dt]
    if account:
        cond += " AND account=%s"; p.append(account)
    return [r["payload"] for r in db.query(
        f"SELECT payload FROM raw_ozon_transaction WHERE {cond}", p)]


def _oz_aggregate(ops):
    """Σ по статьям (categorize_operation), оверхед (ops без items[]), выручка по схеме FBO/FBS."""
    cats = {c: 0.0 for c in CATEGORIES}
    overhead, schema_rev = 0.0, {}
    for op in ops:
        cc = categorize_operation(op)
        for c, v in cc.items():
            cats[c] += v
        if not op.get("items"):
            overhead += sum(cc.values())
        sch = ((op.get("posting") or {}).get("delivery_schema") or "—").upper()
        schema_rev[sch] = schema_rev.get(sch, 0.0) + cc["revenue"]
    return cats, overhead, schema_rev


def _oz_cogs(account, period):
    w, p = ["platform='ozon'", "period_from=%s"], [period]
    if account:
        w.append("account=%s"); p.append(account)
    return db.query(f"SELECT coalesce(sum(cogs),0)::float c FROM margin_by_sku WHERE {' AND '.join(w)}", p)[0]["c"]


def _oz_summary(account, period):
    df, dt = _oz_month(period)
    cats, overhead, schema_rev = _oz_aggregate(_oz_ops(account, df, dt))
    to_payout = sum(cats.values())
    cogs = _oz_cogs(account, period)
    rev = cats["revenue"]
    net = to_payout - cogs
    return {
        "revenue": round(rev, 2), "to_payout": round(to_payout, 2), "cogs": round(cogs, 2),
        "overhead": round(overhead, 2), "net": round(net, 2),
        "margin_pct": round(net / rev * 100, 1) if rev else None,
        "cogs_pct": round(cogs / rev * 100, 1) if rev else None,
        "cats": {c: round(cats[c], 2) for c in CATEGORIES},
        "schema_rev": {k: round(v, 2) for k, v in schema_rev.items()},
    }


@app.get("/ozon", response_class=HTMLResponse)
def ozon_page():
    return (STATIC / "ozon.html").read_text(encoding="utf-8")


@app.get("/reports", response_class=HTMLResponse)
def reports_page():
    return (STATIC / "reports.html").read_text(encoding="utf-8")


@app.get("/reports/wb", response_class=HTMLResponse)
def reports_wb_page():
    return (STATIC / "reports_wb.html").read_text(encoding="utf-8")


@app.get("/reports/yandex", response_class=HTMLResponse)
def reports_yandex_page():
    return (STATIC / "reports_yandex.html").read_text(encoding="utf-8")


@app.get("/reports/wb-clearance", response_class=HTMLResponse)
def reports_wb_clearance_page():
    return (STATIC / "reports_wb_clearance.html").read_text(encoding="utf-8")


class ClearanceItem(BaseModel):
    account: str
    nm_id: int


class ClearanceItems(BaseModel):
    items: list[ClearanceItem] = []


@app.post("/api/wb/clearance/dismiss")
def wb_clearance_dismiss(payload: ClearanceItems):
    """Сотрудник закрывает позиции распродажи (остаток ВБ=0, цену подняли) — прячем из таблицы.
    Пишем в wb_clearance_dismissed (переживает ежедневный перезалив файла), затем перегенерируем страницу."""
    rows = [{"account": it.account, "nm_id": int(it.nm_id)} for it in payload.items if it.account and it.nm_id]
    if rows:
        db.upsert("wb_clearance_dismissed", rows, conflict_cols=["account", "nm_id"])
        import reports.wb_clearance_page as _clr
        _clr.render()
    return {"ok": True, "closed": len(rows)}


@app.post("/api/wb/clearance/restore")
def wb_clearance_restore(payload: ClearanceItems):
    """Вернуть ошибочно закрытую позицию обратно в слежение."""
    n = 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            for it in payload.items:
                if it.account and it.nm_id:
                    cur.execute("DELETE FROM wb_clearance_dismissed WHERE account=%s AND nm_id=%s",
                                (it.account, int(it.nm_id)))
                    n += 1
    import reports.wb_clearance_page as _clr
    _clr.render()
    return {"ok": True, "restored": n}


@app.get("/opex", response_class=HTMLResponse)
def opex_page():
    return (STATIC / "opex.html").read_text(encoding="utf-8")


@app.get("/sites", response_class=HTMLResponse)
def sites_page():
    return (STATIC / "sites.html").read_text(encoding="utf-8")


@app.get("/suppliers", response_class=HTMLResponse)
def suppliers_page():
    return (STATIC / "suppliers.html").read_text(encoding="utf-8")


@app.get("/stale", response_class=HTMLResponse)
def stale_page():
    return (STATIC / "stale.html").read_text(encoding="utf-8")


@app.get("/brak", response_class=HTMLResponse)
def brak_page():
    return (STATIC / "brak.html").read_text(encoding="utf-8")


@app.get("/api/brak")
def brak():
    """Брак/возвраты: что чаще всего возвращают (ВБ по nm: доля возвратов) + склад «Брак».
    Высокая доля возвратов по модели → сигнал не закупать (и у какого поставщика)."""
    period = db.query("SELECT max(period_from)::text p FROM margin_by_sku WHERE platform='wb'")
    period = period[0]["p"] if period and period[0]["p"] else None
    # ВБ: продажи vs возвраты по nm (за всё, что загружено)
    wb = db.query("""
        WITH agg AS (SELECT payload->>'nm_id' nm,
            sum(CASE WHEN payload->>'supplier_oper_name'='Продажа' THEN (payload->>'quantity')::numeric ELSE 0 END) sales,
            sum(CASE WHEN payload->>'supplier_oper_name'='Возврат' THEN (payload->>'quantity')::numeric ELSE 0 END) ret
            FROM raw_wb_report GROUP BY 1)
        SELECT a.nm, c.title, c.vendor_code, a.sales::float sales, a.ret::float ret,
            round((a.ret/nullif(a.sales+a.ret,0)*100)::numeric,1)::float rate
        FROM agg a LEFT JOIN wb_cards c ON c.nm_id::text=a.nm
        WHERE a.ret>0 AND (a.sales+a.ret)>=5 ORDER BY rate DESC, a.ret DESC LIMIT 60""")
    # поставщик по нашему артикулу (vendor_code) из supplier_stock
    cap = db.query("SELECT max(captured_at)::text c FROM supplier_stock")[0]["c"]
    supmap = {}
    if cap:
        for r in db.query("""SELECT DISTINCT ON (key) key, supplier FROM (
                SELECT article key, supplier FROM supplier_stock WHERE captured_at=%s AND supplier IS NOT NULL
                UNION ALL
                SELECT external_code key, supplier FROM supplier_stock WHERE captured_at=%s AND supplier IS NOT NULL
            ) t WHERE key IS NOT NULL AND key<>'' ORDER BY key""", (cap, cap)):
            supmap[r["key"]] = r["supplier"]
    for r in wb:
        r["supplier"] = supmap.get(r.get("vendor_code"))
    # склад «Брак»
    brak_store = []
    if cap:
        brak_store = db.query("""SELECT name, supplier, stock::float,
            round((stock*cost_seb)::numeric,0)::float val
            FROM supplier_stock WHERE captured_at=%s AND store='Брак' AND stock>0
            ORDER BY stock*cost_seb DESC NULLS LAST""", (cap,))
    return {"wb_returns": wb, "brak_store": brak_store, "captured": cap}


OUR_STORES = ["Кантемировская", "Дисквер", "Звездный"]   # наши физические склады

# Профильные товары (печать) для Залежей — whitelist по названию: оставляем картриджи/тонер/
# чернила/фотобарабаны и т.п., отсекаем расходку (короба, БОПП-пакеты, скотч, бумага, папки…).
CORE_KEYWORDS = ["картридж", "тонер", "чернил", "фотобарабан", "drum", "драм",
                 "девелопер", "блок проявки", "печатающ", "тонер-картридж", "ribbon",
                 "ic-", "tc-", "фотовал", "ракель"]
CORE_LIKE = [f"%{k}%" for k in CORE_KEYWORDS]


def _colored(nm):
    n = (nm or "").lower()
    return any(c in n for c in ("cyan", "magenta", "yellow", "голуб", "пурпур", "жёлт",
                                "желт", "циан", "маджент", "purpur"))


_SUP_ORG = re.compile(r"\b(ооо|оао|зао|пао|ао|ип|чп|тд)\b")
_SUP_SUFFIX = re.compile(r"\b(oem|wb|вб|мск|москва|спб|spb|t2|опт|ozon|озон|new|нью|рус)\b")


def _canon_supplier(name):
    """Канонический ключ имени поставщика — чтобы слить дубли (ООО «X» OEM / «X» МСК / «X» (Закрыто))."""
    s = (name or "").lower()
    s = re.sub(r"\(.*?\)", " ", s)            # убрать (Закрыто)/(Изипринт)/(поставщик)
    for ch in "«»\"'`":
        s = s.replace(ch, " ")
    s = _SUP_ORG.sub(" ", s)
    s = _SUP_SUFFIX.sub(" ", s)
    s = re.sub(r"[^a-zа-я0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


@app.get("/api/suppliers")
def suppliers(q: str = "", limit: int = 200):
    """Поставщики (арбитраж): остатки на их «Удалённом складе», дубли имён слиты по каноническому
    ключу (отображаем вариант с наибольшим движением), дата последней закупки из приёмок МС и
    спящие поставщики (давно не закупались / закрыты) — кандидаты на удаление."""
    cap = db.query("SELECT max(captured_at)::text c FROM supplier_stock")[0]["c"]
    if not cap:
        return {"captured": None, "supplier_count": 0, "suppliers": [], "dormant": [],
                "totals": {}, "stores": []}
    raw = db.query("""SELECT coalesce(supplier,'— не указан') s, count(DISTINCT ms_id) n,
        sum(CASE WHEN coalesce(sold_30d,0)>0 THEN 1 ELSE 0 END) moving,
        round(sum(stock*cost_seb)::numeric,0)::float val
        FROM supplier_stock WHERE captured_at=%s AND store='Удаленный склад' AND stock>0
        GROUP BY 1""", (cap,))
    today = _dt.date.today()
    # даты последних закупок: канон-ключ -> самая свежая приёмка
    lp = {}
    for r in db.query("SELECT supplier, last_supply FROM supplier_last_purchase"):
        k = _canon_supplier(r["supplier"])
        if k and r["last_supply"] and (k not in lp or r["last_supply"] > lp[k]):
            lp[k] = r["last_supply"]
    # слияние дублей по канон-ключу
    merged = {}
    for r in raw:
        name = r["s"]
        k = _canon_supplier(name) or name.lower()
        m = merged.setdefault(k, {"key": k, "names": [], "n": 0, "moving": 0, "val": 0.0, "closed": False})
        m["names"].append((name, r["moving"] or 0, r["n"] or 0))
        m["n"] += r["n"] or 0
        m["moving"] += r["moving"] or 0
        m["val"] += r["val"] or 0
        if "закрыт" in (name or "").lower():
            m["closed"] = True
    rows = []
    for k, m in merged.items():
        disp = sorted(m["names"], key=lambda x: (-x[1], -x[2]))[0][0]   # имя с макс. движением
        unspecified = (k == "" or disp.strip().startswith("— не"))
        last = lp.get(k)
        days = (today - last).days if last else None
        # кандидат на удаление: закрыт, ИЛИ давно не закупались И товар не движется
        dormant = (not unspecified) and (
            m["closed"] or ((days is None or days > 90) and m["moving"] == 0))
        rows.append({"supplier": disp, "variants": len(m["names"]), "n": m["n"],
                     "moving": m["moving"], "val": round(m["val"]),
                     "last_supply": last.isoformat() if last else None,
                     "days_since": days, "closed": m["closed"], "dormant": dormant})
    rows.sort(key=lambda x: -(x["val"] or 0))
    active = [r for r in rows if not r["dormant"]]
    # спящие: сначала без приёмок за год (days None), затем по убыванию давности
    dormant = sorted([r for r in rows if r["dormant"]],
                     key=lambda x: -(x["days_since"] if x["days_since"] is not None else 10 ** 6))
    if q:
        ql = q.lower()
        active = [r for r in active if ql in r["supplier"].lower()]
    totals = {"frozen": round(sum(r["val"] or 0 for r in rows)),
              "suppliers": len(rows), "dormant": len(dormant)}
    stores = db.query("""SELECT store, count(DISTINCT ms_id) n, round(sum(stock*cost_seb)::numeric,0)::float val
        FROM supplier_stock WHERE captured_at=%s AND store IN ('Удаленный склад','Транзит') AND stock>0
        GROUP BY 1 ORDER BY val DESC NULLS LAST""", (cap,))
    return {"captured": cap, "supplier_count": len(rows), "totals": totals,
            "suppliers": active[:limit], "dormant": dormant, "stores": stores}


@app.get("/api/stale")
def stale(limit: int = 200):
    """Наши залежи: товары на НАШИХ складах, лежащие >3 мес — кандидаты на распродажу/поиск пары."""
    cap = db.query("SELECT max(captured_at)::text c FROM supplier_stock")[0]["c"]
    if not cap:
        return {"captured": None, "rows": [], "totals": {}}
    rows = db.query("""SELECT name, supplier, store, stock::float,
        round(stock_days::numeric)::int stock_days, round((stock*cost_seb)::numeric,0)::float stock_value
        FROM supplier_stock WHERE captured_at=%s AND store=ANY(%s) AND stock>0 AND stock_days>=90
          AND lower(name) LIKE ANY(%s)
        ORDER BY stock*cost_seb DESC NULLS LAST LIMIT %s""", (cap, OUR_STORES, CORE_LIKE, limit))
    for r in rows:
        r["colored"] = _colored(r["name"])
    tot = db.query("""SELECT count(*) n, round(sum(stock*cost_seb)::numeric,0)::float value
        FROM supplier_stock WHERE captured_at=%s AND store=ANY(%s) AND stock>0 AND stock_days>=90
          AND lower(name) LIKE ANY(%s)""",
        (cap, OUR_STORES, CORE_LIKE))[0]
    tot["colored_n"] = sum(1 for r in rows if r["colored"])
    return {"captured": cap, "rows": rows, "totals": tot}


def _wh_ss_source(stores, cap, prev):
    """Срез по складам supplier_stock (наши/Озон): штук + ₽ по складам, итог, дельта к prev."""
    cur = db.query("""SELECT store, sum(stock)::float units, count(DISTINCT ms_id) n,
        round(sum(stock*cost_seb)::numeric,0)::float val
        FROM supplier_stock WHERE captured_at=%s AND store=ANY(%s) AND stock>0
        GROUP BY store ORDER BY val DESC NULLS LAST""", (cap, stores))
    units = round(sum(r["units"] or 0 for r in cur), 1)
    val = round(sum(r["val"] or 0 for r in cur))
    d_units = d_val = None
    if prev:
        pv = db.query("""SELECT sum(stock)::float units, round(sum(stock*cost_seb)::numeric,0)::float val
            FROM supplier_stock WHERE captured_at=%s AND store=ANY(%s) AND stock>0""", (prev, stores))[0]
        d_units = round(units - (pv["units"] or 0), 1)
        d_val = round(val - (pv["val"] or 0))
    return {"stores": cur, "units": units, "val": val, "d_units": d_units, "d_val": d_val}


def _wh_history(stores, days=7):
    return db.query("""SELECT captured_at::text d, sum(stock)::float units
        FROM supplier_stock WHERE store=ANY(%s) AND stock>0
        GROUP BY captured_at ORDER BY captured_at DESC LIMIT %s""", (stores, days))


def _wh_ozon_fbo():
    """Озон ФБО по аккаунтам из ozon_fbo_stock (Ozon API, free_to_sell) + дельта к прошлому снимку.
    Разрез по юрлицам (Цифровой/Дисквэр), как ВБ ФБО."""
    ocap = db.query("SELECT max(captured_at)::text c FROM ozon_fbo_stock")[0]["c"]
    res = {"accounts": [], "units": 0, "reserved": 0, "d_units": None, "captured": ocap}
    if not ocap:
        return res
    oprev = db.query("SELECT max(captured_at)::text c FROM ozon_fbo_stock WHERE captured_at<%s", (ocap,))[0]["c"]
    res["accounts"] = db.query("""SELECT account, sum(free_to_sell)::float units,
        sum(reserved)::float reserved, count(DISTINCT sku) n
        FROM ozon_fbo_stock WHERE captured_at=%s GROUP BY account ORDER BY account""", (ocap,))
    res["units"] = round(sum(a["units"] or 0 for a in res["accounts"]), 1)
    res["reserved"] = round(sum(a["reserved"] or 0 for a in res["accounts"]), 1)
    if oprev:
        pv = db.query("SELECT sum(free_to_sell)::float u FROM ozon_fbo_stock WHERE captured_at=%s", (oprev,))[0]["u"]
        res["d_units"] = round(res["units"] - (pv or 0), 1)
    return res


def _wh_history_ozon(days=7):
    return db.query("""SELECT captured_at::text d, sum(free_to_sell)::float units
        FROM ozon_fbo_stock GROUP BY captured_at ORDER BY captured_at DESC LIMIT %s""", (days,))


@app.get("/warehouse", response_class=HTMLResponse)
def warehouse_page():
    return (STATIC / "warehouse.html").read_text(encoding="utf-8")


@app.get("/api/warehouse")
def warehouse_api():
    """Наш сток: наши склады + Озон ФБО (supplier_stock) + ВБ ФБО (wb_stocks), с дневной дельтой.
    Плюс дефицит наших складов (что кончается) и динамика штук по дням."""
    cap = db.query("SELECT max(captured_at)::text c FROM supplier_stock")[0]["c"]
    if not cap:
        return {"captured": None}
    prev = db.query("SELECT max(captured_at)::text c FROM supplier_stock WHERE captured_at<%s", (cap,))[0]["c"]
    our = _wh_ss_source(OUR_STORES, cap, prev)
    ozon_fbo = _wh_ozon_fbo()
    # ВБ ФБО — из wb_stocks (своя дата снимка), по аккаунтам + итог, дельта к предыдущему дню
    wcap = db.query("SELECT max(captured_at)::text c FROM wb_stocks")[0]["c"]
    wb_fbo = {"accounts": [], "units": 0, "d_units": None, "captured": wcap}
    if wcap:
        wprev = db.query("SELECT max(captured_at)::text c FROM wb_stocks WHERE captured_at<%s", (wcap,))[0]["c"]
        accs = db.query("""SELECT account, sum(quantity_full)::float units, count(DISTINCT nm_id) n
            FROM wb_stocks WHERE captured_at=%s GROUP BY account ORDER BY account""", (wcap,))
        wb_fbo["accounts"] = accs
        wb_fbo["units"] = round(sum(a["units"] or 0 for a in accs), 1)
        if wprev:
            pv = db.query("SELECT sum(quantity_full)::float u FROM wb_stocks WHERE captured_at=%s", (wprev,))[0]["u"]
            wb_fbo["d_units"] = round(wb_fbo["units"] - (pv or 0), 1)
    # дефицит наших складов (что кончается): наш сток / скорость продаж
    deficit = db.query("""SELECT name, supplier, sum(stock)::float stock, max(in_transit)::float in_transit,
        max(sold_30d)::float sold_30d,
        round((sum(stock)/nullif(max(sold_30d)/30.0,0))::numeric,1)::float days_left
        FROM supplier_stock WHERE captured_at=%s AND store=ANY(%s) AND stock>0 AND sold_30d>0
        GROUP BY ms_id, name, supplier
        HAVING sum(stock)/nullif(max(sold_30d)/30.0,0) <= 25
        ORDER BY days_left ASC LIMIT 80""", (cap, OUR_STORES))
    return {"captured": cap, "prev": prev, "our": our, "ozon_fbo": ozon_fbo, "wb_fbo": wb_fbo,
            "deficit": deficit, "history_our": _wh_history(OUR_STORES),
            "history_ozon": _wh_history_ozon()}


# --- Яндекс.Маркет (raw_yandex_order; выручка = payment+subsidy без отменённых) ---
YA_PAY = "(payload->'prices'->'payment'->>'value')::numeric"
YA_SUB = "coalesce((payload->'prices'->'subsidy'->>'value')::numeric,0)"
YA_REV = f"({YA_PAY} + {YA_SUB})"
YA_OK = "payload->>'status'<>'CANCELLED'"


def _ya_dow(ds):
    d = _dt.date.fromisoformat(ds[:10])
    return (d - _dt.timedelta(days=d.weekday()))


def _ya_business(period: str = ""):
    """Маркет для агрегата главной — ЗА ВЫБРАННЫЙ МЕСЯЦ из yandex_finance_monthly (stats/orders,
    история с января). Выручка = payment+subsidy, возвраты вычтены (REFUND). Расходы МП =
    комиссия+логистика+эквайринг+буст+прочее. COGS с импутацией непокрытых штук."""
    q = db.query("""SELECT revenue::float r, subsidy::float s, orders,
            returns_orders, returns_sum::float rs, cogs::float cogs, cogs_cov_pct::float cov,
            (fee+delivery+transfer+promotion+agency+other_fee+subscription_cost+reviews_cost)::float mp
        FROM yandex_finance_monthly WHERE account='ya_acc1' AND month=%s""", (period or None,))
    if not q:
        return None
    r = q[0]
    rev = round((r["r"] or 0) + (r["s"] or 0))
    cogs = round(r["cogs"] or 0)
    mp_cost = round(r["mp"] or 0)
    net = rev - cogs - mp_cost                                    # настоящая чистая
    return {"revenue": rev, "orders": r["orders"], "cogs": cogs, "mp_cost": mp_cost, "net": net,
            "returns": r["returns_orders"], "returns_sum": round(r["rs"] or 0),
            "cogs_cov_pct": r["cov"],
            "margin_pct": round(net / rev * 100, 1) if rev else None, "gross": False}


@app.get("/download/ms-zero-cogs.csv")
def download_zero_cogs():
    """Список отгрузок МС с нулевой себестоимостью — для ручной правки в МойСклад."""
    return FileResponse(BASE_DIR / "docs" / "ms_zero_cogs_demands.csv",
                        media_type="text/csv", filename="ms_zero_cogs_demands.csv")


@app.get("/download/wb-price-econ.csv")
def download_price_econ():
    """Рост цен май→сейчас по SKU + юнит-экономика (чистая/шт и чистая/нед до и после)."""
    return FileResponse(BASE_DIR / "docs" / "wb_price_change_unit_econ.csv",
                        media_type="text/csv", filename="wb_price_change_unit_econ.csv")


@app.get("/market", response_class=HTMLResponse)
def market_page():
    return (STATIC / "yandex.html").read_text(encoding="utf-8")


@app.get("/api/yandex/summary")
def yandex_summary():
    """Сводка Маркета за собранное окно (~30 дней). Выручка = payment+subsidy без отменённых."""
    if not db.query("SELECT 1 FROM raw_yandex_order WHERE account='ya_acc1' LIMIT 1"):
        return {"ok": False, "revenue": 0, "orders": 0}
    t = db.query(f"""SELECT
        sum(case when {YA_OK} then {YA_REV} else 0 end)::float revenue,
        sum(case when {YA_OK} then {YA_PAY} else 0 end)::float payment,
        sum(case when {YA_OK} then {YA_SUB} else 0 end)::float subsidy,
        count(*) filter(where {YA_OK}) orders,
        count(*) filter(where payload->>'status'='CANCELLED') cancelled,
        sum(case when payload->>'status'='CANCELLED' then {YA_REV} else 0 end)::float cancelled_sum,
        min((payload->>'creationDate')) mn, max((payload->>'creationDate')) mx
        FROM raw_yandex_order WHERE account='ya_acc1'""")[0]
    by_store = db.query(f"""SELECT payload->>'campaignId' store,
        count(*) filter(where {YA_OK}) orders, sum(case when {YA_OK} then {YA_REV} else 0 end)::float revenue
        FROM raw_yandex_order WHERE account='ya_acc1' GROUP BY 1 ORDER BY 3 DESC NULLS LAST""")
    by_status = db.query(f"""SELECT payload->>'status' status, count(*) n,
        sum({YA_REV})::float revenue FROM raw_yandex_order WHERE account='ya_acc1'
        GROUP BY 1 ORDER BY 2 DESC""")
    # COGS: offerId→products.external_code→cost_seb. Покрытие = выручка позиций с известной себест.
    c = db.query(f"""SELECT
        sum((it->'count')::numeric * coalesce(yc.cost_per_unit,0))::float cogs,
        sum(case when yc.cost_per_unit>0 then (it->'prices'->'payment'->>'value')::numeric
              + coalesce((it->'prices'->'subsidy'->>'value')::numeric,0) else 0 end)::float rev_matched
        FROM raw_yandex_order o, jsonb_array_elements(o.payload->'items') it
        LEFT JOIN yandex_cost yc ON yc.offer = it->>'offerId'
        WHERE o.account='ya_acc1' AND o.payload->>'status'<>'CANCELLED'""")[0]
    # Итоги — из yandex_finance_monthly (вся история, единое окно: выручка/расходы/COGS за одни месяцы)
    f = db.query("""SELECT sum(revenue+subsidy)::float rev, sum(revenue)::float pay,
        sum(subsidy)::float sub, sum(orders) orders, sum(cogs)::float cogs,
        sum(fee+delivery+transfer+promotion+agency+other_fee+subscription_cost+reviews_cost)::float mp,
        round(avg(cogs_cov_pct)) cov, min(month)::text mn, max(month)::text mx
        FROM yandex_finance_monthly WHERE account='ya_acc1'""")[0]
    rev = f["rev"] or (t["revenue"] or 0)
    cogs = round(f["cogs"] or 0)
    mp_cost = round(f["mp"] or 0)                      # комиссия+логистика+эквайринг+буст+прочее
    gross = round(rev - cogs)                          # валовая = выручка − COGS
    net = round(rev - cogs - mp_cost)                  # НАСТОЯЩАЯ чистая
    return {"ok": True, "revenue": round(rev), "payment": round(f["pay"] or 0),
            "subsidy": round(f["sub"] or 0), "orders": f["orders"] or t["orders"],
            "cancelled": t["cancelled"], "cancelled_sum": round(t["cancelled_sum"] or 0),
            "cogs": cogs, "cogs_coverage": f["cov"], "gross": gross,
            "gross_margin_pct": round(gross / rev * 100, 1) if rev else None,
            "mp_cost": mp_cost, "net": net,
            "net_margin_pct": round(net / rev * 100, 1) if rev else None,
            "since": (f["mn"] or "")[:10], "until": (f["mx"] or "")[:10],
            "stores": by_store, "by_status": by_status}


@app.get("/api/yandex/weekly")
def yandex_weekly():
    """Маркет по неделям (Пн-старт): заказы + выручка (payment+subsidy, без отмен)."""
    rows = db.query(f"""SELECT (payload->>'creationDate') cd, payload->>'status' st, {YA_REV} rev
        FROM raw_yandex_order WHERE account='ya_acc1'""")
    agg = {}
    for r in rows:
        if not r["cd"] or r["st"] == "CANCELLED":
            continue
        ws = _ya_dow(r["cd"])
        a = agg.setdefault(ws, {"orders": 0, "revenue": 0.0})
        a["orders"] += 1
        a["revenue"] += float(r["rev"] or 0)
    out = [{"label": f"{w.strftime('%d.%m')}–{(w + _dt.timedelta(days=6)).strftime('%d.%m')}",
            "orders": a["orders"], "revenue": round(a["revenue"])}
           for w, a in sorted(agg.items())]
    return {"rows": out}


@app.get("/api/yandex/monthly")
def yandex_monthly():
    """Маркет помесячно (yandex_finance_monthly из stats/orders): выручка (возвраты вычтены),
    субсидия, расходы МП по типам, возвраты, COGS (импутация) и чистая."""
    rows = db.query("""SELECT to_char(month,'YYYY-MM') ym, revenue::float rev, subsidy::float subsidy,
        orders, returns_orders, returns_sum::float rs,
        coalesce(unredeemed_orders,0) unr, coalesce(unredeemed_cost,0)::float unr_cost,
        fee::float fee, delivery::float delivery, transfer::float transfer,
        promotion::float promotion, (agency+other_fee+subscription_cost+reviews_cost)::float other,
        cogs::float cogs, cogs_cov_pct::float cov
        FROM yandex_finance_monthly WHERE account='ya_acc1' ORDER BY month""")
    out = []
    for r in rows:
        rev = round((r["rev"] or 0) + (r["subsidy"] or 0))
        mp = round((r["fee"] or 0) + (r["delivery"] or 0) + (r["transfer"] or 0)
                   + (r["promotion"] or 0) + (r["other"] or 0))
        net = rev - round(r["cogs"] or 0) - mp
        out.append({"month": r["ym"], "revenue": round(r["rev"] or 0),
                    "subsidy": round(r["subsidy"] or 0), "orders": r["orders"],
                    "returns_orders": r["returns_orders"], "returns_sum": round(r["rs"] or 0),
                    "unredeemed_orders": r["unr"], "unredeemed_cost": round(r["unr_cost"]),
                    "fee": round(r["fee"] or 0), "delivery": round(r["delivery"] or 0),
                    "transfer": round(r["transfer"] or 0), "promotion": round(r["promotion"] or 0),
                    "other": round(r["other"] or 0), "mp_cost": mp,
                    "cogs": round(r["cogs"] or 0), "cogs_cov_pct": r["cov"],
                    "net": net, "margin_pct": round(net / rev * 100, 1) if rev else None})
    return {"rows": out}


@app.get("/api/yandex/sku")
def yandex_sku(limit: int = 60):
    """Топ товаров Маркета по выручке + COGS/маржа (offerId→products.external_code→cost_seb).
    Маржа валовая: (выручка − COGS)/выручка; комиссия/логистика Маркета — следующим шагом."""
    rows = db.query(f"""
        SELECT it->>'offerId' offer, max(it->>'offerName') name, max(yc.cost_per_unit)::float cost,
            sum((it->'count')::numeric)::float qty,
            sum(((it->'prices'->'payment'->>'value')::numeric
               + coalesce((it->'prices'->'subsidy'->>'value')::numeric,0)))::float revenue
        FROM raw_yandex_order, jsonb_array_elements(payload->'items') it
        LEFT JOIN yandex_cost yc ON yc.offer = it->>'offerId'
        WHERE account='ya_acc1' AND {YA_OK}
        GROUP BY 1 ORDER BY 5 DESC NULLS LAST LIMIT %s""", (limit,))
    for r in rows:
        if r["cost"] and r["qty"]:
            r["cogs"] = round(r["cost"] * r["qty"])
            r["gross"] = round((r["revenue"] or 0) - r["cogs"])
            r["margin_pct"] = round(r["gross"] / r["revenue"] * 100, 1) if r["revenue"] else None
        else:
            r["cogs"] = r["gross"] = r["margin_pct"] = None
    return {"rows": rows}


# --- Продажи с собственных сайтов (МойСклад, поле «Проект») ---
MS_API = "https://api.moysklad.ru/api/remap/1.2"
SITE_PROJECTS = {"Digitalsquare": "919ca5eb-61e5-11eb-0a80-07d50002b5bb",
                 "Kartridge.org": "04d8b84e-8785-11e6-7a69-971100007175",
                 "Алиэкспресс": "c0268424-713e-11ed-0a80-07080005d83b"}
_MS_STATE_TYPES = {}


def _ms_get(path, **params):
    tok = os.getenv("MOYSKLAD_TOKEN")
    r = requests.get(f"{MS_API}/{path}", headers={"Authorization": f"Bearer {tok}",
                     "Accept-Encoding": "gzip"}, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def _ms_state_types():
    global _MS_STATE_TYPES
    if not _MS_STATE_TYPES:
        meta = _ms_get("entity/customerorder/metadata")
        _MS_STATE_TYPES = {s["name"]: s.get("stateType") for s in meta.get("states", [])}
    return _MS_STATE_TYPES


def _classify_state(name, stype):
    n = name or ""
    if n.startswith("Отмен") or stype == "Unsuccessful":
        return "cancelled"
    if n in ("Доставлен", "Оплачен") or stype == "Successful":
        return "fulfilled"
    return "pending"


@app.get("/api/sites")
def sites(period: str = ""):
    """Продажи с сайтов из МС по полю «Проект» (Digitalsquare/Kartridge.org/Алиэкспресс):
    выполнено (доход), отменено (+причина), в работе. По статусам заказов МС."""
    period = period or db.query("SELECT max(period_from)::text p FROM margin_by_sku")[0]["p"]
    df, dt = _oz_month(period)
    stypes = _ms_state_types()
    projects = []
    for name, pid in SITE_PROJECTS.items():
        href = f"{MS_API}/entity/project/{pid}"
        by, off = {}, 0
        while True:
            j = _ms_get("entity/customerorder",
                        filter=f"project={href};moment>={df} 00:00:00;moment<={dt} 23:59:59",
                        expand="state", limit=100, offset=off)
            rows = j.get("rows", [])
            for o in rows:
                st = (o.get("state") or {}).get("name", "(без статуса)")
                rec = by.setdefault(st, [0, 0.0])
                rec[0] += 1
                rec[1] += (o.get("sum", 0) or 0) / 100
            off += len(rows)
            if off >= j.get("meta", {}).get("size", 0) or not rows:
                break
        cats = {"fulfilled": [0, 0.0], "cancelled": [0, 0.0], "pending": [0, 0.0]}
        reasons = []
        for st, (n, s) in by.items():
            c = _classify_state(st, stypes.get(st))
            cats[c][0] += n
            cats[c][1] += s
            if c == "cancelled":
                reasons.append({"reason": st, "count": n, "sum": round(s, 2)})
        reasons.sort(key=lambda x: -x["sum"])
        projects.append({
            "project": name, "orders": sum(v[0] for v in by.values()),
            "fulfilled": round(cats["fulfilled"][1], 2), "fulfilled_n": cats["fulfilled"][0],
            "cancelled": round(cats["cancelled"][1], 2), "cancelled_n": cats["cancelled"][0],
            "pending": round(cats["pending"][1], 2), "pending_n": cats["pending"][0],
            "reasons": reasons})
    return {"period": period, "projects": projects,
            "income": round(sum(p["fulfilled"] for p in projects), 2),
            "cancelled": round(sum(p["cancelled"] for p in projects), 2),
            "cancelled_n": sum(p["cancelled_n"] for p in projects),
            "pending": round(sum(p["pending"] for p in projects), 2)}


@app.get("/api/ozon/summary")
def ozon_summary(account: str = "", period: str = ""):
    """Большие цифры Ozon + разбивка по аккаунтам + дельта к прошлому месяцу."""
    period = period or _oz_last_period()
    if not period:
        return {"period": None, "revenue": 0, "net": 0, "accounts": []}
    cur = _oz_summary(account, period)
    cur["period"] = period
    accts = [r["account"] for r in db.query("SELECT DISTINCT account FROM raw_ozon_transaction ORDER BY 1")]
    cur["accounts"] = []
    for a in accts:
        s = _oz_summary(a, period)
        cur["accounts"].append({"account": a, "name": OZ_NAMES.get(a, a),
            "revenue": s["revenue"], "net": s["net"], "margin_pct": s["margin_pct"]})
    prev_p = _prev_period("ozon", account, period)
    if prev_p:
        pr = _oz_summary(account, prev_p)
        cur["prev_period"] = prev_p
        cur["delta"] = {}
        for k in ("revenue", "net", "cogs", "margin_pct"):
            c, o = cur.get(k), pr.get(k)
            cur["delta"][k] = None if c is None or o is None else {
                "abs": round(c - o, 2), "pct": (None if not o else round((c - o) / abs(o) * 100, 1))}
    else:
        cur["prev_period"] = None; cur["delta"] = None
    return cur


@app.get("/api/ozon/expenses")
def ozon_expenses_api(account: str = "", period: str = ""):
    """Статьи расходов (₽ + % выручки + управляемость) + доли FBO/FBS."""
    period = period or _oz_last_period()
    if not period:
        return {"period": None, "items": [], "schemas": []}
    s = _oz_summary(account, period)
    rev = s["revenue"] or 1
    items = []
    for c in CATEGORIES:
        if c == "revenue":
            continue
        v = s["cats"][c]
        if abs(v) < 1:
            continue
        items.append({"key": c, "name": OZ_RU[c], "amount": round(v, 2),
                      "pct": round(abs(v) / rev * 100, 1), "control": OZ_CTRL.get(c, "yellow")})
    items.sort(key=lambda x: x["amount"])   # крупнейшие расходы (отрицательные) — вверх
    total_sch = sum(s["schema_rev"].values()) or 1
    schemas = [{"schema": k, "revenue": v, "pct": round(v / total_sch * 100, 1)}
               for k, v in sorted(s["schema_rev"].items(), key=lambda x: -x[1]) if abs(v) >= 1]
    # Сплит строки «Продажи» из отчёта о реализации Ozon (/v2/finance/realization) — точно как в ЛК:
    # Выручка + Баллы за скидки + Программы партнёров. None, если отчёт за месяц ещё не собран.
    split = None
    if re.match(r"^\d{4}-\d{2}", period or ""):
        split = _oz_realization_split(account or None, int(period[:4]), int(period[5:7]))
    return {"period": period, "revenue": s["revenue"], "overhead": s["overhead"],
            "items": items, "schemas": schemas, "sales_split": split}


@app.get("/api/ozon/mp-current")
def ozon_mp_current():
    """Живой ТЕКУЩИЙ месяц (вне статического снапшота) + прогноз на конец месяца — для
    вкладки «Отчёты МП». Готовые к вставке ячейки {jul,fc}: {txt, cls} по строкам Баланса
    + операционным показателям. См. reports/ozon_mp_report."""
    return _ozmp.current_report()


@app.get("/api/wb/mp-current")
def wb_mp_current():
    """Живой ТЕКУЩИЙ месяц WB (вне статического снапшота) + прогноз на конец месяца — для
    вкладки «Отчёты МП · WB». Готовые к вставке ячейки {cur,fc}: {txt, cls} по строкам
    Финансового отчёта ВБ + операционным показателям. См. reports/wb_mp_report."""
    return _wbmp.current_report()


@app.get("/api/yandex/mp-current")
def yandex_mp_current():
    """Живой ТЕКУЩИЙ месяц Яндекс.Маркета (вне статического снапшота) + прогноз на конец месяца —
    для вкладки «Отчёты МП · Яндекс». Готовые ячейки {cur,fc}: {txt, cls} по строкам витрины ЯМ.
    Живой месяц структурно занижает маржу (order-based COGS vs лаг доставки) — прогноз проецирует
    полный месяц по юнит-экономике закрытых месяцев. См. reports/yandex_mp_report."""
    return _yamp.current_report()


OZ_SKU_SORT = {"revenue_buyer": "m.revenue_buyer", "net_profit": "m.net_profit",
               "margin_pct": "m.margin_pct", "cogs": "m.cogs"}


def _ozon_pricing(account, df, dt):
    """{sku: {sale_price, old_price, discount_pct}} из постингов (financial_data).
    products[] и financial_data.products[] совмещаем по позиции (ORDINALITY)."""
    accts = [account] if account else ["oz_acc1", "oz_acc2"]
    rows = db.query("""
        SELECT pr.prod->>'sku' sku,
            sum(coalesce((f.fd->>'quantity')::numeric,(pr.prod->>'quantity')::numeric,0)) qty,
            sum(coalesce((f.fd->>'price')::numeric,(pr.prod->>'price')::numeric,0)
                *coalesce((f.fd->>'quantity')::numeric,(pr.prod->>'quantity')::numeric,1)) rev,
            sum(coalesce((f.fd->>'old_price')::numeric,(f.fd->>'price')::numeric,(pr.prod->>'price')::numeric,0)
                *coalesce((f.fd->>'quantity')::numeric,(pr.prod->>'quantity')::numeric,1)) oldrev
        FROM raw_ozon_posting po
          JOIN LATERAL jsonb_array_elements(po.payload->'products') WITH ORDINALITY AS pr(prod, idx) ON true
          LEFT JOIN LATERAL jsonb_array_elements(po.payload->'financial_data'->'products')
              WITH ORDINALITY AS f(fd, fidx) ON f.fidx = pr.idx
        WHERE po.account = ANY(%s) AND po.in_process_at::date BETWEEN %s AND %s
        GROUP BY 1""", (accts, df, dt))
    out = {}
    for r in rows:
        q = float(r["qty"] or 0)
        if q <= 0 or not r["sku"]:
            continue
        sp, op = float(r["rev"] or 0) / q, float(r["oldrev"] or 0) / q
        out[r["sku"]] = {"sale_price": round(sp, 2), "old_price": round(op, 2),
                         "discount_pct": round((op - sp) / op * 100, 1) if op else 0}
    return out


@app.get("/api/ozon/promos")
def ozon_promos(account: str = "", period: str = ""):
    """Акции Ozon из постингов: по каждой акции — units/выручка/скидка; и SKU, которые плохо
    отрабатывают (большая скидка при низкой/отрицательной марже)."""
    period = period or _oz_last_period()
    if not period:
        return {"actions": [], "bad": []}
    df, dt = _oz_month(period)
    accts = [account] if account else ["oz_acc1", "oz_acc2"]
    # по акциям (товар может быть в нескольких акциях — считаем под каждой)
    arows = db.query("""
        SELECT a.act act, sum(coalesce((f.fd->>'quantity')::numeric,1)) units,
            sum((f.fd->>'price')::numeric*coalesce((f.fd->>'quantity')::numeric,1)) rev,
            sum(((f.fd->>'old_price')::numeric-(f.fd->>'price')::numeric)
                *coalesce((f.fd->>'quantity')::numeric,1)) disc,
            count(DISTINCT f.fd->>'product_id') skus
        FROM raw_ozon_posting po
          JOIN LATERAL jsonb_array_elements(po.payload->'financial_data'->'products') AS f(fd) ON true
          JOIN LATERAL jsonb_array_elements_text(f.fd->'actions') AS a(act) ON true
        WHERE po.account=ANY(%s) AND po.in_process_at::date BETWEEN %s AND %s
        GROUP BY a.act ORDER BY rev DESC""", (accts, df, dt))
    actions = []
    for r in arows:
        rev, disc = float(r["rev"] or 0), float(r["disc"] or 0)
        actions.append({"action": r["act"], "units": round(float(r["units"] or 0)),
                        "revenue": round(rev, 2), "discount": round(disc, 2), "skus": r["skus"],
                        "discount_pct": round(disc / (rev + disc) * 100, 1) if (rev + disc) else 0})
    # SKU, плохо отрабатывающие в акциях: скидка из постингов + маржа из витрины
    pricing = _ozon_pricing(account, df, dt)
    marg = {r["article"]: r for r in db.query(
        """SELECT article, margin_pct::float, net_profit::float, revenue_buyer::float
           FROM margin_by_sku WHERE platform='ozon' AND period_from=%s
             AND account=ANY(%s) AND revenue_buyer>0""", (period, accts))}
    names = _l3_names("ozon", account, list(pricing.keys())) if pricing else {}
    bad = []
    for sku, pr in pricing.items():
        if pr["discount_pct"] < 10:
            continue
        mr = marg.get(sku) or {}
        bad.append({"sku": sku, "title": names.get(sku), "discount_pct": pr["discount_pct"],
                    "sale_price": pr["sale_price"], "old_price": pr["old_price"],
                    "margin_pct": mr.get("margin_pct"), "revenue": mr.get("revenue_buyer")})
    bad.sort(key=lambda x: (x["margin_pct"] if x["margin_pct"] is not None else 0, -x["discount_pct"]))
    return {"period": period, "actions": actions, "bad": bad[:30],
            "total_discount": round(sum(a["discount"] for a in actions), 2)}


@app.get("/api/ozon/ratings")
def ozon_ratings(account: str = "", threshold: float = 4.3, min_reviews: int = 3, limit: int = 150):
    """SKU Ozon с низким звёздным рейтингом (<threshold, ≥min_reviews отзывов — отсечь шум).
    Архивные карточки (ozon_product.is_archived) исключаем; имя берём из каталога Ozon (а не из
    отгрузок), чтобы живые карточки без продаж тоже были с названием. Сорт по числу отзывов."""
    accts = [account] if account else ["oz_acc1", "oz_acc2"]
    rows = db.query("""SELECT g.sku, g.avg_rating::float, g.reviews_count, g.r1, g.r2, g.r3, g.r4, g.r5,
            p.name title
        FROM ozon_rating g
        LEFT JOIN ozon_product p ON p.account=g.account AND p.sku=g.sku
        WHERE g.account=ANY(%s) AND g.avg_rating < %s AND g.reviews_count >= %s
          AND COALESCE(p.is_archived, false) = false
        ORDER BY g.reviews_count DESC, g.avg_rating ASC LIMIT %s""",
        (accts, threshold, min_reviews, limit))
    # запасной источник имени — из отгрузок, для sku вне каталога
    miss = [r["sku"] for r in rows if not r.get("title")]
    if miss:
        nm = {x["a"]: x["title"] for x in db.query(
            """SELECT DISTINCT ON (it->>'sku') it->>'sku' a, it->>'name' title
               FROM raw_ozon_transaction, jsonb_array_elements(payload->'items') it
               WHERE it->>'sku'=ANY(%s) ORDER BY it->>'sku'""", (miss,))}
        for r in rows:
            if not r.get("title"):
                r["title"] = nm.get(r["sku"])
    return {"rows": rows, "count": len(rows), "threshold": threshold}


@app.get("/api/ozon/ads")
def ozon_ads_api(account: str = "oz_acc1", period: str = ""):
    """Реклама Ozon Performance по кампаниям: расход, выручка с рекламы, ДРР, тип оплаты.
    ДРР кампании = расход/выручка-в-продвижении; общий ДРР = расход/вся выручка Ozon."""
    period = period or db.query(
        "SELECT max(period)::text p FROM ozon_ads WHERE account=%s", (account,))[0]["p"]
    if not period:
        return {"period": None, "campaigns": [], "totals": {}}
    rows = db.query("""SELECT campaign_id, title, pay_model, adv_type, state,
        spend::float, views, clicks, ad_revenue::float, sold::float
        FROM ozon_ads WHERE account=%s AND period=%s
        ORDER BY spend DESC NULLS LAST""", (account, period))
    for r in rows:
        r["drr"] = round(r["spend"] / r["ad_revenue"] * 100, 1) if r["ad_revenue"] else None
    spend = round(sum(r["spend"] or 0 for r in rows))
    ad_rev = round(sum(r["ad_revenue"] or 0 for r in rows))
    by_model = {}
    for r in rows:
        m = by_model.setdefault(r["pay_model"], {"spend": 0.0, "ad_revenue": 0.0, "n": 0})
        m["spend"] += r["spend"] or 0
        m["ad_revenue"] += r["ad_revenue"] or 0
        m["n"] += 1
    for m in by_model.values():
        m["spend"] = round(m["spend"])
        m["ad_revenue"] = round(m["ad_revenue"])
        m["drr"] = round(m["spend"] / m["ad_revenue"] * 100, 1) if m["ad_revenue"] else None
    oz_rev = (_oz_summary(account, period) or {}).get("revenue") or 0
    totals = {"spend": spend, "ad_revenue": ad_rev,
              "drr": round(spend / ad_rev * 100, 1) if ad_rev else None,
              "drr_of_total": round(spend / oz_rev * 100, 1) if oz_rev else None,
              "ozon_revenue": round(oz_rev), "by_model": by_model, "campaigns": len(rows)}
    return {"period": period, "campaigns": rows, "totals": totals}


@app.get("/api/ozon/bids")
def ozon_bids_api(account: str = "oz_acc1"):
    """Ставки Ozon по SKU (последний снимок) + дельта к предыдущему дню. Для вкладки «Ставки»."""
    cap = db.query("SELECT max(captured_at)::text c FROM ozon_bids WHERE account=%s", (account,))[0]["c"]
    if not cap:
        return {"captured": None, "rows": [], "campaigns": []}
    prev = db.query("SELECT max(captured_at)::text c FROM ozon_bids WHERE account=%s AND captured_at<%s",
                    (account, cap))[0]["c"]
    rows = db.query("""SELECT campaign_id, campaign_title, adv_type, sku, title,
        bid::float, target_cir::float FROM ozon_bids
        WHERE account=%s AND captured_at=%s ORDER BY campaign_title, title""", (account, cap))
    if prev:
        pm = {(r["campaign_id"], r["sku"]): r["bid"] for r in db.query(
            "SELECT campaign_id, sku, bid::float FROM ozon_bids WHERE account=%s AND captured_at=%s",
            (account, prev))}
        for r in rows:
            pb = pm.get((r["campaign_id"], r["sku"]))
            r["bid_delta"] = round(r["bid"] - pb, 2) if pb is not None else None
    camps = sorted({(r["campaign_id"], r["campaign_title"]) for r in rows})
    return {"captured": cap, "account": account, "rows": rows,
            "campaigns": [{"id": c[0], "title": c[1]} for c in camps]}


class BidSet(BaseModel):
    account: str = "oz_acc1"
    campaign_id: str
    sku: str
    bid: float   # рубли


@app.post("/api/ozon/bids/set")
def ozon_bid_set(payload: BidSet):
    """Записать ставку SKU в живую кампанию Ozon (Performance). bid в рублях → микрорубли."""
    from collectors.ozon_ads import _token, PERF, has_creds
    if not has_creds(payload.account):
        return {"ok": False, "error": "нет Performance-кредов у аккаунта"}
    if not (0 < payload.bid <= 100000):
        return {"ok": False, "error": "ставка вне допустимого диапазона"}
    micro = int(round(payload.bid * 1_000_000))
    H = {"Authorization": f"Bearer {_token(payload.account)}", "Content-Type": "application/json"}
    r = requests.post(f"{PERF}/api/client/campaign/{payload.campaign_id}/products", headers=H,
                      json={"bids": [{"sku": payload.sku, "bid": micro}]}, timeout=40)
    if r.status_code != 200:
        return {"ok": False, "error": f"Ozon HTTP {r.status_code}: {r.text[:160]}"}
    db.execute("""UPDATE ozon_bids SET bid=%s WHERE account=%s AND campaign_id=%s AND sku=%s
        AND captured_at=current_date""", (payload.bid, payload.account, payload.campaign_id, payload.sku))
    return {"ok": True, "bid": payload.bid}


# --- Ozon: задача «вывоз со склада FBO» — список кандидатов + отметка «заявка оформлена» ---
# Данные из ozon_removal_candidates (сборка недельная, вторник — reports/ozon_removal_candidates).
# Отметки галочкой пишутся в ozon_removal_submitted — тот же реестр, что у Telegram-бота
# (/oformleno). Так дашборд и бот держат единое состояние «что уже отправлено на вывоз».
@app.get("/api/ozon/removal")
def ozon_removal():
    from reports.ozon_removal_candidates import _short_name, RULE_TXT, ACC_LABEL
    rd = db.query("SELECT max(run_date)::text d FROM ozon_removal_candidates")[0]["d"]
    if not rd:
        return {"run_date": None, "accounts": [], "pending": 0, "done": 0}
    rows = db.query("""SELECT c.account, c.warehouse, c.offer_id, c.qty, c.name, c.color,
            c.days_without_sales, c.rules, (s.offer_id IS NOT NULL) AS done
        FROM ozon_removal_candidates c
        LEFT JOIN ozon_removal_submitted s
          ON s.account=c.account AND s.offer_id=c.offer_id AND s.warehouse=c.warehouse
        WHERE c.run_date=%s AND c.rules<>'W'
        ORDER BY c.account, c.warehouse, c.offer_id""", (rd,))
    accs = {}
    for r in rows:
        a = accs.setdefault(r["account"], {"account": r["account"],
                                           "name": ACC_LABEL.get(r["account"], r["account"]),
                                           "warehouses": {}})
        wh = a["warehouses"].setdefault(r["warehouse"], [])
        wh.append({"offer_id": r["offer_id"], "qty": r["qty"], "name": _short_name(r["name"]),
                   "color": r["color"], "dws": r["days_without_sales"], "done": r["done"],
                   "reasons": [RULE_TXT.get(t, t) for t in r["rules"].split(",")]})
    out = [{"account": a["account"], "name": a["name"],
            "warehouses": [{"warehouse": w, "items": items} for w, items in a["warehouses"].items()]}
           for a in accs.values()]
    pending = sum(1 for r in rows if not r["done"])
    done = sum(1 for r in rows if r["done"])
    return {"run_date": rd, "accounts": out, "pending": pending, "done": done,
            "qty_pending": sum(r["qty"] for r in rows if not r["done"])}


class RemovalMark(BaseModel):
    account: str
    offer_id: str
    warehouse: str
    done: bool


@app.post("/api/ozon/removal/mark")
def ozon_removal_mark(payload: RemovalMark):
    """Отметить/снять «заявка на вывоз оформлена» по позиции (account, offer_id, warehouse)."""
    if payload.done:
        r = db.query("""SELECT sku, qty, name FROM ozon_removal_candidates
            WHERE account=%s AND offer_id=%s AND warehouse=%s
            ORDER BY run_date DESC LIMIT 1""", (payload.account, payload.offer_id, payload.warehouse))
        if not r:
            return {"ok": False, "error": "позиция не найдена в текущем списке"}
        import datetime as _dt
        db.upsert("ozon_removal_submitted", [{
            "account": payload.account, "offer_id": payload.offer_id, "warehouse": payload.warehouse,
            "sku": r[0]["sku"], "qty": r[0]["qty"], "name": r[0]["name"],
            "submitted_at": _dt.date.today()}],
            conflict_cols=["account", "offer_id", "warehouse"],
            update_cols=["sku", "qty", "name", "submitted_at"])
    else:
        db.execute("DELETE FROM ozon_removal_submitted WHERE account=%s AND offer_id=%s AND warehouse=%s",
                   (payload.account, payload.offer_id, payload.warehouse))
    return {"ok": True, "done": payload.done}


WB_ADV_TYPE = {4: "Каталог", 5: "Карточка", 6: "Поиск", 7: "Главная", 8: "Авто", 9: "Аукцион"}


@app.get("/api/wb/ads")
def wb_ads_api(account: str = "wb_acc1", period: str = ""):
    """Реклама WB «Продвижение» по кампаниям: расход, выручка, ДРР, тип."""
    period = period or db.query(
        "SELECT max(period)::text p FROM wb_ads WHERE account=%s", (account,))[0]["p"]
    if not period:
        return {"period": None, "campaigns": [], "totals": {}}
    rows = db.query("""SELECT advert_id, name, adv_type, status, spend::float, views, clicks,
        orders, revenue::float, ctr::float, cpc::float FROM wb_ads
        WHERE account=%s AND period=%s ORDER BY spend DESC NULLS LAST""", (account, period))
    for r in rows:
        r["type_name"] = WB_ADV_TYPE.get(r["adv_type"], str(r["adv_type"]))
        r["drr"] = round(r["spend"] / r["revenue"] * 100, 1) if r["revenue"] else None
    spend = round(sum(r["spend"] or 0 for r in rows))
    rev = round(sum(r["revenue"] or 0 for r in rows))
    totals = {"spend": spend, "revenue": rev,
              "orders": sum(r["orders"] or 0 for r in rows),
              "clicks": sum(r["clicks"] or 0 for r in rows),
              "drr": round(spend / rev * 100, 1) if rev else None,
              "campaigns": len(rows)}
    return {"period": period, "campaigns": rows, "totals": totals}


@app.get("/api/wb/promo")
def wb_promo(account: str = "", period: str = "", min_disc: float = 15, limit: int = 120):
    """Акции/скидки/СПП WB: где большая скидка или СПП съедают маржу — кандидаты поднять цену
    / вывести из акций. Цены/скидки/СПП из raw_wb_report (_wb_pricing), маржа из margin_by_sku."""
    period = period or db.query(
        "SELECT max(period_from)::text p FROM margin_by_sku WHERE platform='wb'")[0]["p"]
    if not period:
        return {"period": None, "rows": [], "totals": {}}
    accts = [account] if account else ["wb_acc1", "wb_acc2"]
    rows = db.query("""
        SELECT m.article nm_id, c.title, c.vendor_code, m.qty::float,
            m.revenue_buyer::float, m.net_profit::float, s.our_price::float,
            CASE WHEN s.our_price>0 AND m.qty>0
                 THEN round(m.net_profit/(s.our_price*m.qty)*100,1) END::float margin_own
        FROM margin_by_sku m
        LEFT JOIN wb_cards c ON c.account=m.account AND c.nm_id::text=m.article
        LEFT JOIN sales s ON s.platform=m.platform AND s.account=m.account
             AND s.period_from=m.period_from AND s.article=m.article
        WHERE m.platform='wb' AND m.account=ANY(%s) AND m.period_from=%s
          AND m.article<>'0' AND m.qty>0 AND m.revenue_buyer>0""", (accts, period))
    pr = _wb_pricing(account, period)
    for r in rows:
        x = pr.get(r["nm_id"]) or {}
        r["price_before"] = x.get("price_before")
        r["price_after"] = x.get("price_after")
        r["discount_pct"] = x.get("discount_pct", 0)
        r["spp_pct"] = x.get("spp_pct", 0)
    rev = sum(r["revenue_buyer"] or 0 for r in rows)
    wavg = lambda key: (round(sum((r[key] or 0) * (r["revenue_buyer"] or 0) for r in rows) / rev, 1)
                        if rev else 0)
    for r in rows:
        r["spp_rub"] = round((r["revenue_buyer"] or 0) * (r["spp_pct"] or 0) / 100)
    spp_rub = sum(r["spp_rub"] for r in rows)
    # на ВБ скидка продавца ≈0, теряем на СПП → проблемные = низкая маржа, сорт по ₽ СПП (где больнее).
    # (если у позиции есть и ручная скидка ≥min_disc — тоже сюда)
    bad = sorted([r for r in rows if r["margin_own"] is not None and r["margin_own"] < 10
                  and ((r["spp_pct"] or 0) >= 20 or (r["discount_pct"] or 0) >= min_disc)],
                 key=lambda r: -r["spp_rub"])
    totals = {"revenue": round(rev), "avg_discount": wavg("discount_pct"),
              "avg_spp": wavg("spp_pct"), "spp_rub": round(spp_rub),
              "bad_count": len(bad), "bad_revenue": round(sum(r["revenue_buyer"] or 0 for r in bad)),
              "skus": len(rows)}
    return {"period": period, "rows": bad[:limit], "totals": totals}


WB_FUNNEL_SORT = {
    "open": "open_count", "cart": "cart_count", "orders": "order_count",
    "order_sum": "order_sum", "buyout_pct": "buyout_pct",
    "cart_to_order": "cart_to_order_pct", "rating": "feedback_rating",
    "traffic_delta": "(open_count - past_open_count)"}


def _wb_funnel_period():
    r = db.query("SELECT max(period) p FROM wb_funnel")
    return r[0]["p"].isoformat() if r and r[0]["p"] else None


@app.get("/api/wb/funnel")
def wb_funnel(account: str = "", period: str = "", sort: str = "order_sum",
              order: str = "desc", problem: bool = False, low_rating: bool = False,
              limit: int = 300):
    """Воронка WB (трафик/клики/конверсии/рейтинг карточки) из wb_funnel.
    problem    = трафик есть, а продаж нет (≥30 переходов и 0 заказов) — сливаем показы.
    low_rating = рейтинг карточки по отзывам <4.3.
    Возвращает строки + агрегат с динамикой трафика (open vs прошлый период)."""
    period = period or _wb_funnel_period()
    if not period:
        return {"rows": [], "count": 0, "agg": {}}
    accts = [account] if account else ["wb_acc1", "wb_acc2"]
    conds, p = ["account=ANY(%s)", "period=%s"], [accts, period]
    if problem:
        conds.append("open_count >= 30 AND order_count = 0")
    if low_rating:
        conds.append("feedback_rating > 0 AND feedback_rating < 4.3")
    sort_sql = WB_FUNNEL_SORT.get(sort, "order_sum")
    order = "DESC" if order.lower() == "desc" else "ASC"
    where = " AND ".join(conds)
    rows = db.query(f"""
        SELECT nm_id, account, title, vendor_code, brand, subject_name,
            product_rating::float, feedback_rating::float,
            open_count, cart_count, order_count, order_sum::float,
            buyout_count, buyout_sum::float,
            add_to_cart_pct::float, cart_to_order_pct::float, buyout_pct::float,
            share_order_pct::float, stock_wb, stock_mp,
            past_open_count, past_order_sum::float,
            (open_count - past_open_count) AS open_delta
        FROM wb_funnel WHERE {where}
        ORDER BY {sort_sql} {order} NULLS LAST LIMIT %s""", p + [limit])
    agg = db.query(f"""
        SELECT count(*) n,
            COALESCE(sum(open_count),0) open_cur, COALESCE(sum(past_open_count),0) open_past,
            COALESCE(sum(cart_count),0) cart, COALESCE(sum(order_count),0) orders,
            COALESCE(sum(order_sum),0)::float order_sum,
            COALESCE(sum(buyout_count),0) buyouts,
            COALESCE(sum(case when feedback_rating>0 and feedback_rating<4.3 then 1 else 0 end),0) low_rated
        FROM wb_funnel WHERE account=ANY(%s) AND period=%s""", (accts, period))[0]
    oc, op = agg["open_cur"], agg["open_past"]
    agg["open_delta_pct"] = round((oc - op) / op * 100, 1) if op else None
    agg["cart_conv"] = round(agg["cart"] / oc * 100, 1) if oc else 0
    agg["order_conv"] = round(agg["orders"] / agg["cart"] * 100, 1) if agg["cart"] else 0
    agg["buyout_conv"] = round(agg["buyouts"] / agg["orders"] * 100, 1) if agg["orders"] else 0
    return {"rows": rows, "count": len(rows), "agg": agg, "period": period}


@app.get("/api/ozon/sku")
def ozon_sku(account: str = "", period: str = "", problem: bool = False,
             sort: str = "revenue_buyer", order: str = "desc", q: str = "", limit: int = 300):
    """SKU-уровень Ozon из margin_by_sku (без wb_cards/sales). Имя товара — из items[] сырья."""
    period = period or _oz_last_period()
    if not period:
        return {"rows": [], "count": 0}
    conds, p = ["m.platform='ozon'", "m.period_from=%s", "m.article<>'0'"], [period]
    if account:
        conds.append("m.account=%s"); p.append(account)
    conds.append("m.net_profit<0 AND m.revenue_buyer>0" if problem else "m.revenue_buyer>0")
    if q:
        conds.append("m.article ILIKE %s"); p.append(f"%{q}%")
    sort_sql = OZ_SKU_SORT.get(sort, "m.revenue_buyer")
    order = "DESC" if order.lower() == "desc" else "ASC"
    rows = db.query(f"""
        SELECT m.article sku, m.account, m.revenue_buyer::float, m.cogs::float,
            m.commission::float, m.logistics::float, m.net_profit::float,
            round(m.margin_pct,1)::float margin_pct
        FROM margin_by_sku m WHERE {' AND '.join(conds)}
        ORDER BY {sort_sql} {order} NULLS LAST LIMIT %s""", p + [limit])
    skus = [r["sku"] for r in rows]
    if skus:
        nm = {x["sku"]: x["name"] for x in db.query(
            """SELECT DISTINCT ON (it->>'sku') it->>'sku' sku, it->>'name' name
               FROM raw_ozon_transaction, jsonb_array_elements(payload->'items') it
               WHERE it->>'sku' = ANY(%s) ORDER BY it->>'sku'""", (skus,))}
        for r in rows:
            r["title"] = nm.get(r["sku"])
        pricing = _ozon_pricing(account, *_oz_month(period))   # цены из постингов
        for r in rows:
            pr = pricing.get(r["sku"])
            if pr:
                r["sale_price"], r["old_price"], r["discount_pct"] = (
                    pr["sale_price"], pr["old_price"], pr["discount_pct"])
    return {"rows": rows, "count": len(rows)}


@app.get("/api/ozon/weekly")
def ozon_weekly(account: str = "", period: str = ""):
    """Недели Пн–Вс месяца: выручка/к перечислению; чистая — оценка (месячный COGS по доле выручки)."""
    period = period or _oz_last_period()
    if not period:
        return {"rows": []}
    df, dt = _oz_month(period)
    accts = [account] if account else ["oz_acc1", "oz_acc2"]
    ad = _weekly_adspend("ozon", accts, period)
    by = _dd(list)
    for op in _oz_ops(account, df, dt):
        d = _dt.date.fromisoformat((op.get("operation_date") or "")[:10])
        by[d - _dt.timedelta(days=d.weekday())].append(op)
    out = []
    for ws in sorted(by):
        cats, _, _ = _oz_aggregate(by[ws])
        rev = round(cats["revenue"], 2)
        adv = round(ad.get(ws, 0))
        out.append({"label": f"{ws.strftime('%d.%m')}–{(ws + _dt.timedelta(days=6)).strftime('%d.%m')}",
                    "revenue": rev, "to_payout": round(sum(cats.values()), 2),
                    "ad": adv, "ad_pct": round(adv / rev * 100, 1) if rev else 0})
    cogs_m = _oz_cogs(account, period)
    rev_m = sum(w["revenue"] for w in out) or 1
    for w in out:
        w["net"] = round(w["to_payout"] - cogs_m * (w["revenue"] / rev_m), 2)
    return {"rows": out}


@app.get("/api/ozon/breakdown")
def ozon_breakdown(account: str = "", period: str = ""):
    """ВСЕ доходы/расходы по «сырым» типам операций Ozon (operation_type_name).
    Σ всех строк = к перечислению (то, что Ozon реально перевёл). Ничего не агрегируем/прячем."""
    period = period or _oz_last_period()
    if not period:
        return {"income": [], "expense": []}
    df, dt = _oz_month(period)
    cond = "(payload->>'operation_date')::date BETWEEN %s AND %s"
    p = [df, dt]
    if account:
        cond += " AND account=%s"; p.append(account)
    rows = db.query(f"""
        SELECT coalesce(payload->>'operation_type_name', payload->>'operation_type') name,
            sum((payload->>'amount')::numeric)::float amt, count(*) n
        FROM raw_ozon_transaction WHERE {cond} GROUP BY 1 ORDER BY 2""", p)
    income = sorted([r for r in rows if r["amt"] and r["amt"] > 0], key=lambda x: -x["amt"])
    expense = [r for r in rows if r["amt"] and r["amt"] < 0]   # уже по возрастанию (крупнейший расход первым)
    return {"period": period,
            "income": income, "expense": expense,
            "income_total": round(sum(r["amt"] for r in income), 2),
            "expense_total": round(sum(r["amt"] for r in expense), 2),
            "to_payout": round(sum(r["amt"] for r in rows if r["amt"]), 2)}


# =========================================================================
# СИГНАЛЫ / РЕКОМЕНДАЦИИ — бизнес-уровень (главный экран). Цель-якорь:
# 5 млн чистой после ФОТ = здоровый бизнес. Считаются из агрегатов WB+Ozon,
# штата (opex), тренда объёма заказов, управляемых расходов и убыточных SKU.
# =========================================================================
HEALTHY_AFTER_OPEX = 5_000_000


def _order_volume():
    """{period_from 'YYYY-MM-01': единицы заказов} — WB (qty из margin) + Ozon (записи items в продажах)."""
    out = {}
    for r in db.query("""SELECT period_from::text p, coalesce(sum(qty),0)::float u
                         FROM margin_by_sku WHERE platform='wb' GROUP BY 1"""):
        out[r["p"]] = out.get(r["p"], 0) + (r["u"] or 0)
    for r in db.query("""SELECT to_char((payload->>'operation_date')::date,'YYYY-MM-01') p,
        coalesce(sum(jsonb_array_length(coalesce(payload->'items','[]'::jsonb)))
                 FILTER (WHERE (payload->>'accruals_for_sale')::numeric>0),0)::float u
        FROM raw_ozon_transaction GROUP BY 1"""):
        if r["p"]:
            out[r["p"]] = out.get(r["p"], 0) + (r["u"] or 0)
    return out


ACC_NAME = {"wb_acc1": "Цифровой квадрат", "wb_acc2": "Дисквэр",
            "oz_acc1": "Цифровой квадрат", "oz_acc2": "Дисквэр"}


def _wb_acct_metrics(period):
    out = {}
    for a in ("wb_acc1", "wb_acc2"):
        s = _summary_one("wb", a, period)
        rev = s.get("own_revenue") or 0                 # ВБ — по нашей цене
        out[a] = {"name": ACC_NAME[a], "revenue": rev, "net": s["net"], "cogs": s.get("cogs") or 0,
                  "margin": (s["net"] / rev * 100) if rev else None,
                  "cogs_pct": ((s.get("cogs") or 0) / rev * 100) if rev else None}
    return out


def _oz_acct_metrics(period):
    out = {}
    for a in ("oz_acc1", "oz_acc2"):
        s = _oz_summary(a, period)
        rev = s["revenue"]
        out[a] = {"name": ACC_NAME[a], "revenue": rev, "net": s["net"], "cogs": s["cogs"],
                  "margin": s["margin_pct"],
                  "cogs_pct": (s["cogs"] / rev * 100) if rev else None}
    return out


def _compare_accounts(metrics, tip, label):
    """Сравнить юрлица внутри площадки: пометить отстающее по марже и по себестоимости."""
    mv = [x for x in metrics.values() if x["revenue"] > 0 and x["margin"] is not None]
    if len(mv) >= 2:
        mv.sort(key=lambda x: x["margin"])
        lo, hi = mv[0], mv[-1]
        if hi["margin"] - lo["margin"] >= 5:
            tip("warn", f"{label}: у «{lo['name']}» маржа {lo['margin']:.1f}% против {hi['margin']:.1f}% "
                        f"у «{hi['name']}» — отстаёт",
                "Разобрать у отстающего юрлица: цены, закуп, микс. Подтянуть к лучшему — иначе не выйдем на целевой уровень.")
    cv = [x for x in metrics.values() if x["revenue"] > 0 and x["cogs_pct"] is not None]
    if len(cv) >= 2:
        cv.sort(key=lambda x: x["cogs_pct"])
        if cv[-1]["cogs_pct"] - cv[0]["cogs_pct"] >= 5:
            w = cv[-1]
            tip("warn", f"{label}: себестоимость у «{w['name']}» {w['cogs_pct']:.0f}% выручки — выше остальных",
                "Дорогой закуп/микс. Проверить закупочные цены и наценку — высокий COGS режет маржу, цель недостижима.")


def _mp_metrics(scope, period):
    if scope == "wb":
        s = _summary_one("wb", "", period)
        rev = s.get("own_revenue") or 0
        return {"revenue": rev, "margin": s.get("margin_own"),
                "cogs_pct": ((s.get("cogs") or 0) / rev * 100) if rev else None}
    s = _oz_summary("", period)
    rev = s["revenue"]
    return {"revenue": rev, "margin": s["margin_pct"],
            "cogs_pct": (s["cogs"] / rev * 100) if rev else None}


_RU_MON = ["", "янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _compare_period(scope, period, tip):
    """Сравнить площадку с прошлым месяцем — маржа и себестоимость (устойчивы к неполному месяцу)."""
    prev_p = _prev_period(scope, "", period)
    if not prev_p:
        return
    cm, pm = _mp_metrics(scope, period), _mp_metrics(scope, prev_p)
    ml = _RU_MON[int(prev_p[5:7])]
    if cm["margin"] is not None and pm["margin"] is not None:
        d = cm["margin"] - pm["margin"]
        if d <= -2:
            tip("warn", f"Маржа упала {pm['margin']:.1f}%→{cm['margin']:.1f}% к {ml} — принять меры",
                "Причины: рост COGS/закупки, рост рекламы, демпинг. (Текущий месяц может быть неполным.)")
        elif d >= 2:
            tip("good", f"Маржа выросла {pm['margin']:.1f}%→{cm['margin']:.1f}% к {ml}",
                "Положительный тренд — закрепить, что сработало.")
    if cm["cogs_pct"] is not None and pm["cogs_pct"] is not None and cm["cogs_pct"] - pm["cogs_pct"] >= 3:
        tip("warn", f"Себестоимость выросла {pm['cogs_pct']:.0f}%→{cm['cogs_pct']:.0f}% выручки к {ml}",
            "Дорогой микс/закупка. Проверить наценку на новинки — закладывать целевую маржу сразу.")


@app.get("/api/weekly_business")
def weekly_business(period: str = ""):
    """Недельная динамика бизнеса (ВБ + Ozon): недели, ПЕРЕСЕКАЮЩИЕ выбранный месяц, каждая
    склеена ЦЕЛИКОМ — граничная неделя не режется по границе месяца (хвосты соседних месяцев
    включаются). Чистая = к перечислению − логистика − COGS − накладные. Таблица главного экрана."""
    period = period or db.query("SELECT max(period_from)::text p FROM margin_by_sku")[0]["p"]
    df, dt = _oz_month(period)
    d0, d1 = _dt.date.fromisoformat(df), _dt.date.fromisoformat(dt)
    wk_start = d0 - _dt.timedelta(days=d0.weekday())                          # пн недели с 1-м числом
    wk_end = d1 - _dt.timedelta(days=d1.weekday()) + _dt.timedelta(days=6)    # вс последней недели
    wk = {}  # wkdate -> {"rev":, "net":}

    def add(k, rev, net):
        d = wk.setdefault(k, {"rev": 0.0, "net": 0.0})
        d["rev"] += rev or 0
        d["net"] += net or 0

    # WB: по rr_dt в границах склеенных недель, БЕЗ фильтра месяца загрузки (rrd_id уникален,
    # соседние окна сбора не дублируются). cpu — последняя по nm (replacement стабилен,
    # как в /api/weekly rolling); витрина margin_by_sku теперь на модели формирования.
    for r in db.query("""
        WITH cpu AS (SELECT DISTINCT ON (article) article, cogs/nullif(qty,0) u
            FROM margin_by_sku WHERE platform='wb' AND qty>0 AND cogs>0
            ORDER BY article, period_from DESC),
        r AS (SELECT date_trunc('week',(payload->>'rr_dt')::date)::date::text wk,
            payload->>'nm_id' nm, payload->>'supplier_oper_name' op,
            coalesce((payload->>'quantity')::numeric,0) q, coalesce((payload->>'retail_amount')::numeric,0) ra,
            coalesce((payload->>'ppvz_for_pay')::numeric,0) pay, coalesce((payload->>'delivery_rub')::numeric,0) del,
            coalesce((payload->>'storage_fee')::numeric,0)+coalesce((payload->>'acceptance')::numeric,0)
              +coalesce((payload->>'deduction')::numeric,0)+coalesce((payload->>'penalty')::numeric,0) ov
            FROM raw_wb_report WHERE (payload->>'rr_dt')::date BETWEEN %s AND %s)
        SELECT wk,
            sum(CASE WHEN op='Продажа' THEN ra WHEN op='Возврат' THEN -ra ELSE 0 END)::float rev,
            (sum(pay)-sum(del)-sum(ov)
             -sum(CASE WHEN op='Продажа' THEN q*coalesce(c.u,0)
                       WHEN op='Возврат' THEN -q*coalesce(c.u,0) ELSE 0 END))::float net
        FROM r LEFT JOIN cpu c ON c.article=r.nm WHERE wk IS NOT NULL GROUP BY wk""",
                       (wk_start.isoformat(), wk_end.isoformat())):
        add(r["wk"], r["rev"], r["net"])

    # Ozon: operation_date в границах склеенных недель. COGS месячный → на неделю по доле
    # выручки недели В ЭТОМ месяце (граничная неделя получает доли из ОБОИХ месяцев).
    span_from = wk_start.replace(day=1)
    span_to = _oz_month(wk_end.replace(day=1).isoformat())[1]
    ozw = db.query("""SELECT date_trunc('week',(payload->>'operation_date')::date)::date::text wk,
        date_trunc('month',(payload->>'operation_date')::date)::date::text mm,
        coalesce(sum((payload->>'accruals_for_sale')::numeric),0)::float rev,
        coalesce(sum((payload->>'amount')::numeric),0)::float topay
        FROM raw_ozon_transaction WHERE (payload->>'operation_date')::date BETWEEN %s AND %s
        AND payload->>'operation_date' IS NOT NULL GROUP BY 1,2""", (span_from.isoformat(), span_to))
    mm_rev = {}
    for r in ozw:
        mm_rev[r["mm"]] = mm_rev.get(r["mm"], 0.0) + (r["rev"] or 0)
    mm_cogs = {mm: _oz_cogs("", mm) for mm in mm_rev}
    for r in ozw:
        if r["wk"] and wk_start.isoformat() <= r["wk"] <= wk_end.isoformat():
            share = (r["rev"] or 0) / (mm_rev.get(r["mm"]) or 1)
            add(r["wk"], r["rev"], (r["topay"] or 0) - mm_cogs.get(r["mm"], 0.0) * share)

    # Расход на рекламу по неделям (ВБ + Озон, оба юрлица) — тоже склеенными неделями
    adw = {}
    for pl in ("wb", "ozon"):
        accs = [r["account"] for r in db.query(
            "SELECT DISTINCT account FROM ad_spend_daily WHERE platform=%s", (pl,))]
        if not accs:
            continue
        for w, ad in _weekly_adspend(pl, accs).items():
            key = w.isoformat() if hasattr(w, "isoformat") else str(w)
            adw[key] = adw.get(key, 0) + (ad or 0)

    rows = []
    for k in sorted(wk):
        if not (wk_start.isoformat() <= k <= wk_end.isoformat()):
            continue
        rev, net = wk[k]["rev"], wk[k]["net"]
        if rev < 1:
            continue
        ad = round(adw.get(k, 0), 2)
        rows.append({"wk": k, "rev": round(rev, 2), "net": round(net, 2),
                     "margin": round(net / rev * 100, 1) if rev else None,
                     "ad": ad, "net_after_ad": round(net - ad, 2)})
    return {"rows": rows}


def _l3_names(plat, account, articles):
    if plat == "wb":
        rows = db.query("SELECT nm_id::text a, title FROM wb_cards WHERE account=%s AND nm_id::text=ANY(%s)",
                        (account, articles))
        return {r["a"]: r["title"] for r in rows}
    rows = db.query("""SELECT DISTINCT ON (it->>'sku') it->>'sku' a, it->>'name' title
        FROM raw_ozon_transaction, jsonb_array_elements(payload->'items') it
        WHERE it->>'sku'=ANY(%s) ORDER BY it->>'sku'""", (articles,))
    return {r["a"]: r["title"] for r in rows}


@app.get("/api/signals")
def signals(period: str = "", scope: str = "", account: str = ""):
    """Рекомендации по СЛОЯМ. L1 (scope='') — стратегия всего бизнеса. L2 (scope=wb|ozon, без
    account) — сравнение юрлиц + тренд + рычаги МП. L3 (scope+account) — точечные SKU-проблемы
    этого юрлица (убыточные/габариты/скидки). Каждый слой — своё, без дублирования."""
    period = period or db.query("SELECT max(period_from)::text p FROM margin_by_sku")[0]["p"]
    tips = []

    def tip(sev, title, text):
        tips.append({"sev": sev, "title": title, "text": text})

    def m(v):
        return f"{round(v):,}".replace(",", " ") + " ₽"

    # --- L3: КОНКРЕТНОЕ ЮРЛИЦО — точечные SKU-проблемы (НЕ дублируем L2) ---
    if scope in ("wb", "ozon") and account:
        loss = db.query("""SELECT article, net_profit::float net FROM margin_by_sku
            WHERE platform=%s AND account=%s AND period_from=%s
              AND net_profit<0 AND revenue_buyer>0 AND article<>'0'
            ORDER BY net_profit ASC LIMIT 5""", (scope, account, period))
        if loss:
            names = _l3_names(scope, account, [r["article"] for r in loss])
            lst = "; ".join(f"{(names.get(r['article']) or r['article'])[:22]} ({m(r['net'])})" for r in loss[:3])
            tip("high", f"Топ убыточных: {lst}",
                "В минусе после комиссий — поднять цену или вывести (полный список ниже).")
        if scope == "wb":
            an = db.query(f"""SELECT count(*) n FROM wb_cards c
                JOIN margin_by_sku msk ON msk.account=c.account AND msk.article=c.nm_id::text AND msk.qty>0
                WHERE c.account=%s AND msk.period_from=%s
                  AND (c.dims_valid=false OR c.volume_l IS NULL OR c.weight_kg IS NULL
                       OR c.weight_kg/NULLIF(c.volume_l,0) < %s OR c.weight_kg/NULLIF(c.volume_l,0) > %s)""",
                (account, period, DENS_LOW, DENS_HIGH))[0]["n"]
            if an:
                tip("info", f"{an} SKU с подозрительными габаритами",
                    "Перемерить Д×Ш×В — дешевле, чем поднимать цену (список «Габариты» ниже).")
            hi = sum(1 for v in _wb_pricing(account, period).values() if v["spp_pct"] >= 30)
            if hi:
                tip("info", f"{hi} SKU с СПП ≥30%",
                    "Большая скидка площадки за наш счёт — поднять базовую цену, чтобы удержать маржу.")
        else:
            hi = sum(1 for v in _ozon_pricing(account, *_oz_month(period)).values() if v["discount_pct"] >= 20)
            if hi:
                tip("info", f"{hi} SKU продаются со скидкой ≥20%",
                    "Акции/скидки съедают цену — проверить, оправдан ли объём, иначе вывести из акций.")
        return {"period": period, "scope": scope, "account": account, "level": "l3",
                "tips": tips or [{"sev": "good", "title": "Точечных проблем по юрлицу не видно",
                                  "text": "Убыточных/габаритных позиций мало."}]}

    biz = business(period)
    ox = opex(period)
    t = biz.get("total", {})
    after = biz.get("net_after_opex")
    margin = t.get("margin_pct")
    dl = biz.get("delta") or {}
    vol, months = {}, []

    def _loss(plat):
        return db.query("""SELECT count(*) n, coalesce(sum(net_profit),0)::float s FROM margin_by_sku
            WHERE platform=%s AND period_from=%s AND net_profit<0 AND revenue_buyer>0 AND article<>'0'""",
            (plat, period))[0]

    # --- L2 УРОВЕНЬ ПЛОЩАДКИ: сравнение юрлиц + тренд + рычаги (без SKU-деталей) ---
    if scope == "wb":
        _compare_accounts(_wb_acct_metrics(period), tip, "ВБ")
        _compare_period("wb", period, tip)
        lf = _loss("wb")
        if lf["n"] >= 3:
            tip("info", f"Всего {lf['n']} убыточных SKU по ВБ (минус {m(abs(lf['s']))})",
                "Выбери юрлицо (Цифровой/Дисквэр) — покажу топ именно по нему.")
    elif scope == "ozon":
        _compare_accounts(_oz_acct_metrics(period), tip, "Ozon")
        _compare_period("ozon", period, tip)
        ozc = _oz_summary("", period)["cats"]
        oz_rev = (biz.get("ozon", {}) or {}).get("revenue") or 1
        adv, pen = -ozc.get("advertising", 0), -ozc.get("penalties", 0)
        if adv and adv / oz_rev > 0.06:
            tip("warn", f"Реклама {m(adv)} = {adv / oz_rev * 100:.0f}% выручки",
                "Управляемо — резать кампании с минусовым ДРР.")
        if pen and pen / oz_rev > 0.01:
            tip("warn", f"Штрафы {m(pen)} — операционные потери",
                "Слот/просрочка/индекс ошибок — устранимо настройкой склада и отгрузок.")
        lf = _loss("ozon")
        if lf["n"] >= 3:
            tip("info", f"Всего {lf['n']} убыточных SKU по Ozon (минус {m(abs(lf['s']))})",
                "Выбери юрлицо — покажу топ именно по нему.")
        return {"period": period, "scope": scope, "tips": tips or [
            {"sev": "good", "title": "Проблем по Ozon не видно", "text": "Метрики юрлиц близки, расходы под контролем."}]}

    # --- ГЛОБАЛЬНО (главный экран) ---
    if scope == "wb":
        return {"period": period, "scope": scope, "tips": tips or [
            {"sev": "good", "title": "Проблем по ВБ не видно", "text": "Метрики юрлиц близки."}]}

    # 1. ЦЕЛЬ — 5 млн чистой после ФОТ
    if after is not None:
        if after >= HEALTHY_AFTER_OPEX:
            tip("good", f"🎯 Цель достигнута: чистая после ФОТ {m(after)} ≥ 5 млн",
                "Бизнес здоров и силён — можно рекомендовать премии команде.")
        else:
            tip("high", f"До цели 5 млн чистой после ФОТ не хватает {m(HEALTHY_AFTER_OPEX - after)} "
                        f"(сейчас {m(after)})",
                "Рычаги: ① поднять маржу (цены/ассортимент), ② нарастить оборот, ③ урезать постоянные расходы.")

    # 1b. ПОТЕНЦИАЛ к чистой — сколько вернём, устранив неэффективности
    loss_gain = -(db.query("""SELECT coalesce(sum(net_profit),0)::float s FROM margin_by_sku
        WHERE period_from=%s AND net_profit<0 AND revenue_buyer>0 AND article<>'0'""", (period,))[0]["s"])
    pen_gain = -_oz_summary("", period)["cats"].get("penalties", 0)
    vol = _order_volume()
    months = sorted(vol)
    cur_v = vol.get(period, 0)
    peak = max(vol.values()) if vol else 0
    packers = [i for i in (ox.get("items") or [])
               if "упаков" in ((i.get("role") or "") + (i.get("name") or "")).lower()]
    npk = len(packers)
    avg_sal = (sum(i["amount"] for i in packers) / npk) if npk else 0
    excess = round(npk * (1 - cur_v / peak)) if (npk and peak and cur_v < peak) else 0
    pack_gain = max(0, excess) * avg_sal
    potential = loss_gain + pen_gain + pack_gain
    if potential > 1000:
        parts = []
        if loss_gain > 1:
            parts.append(f"убыточные SKU +{m(loss_gain)}")
        if pen_gain > 1:
            parts.append(f"штрафы Ozon +{m(pen_gain)}")
        if pack_gain > 1:
            parts.append(f"упаковка −{excess} чел +{m(pack_gain)}")
        tip("good", f"💰 Потенциал +{m(potential)}/мес к чистой — если устранить неэффективности",
            "; ".join(parts) + ". Это «быстрые деньги»; основной разрыв до 5 млн закрывается ростом оборота (реклама/ассортимент).")

    # 2. ПРОБЛЕМНАЯ ЗОНА: маржа ВБ vs Ozon
    wbm, ozm = (biz.get("wb", {}) or {}).get("margin_pct"), (biz.get("ozon", {}) or {}).get("margin_pct")
    if wbm is not None and ozm is not None and abs(wbm - ozm) >= 5:
        worse, wv, better, bv = ("Wildberries", wbm, "Ozon", ozm) if wbm < ozm else ("Ozon", ozm, "Wildberries", wbm)
        tip("warn", f"Маржа {worse} {wv:.1f}% заметно ниже {better} ({bv:.1f}%) — обратить внимание",
            f"Изучить {worse}: цены, комиссия/логистика, COGS. Разобрать на странице площадки (сравнение юрлиц там же).")

    # 3. УПАКОВЩИКИ vs ОБЪЁМ ЗАКАЗОВ
    vol = _order_volume()
    months = sorted(vol)
    cur_v = vol.get(period, 0)
    prev_v = vol.get(months[months.index(period) - 1]) if period in months and months.index(period) > 0 else None
    packers = [i for i in (ox.get("items") or [])
               if "упаков" in ((i.get("role") or "") + (i.get("name") or "")).lower()]
    npk = len(packers)
    if npk and cur_v and prev_v and cur_v < prev_v * 0.85:
        tip("warn", f"Заказы упали {int(prev_v)}→{int(cur_v)} ед, упаковщиков {npk} (нагрузка {cur_v / npk:.0f} ед/чел)",
            "Объём падает при том же штате упаковки → сократить упаковщиков ИЛИ нарастить заказы. "
            "Внеси штат прошлых месяцев в расходы — появится тренд «заказы на упаковщика».")

    # 4. ДОЛЯ ПОСТОЯННЫХ РАСХОДОВ
    rev = t.get("revenue")
    if rev and ox.get("total") and ox["total"] / rev * 100 > 12:
        tip("warn", f"Постоянные расходы {ox['total'] / rev * 100:.0f}% выручки ({m(ox['total'])})",
            "Высокая доля — при падении оборота ФОТ+аренда быстро съедают прибыль. Держать штат под нагрузку.")

    # 5. ПАДЕНИЕ маржи к прошлому месяцу
    if margin is not None and dl.get("total_margin") and (dl["total_margin"].get("abs") or 0) <= -2:
        tip("warn", f"Маржа бизнеса упала на {abs(dl['total_margin']['abs'])} п.п. (до {margin}%) — принять меры",
            "Причины: рост COGS, рост рекламы, демпинг. Разобрать по площадкам.")

    if not tips:
        tip("good", "Явных проблем не видно", "Метрики в норме. Держать курс к цели 5 млн после ФОТ.")

    return {"period": period, "scope": scope, "tips": tips,
            "volume": [{"month": mo, "units": round(vol[mo])} for mo in months],
            "after_opex": after, "target": HEALTHY_AFTER_OPEX,
            "to_target": round(max(0, HEALTHY_AFTER_OPEX - (after or 0)), 2)}
