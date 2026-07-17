"""collectors/ms_return_cogs.py — возвраты покупателей МойСклад (entity/salesreturn) для сторно COGS.

Возврат покупателя оформляется документом salesreturn. У него есть ссылка `demand` на ИСХОДНУЮ
отгрузку; `demand.name` = WB assembly_id — надёжный мост к себесту (ms_demand_cogs) и к nm
(raw_wb_report). Внимание: сам `salesreturn.name` совпадает с assembly_id лишь частично (~26%),
поэтому ключуемся ТОЛЬКО по demand.name (через expand=demand).

Склад назначения делит судьбу товара:
  * всё, КРОМЕ «Брак» (Звездный/Дисквер/Кантемировская …) — продаваемый сток → сторнируем COGS
    (иначе при перепродаже вернувшегося товара себест спишется дважды);
  * «Брак» — дефект → себест остаётся расходом (не сторнируем).

Пишет в ms_return_cogs (миграция 044) идемпотентно по return_id. unit_cogs/storno_cogs — МС-оценка
для аудита; фактический вычет считает витрина margin_by_sku по строкам «Возврат» raw_wb_report
(месяц возврата, per-nm), беря sellable-гейт и себест/шт отсюда и из кэша отгрузок.

⚠ МС-грабли: `expand` на list-эндпоинте молча отключается при limit>100 → пагинируем по limit=100.

Запуск:  ./venv/bin/python -m collectors.ms_return_cogs           # оба юрлица WB
"""
import sys
import urllib.parse
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
# переиспользуем МС-клиент коллектора отгрузок (throttle + 429/5xx-ретраи) и резолверы
from collectors.ms_demand_cogs import get, _resolve_href, _hid, ACC_ORG  # noqa: E402

AGENT = "Покупатель ВБ"
DEFECT_STORES = {"Брак"}          # единственный не-сток бакет; всё прочее — продаваемый сток


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


def collect(account="wb_acc1", moment_from="2025-09-01"):
    org_name = ACC_ORG[account]
    print(f"[{account}] возвраты МС: юрлицо {org_name}; резолв org/agent…", flush=True)
    org_href = _resolve_href("organization", org_name)
    org_id = _hid(org_href)
    agent_href = _resolve_href("counterparty", AGENT)

    # себест/шт исходных отгрузок из кэша (мост demand_name = assembly_id)
    unit = {}
    for r in db.query("""SELECT demand_name, cogs, qty FROM ms_demand_cogs
                         WHERE org=%s AND agent=%s""", (org_id, AGENT)):
        c, q = float(r["cogs"] or 0), float(r["qty"] or 0)
        if q > 0:
            unit[r["demand_name"]] = c / q

    rows = list_salesreturns(org_href, agent_href, moment_from)
    print(f"[{account}] salesreturn'ов в окне (moment>={moment_from}): {len(rows)}", flush=True)

    buf = []
    stores, n_sell, n_defect, n_nocost = {}, 0, 0, 0
    for d in rows:
        store = ((d.get("store") or {}).get("name")) or None
        dem = (d.get("demand") or {}).get("name")
        # fail-closed: сторнируем ТОЛЬКО при известном не-дефектном складе. Нет склада / склад не
        # развернулся → НЕ сток (сторно не применяем), чтобы дефект не протёк в сторно по умолчанию.
        sellable = bool(store) and store not in DEFECT_STORES
        ret_qty = _positions_qty(d)
        u = unit.get(dem)
        storno = (u * ret_qty) if (sellable and u is not None) else 0.0
        moment = d.get("moment")
        stores[store or "—"] = stores.get(store or "—", 0) + 1
        n_sell += sellable
        n_defect += (not sellable)
        if sellable and u is None:
            n_nocost += 1
        buf.append({
            "return_id": d.get("id"), "return_name": d.get("name"),
            "org": org_id, "agent": AGENT, "demand_name": dem,
            "moment": moment, "ym": (moment or "")[:7] or None,
            "store": store, "sellable": bool(sellable),
            "ret_qty": ret_qty, "unit_cogs": u,
            "storno_cogs": round(storno, 2),
        })
    if buf:
        db.upsert("ms_return_cogs", buf, conflict_cols=["return_id"])
    tot_storno = sum(b["storno_cogs"] for b in buf)
    print(f"[{account}] склады: {stores}", flush=True)
    print(f"[{account}] сток(сторно) {n_sell} | брак {n_defect} | "
          f"сток без себеста в кэше {n_nocost}", flush=True)
    print(f"[{account}] ГОТОВО: {len(buf)} возвратов, МС-оценка сторно {tot_storno:,.0f} ₽"
          .replace(",", " "), flush=True)
    return len(buf), tot_storno


def main():
    accounts = sys.argv[1:] or ["wb_acc1", "wb_acc2"]
    for acc in accounts:
        collect(acc)


if __name__ == "__main__":
    main()
