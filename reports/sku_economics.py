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
Логистика/приёмка: свой трейлинг за 6 мес (штук ≥ MIN_QTY_FACT) → факт опорного месяца, если он не
тонкий → верхний квартиль фактической логистики в СВОЁМ литровом ведре (короб из габаритов карточки)
→ медиана каталога. Фолбэк намеренно консервативен: не знаем — ошибаемся в сторону расхода.
Рядом с форвардом: trail_* (факт окна) + last_sale_date/days_since_sale (форвард врёт, если продажи встали).
Рычаг маржи — глубина НАШЕЙ акции (роняет базу→payout), НЕ СПП (её несёт ВБ).
Себест (cogs_u): отгружался → себест отгрузок МС из margin_by_sku (fin, себест ВСЕГО документа на штуку —
верно и для одиночек, и для комплектов, отдельной ветки наборов НЕТ); не отгружался → живая закупка
TheCartridge по своему коду; нет и её → оценка по предмету. Карточку МС (buy_price/cost_seb) НЕ используем.
ВЕЗДЕ порог MIN_QTY_FACT: ставка, посчитанная по 1–2 штукам (логистика, payout), — шум, а не факт.
READ-ONLY по margin_by_sku / sales / ms_product. Пишет только свою mkt_sku_economics.

Период (period_econ) — ПРОШЛЫЙ ПОЛНЫЙ месяц, вычисляется на дату запуска (не хардкод).

Запуск:  ./venv/bin/python reports/sku_economics.py [wb_acc1] [YYYY-MM-01 — необязательно]
"""
import os
import sys
import pathlib
import math
import statistics
from datetime import date, datetime, timedelta, timezone

from dotenv import load_dotenv
from psycopg2.extras import Json

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from reports.margin_control import _mapping, COGS_STALE_GAP  # noqa: E402  — общий мост nm → код
# TheCartridge и ЕДИНЫЙ порог протухшего себеста (объявлен один раз, в margin_control).

load_dotenv(BASE_DIR / ".env")

# Порог «толстой» выборки: ниже него месячные ставки расходов на штуку — шум тонкого хвоста,
# берём медиану каталога (см. память feedback_incomplete_month_sku_trap).
MIN_QTY_FACT = 5


def _f(v):
    return None if v is None else float(v)


def prev_month_first(today=None):
    """Первое число ПРОШЛОГО полного месяца. Период витрины не хардкодим: 7 августа → 2026-07-01."""
    t = today or date.today()
    return (t.replace(day=1) - timedelta(days=1)).replace(day=1)


def build(account="wb_acc1", period=None):
    period = period or prev_month_first().isoformat()
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

    # 3b) НАБОРОВ ОТДЕЛЬНОЙ ВЕТКОЙ БОЛЬШЕ НЕТ (проверено 07.08.2026).
    #     Прежняя логика: «несколько разных ms_id в отгрузках nm ⇒ комплект», себест = Σпозиций/max(qty).
    #     Оба звена оказались неверны:
    #       • детектор ловил обычные одиночки — в отгрузке рядом лежал ДРУГОЙ товар (216421567:
    #         3 ms_id, а npos=1 в каждом документе; настоящих наборов avg npos>=2 всего 262 из 525);
    #       • сама формула занижала в ~4 раза даже там, где набор настоящий (253913048: BOM 642 ₽
    #         против фактических 2505 ₽/шт; 216453663: 851 против 2934; 181105203: 16 против 255).
    #     margin_by_sku (fin) уже считает cogs как себест ВСЕГО документа отгрузки на проданную
    #     штуку — и для одиночек, и для комплектов. Берём его, ничего не пересчитывая (правило 5).

    # 3c) Трейлинг-ставки расходов ПО SKU (логистика/хранение/приёмка на штуку) за 6 месяцев.
    #     Медиана каталога — плохой прокси: у мелкой лёгкой карточки логистика реально 52 ₽,
    #     а не 246 (263820406: медиана давала «маржу −23 %» там, где по факту +19…+36 %).
    rates = {int(r["nm"]): r for r in db.query("""
      SELECT article::bigint nm, sum(qty) q, sum(logistics) log, sum(storage) stor, sum(acceptance) acc
      FROM margin_by_sku
      WHERE platform='wb' AND account=%s AND qty>0 AND article ~ '^[0-9]+$'
        AND period_from >= (current_date - interval '6 months')
      GROUP BY 1
    """, (account,))}
    print(f"трейлинг-ставки расходов: {len(rates)} SKU за 6 мес")

    # 3d) Фолбэк логистики для карточек БЕЗ своих продаж — по ОБЪЁМУ КОРОБА, не по медиане каталога.
    #     Логистика ВБ = функция литража (тариф base+liter×(⌈V⌉−1)×коэфф), а медиана каталога 246 ₽
    #     навешивала полновесный короб на карточку 0.2 л и рисовала −21 % там, где реально +30 %
    #     (272510281, 264242096/100/104 — все 0.2–0.4 л). Ставки НЕ моделируем формулой: берём
    #     медиану НАШЕЙ фактической логистики в том же литровом ведре (см. правило «ничего не
    #     придумывать»: это наш факт, сгруппированный по реальному драйверу цены).
    vol = {}
    for r in db.query("""
      SELECT nm_id, (payload->'dimensions'->>'length')::numeric l,
             (payload->'dimensions'->>'width')::numeric w, (payload->'dimensions'->>'height')::numeric h
      FROM raw_wb_card_content
      WHERE account=%s AND (payload->'dimensions'->>'length') IS NOT NULL
    """, (account,)):
        try:
            v = float(r["l"]) * float(r["w"]) * float(r["h"]) / 1000.0
        except (TypeError, ValueError):
            continue
        if v > 0:
            vol[int(r["nm_id"])] = math.ceil(v)

    _lb = {}
    for _nm, _r in rates.items():
        _q, _k = _f(_r["q"]) or 0, vol.get(_nm)
        if _k and _q >= MIN_QTY_FACT:
            _lb.setdefault(_k, []).append((_f(_r["log"])/_q, _f(_r["acc"])/_q))
    #     Берём не медиану ведра, а ВЕРХНИЙ КВАРТИЛЬ: внутри ведра разброс, и медиана в 27 %
    #     случаев занижала логистику (худший промах −1789 ₽) — то есть ЗАВЫШАЛА маржу. Там,
    #     где мы не знаем, ошибаться надо в сторону осторожности, а не оптимизма.
    def _p75(v):
        return statistics.quantiles(v, n=4)[2] if len(v) >= 4 else max(v)

    log_by_liter = {k: (_p75([x[0] for x in v]), _p75([x[1] for x in v]))
                    for k, v in _lb.items() if len(v) >= 5}
    #     КРИВАЯ ОБЪЁМА для литражей, где своего ведра нет (редкие крупные короба: 45 л, 24 л,
    #     20 л — по одному-двум SKU, ведро не наберётся никогда). Раньше они падали на медиану
    #     каталога 246 ₽ — короб 45 л по цене среднего картриджа, занижение расхода в разы, то есть
    #     ЗАВЫШЕНИЕ маржи ровно на самых объёмных карточках. Тариф ВБ линеен по объёму
    #     (база + за литр), поэтому строим прямую по наполненным вёдрам методом наименьших
    #     квадратов и предсказываем ею. Медиана каталога остаётся ТОЛЬКО для карточек без габаритов.
    def _fit(pts):
        n = len(pts)
        if n < 3:
            return None
        mx = sum(p[0] for p in pts) / n
        my = sum(p[1] for p in pts) / n
        den = sum((p[0] - mx) ** 2 for p in pts)
        if den <= 0:
            return None
        b = sum((p[0] - mx) * (p[1] - my) for p in pts) / den
        return (my - b * mx, b)

    log_fit = _fit([(k, v[0]) for k, v in sorted(log_by_liter.items())])
    acc_fit = _fit([(k, v[1]) for k, v in sorted(log_by_liter.items())])

    def _by_volume(v):
        """Логистика/приёмка на штуку для короба объёмом v л. Своё ведро → его p75; иначе кривая."""
        if v in log_by_liter:
            return log_by_liter[v]
        if log_fit and acc_fit:
            lu = max(log_fit[0] + log_fit[1] * v, min(x[0] for x in log_by_liter.values()))
            au = max(acc_fit[0] + acc_fit[1] * v, 0.0)
            return lu, au
        return None

    print(f"фолбэк логистики по литражу: {len(log_by_liter)} вёдер "
          f"({min(log_by_liter, default=0)}–{max(log_by_liter, default=0)} л)"
          + (f", кривая {log_fit[0]:.0f}+{log_fit[1]:.1f}×V ₽ для остальных объёмов" if log_fit else "")
          + f"; медиана каталога {LOG_M:.0f} ₽ остаётся только для карточек без габаритов")

    def _rates_u(nm, s, q):
        """Логистика/хранение/приёмка на ШТУКУ для карточки nm.
        Приоритет: свой трейлинг за 6 мес (штук ≥ MIN_QTY_FACT) → факт опорного месяца, если он
        не тонкий → медиана каталога. Тонкая выборка врёт: у 216421567 в июле продана 1 шт при
        1542 ₽ логистики → «маржа −67 %» на товаре, который реально даёт ~+300 ₽. Выброс сверх
        3× медианы срезаем в медиану."""
        r = rates.get(nm)
        rq = _f(r["q"]) if r else 0
        if r and rq and rq >= MIN_QTY_FACT:
            lu, su, au = _f(r["log"])/rq, _f(r["stor"])/rq, _f(r["acc"])/rq
        elif s and q and q >= MIN_QTY_FACT:
            lu, su, au = _f(s["logistics"])/q, _f(s["storage"])/q, _f(s["acceptance"])/q
        elif vol.get(nm) and _by_volume(vol[nm]):     # своих продаж нет → короб: ведро, иначе кривая
            lu, au = _by_volume(vol[nm])
            return lu, STOR_M, au
        else:                                        # габаритов нет вовсе → медиана каталога
            return LOG_M, STOR_M, ACC_M
        if LOG_M and lu > 3*LOG_M:
            lu = LOG_M
        if ACC_M and au > 3*ACC_M:
            au = ACC_M
        return lu, su, au

    # 3e) Себест для НИКОГДА НЕ ОТГРУЖАВШИХСЯ — живая закупка TheCartridge по своему коду.
    #     Прежний фолбэк «медиана себеста отгрузок по предмету» (analog) оказался главным
    #     источником ЗАВЫШЕНИЯ маржи: предмет «Картриджи для принтеров» — одно ведро от 34 ₽
    #     до 3000 ₽, и на 7680 карточках медиана занижала себест на 390 ₽ (у 4469 из них —
    #     ниже 70 % живой закупки, суммарно 9.8 млн ₽ недоучтённой себестоимости).
    #     Живая закупка — РЕАЛЬНАЯ цена по этому самому коду, а не оценка по соседям.
    tc_today = {r["external_code"]: (_f(r["buy_price"]), r["status"])
                for r in db.query("SELECT external_code, buy_price, status FROM tc_buy_price_latest")}
    tc_last = {r["external_code"]: _f(r["buy_price"])
               for r in db.query("SELECT external_code, buy_price FROM tc_buy_price_last_known")}
    repl_live = {}
    for _nm, (_ec, _src) in _mapping(account, set(tc_today.keys())).items():
        _p, _st = tc_today.get(_ec, (None, None))
        if _p is None or _st != "ok":
            _p = tc_last.get(_ec)
        if _p and _p > 0:
            repl_live[int(_nm)] = _p
    print(f"живая закупка как фолбэк себеста: {len(repl_live)} SKU")

    # 3f) ЗАКУПОЧНАЯ ИЗ МОЙСКЛАДА — для карточек, которых нет ни в отгрузках, ни у TheCartridge.
    #     «Не продавалась» не значит «данных нет»: закупочная цена в МС — это прайс поставщика,
    #     он обновляется ежедневно (замечание Сергея 07.08.2026). Мосты берём ТЕ ЖЕ, что и для
    #     TheCartridge, — полный vendorCode и префикс[:4] (материнский код). Слепого матча по
    #     названию НЕ делаем: именно он когда-то смэтчил картридж на заправку.
    #     ВНИМАНИЕ на пересечении 8267 SKU закупочная МС медианно на 25 % ВЫШЕ живой закупки
    #     TheCartridge: МС — наша фактическая цена у поставщика, TheCartridge — лучшее предложение
    #     платформы. Поэтому МС стоит ПОСЛЕ live: где есть обе, берём live (не завышаем расход
    #     задним числом), а где живой нет — МС честнее любой оценки по предмету.
    repl_ms = {}
    for r in db.query("""
      SELECT c.nm_id, p.buy_price bp
        FROM wb_cards c
        JOIN ms_product p
          ON (p.external_code = c.vendor_code
              OR (c.vendor_code ~ '^[0-9]{5,}$' AND p.external_code = left(c.vendor_code, 4)))
       WHERE c.account = %s AND coalesce(p.buy_price, 0) > 0
    """, (account,)):
        repl_ms[int(r["nm_id"])] = _f(r["bp"])
    print(f"закупочная МойСклад как фолбэк себеста: {len(repl_ms)} SKU")

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
        # себест: отгружался → себест отгрузок МС (margin_by_sku, там и одиночки, и комплекты —
        # см. 3b); не отгружался → живая закупка по своему коду; нет и её → оценка по предмету.
        # Карточку МС (buy_price/cost_seb) НЕ используем — забраковано клиентом.
        if nm in repl_ship:
            cogs, src = repl_ship[nm], "shipment"     # одиночный: себест отгрузок (посл. период)
            # ПРОТУХШИЙ FIFO (07.08.2026). Себест отгрузок честно помнит цену, по которой товар
            # закупался — но если с тех пор он подорожал, эта цена больше не описывает нашу
            # экономику: продав штуку, мы купим замену ДОРОЖЕ. Экономика здесь форвардная (по ней
            # решают цену и рекламу), поэтому при разрыве больше COGS_STALE_GAP берём живую
            # закупку. Она выше — маржа честно опускается.
            _live = repl_live.get(nm)
            if _live and cogs < _live * (1 - COGS_STALE_GAP):
                cogs, src = _live, "live_stale"
        elif nm in repl_live:
            cogs, src = repl_live[nm], "live"         # не отгружался → живая закупка по своему коду
        elif nm in repl_ms:
            cogs, src = repl_ms[nm], "ms"             # нет и у TheCartridge → прайс поставщика в МС
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
            # Ставки расходов берём из факта ТОЛЬКО на достаточной выборке. На тонком хвосте
            # (qty 1–2) месячная логистика SKU делится на одну штуку и даёт дичь: у 216421567
            # в июле продана 1 шт, а логистики списано 1542 ₽ → «маржа −67 %» на товаре, который
            # реально приносит ~+380 ₽ (август: qty 1, логистика 201, чистая 378).
            # Ниже порога и при выбросе > 3× медианы — считаем по медиане каталога.
            log_u, stor_u, acc_u = _rates_u(nm, s, q)
            net_u_act = (_f(s["net_profit"])/q) if q else None
            margin_act = (100*_f(s["net_profit"])/rw) if rw else None      # от реализации (после СПП)
            # маржа месяца от НАШЕЙ промо-цены = прибыль / выручка-до-СПП (revenue_buyer).
            # qty у наборов раздут компонентами, но здесь ÷qty сокращается → чисто и для наборов.
            margin_own_act = (100*_f(s["net_profit"])/rb) if rb else None
            sold_flag = True
        else:  # не продавался в опорном месяце: свои трейлинг-ставки, иначе медиана каталога
            spp = SPP_M
            log_u, stor_u, acc_u = _rates_u(nm, None, 0)
            q = net_u_act = margin_act = margin_own_act = None
            sold_flag = False

        # payout-ratio: per-SKU из трейлинг-факта (к_перечисл/база, СПП-независим) → фолбэк медиана
        t = trail.get(nm)
        tb = _f(t["base"]) if t else None
        # ГЕЙТ ТОНКОЙ ВЫБОРКИ (07.08.2026). payout по ОДНОЙ проданной штуке — шум, а не ставка:
        # у 192567526 единственная июльская продажа дала payout 0.381 при предметном 0.609, и
        # витрина нарисовала −2 % там, где товар в норме. Симметрично и опаснее: из 1130 тонких
        # SKU у 559 payout ВЫШЕ предметного — это 69 тыс ₽ ЗАВЫШЕННОЙ маржи. Ниже порога
        # берём предметную медиану; свои trail_* при этом сохраняем как справку.
        if tb and tb > 0 and (_f(t["qty"]) or 0) < MIN_QTY_FACT:
            tb = None
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
            # built_at ставим явно: у таблицы это DEFAULT now(), а апсерт дефолт не трогает —
            # без этого штамп остаётся датой ПЕРВОЙ вставки строки и врёт про свежесть витрины.
            "built_at": datetime.now(timezone.utc),
        })

    db.upsert("mkt_sku_economics", recs, conflict_cols=["account", "nm_id"])
    with_cogs = sum(1 for r in recs if r["cogs_u"] is not None)
    print(f"[sku_economics {account}] строк {len(recs)}, с себестом {with_cogs} "
          f"({100*with_cogs//max(len(recs),1)}%), без себеста {n_no_cogs}; продано-факт {len(sold)}")
    return len(recs)


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "wb_acc1"
    per = sys.argv[2] if len(sys.argv) > 2 else None
    build(acc, per)
