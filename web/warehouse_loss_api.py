# поток: ev — раздел «Склад» дашборда: товар на пострадавших складах ВБ.
# Отдельным роутером, а не в web/app.py (4300 строк, территория fin).
# Витрина wb_wh_loss наполняется ops/wb_lost_stock.py (миграция 506).
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from core import db

router = APIRouter()
STATIC = Path(__file__).parent / "static"

GROUPS = [
    ("lost", "Уничтожены полностью", "🔥",
     "Товар считаем потерянным: склада нет, остаток в снимках заморожен."),
    ("damaged", "Серьёзно повреждены", "💥",
     "Не подтверждённая утрата: часть товара может быть цела. ВБ обещал показать "
     "пострадавший остаток отдельной колонкой «Отчёта по остаткам» — по ней и сверять."),
]


@router.get("/warehouse/loss", response_class=HTMLResponse)
def warehouse_loss_page():
    return (STATIC / "warehouse_loss.html").read_text(encoding="utf-8")


@router.get("/api/warehouse/loss")
def warehouse_loss():
    """Товар на пострадавших складах ВБ: группа → склад → позиции."""
    rows = db.query("""SELECT warehouse, wh_label, status, hit_date, snap_date, snap_how, account,
                              nm_id, vendor_code, qty, components, unit_cost, supply_date,
                              cost_total, vitrina_u, vitrina_src, built_at
                       FROM wb_wh_loss ORDER BY status, hit_date, warehouse,
                                                coalesce(cost_total, 0) DESC""")
    if not rows:
        return {"built_at": None, "groups": [], "total": {}}
    built = max(r["built_at"] for r in rows)
    by_wh = {}
    for r in rows:
        w = by_wh.setdefault(r["warehouse"], {
            "warehouse": r["warehouse"], "label": r["wh_label"], "status": r["status"],
            "hit_date": r["hit_date"], "snap_date": r["snap_date"], "snap_how": r["snap_how"],
            "qty": 0, "cost": 0.0, "vitrina": 0.0, "no_price_qty": 0, "items": []})
        w["qty"] += r["qty"]
        w["cost"] += float(r["cost_total"] or 0)
        w["vitrina"] += float(r["vitrina_u"] or 0) * r["qty"]
        if r["cost_total"] is None:
            w["no_price_qty"] += r["qty"]
        w["items"].append({k: r[k] for k in ("account", "nm_id", "vendor_code", "qty", "components",
                                             "unit_cost", "supply_date", "cost_total",
                                             "vitrina_u", "vitrina_src")})
    groups = []
    for status, title, icon, note in GROUPS:
        whs = [w for w in by_wh.values() if w["status"] == status]
        whs.sort(key=lambda w: -w["cost"])
        groups.append({"status": status, "title": title, "icon": icon, "note": note,
                       "qty": sum(w["qty"] for w in whs), "cost": sum(w["cost"] for w in whs),
                       "vitrina": sum(w["vitrina"] for w in whs),
                       "no_price_qty": sum(w["no_price_qty"] for w in whs),
                       "warehouses": whs})
    return {"built_at": built, "groups": groups,
            "total": {"qty": sum(g["qty"] for g in groups),
                      "cost": sum(g["cost"] for g in groups),
                      "vitrina": sum(g["vitrina"] for g in groups)}}
