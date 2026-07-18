"""collectors/ms_return_cogs.py — возвраты покупателей МойСклад (entity/salesreturn) для сторно COGS.

Возврат покупателя оформляется документом salesreturn. У него есть ссылка `demand` на ИСХОДНУЮ
отгрузку; `demand.name` — надёжный мост к себесту (ms_demand_cogs) и к товару:
  * WB:   demand.name = assembly_id (мост к nm через raw_wb_report). Сам salesreturn.name совпадает
          с assembly_id лишь ~26% → ключуемся ТОЛЬКО по demand.name (expand=demand).
  * Ozon: demand.name = posting_number отгрузки (первые 2 сегмента = ключ витрины margin_ozon_sku).

Склад назначения делит судьбу товара (сторнируем COGS только для продаваемого стока):
  * WB   не-сток = {«Брак»};                    всё прочее (Звездный/Дисквер/Кантемировская) — сток.
  * Ozon не-сток = {«Брак», «Озон»};            «Озон» = товар, удержанный на стороне Озона (не наш
    сток, в наших отгрузках почти не встречается → задвоения COGS нет). Сток = Звездный/Дисквер/
    Кантемировская.
fail-closed: нет склада / склад не развернулся → НЕ сток (сторно не применяем).

Пишет в ms_return_cogs (миграция 044) идемпотентно по return_id. unit_cogs/storno_cogs — МС-оценка
для аудита; фактический вычет считают витрины (margin_by_sku для WB, margin_ozon_sku для Ozon) по
своим строкам возврата в месяце возврата, беря отсюда только sellable-гейт (posting/assembly → склад).

⚠ МС-грабли: `expand` на list-эндпоинте молча отключается при limit>100 → пагинируем по limit=100.

Запуск:  ./venv/bin/python -m collectors.ms_return_cogs                       # оба юрлица WB
         ./venv/bin/python -m collectors.ms_return_cogs ozon oz_acc1 oz_acc2  # Ozon
"""
import sys
import urllib.parse
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
# переиспользуем МС-клиент коллектора отгрузок (throttle + 429/5xx-ретраи), резолверы и конфиг платформ
from collectors.ms_demand_cogs import (  # noqa: E402
    get, _resolve_href, _hid, _norm_posting, PLATFORM)

# не-сток склады по платформе (fail-closed по неизвестному складу)
NON_STOCK = {
    "wb": {"Брак"},
    "ozon": {"Брак", "Озон"},   # «Озон» = удержано на стороне Озона, не наш продаваемый сток
}


def _positions_qty(d):
    """Σ quantity позиций возврата (positions развёрнуты через expand=positions.assortment)."""
    pos = d.get("positions") or {}
    rows = pos.get("rows", []) if isinstance(pos, dict) else []
    return sum(p.get("quantity", 0) or 0 for p in rows)


def list_salesreturns(org_href, agent_href, moment_from):
    """[salesreturn dict] c развёрнутыми demand/store/positions. Пагинация limit=100 —
    иначе МС молча игнорирует expand."""
    flt = urllib.parse.quote(
        f"organization={org_href};agent={agent_href};moment>={moment_from} 00:00:00")
    out, offset = [], 0
    while True:
        j = get(f"/entity/salesreturn?limit=100&offset={offset}"
                f"&filter={flt}&expand=demand,store,positions.assortment")
        rows = j.get("rows", [])
        out += rows
        offset += 100
        if not rows or offset >= j.get("meta", {}).get("size", 0):
            break
    return out


def _unit_cogs_map(org_id, agents, platform):
    """{ключ: себест/шт исходной отгрузки} из кэша ms_demand_cogs — МС-оценка для аудита.
    Ключ: WB — demand_name(assembly_id); Ozon — norm_posting(demand_name) (агрегируем по норме)."""
    rows = db.query("""SELECT demand_name, cogs, qty FROM ms_demand_cogs
                       WHERE org=%s AND agent = ANY(%s)""", (org_id, agents))
    if platform == "ozon":
        acc = {}
        for r in rows:
            k = _norm_posting(r["demand_name"])
            c, q = acc.get(k, (0.0, 0.0))
            acc[k] = (c + float(r["cogs"] or 0), q + float(r["qty"] or 0))
        return {k: c / q for k, (c, q) in acc.items() if q > 0}
    unit = {}
    for r in rows:
        c, q = float(r["cogs"] or 0), float(r["qty"] or 0)
        if q > 0:
            unit[r["demand_name"]] = c / q
    return unit


def _unit_key(dem, platform):
    return _norm_posting(dem) if platform == "ozon" else dem


def collect(account="wb_acc1", platform="wb", moment_from="2025-09-01"):
    config = PLATFORM[platform]
    org_name = config["org_map"][account]
    agents = config["agents"]
    non_stock = NON_STOCK[platform]
    print(f"[{account}/{platform}] возвраты МС: юрлицо {org_name}; агенты {agents}; "
          f"резолв org…", flush=True)
    org_href = _resolve_href("organization", org_name)
    org_id = _hid(org_href)
    unit = _unit_cogs_map(org_id, agents, platform)

    buf = []
    stores, n_sell, n_defect, n_nocost = {}, 0, 0, 0
    for agent in agents:
        agent_href = _resolve_href("counterparty", agent)
        rows = list_salesreturns(org_href, agent_href, moment_from)
        print(f"[{account}] агент «{agent}»: salesreturn'ов (moment>={moment_from}): {len(rows)}",
              flush=True)
        for d in rows:
            store = ((d.get("store") or {}).get("name")) or None
            dem = (d.get("demand") or {}).get("name")
            sellable = bool(store) and store not in non_stock
            ret_qty = _positions_qty(d)
            u = unit.get(_unit_key(dem, platform)) if dem else None
            storno = (u * ret_qty) if (sellable and u is not None) else 0.0
            moment = d.get("moment")
            stores[store or "—"] = stores.get(store or "—", 0) + 1
            n_sell += sellable
            n_defect += (not sellable)
            if sellable and u is None:
                n_nocost += 1
            buf.append({
                "return_id": d.get("id"), "return_name": d.get("name"),
                "org": org_id, "agent": agent, "demand_name": dem,
                "moment": moment, "ym": (moment or "")[:7] or None,
                "store": store, "sellable": bool(sellable),
                "ret_qty": ret_qty, "unit_cogs": u,
                "storno_cogs": round(storno, 2),
            })
    if buf:
        db.upsert("ms_return_cogs", buf, conflict_cols=["return_id"])
    tot_storno = sum(b["storno_cogs"] for b in buf)
    print(f"[{account}] склады: {stores}", flush=True)
    print(f"[{account}] сток(сторно) {n_sell} | не-сток {n_defect} | "
          f"сток без себеста в кэше {n_nocost}", flush=True)
    print(f"[{account}] ГОТОВО: {len(buf)} возвратов, МС-оценка сторно {tot_storno:,.0f} ₽"
          .replace(",", " "), flush=True)
    return len(buf), tot_storno


def main():
    args = sys.argv[1:]
    if args and args[0] in PLATFORM:
        platform = args[0]
        accounts = args[1:] or (["wb_acc1", "wb_acc2"] if platform == "wb"
                                else ["oz_acc1", "oz_acc2"])
    else:
        platform, accounts = "wb", (args or ["wb_acc1", "wb_acc2"])
    for acc in accounts:
        collect(acc, platform=platform)


if __name__ == "__main__":
    main()
