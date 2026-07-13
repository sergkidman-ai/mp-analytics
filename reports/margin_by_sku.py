"""reports/margin_by_sku.py — витрина маржи WB. МОДЕЛЬ «ПЕРИОД = ДАТА ФОРМИРОВАНИЯ ОТЧЁТА».

Один недельный отчёт ВБ (realizationreport_id) целиком падает в месяц своего create_dt
(согласованная модель 06-29; подтверждена эталоном: янв–апр −0.8..−1.9%). Деньги агрегируются
из raw_wb_report по месяцу формирования той же логикой Продажа/Возврат, что collectors/wb.py.

COGS:
  FBS — готовый себест отгрузки МС из ms_demand_cogs (report/stock/byoperation, FIFO на moment
        документа; см. collectors/ms_demand_cogs.py), матч assembly_id=demand_name НАПРЯМУЮ.
        Мульти-nm отгрузка делится по nm пропорционально штукам.
  FBO/непокрытое — цепочка фолбэков (по приоритету):
        1) cpu этого месяца (из матчей FBS текущей сборки)
        2) cpu истории nm (margin_by_sku прошлых месяцев)
        3) группа cost_seb по артикулу; 4) по префиксу (5зн→4, 6зн→5/4)
        5) состав набора (set_cost); 6) свежая закупочная (ms_product.buy_price по группе)
        7) ручной себест (cogs_manual, диктует клиент)

net_profit = to_pay − logistics − storage − acceptance − other − COGS. Деньги — якорь (не штуки).
Запуск:  ./venv/bin/python reports/margin_by_sku.py [wb_acc1 [2026-01-01 2026-01-31]]
"""
import os
import re
import sys
import time
import pathlib
from collections import defaultdict

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

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


def demand_cogs_from_cache(account):
    """{demand_name(assembly_id): cogs} — готовый себест отгрузки из кэша ms_demand_cogs."""
    org_name = ACC_ORG.get(account, ACC_ORG["wb_acc1"])
    org_href = _ms("entity/organization", {"filter": f"name={org_name}", "limit": 1})["rows"][0]["meta"]["href"]
    org_id = _href_id(org_href)
    rows = db.query("SELECT demand_name, cogs FROM ms_demand_cogs WHERE org=%s", (org_id,))
    return {r["demand_name"]: float(r["cogs"] or 0) for r in rows}


def _fallback_sources(account):
    """Справочники для цепочки фолбэков (грузим один раз на сборку)."""
    cpu_hist = {r["article"]: float(r["u"]) for r in db.query("""
        SELECT DISTINCT ON (article) article, cogs/nullif(qty,0) u FROM margin_by_sku
        WHERE platform='wb' AND account=%s AND cogs>0 AND qty>0
        ORDER BY article, period_from DESC""", (account,))}
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


def _chain_cpu(nm, sa, cpu_hist, grp, setc, buy, manual):
    """Себест/шт по цепочке фолбэков (шаги 2–7). None если не нашлось нигде."""
    if nm in cpu_hist:
        return cpu_hist[nm], "cpu_hist"
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
    cogs_order = demand_cogs_from_cache(account)

    # Деньги по nm из отчётов месяца формирования (семантика = collectors/wb.normalize_sales).
    raw = db.query("""
        SELECT payload->>'nm_id' nm, payload->>'sa_name' sa,
               payload->>'supplier_oper_name' op, payload->>'assembly_id' aid,
               coalesce((payload->>'quantity')::numeric,0) q,
               coalesce((payload->>'retail_price_withdisc_rub')::numeric,0) rpw,
               coalesce((payload->>'retail_amount')::numeric,0) ra,
               coalesce((payload->>'ppvz_for_pay')::numeric,0) pay,
               coalesce((payload->>'delivery_rub')::numeric,0) del,
               coalesce((payload->>'storage_fee')::numeric,0) st,
               coalesce((payload->>'acceptance')::numeric,0) acc,
               coalesce((payload->>'deduction')::numeric,0)+coalesce((payload->>'penalty')::numeric,0) oth
        FROM raw_wb_report
        WHERE account=%s AND to_char((payload->>'create_dt')::date,'YYYY-MM')=%s""",
                   (account, ym))
    if not raw:
        print(f"  нет отчётов с формированием в {ym} — пропуск", flush=True)
        return

    money = defaultdict(lambda: defaultdict(float))
    sa_of = {}
    asm = defaultdict(lambda: defaultdict(float))   # aid -> nm -> qty (только Продажа, FBS)
    for r in money_rows_iter(raw):
        nm, a = r["nm"], money[r["nm"]]
        sa_of.setdefault(nm, r["sa"])
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
        a["to_pay"] += r["pay"]
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
        c = cogs_order.get(aid) or None
        for nm, q in nms.items():
            if c is not None and tot_q > 0:
                mc[nm] += c * q / tot_q
                mu[nm] += q
            else:
                unmatched_units[nm] += q

    cpu_hist, grp, setc, buy, manual = _fallback_sources(account)
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
                u, src = _chain_cpu(nm, sa_of.get(nm), cpu_hist, grp, setc, buy, manual)
                if u is not None:
                    cogs += u * rest
                    cov[src] += rest
                else:
                    cov["нет"] += rest
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
    print(f"  записано {len(recs)} nm_id за {ym}", flush=True)


def money_rows_iter(raw):
    """Нормализация типов строк денег (Decimal→float) — единая точка."""
    for r in raw:
        yield {"nm": r["nm"], "sa": r["sa"], "op": r["op"], "aid": r["aid"],
               "q": float(r["q"]), "rpw": float(r["rpw"]), "ra": float(r["ra"]),
               "pay": float(r["pay"]), "del": float(r["del"]), "st": float(r["st"]),
               "acc": float(r["acc"]), "oth": float(r["oth"])}


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "wb_acc1"
    if len(sys.argv) > 3:
        build(acc, sys.argv[2], sys.argv[3])
    else:
        build(acc)
