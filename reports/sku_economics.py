"""reports/sku_economics.py — витрина mkt_sku_economics: юнит-экономика ПО ВСЕМ SKU acc1.

3-ценовой стек WB (все три видны напрямую из 2 API):
  before(2671, v4 basic) ──промо──▶ promo(2324, Prices discountedPrice) ──СПП──▶ buyer(1859, v4 product)
Маржа считается ОТ ЦЕНЫ ПОКУПАТЕЛЯ ПОСЛЕ СПП (buyer = revenue_wb).
Форвард НЕ моделирует комиссию/СПП по отдельности — использует стабильный payout-ratio
(к_перечислению/база ≈0.63, от СПП НЕ зависит: комиссия ВБ гасит СПП 1:1). payout — per-SKU из
трейлинг-факта (raw_wb_report, окно TRAIL_DAYS), фолбэк — медиана:
  buyer_price  = market_price (v4 product) | фолбэк promo*(1 - spp)   # НЕ вычитать СПП дважды
  to_pay_u     = promo_price * payout_ratio                          # база = акционная цена (list×(1-акция))
  net_u        = to_pay_u - logistics_u - storage_u - accept_u - cogs_u
  margin_pct_wb= net_u / buyer_price
Логистика/приёмка: ФАКТ для проданных (margin_by_sku+sales), МЕДИАНА для непроданных.
Рядом с форвардом: trail_* (факт окна) + last_sale_date/days_since_sale (форвард врёт, если продажи встали).
Рычаг маржи — глубина НАШЕЙ акции (роняет базу→payout), НЕ СПП (её несёт ВБ).
Себест (cogs_u): ТОЛЬКО из отгрузок МС за всю историю (margin_by_sku/ms_demand_pos, fin) — набор=BOM позиций
отгрузки, одиночный=себест отгрузок последнего периода, никогда не отгружался=оценка по предмету. Карточку МС
(buy_price/cost_seb) НЕ используем — забраковано клиентом, данные в карточках неверны.
READ-ONLY по margin_by_sku / sales / ms_product. Пишет только свою mkt_sku_economics.

Запуск:  ./venv/bin/python reports/sku_economics.py [wb_acc1] [2026-06-01]
"""
import os
import sys
import pathlib
import statistics
from datetime import date

from dotenv import load_dotenv
from psycopg2.extras import Json

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")


def _f(v):
    return None if v is None else float(v)


def build(account="wb_acc1", period="2026-06-01"):
    # 1) Медианные ставки расходов от ПРОДАННОГО (импутация для непроданных)
    med = db.query("""
      SELECT
        percentile_cont(0.5) WITHIN GROUP (ORDER BY 1-s.revenue_wb/NULLIF(s.revenue_buyer,0)) spp,
        -- payout = к перечислению / база(до СПП); ≈0.63, от СПП НЕ зависит → форвардим им, не комиссией
        percentile_cont(0.5) WITHIN GROUP (ORDER BY s.to_pay/NULLIF(s.revenue_buyer,0))        payout,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY m.logistics/NULLIF(m.qty,0))               log_u,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY m.storage/NULLIF(m.qty,0))                 stor_u,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY m.acceptance/NULLIF(m.qty,0))              acc_u
      FROM margin_by_sku m
      JOIN sales s ON s.platform='wb' AND s.account=m.account AND s.article=m.article
                  AND s.period_from=m.period_from AND s.granularity='month'
      WHERE m.platform='wb' AND m.account=%s AND m.period_from=%s AND m.qty>0 AND s.revenue_buyer>0
    """, (account, period))[0]
    SPP_M, PAYOUT_M = _f(med["spp"]) or 0.289, _f(med["payout"]) or 0.637
    LOG_M, STOR_M, ACC_M = _f(med["log_u"]) or 238.0, _f(med["stor_u"]) or 0.0, _f(med["acc_u"]) or 20.0
    print(f"медианы: СПП {SPP_M:.3f} payout {PAYOUT_M:.3f} логистика/шт {LOG_M:.0f} приёмка/шт {ACC_M:.0f}")

    # 2) Факт по проданным (для сверки и точных ставок)
    sold = {int(r["article"]): r for r in db.query("""
      SELECT m.article, m.qty, m.commission, m.logistics, m.storage, m.acceptance, m.net_profit,
             s.revenue_wb, s.revenue_buyer
      FROM margin_by_sku m
      JOIN sales s ON s.platform='wb' AND s.account=m.account AND s.article=m.article
                  AND s.period_from=m.period_from AND s.granularity='month'
      WHERE m.platform='wb' AND m.account=%s AND m.period_from=%s AND m.qty>0 AND m.article ~ '^[0-9]+$'
    """, (account, period))}

    # 3) Себест из ОТГРУЗОК МС за ВСЮ ИСТОРИЮ (fin, read-only) — единственный источник.
    #    Реальная закупка на дату отгрузки по FIFO; берём себест/шт последнего периода, где nm
    #    отгружался. Карточку МС (buy_price) НЕ используем — забраковано, данные неверны.
    repl_ship = {int(r["nm"]): _f(r["repl"]) for r in db.query("""
      SELECT DISTINCT ON (article) article::bigint nm, cogs/NULLIF(qty,0) repl
      FROM margin_by_sku
      WHERE platform='wb' AND account=%s AND qty>0 AND cogs>0 AND article ~ '^[0-9]+$'
      ORDER BY article, period_from DESC
    """, (account,))}

    # 3b) НАБОРЫ (комплекты): один WB nm = НЕСКОЛЬКО товаров МС в отгрузке. У margin_by_sku
    #     qty раздут компонентами → cogs/qty = per-компонент, а цена карточки per-НАБОР → занижение.
    multi_ms = {int(r["nm"]) for r in db.query("""
      WITH ship AS (
        SELECT w.payload->>'nm_id' nm, pos.ms_id, sum(pos.qty) q
        FROM raw_wb_report w
        JOIN ms_demand_cogs d  ON d.demand_name = w.payload->>'assembly_id'
        JOIN ms_demand_pos pos ON pos.demand_id = d.demand_id
        WHERE w.account=%s AND w.payload->>'nm_id' ~ '^[0-9]+$'
        GROUP BY 1, 2 HAVING sum(pos.qty) >= 2)
      SELECT nm FROM ship GROUP BY nm HAVING count(*) > 1
    """, (account,))}

    # 3c) Себест НАБОРА = Σ(себест позиций отгрузки) / кол-во наборов (≈ max qty компонента).
    #     Всё из ОТГРУЗОК: ms_demand_pos.cost = фактический итог позиции (Σ по документу = cogs),
    #     БЕЗ карточки. Заменяет прежний BOM по buy_price.
    repl_set = {int(r["nm"]): _f(r["bom"]) for r in db.query("""
      WITH ship AS (
        SELECT w.payload->>'nm_id' nm, pos.ms_id, sum(pos.cost) cost, sum(pos.qty) q
        FROM raw_wb_report w
        JOIN ms_demand_cogs d  ON d.demand_name = w.payload->>'assembly_id'
        JOIN ms_demand_pos pos ON pos.demand_id = d.demand_id
        WHERE w.account=%s AND w.payload->>'nm_id' ~ '^[0-9]+$'
        GROUP BY 1, pos.ms_id)
      SELECT nm::bigint nm, sum(cost) / NULLIF(max(q),0) bom
      FROM ship GROUP BY nm HAVING count(*) > 1 AND max(q) >= 2
    """, (account,))}

    # 5) subject из card_content
    subj = {int(r["nm_id"]): r["subject"] for r in db.query("""
      SELECT nm_id, payload->>'subjectName' subject FROM raw_wb_card_content WHERE account=%s
    """, (account,))}

    # 5b) Дата последней продажи (raw_wb_report) — сигнал «продаётся ли по текущей цене»
    last_sale = {int(r["nm"]): r["d"] for r in db.query("""
      SELECT payload->>'nm_id' nm, max((payload->>'rr_dt')::date) d
      FROM raw_wb_report
      WHERE account=%s AND payload->>'doc_type_name'='Продажа'
        AND (payload->>'quantity')::numeric > 0 AND payload->>'nm_id' ~ '^[0-9]+$'
      GROUP BY 1
    """, (account,))}

    # 5c) Трейлинг-факт per-SKU (окно TRAIL_DAYS дн): payout=к_перечисл/база (СПП-независим), реализация, СПП, qty
    TRAIL_DAYS = 90
    trail = {int(r["nm"]): r for r in db.query("""
      SELECT payload->>'nm_id' nm,
             sum((payload->>'quantity')::numeric)                  qty,
             sum((payload->>'ppvz_for_pay')::numeric)              pay,
             sum((payload->>'retail_price_withdisc_rub')::numeric) base,
             sum((payload->>'retail_amount')::numeric)             realized
      FROM raw_wb_report
      WHERE account=%s AND payload->>'doc_type_name'='Продажа'
        AND (payload->>'quantity')::numeric > 0
        AND (payload->>'retail_price_withdisc_rub')::numeric > 0
        AND payload->>'nm_id' ~ '^[0-9]+$'
        AND (payload->>'rr_dt')::date >= (CURRENT_DATE - %s::int)
      GROUP BY 1
      HAVING sum((payload->>'retail_price_withdisc_rub')::numeric) > 0
    """, (account, TRAIL_DAYS))}

    # 5d) Медиана payout ПО ПРЕДМЕТУ WB (subjectName) — для непроданных вместо глобальной.
    #     ВБ задаёт комиссию на уровне предмета; у нас предметов мало (картриджи/чернила),
    #     payout ~равный, но так корректнее и защищает от появления категорий с иным тарифом.
    payout_by_subj = {r["subj"]: _f(r["payout"]) for r in db.query("""
      WITH tr AS (
        SELECT payload->>'nm_id' nm,
               sum((payload->>'ppvz_for_pay')::numeric)
                 / NULLIF(sum((payload->>'retail_price_withdisc_rub')::numeric),0) payout
        FROM raw_wb_report
        WHERE account=%s AND payload->>'doc_type_name'='Продажа'
          AND (payload->>'quantity')::numeric>0 AND (payload->>'retail_price_withdisc_rub')::numeric>0
          AND (payload->>'rr_dt')::date >= (CURRENT_DATE - %s::int)
        GROUP BY 1)
      SELECT c.payload->>'subjectName' subj,
             percentile_cont(0.5) WITHIN GROUP (ORDER BY tr.payout) payout
      FROM tr JOIN raw_wb_card_content c ON c.nm_id = tr.nm::bigint AND c.account=%s
      WHERE tr.payout IS NOT NULL
      GROUP BY 1 HAVING count(*) >= 10
    """, (account, TRAIL_DAYS, account))}

    # 3d) Оценка по АНАЛОГУ для никогда не отгружавшихся: медиана себеста отгрузок по ПРЕДМЕТУ
    #     (subjectName, ≥5 отгружавшихся образцов). Тоже из отгрузок, не карточка. src='analog'.
    _sc = {}
    for _nm, _c in repl_ship.items():
        _s = subj.get(_nm)
        if _s and _c and _c > 0:
            _sc.setdefault(_s, []).append(_c)
    repl_analog = {s: statistics.median(v) for s, v in _sc.items() if len(v) >= 5}
    print(f"аналог-оценка по предметам: {len(repl_analog)} категорий (медиана себеста отгрузок)")

    # 6) Универсум — все карточки с ценой. 3-ценовой стек:
    #    price = до акции (v4 basic); discounted_price = акционная, ДО СПП (база комиссии/СПП);
    #    market_price = v4 product = цена покупателя ПОСЛЕ СПП.
    prices = db.query("""
      SELECT nm_id, vendor_code,
             price            AS price_before_promo,   -- 2671, до акции
             discounted_price AS promo_price,          -- 2324, акционная (после промо, ДО СПП) = база комиссии/СПП
             market_price,                             -- 1859, после СПП (цена покупателя)
             discount_pct
      FROM wb_price
      WHERE account=%s AND COALESCE(market_price, discounted_price) > 0
    """, (account,))

    today = date.today()
    recs, n_no_cogs = [], 0
    for pr in prices:
        nm = int(pr["nm_id"])
        before = _f(pr["price_before_promo"])  # 2671 — до акции (v4 basic / Prices price)
        promo = _f(pr["promo_price"])          # 2324 — акционная, ДО СПП = БАЗА комиссии/СПП (Prices discountedPrice)
        mkt = _f(pr["market_price"])           # 1859 — v4 product, цена покупателя ПОСЛЕ СПП
        # себест ТОЛЬКО из отгрузок МС за всю историю (карточку НЕ используем):
        # набор → BOM позиций отгрузки; одиночный → себест отгрузок; иначе → оценка по предмету.
        if nm in repl_set:
            cogs, src = repl_set[nm], "set"           # комплект: BOM из себеста позиций отгрузки
        elif nm in repl_ship:
            cogs, src = repl_ship[nm], "shipment"     # одиночный: себест отгрузок (посл. период)
        elif subj.get(nm) in repl_analog:
            cogs, src = repl_analog[subj.get(nm)], "analog"   # никогда не продавался → оценка по предмету
        else:
            cogs, src = None, None
            n_no_cogs += 1
        s = sold.get(nm)
        if s:  # ФАКТ ставок расходов (логистика/приёмка) + факт-маржа за полный месяц
            q = _f(s["qty"]) or 0
            rb, rw = _f(s["revenue_buyer"]), _f(s["revenue_wb"])
            spp = (1 - rw/rb) if rb else SPP_M
            log_u = (_f(s["logistics"])/q) if q else LOG_M
            stor_u = (_f(s["storage"])/q) if q else STOR_M
            acc_u = (_f(s["acceptance"])/q) if q else ACC_M
            net_u_act = (_f(s["net_profit"])/q) if q else None
            margin_act = (100*_f(s["net_profit"])/rw) if rw else None      # от реализации (после СПП)
            # маржа месяца от НАШЕЙ промо-цены = прибыль / выручка-до-СПП (revenue_buyer).
            # qty у наборов раздут компонентами, но здесь ÷qty сокращается → чисто и для наборов.
            margin_own_act = (100*_f(s["net_profit"])/rb) if rb else None
            sold_flag = True
        else:  # МЕДИАНА (непроданные)
            spp, log_u, stor_u, acc_u = SPP_M, LOG_M, STOR_M, ACC_M
            q = net_u_act = margin_act = margin_own_act = None
            sold_flag = False

        # payout-ratio: per-SKU из трейлинг-факта (к_перечисл/база, СПП-независим) → фолбэк медиана
        t = trail.get(nm)
        tb = _f(t["base"]) if t else None
        if tb and tb > 0:
            tp, tq, tr = _f(t["pay"]), _f(t["qty"]), _f(t["realized"])
            payout, payout_src = tp / tb, "sku"
            trail_qty = tq
            trail_real_u = (tr / tq) if tq else None
            trail_spp = (1 - tr / tb)
        else:  # нет своих продаж → payout по предмету WB, фолбэк глобальная медиана
            subj_p = payout_by_subj.get(subj.get(nm))
            payout = subj_p if subj_p else PAYOUT_M
            payout_src = "subject" if subj_p else "median"
            trail_qty = trail_real_u = trail_spp = None

        # Цена покупателя (после СПП) = знаменатель маржи. v4 product напрямую; фолбэк — промо×(1−СПП).
        # НЕ вычитаем СПП дважды: market_price уже после СПП, promo — до СПП.
        buyer = mkt if mkt else (promo * (1 - spp) if promo else None)
        # База форварда = акционная цена (promo). Фолбэк: before, либо восстановить из buyer.
        fwd_base = promo if promo else (before if before else (buyer / (1 - spp) if (buyer and spp < 1) else buyer))
        # net форвардим ЧЕРЕЗ payout (СПП-независим), НЕ через commission%×promo: to_pay = база × payout.
        to_pay_u = (fwd_base * payout) if fwd_base is not None else None
        wb_cut_u = (fwd_base - to_pay_u) if (fwd_base is not None and to_pay_u is not None) else None  # полное удержание ВБ
        net_u = (to_pay_u - log_u - stor_u - acc_u - cogs) \
            if (cogs is not None and to_pay_u is not None) else None
        margin_wb = (100*net_u/buyer) if (net_u is not None and buyer) else None
        # производные 3-ценового стека + сигнал продаж
        promo_frac = (1 - promo/before) if (before and promo) else None
        spp_card = (1 - buyer/promo) if (buyer and promo) else None
        lsd = last_sale.get(nm)
        dss = (today - lsd).days if lsd else None

        # Маржа: от НАШЕЙ ПРОМО-цены (что задаём в акцию, до СПП) — KPI ≥25% + от реализации (справочно).
        margin_own = (100*net_u/fwd_base) if (net_u is not None and fwd_base) else None
        # Сценарий «маржа vs глубина акции» + точка безубытка + 25%-лимит акции.
        scenario, breakeven, promo_limit_25 = None, None, None
        spp_est = spp_card if spp_card is not None else (trail_spp if trail_spp is not None else spp)
        if before and cogs is not None:
            fixed = log_u + stor_u + acc_u + cogs           # расходы вне payout (логистика/хранение/приёмка/COGS)
            depths = sorted(set([0.0, 0.1, 0.2, 0.3, 0.4, 0.5] +
                                ([round(promo_frac, 2)] if promo_frac is not None else [])))
            scenario = []
            for d in depths:
                b = before * (1 - d)
                tp = b * payout
                n = tp - fixed
                byr = b * (1 - spp_est) if spp_est is not None else None
                scenario.append({
                    "promo_pct": round(d, 4), "base": round(b), "buyer_u": (round(byr) if byr else None),
                    "to_pay_u": round(tp), "net_u": round(n),
                    "margin_own": (round(100*n/b, 1) if b else None),        # от ПРОМО-цены на этой глубине (KPI)
                    "margin_wb": (round(100*n/byr, 1) if byr else None),     # от реализации после СПП
                    "current": (promo_frac is not None and abs(d - promo_frac) < 0.005),
                })
            if payout > 0:
                breakeven = round(1 - fixed/(payout*before), 4)             # net=0 → base=fixed/payout
                # глубина, где маржа-от-промо=25%: payout − fixed/(before(1−d)) = 0.25
                if payout > 0.25:
                    promo_limit_25 = round(1 - fixed/((payout - 0.25)*before), 4)

        recs.append({
            "account": account, "nm_id": nm, "vendor_code": pr["vendor_code"], "subject": subj.get(nm),
            # 3-ценовой стек
            "price_before_promo": (round(before, 2) if before is not None else None),
            "promo_price": (round(promo, 2) if promo is not None else None),
            "buyer_price": (round(buyer, 2) if buyer is not None else None),
            "promo_pct": (round(promo_frac, 4) if promo_frac is not None else None),
            "spp_pct_card": (round(spp_card, 4) if spp_card is not None else None),
            "price_card": (round(buyer, 2) if buyer is not None else None),  # deprecated = buyer_price
            "cogs_u": (round(cogs, 2) if cogs is not None else None),
            "cogs_source": src, "spp_pct": round(spp, 4),
            # payout-ратио модель (замена commission%)
            "payout_ratio": round(payout, 4), "payout_source": payout_src,
            "to_pay_u": (round(to_pay_u, 2) if to_pay_u is not None else None),
            "commission_pct": round(1 - payout, 4),                                # доля базы, удержанная ВБ
            "commission_u": (round(wb_cut_u, 2) if wb_cut_u is not None else None),  # полное удержание ВБ из базы
            "revenue_wb_u": (round(buyer, 2) if buyer is not None else None),
            "logistics_u": round(log_u, 2), "storage_u": round(stor_u, 2), "accept_u": round(acc_u, 2),
            "net_u": (round(net_u, 2) if net_u is not None else None),
            "margin_pct_own": (round(margin_own, 2) if margin_own is not None else None),  # KPI: от нашей цены
            "margin_pct_wb": (round(margin_wb, 2) if margin_wb is not None else None),     # от реализации
            # сценарий маржа vs глубина акции + точка безубытка + 25%-лимит
            "scenario_promo": (Json(scenario) if scenario is not None else None),
            "promo_breakeven_pct": breakeven,
            "promo_limit_25": promo_limit_25,
            # трейлинг-факт
            "trail_days": TRAIL_DAYS,
            "trail_qty": (round(trail_qty, 1) if trail_qty is not None else None),
            "trail_realized_u": (round(trail_real_u, 2) if trail_real_u is not None else None),
            "trail_spp_pct": (round(trail_spp, 4) if trail_spp is not None else None),
            "sold_flag": sold_flag, "qty_period": q,
            "net_u_actual": (round(net_u_act, 2) if net_u_act is not None else None),
            "margin_pct_wb_actual": (round(margin_act, 2) if margin_act is not None else None),
            "margin_pct_own_actual": (round(margin_own_act, 2) if margin_own_act is not None else None),  # месяц от промо
            "last_sale_date": lsd, "days_since_sale": dss,
            "period_econ": period,
        })

    db.upsert("mkt_sku_economics", recs, conflict_cols=["account", "nm_id"])
    with_cogs = sum(1 for r in recs if r["cogs_u"] is not None)
    print(f"[sku_economics {account}] строк {len(recs)}, с себестом {with_cogs} "
          f"({100*with_cogs//max(len(recs),1)}%), без себеста {n_no_cogs}; продано-факт {len(sold)}")
    return len(recs)


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "wb_acc1"
    per = sys.argv[2] if len(sys.argv) > 2 else "2026-06-01"
    build(acc, per)
