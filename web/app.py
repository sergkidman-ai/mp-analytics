"""web/app.py — дашборд «Пульт бизнеса» (BI маркетплейсов).

FastAPI: читает Postgres (данные обновляются run_daily.py 2×/день), отдаёт JSON-API + фронт.
Drill-down: большие цифры → SKU → (позже категории/заказы). Фильтры: площадка/аккаунт/период.
За хостовым nginx с basic-auth (bi.metaverseworld.ru). Локально: 127.0.0.1:8090.

Запуск:  ./venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8090
"""
import sys
import pathlib

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

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
    own = db.query(f"""SELECT coalesce(sum(our_price*qty),0)::float own_revenue
        FROM sales{w2 + (' AND ' if w2 else ' WHERE ')}qty>0 AND our_price IS NOT NULL""", p2)[0]
    r["own_revenue"] = own["own_revenue"]
    r["margin_own"] = round(r["net"] / own["own_revenue"] * 100, 1) if own["own_revenue"] else None
    # СПП (эффективная) = насколько цена покупателя ниже нашей = (наша − покупательская)/наша
    r["spp_pct"] = round((own["own_revenue"] - r["revenue"]) / own["own_revenue"] * 100, 1) \
        if own["own_revenue"] else None
    # расходы из сырого отчёта WB по типу удержания: реклама / отзывы / хранение / прочее.
    # Это «нераспределённые» (без привязки к nm_id) — уже вычтены из net, показываем отдельно.
    rw, rp = ["account=%s"], [account or "wb_acc1"]
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
        for k in ("net", "revenue", "cogs", "own_revenue", "qty", "margin_pct", "margin_own",
                  "adv_spend", "commission_pct", "returns_sum", "logistics", "spp_pct"):
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
            -- прирост к НАШЕЙ цене (доля) для выхода на 0.10 маржи от цены ВБ:
            -- x = (0.10*revenue - net)/(0.61*our_price*qty - 0.10*revenue)
            -- на сколько % поднять НАШУ цену, чтобы маржа от нашей цены достигла 25%:
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
    return {"rows": rows, "count": len(rows)}


@app.get("/api/opex")
def opex(period: str = ""):
    """Постоянные расходы (ФОТ + аренда) — бизнес-уровень, действуют с effective_from.
    Возвращает список + итоги + чистую ВСЕГО бизнеса (оба ВБ) за месяц минус эти расходы."""
    if not period:
        return {"applies": False, "items": [], "total": 0}
    items = db.query("""SELECT name, role, category, base::float, tax_pct::float, amount::float
        FROM opex WHERE effective_from <= %s ORDER BY category, amount DESC""", (period,))
    if not items:
        return {"applies": False, "items": [], "total": 0, "period": period}
    fot = sum(i["amount"] for i in items if i["category"] == "salary")
    rent = sum(i["amount"] for i in items if i["category"] == "rent")
    total = fot + rent
    # чистая ВСЕГО бизнеса (все аккаунты/площадки) за выбранный месяц
    biz = db.query("""SELECT coalesce(sum(net_profit),0)::float net FROM margin_by_sku
        WHERE period_from=%s""", (period,))[0]["net"]
    return {"applies": True, "period": period, "items": items,
            "fot": round(fot, 2), "rent": round(rent, 2), "total": round(total, 2),
            "biz_net": round(biz, 2), "net_after": round(biz - total, 2),
            "headcount": sum(1 for i in items if i["category"] == "salary")}


def _biz_for(period):
    """Агрегат по ВСЕМ аккаунтам за период (через _summary_one на аккаунт) + разбивка."""
    accts = [r["account"] for r in db.query(
        "SELECT DISTINCT account FROM margin_by_sku WHERE period_from=%s ORDER BY 1", (period,))]
    per = [(a, _summary_one("", a, period)) for a in accts]
    agg = {}
    for k in ("revenue", "net", "cogs", "commission", "logistics", "qty", "own_revenue",
              "adv_spend", "reviews_spend", "storage_all", "returns_sum",
              "loss_count", "loss_sum", "loss_qty", "unalloc"):
        agg[k] = sum((p.get(k) or 0) for _, p in per)
    rev, own = agg["revenue"], agg["own_revenue"]
    agg["margin_pct"] = round(agg["net"] / rev * 100, 1) if rev else None
    agg["margin_own"] = round(agg["net"] / own * 100, 1) if own else None
    agg["spp_pct"] = round((own - rev) / own * 100, 1) if own else None
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
    cur, per = _biz_for(period)
    op = db.query("""SELECT coalesce(sum(amount),0)::float t FROM opex WHERE effective_from<=%s""", (period,))[0]["t"]
    cur["opex"] = round(op, 2)
    cur["net_after_opex"] = round(cur["net"] - op, 2)
    prev_p = _prev_period("", "", period)
    if prev_p:
        prv, _ = _biz_for(prev_p)
        cur["prev_period"] = prev_p
        cur["delta"] = {}
        for k in ("net", "revenue", "cogs", "margin_pct", "margin_own", "spp_pct",
                  "commission_pct", "adv_spend", "loss_count"):
            c, o = cur.get(k), prv.get(k)
            cur["delta"][k] = None if c is None or o is None else {
                "abs": round(c - o, 2), "pct": (None if not o else round((c - o) / abs(o) * 100, 1))}
    else:
        cur["prev_period"] = None
        cur["delta"] = None
    cur["accounts"] = [{"account": a, "name": {"wb_acc1": "Цифровой квадрат", "wb_acc2": "Дисквэр"}.get(a, a),
                        "revenue": p["revenue"], "net": p["net"],
                        "margin_pct": p["margin_pct"], "margin_own": p["margin_own"]} for a, p in per]
    cur["period"] = period
    return cur


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
    acc = account or "wb_acc1"
    if rolling and rolling > 0:
        # глобальная себест/ед по nm (стабильна) + все периоды; берём последние N недель
        rows = db.query("""
            WITH cpu AS (SELECT article, sum(cogs)/sum(qty) u FROM margin_by_sku
                WHERE account=%s AND qty>0 AND cogs>0 GROUP BY article),
            r AS (SELECT date_trunc('week',(payload->>'rr_dt')::date)::date wk,
                payload->>'nm_id' nm, payload->>'supplier_oper_name' op,
                coalesce((payload->>'quantity')::numeric,0) q, coalesce((payload->>'retail_amount')::numeric,0) ra,
                coalesce((payload->>'ppvz_for_pay')::numeric,0) pay, coalesce((payload->>'delivery_rub')::numeric,0) del,
                coalesce((payload->>'storage_fee')::numeric,0) stor, coalesce((payload->>'acceptance')::numeric,0) acc,
                coalesce((payload->>'deduction')::numeric,0) ded, coalesce((payload->>'penalty')::numeric,0) pen
                FROM raw_wb_report WHERE account=%s)
            SELECT wk,
                sum(CASE WHEN op='Продажа' THEN ra WHEN op='Возврат' THEN -ra ELSE 0 END)::float rev,
                sum(pay)::float topay, sum(del)::float logi,
                (sum(stor)+sum(acc)+sum(ded)+sum(pen))::float overhead,
                sum(CASE WHEN op='Продажа' THEN q WHEN op='Возврат' THEN -q ELSE 0 END)::float qty,
                sum(CASE WHEN op='Продажа' THEN q*coalesce(c.u,0)
                         WHEN op='Возврат' THEN -q*coalesce(c.u,0) ELSE 0 END)::float cogs
            FROM r LEFT JOIN cpu c ON c.article=r.nm GROUP BY wk ORDER BY wk DESC LIMIT %s""",
            (acc, acc, rolling))
        rows = list(reversed(rows))
    elif not period:
        return {"rows": []}
    else:
        rows = db.query("""
        WITH cpu AS (SELECT article, cogs/qty u FROM margin_by_sku
            WHERE account=%s AND period_from=%s AND qty>0 AND cogs>0),
        r AS (SELECT date_trunc('week',(payload->>'rr_dt')::date)::date wk,
            payload->>'nm_id' nm, payload->>'supplier_oper_name' op,
            coalesce((payload->>'quantity')::numeric,0) q, coalesce((payload->>'retail_amount')::numeric,0) ra,
            coalesce((payload->>'ppvz_for_pay')::numeric,0) pay, coalesce((payload->>'delivery_rub')::numeric,0) del,
            coalesce((payload->>'storage_fee')::numeric,0) stor, coalesce((payload->>'acceptance')::numeric,0) acc,
            coalesce((payload->>'deduction')::numeric,0) ded, coalesce((payload->>'penalty')::numeric,0) pen
            FROM raw_wb_report WHERE account=%s AND period_from=%s)
        SELECT wk,
            sum(CASE WHEN op='Продажа' THEN ra WHEN op='Возврат' THEN -ra ELSE 0 END)::float rev,
            sum(pay)::float topay, sum(del)::float logi,
            (sum(stor)+sum(acc)+sum(ded)+sum(pen))::float overhead,
            sum(CASE WHEN op='Продажа' THEN q WHEN op='Возврат' THEN -q ELSE 0 END)::float qty,
            sum(CASE WHEN op='Продажа' THEN q*coalesce(c.u,0)
                     WHEN op='Возврат' THEN -q*coalesce(c.u,0) ELSE 0 END)::float cogs
        FROM r LEFT JOIN cpu c ON c.article=r.nm GROUP BY wk ORDER BY wk""",
        (acc, period, acc, period))
    out = []
    for x in rows:
        rev = x["rev"] or 0
        if rev < 1000:
            continue
        out.append({
            "wk": x["wk"], "rev": rev, "qty": x["qty"],
            "cogs_pct": round(x["cogs"] / rev * 100, 1),
            "logi_pct": round(x["logi"] / rev * 100, 1),
            "opmargin": round((x["topay"] - x["logi"] - x["cogs"]) / rev * 100, 1),
            "net_pct": round((x["topay"] - x["logi"] - x["cogs"] - x["overhead"]) / rev * 100, 1),
        })
    return {"rows": out}


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
def stocks(account: str = "wb_acc1"):
    """Проблемные точки по остаткам WB (FBO): что лежит + возвраты в пути."""
    cap = db.query("SELECT max(captured_at)::text m FROM wb_stocks")[0]["m"]
    if not cap:
        return {"captured_at": None, "total": {}, "by_subject": []}
    by = db.query("""SELECT subject, sum(quantity)::float qty,
        sum(in_way_from_client)::float returns
        FROM wb_stocks WHERE account=%s AND captured_at=%s GROUP BY 1 ORDER BY 2 DESC""",
                  (account, cap))
    tot = db.query("""SELECT sum(quantity)::float qty, sum(quantity_full)::float full,
        sum(in_way_from_client)::float returns, count(DISTINCT nm_id) nm
        FROM wb_stocks WHERE account=%s AND captured_at=%s""", (account, cap))[0]
    # стоимость остатков на ФБО по себестоимости: остаток × себест/ед (из margin, последний период)
    val = db.query("""
        WITH cost AS (
            SELECT DISTINCT ON (article) article, cogs/qty AS unit_cost
            FROM margin_by_sku WHERE account=%s AND qty>0 AND cogs>0
            ORDER BY article, period_from DESC)
        SELECT coalesce(sum(st.quantity*c.unit_cost),0)::float fbo_value,
               count(DISTINCT st.nm_id) FILTER (WHERE c.unit_cost IS NOT NULL) nm_valued,
               count(DISTINCT st.nm_id) nm_total
        FROM wb_stocks st LEFT JOIN cost c ON c.article=st.nm_id::text
        WHERE st.account=%s AND st.captured_at=%s AND st.quantity>0""",
                   (account, account, cap))[0]
    return {"captured_at": cap, "total": tot, "by_subject": by,
            "fbo_value": val["fbo_value"], "fbo_nm_valued": val["nm_valued"],
            "fbo_nm_total": val["nm_total"]}


# Пороги плотности (кг/л) для подозрительных карточек. Медиана ≈0.14, p05≈0.04, p95≈0.61.
DENS_LOW, DENS_HIGH = 0.05, 0.7


@app.get("/api/anomalies")
def anomalies(account: str = "wb_acc1", limit: int = 50):
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
        WHERE c.account=%s
          AND (c.dims_valid=false OR c.volume_l IS NULL OR c.weight_kg IS NULL
               OR c.weight_kg/NULLIF(c.volume_l,0) < %s
               OR c.weight_kg/NULLIF(c.volume_l,0) > %s)
        ORDER BY m.qty DESC NULLS LAST, m.logistics DESC LIMIT %s""",
        (DENS_LOW, DENS_HIGH, account, DENS_LOW, DENS_HIGH, limit))
    return {"rows": rows, "count": len(rows), "median_density": 0.137}
