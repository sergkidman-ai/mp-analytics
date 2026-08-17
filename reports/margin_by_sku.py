"""reports/margin_by_sku.py — витрина маржи WB. МОДЕЛЬ «ПЕРИОД = ДАТА ФОРМИРОВАНИЯ ОТЧЁТА».

Один недельный отчёт ВБ (realizationreport_id) целиком падает в месяц своего create_dt
(согласованная модель 06-29; подтверждена эталоном: янв–апр −0.8..−1.9%). Деньги агрегируются
из raw_wb_report по месяцу формирования той же логикой Продажа/Возврат, что collectors/wb.py.

COGS:
  FBS — готовый себест отгрузки МС из ms_demand_cogs (report/stock/byoperation, FIFO на moment
        документа; см. collectors/ms_demand_cogs.py), матч assembly_id=demand_name НАПРЯМУЮ.
        Мульти-nm отгрузка делится по nm пропорционально штукам.
  FBO/непокрытое — цепочка фолбэков (по приоритету). Первые четыре шага — производные ФАКТА
        FIFO (тот же товар, просто другая продажа), дальше идут суррогаты:
        1) cpu этого месяца (из матчей FBS текущей сборки)
        2) cpu истории nm (margin_by_sku прошлых месяцев)
        3) FIFO ТОВАРА МС на дату продажи — ближайшая предшествующая отгрузка того же товара
           на любой площадке (reports/fifo_fallback.py)
        4) набор: Σ FIFO компонентов состава (set_cost.components), только при полном покрытии
        5) группа cost_seb по артикулу; 6) по префиксу (5зн→4, 6зн→5/4)
        7) состав набора по закупочным (set_cost.cost); 8) свежая закупочная (ms_product.buy_price)
        9) ручной себест (cogs_manual, диктует клиент)

net_profit = to_pay − logistics − storage − acceptance − other − COGS. Деньги — якорь (не штуки).
Запуск:  ./venv/bin/python reports/margin_by_sku.py [wb_acc1 [2026-01-01 2026-01-31]]
"""
import os
import re
import sys
import time
import datetime
import pathlib
from collections import defaultdict

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from reports import fifo_fallback  # noqa: E402  (FIFO товара МС — шаги 3–4 цепочки)

load_dotenv(BASE_DIR / ".env")
MS_TOK = os.getenv("MOYSKLAD_TOKEN")
MS = "https://api.moysklad.ru/api/remap/1.2"
H = {"Authorization": f"Bearer {MS_TOK}", "Accept-Encoding": "gzip",
     "Accept": "application/json;charset=utf-8"}

# Юрлицо МойСклад по WB-аккаунту (COGS стыкуется по org+agent «Покупатель ВБ»).
ACC_ORG = {"wb_acc1": 'ООО "ЦИФРОВОЙ КВАДРАТ"', "wb_acc2": 'ООО "ДИСКВЭР"'}


def _ms(path, params=None):
    r = requests.get(f"{MS}/{path}", headers=H, params=params, timeout=60)
    time.sleep(0.25)
    return r.json()


def _href_id(href):
    return href.rstrip("/").split("/")[-1]


def _org_id(account):
    """id организации МС по WB-аккаунту (один резолв на сборку)."""
    org_name = ACC_ORG.get(account, ACC_ORG["wb_acc1"])
    org_href = _ms("entity/organization", {"filter": f"name={org_name}", "limit": 1})["rows"][0]["meta"]["href"]
    return _href_id(org_href)


def demand_cogs_from_cache(org_id):
    """{demand_name(assembly_id): (cogs, qty)} — себест и штуки отгрузки из кэша ms_demand_cogs."""
    rows = db.query("""SELECT demand_name, cogs, qty FROM ms_demand_cogs
        WHERE org=%s AND agent='Покупатель ВБ'""", (org_id,))
    return {r["demand_name"]: (float(r["cogs"] or 0), float(r["qty"] or 0)) for r in rows}


def sold_units_by_demand(account):
    """{assembly_id: штук ПРОДАНО по отчётам ВБ за всю историю} — знаменатель себеста/шт при сторно.

    Начисление себеста делит cogs документа на штуки ВБ (`c * q / tot_q` ниже), поэтому и сторно
    обязано делить на них же. Штуки МС (`ms_demand_cogs.qty`) для наборов больше: набор = 1 позиция
    ВБ = 4–5 строк компонентов в МС, и деление на них занижало сторно в 4–5 раз (159 возвратов,
    недо-сторно 145 тыс ₽ за окт-2025…авг-2026; правка по решению Сергея 2026-08-17).
    Всю историю, а не месяц: возврат почти всегда приходит позже месяца продажи.
    """
    return {r["aid"]: float(r["q"]) for r in db.query("""
        SELECT payload->>'assembly_id' aid,
               sum(coalesce((payload->>'quantity')::numeric,0)) q
          FROM raw_wb_report
         WHERE account=%s AND payload->>'supplier_oper_name'='Продажа'
           AND coalesce(payload->>'assembly_id','0') <> '0'
         GROUP BY 1 HAVING sum(coalesce((payload->>'quantity')::numeric,0)) > 0""", (account,))}


def return_sellable_qty(org_id):
    """{assembly_id(demand_name): Σ ret_qty} возвратов в ПРОДАВАЕМЫЙ сток по данным МойСклада.
    БОЛЬШЕ НЕ ГЕЙТ для сторно COGS (решение Сергея 2026-08-13, см. storno_budget) — оставлена как
    справочная: раздел «Себестоимость» показывает по ней брак отдельным убытком.
    Источник — collectors/ms_return_cogs."""
    return {r["demand_name"]: float(r["q"] or 0) for r in db.query(
        """SELECT demand_name, sum(ret_qty) q FROM ms_return_cogs
           WHERE org=%s AND sellable AND demand_name IS NOT NULL GROUP BY demand_name""",
        (org_id,))}


def storno_budget(account, ym):
    """{assembly_id: штук, по которым выручка начислена и ещё НЕ сторнирована к началу месяца ym}.

    Решение Сергея 2026-08-13: в отчётах МП себестоимость считается ТОЛЬКО по реализованному
    покупателю товару, а критерий — факт начисления выручки в САМОМ отчёте. Поэтому бюджет сторно
    больше не берётся из МойСклада (`return_sellable_qty`) и не режется складом «Брак»: ВБ
    сторнировал выручку → сторнируем себест, независимо от того, куда приехал товар и провели ли
    возврат в МС. Потери от брака живут в разделе «Себестоимость», а не здесь.

    Бюджет = продано штук по этой отгрузке во ВСЕХ отчётах − возвращено в отчётах ДО ym. Он нужен
    только против двойного сторно (один возврат отражён в двух отчётных месяцах) и против сторно
    того, что никогда не начислялось; внутри месяца добирается через storno_used.
    """
    return {r["aid"]: float(r["b"]) for r in db.query("""
        WITH s AS (
            SELECT payload->>'assembly_id' aid,
                   sum(CASE WHEN payload->>'supplier_oper_name'='Продажа'
                            THEN coalesce((payload->>'quantity')::numeric,0) ELSE 0 END) sold,
                   sum(CASE WHEN payload->>'supplier_oper_name'='Возврат'
                             AND to_char((payload->>'create_dt')::date,'YYYY-MM') < %s
                            THEN coalesce((payload->>'quantity')::numeric,0) ELSE 0 END) ret_before
            FROM raw_wb_report
            WHERE account=%s AND coalesce(payload->>'assembly_id','0') <> '0'
            GROUP BY 1)
        SELECT aid, greatest(sold - ret_before, 0) b FROM s WHERE sold > 0""", (ym, account))}


def _fallback_sources(account, before=None):
    """Справочники для цепочки фолбэков (грузим один раз на сборку).

    before — первый день собираемого месяца: история себеста берётся ТОЛЬКО из месяцев РАНЬШЕ него
    (правило Сергея 2026-08-14: себест не может быть позже отгрузки). Без этого пересборка января
    подставляла себест из самого свежего месяца витрины, то есть цену из будущего."""
    # cpu истории — ТОЛЬКО по nm, у которых была своя FBS-отгрузка с FIFO. Иначе шаг
    # самозакрепляет суррогат: nm без единого FIFO получил себест из cost_seb, тот лёг в витрину,
    # а на следующей сборке вернулся сюда как «история» и навсегда закрыл дорогу шагам 3–4.
    cpu_hist = {r["article"]: float(r["u"]) for r in db.query("""
        WITH fifo_nm AS (
            SELECT DISTINCT w.payload->>'nm_id' nm FROM raw_wb_report w
            JOIN ms_demand_cogs d ON d.demand_name = w.payload->>'assembly_id'
            WHERE w.account=%s AND coalesce(d.cogs,0) > 0)
        SELECT DISTINCT ON (article) article, cogs/nullif(qty,0) u FROM margin_by_sku
        WHERE platform='wb' AND account=%s AND cogs>0 AND qty>0
          AND article IN (SELECT nm FROM fifo_nm)
          AND (%s::date IS NULL OR period_from < %s::date)
        ORDER BY article, period_from DESC""", (account, account, before, before))}
    grp = {}
    for r in db.query("""
        SELECT external_code, min(cost_seb) FILTER (WHERE cost_seb>0) mn,
               max(cost_seb) FILTER (WHERE cost_seb>0) mx,
               bool_or(title ILIKE '%%набор%%' OR title ILIKE '%%комплект%%') is_set
        FROM products WHERE external_code IS NOT NULL GROUP BY external_code"""):
        if r["mn"] or r["mx"]:
            grp[r["external_code"]] = (float(r["mn"] or 0), float(r["mx"] or 0), r["is_set"])
    setc = {r["external_code"]: float(r["cost"]) for r in db.query(
        "SELECT external_code, cost FROM set_cost WHERE covered=n_components AND cost>0")}
    buy = {r["external_code"]: float(r["mn"]) for r in db.query("""
        SELECT external_code, min(buy_price) FILTER (WHERE buy_price>0) mn
        FROM ms_product WHERE external_code IS NOT NULL GROUP BY external_code HAVING
        min(buy_price) FILTER (WHERE buy_price>0) IS NOT NULL""")}
    manual = {r["article"]: float(r["unit_cost"]) for r in db.query(
        "SELECT article, unit_cost FROM cogs_manual WHERE platform='wb'")}
    return cpu_hist, grp, setc, buy, manual


def _grp_cost(g):
    mn, mx, is_set = g
    return (mx if (is_set and mx) else (mn or None)) or None


def _chain_cpu(nm, sa, cpu_hist, grp, setc, buy, manual, ff=None, day=None):
    """Себест/шт по цепочке фолбэков (шаги 2–9). None если не нашлось нигде.

    ff/day — FIFO товара МС на дату продажи (reports/fifo_fallback.py): шаги 3–4, идут ПЕРЕД
    суррогатами из карточки (cost_seb), потому что это факт списания того же товара."""
    if nm in cpu_hist:
        return cpu_hist[nm], "cpu_hist"
    if ff is not None and day is not None:
        u, src = ff.unit(sa, day)
        if u:
            return u, src
    # Группа МС = ведущие цифры артикула (правило клиента): Цифровой «07772»=0777+2,
    # Дисквэр «3212wqfn7m9y»=3212+случайный хвост. Пробуем полный артикул, потом 5, потом 4 цифры.
    keys = [sa] if sa else []
    m = re.match(r"^(\d{4,6})", sa or "")
    if m:
        digits = m.group(1)
        if len(digits) >= 5:
            keys.append(digits[:5])
        keys.append(digits[:4])
    for k in keys:
        if k in grp:
            u = _grp_cost(grp[k])
            if u:
                return u, "grp"
    for k in keys:
        if k in setc:
            return setc[k], "set"
    for k in keys:
        if k in buy:
            return buy[k], "buy"
    if nm in manual:
        return manual[nm], "manual"
    return None, "нет"


def build(account="wb_acc1", date_from="2026-05-01", date_to="2026-05-31"):
    """Витрина за МЕСЯЦ ФОРМИРОВАНИЯ = месяц date_from (ключи периода — границы месяца)."""
    ym = date_from[:7]
    print(f"Витрина маржи {account} {ym} (по формированию)…", flush=True)
    org_id = _org_id(account)
    cogs_order = demand_cogs_from_cache(org_id)       # {aid: (cogs, qty)}
    sell_budget = storno_budget(account, ym)          # {aid: штук с начисленной и не сторнированной выручкой}
    sold_units = sold_units_by_demand(account)        # {aid: штук ВБ} — знаменатель себеста/шт сторно

    # Деньги по nm из отчётов месяца формирования (семантика = collectors/wb.normalize_sales).
    raw = db.query("""
        SELECT payload->>'nm_id' nm, payload->>'sa_name' sa,
               payload->>'supplier_oper_name' op, payload->>'assembly_id' aid,
               payload->>'order_dt' ord_dt,
               coalesce((payload->>'quantity')::numeric,0) q,
               coalesce((payload->>'retail_price_withdisc_rub')::numeric,0) rpw,
               coalesce((payload->>'retail_amount')::numeric,0) ra,
               coalesce((payload->>'ppvz_for_pay')::numeric,0) pay,
               coalesce((payload->>'delivery_rub')::numeric,0) del,
               coalesce((payload->>'storage_fee')::numeric,0) st,
               coalesce((payload->>'acceptance')::numeric,0) acc,
               coalesce((payload->>'deduction')::numeric,0)+coalesce((payload->>'penalty')::numeric,0)+coalesce((payload->>'cashback_amount')::numeric,0) oth
        FROM raw_wb_report
        WHERE account=%s AND to_char((payload->>'create_dt')::date,'YYYY-MM')=%s""",
                   (account, ym))
    if not raw:
        print(f"  нет отчётов с формированием в {ym} — пропуск", flush=True)
        return

    money = defaultdict(lambda: defaultdict(float))
    sa_of = {}
    asm = defaultdict(lambda: defaultdict(float))   # aid -> nm -> qty (только Продажа, FBS)
    storno = defaultdict(float)                      # nm -> сторно COGS возвратов в сток (месяц возврата)
    storno_used = defaultdict(float)                 # aid -> уже сторнировано штук (лимит = sell_budget)
    storno_fb = defaultdict(float)                   # источник фолбэка -> штук сторно без моста к отгрузке
    cpu_hist, grp, setc, buy, manual = _fallback_sources(account, date_from)
    ff = fifo_fallback.load()                        # FIFO товара МС (шаги 3–4 цепочки)
    # Опорная дата фолбэка FIFO — ДЕНЬ ЗАКАЗА строки (order_dt), а не конец месяца
    # (решение Сергея 16.08.2026). Конец месяца брал последнюю отгрузку товара ПОСЛЕ продажи,
    # то есть себест из будущего; правильный ориентир — цена списания на день, когда товар продан.
    month_end = datetime.date.fromisoformat(date_to)

    def _day(s):
        """Дата заказа строки; если ВБ её не дал — конец периода (прежнее поведение)."""
        try:
            return datetime.date.fromisoformat(s) if s else month_end
        except ValueError:
            return month_end

    last_ord = {}                                    # nm -> день последнего заказа в периоде
    for r in money_rows_iter(raw):
        nm, a = r["nm"], money[r["nm"]]
        sa_of.setdefault(nm, r["sa"])
        d = _day(r["ord"])
        if r["op"] == "Продажа" and (nm not in last_ord or d > last_ord[nm]):
            last_ord[nm] = d
        if r["op"] == "Продажа":
            a["qty"] += r["q"]
            a["revenue_buyer"] += r["rpw"]
            a["commission"] += r["ra"] - r["pay"]
            if r["aid"] and r["aid"] != "0":
                asm[r["aid"]][nm] += r["q"]
        elif r["op"] == "Возврат":
            a["qty"] -= r["q"]
            a["returns_sum"] += r["ra"]
            a["revenue_buyer"] -= r["rpw"]
            a["commission"] -= (r["ra"] - r["pay"])
            # Сторно COGS: ВБ сторнировал выручку по этой отгрузке — значит товар покупателю не
            # реализован, и его себест уходит из расходов отчёта. Ни склад возврата, ни наличие
            # документа возврата в МС роли не играют (решение Сергея 2026-08-13): критерий —
            # факт сторно выручки в самом отчёте. Себест/шт = cogs/qty исходной отгрузки (метод B),
            # при отсутствии моста к отгрузке — фолбэк по истории SKU (_chain_cpu). Бюджет
            # storno_budget защищает только от двойного сторно и от сторно неначисленного.
            # Период — месяц ФОРМИРОВАНИЯ WB-строки Возврат (= месяц денежного реверса, метод A);
            # НЕ месяц МС-документа (он отстаёт на 1–3 мес — сверено).
            aid = r["aid"]
            if aid and aid != "0":
                take = min(r["q"], max(0.0, sell_budget.get(aid, 0.0) - storno_used[aid]))
                cq = cogs_order.get(aid)
                # Знаменатель — штуки ВБ этой отгрузки (как при начислении), НЕ штуки МС:
                # набор = 1 позиция ВБ = несколько строк в МС (см. sold_units_by_demand).
                den = sold_units.get(aid) or (cq[1] if cq else 0)
                if take > 0:
                    if cq and cq[0] and den > 0:
                        storno[nm] += cq[0] / den * take
                        storno_used[aid] += take
                    else:
                        u, src = _chain_cpu(nm, r["sa"], cpu_hist, grp, setc, buy, manual, ff, d)
                        if u is not None:
                            storno[nm] += u * take
                            storno_used[aid] += take
                            storno_fb[src] += take
        # «Возврат» в сырье ВБ лежит с ПОЛОЖИТЕЛЬНЫМ ppvz_for_pay — вычитаем,
        # иначе «К перечислению» и чистая завышаются на 2× сумму возвратов.
        a["to_pay"] += (-r["pay"] if r["op"] == "Возврат" else r["pay"])
        a["logistics"] += r["del"]
        a["storage"] += r["st"]
        a["acceptance"] += r["acc"]
        a["other"] += r["oth"]

    # COGS FBS: себест отгрузки на nm пропорционально штукам внутри отгрузки.
    mc = defaultdict(float)    # nm -> matched cogs
    mu = defaultdict(float)    # nm -> matched units
    unmatched_units = defaultdict(float)
    for aid, nms in asm.items():
        tot_q = sum(nms.values())
        # нулевой себест в кэше = МС не знает цену отгрузки → считаем НЕсматченным,
        # иначе ноль проходит как «покрыто» и завышает чистую (слепое пятно метрики)
        cq = cogs_order.get(aid)
        c = (cq[0] if cq else 0) or None
        for nm, q in nms.items():
            if c is not None and tot_q > 0:
                mc[nm] += c * q / tot_q
                mu[nm] += q
            else:
                unmatched_units[nm] += q

    cov = defaultdict(float)
    recs = []
    for nm, a in money.items():
        qty = a["qty"]
        cogs = mc.get(nm, 0.0)
        cov["exact"] += mu.get(nm, 0.0)
        # непокрытые FBS-штуки + FBO-штуки (всё, что продано сверх matched; qty уже нетто)
        rest = max(0.0, qty - mu.get(nm, 0.0))
        if rest > 0:
            if mu.get(nm, 0) > 0:                      # 1) cpu этого месяца
                cogs += (mc[nm] / mu[nm]) * rest
                cov["cpu_month"] += rest
            else:
                u, src = _chain_cpu(nm, sa_of.get(nm), cpu_hist, grp, setc, buy, manual, ff,
                                    last_ord.get(nm, month_end))
                if u is not None:
                    cogs += u * rest
                    cov[src] += rest
                else:
                    cov["нет"] += rest
        # Сторно себеста вернувшегося в сток товара (месяц возврата). Может увести cogs в минус,
        # если исходная продажа была в другом месяце — это корректный кредит себеста в месяц возврата.
        cogs -= storno.get(nm, 0.0)
        rev = a["revenue_buyer"]
        net = a["to_pay"] - a["logistics"] - a["storage"] - a["acceptance"] - a["other"] - cogs
        recs.append({
            "article": nm, "platform": "wb", "account": account,
            "period_from": date_from, "period_to": date_to,
            "qty": qty, "revenue_buyer": rev, "cogs": cogs,
            "commission": a["commission"], "logistics": a["logistics"],
            "returns_sum": a["returns_sum"], "storage": a["storage"],
            "acceptance": a["acceptance"], "other": a["other"],
            "net_profit": net, "margin_pct": (net / rev * 100) if rev else None,
            "commission_pct": (a["commission"] / rev * 100) if rev else None,
        })
    # период пересобирается целиком: старые nm, исчезнувшие из отчётов, не должны залипать
    db.execute("""DELETE FROM margin_by_sku WHERE platform='wb' AND account=%s
                  AND period_from=%s AND period_to=%s""", (account, date_from, date_to))
    db.upsert("margin_by_sku", recs, conflict_cols=[
        "article", "platform", "account", "period_from", "period_to"])

    tot = sum(cov.values()) or 1
    covered = tot - cov.get("нет", 0)
    detail = ", ".join(f"{k} {v:.0f}" for k, v in sorted(cov.items(), key=lambda x: -x[1]) if v)
    print(f"  COGS-покрытие: {covered/tot*100:.1f}% из {tot:.0f} шт ({detail})", flush=True)
    st_sum = sum(storno.values())
    if st_sum:
        fb = ""
        if storno_fb:
            fb = " | без моста к отгрузке: " + ", ".join(f"{k} {v:.0f} шт" for k, v in storno_fb.items())
        print(f"  сторно COGS возвратов: −{st_sum:,.0f} ₽ по {sum(1 for v in storno.values() if v)} nm{fb}"
              .replace(",", " "), flush=True)
    print(f"  записано {len(recs)} nm_id за {ym}", flush=True)


def money_rows_iter(raw):
    """Нормализация типов строк денег (Decimal→float) — единая точка."""
    for r in raw:
        yield {"nm": r["nm"], "sa": r["sa"], "op": r["op"], "aid": r["aid"],
               "ord": (r["ord_dt"] or "")[:10],
               "q": float(r["q"]), "rpw": float(r["rpw"]), "ra": float(r["ra"]),
               "pay": float(r["pay"]), "del": float(r["del"]), "st": float(r["st"]),
               "acc": float(r["acc"]), "oth": float(r["oth"])}


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "wb_acc1"
    if len(sys.argv) > 3:
        build(acc, sys.argv[2], sys.argv[3])
    else:
        build(acc)
