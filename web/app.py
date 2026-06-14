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
def index():
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


@app.get("/api/summary")
def summary(platform: str = "", account: str = "", period: str = ""):
    """Большие цифры по выбранному срезу."""
    w, p = _where(platform, account, period)
    r = db.query(f"""SELECT count(*) n_sku,
        coalesce(sum(qty),0)::float qty,
        coalesce(sum(revenue_buyer),0)::float revenue,
        coalesce(sum(cogs),0)::float cogs,
        coalesce(sum(commission),0)::float commission,
        coalesce(sum(logistics),0)::float logistics,
        coalesce(sum(net_profit),0)::float net,
        coalesce(sum(CASE WHEN net_profit<0 AND qty>0 AND article<>'0' THEN 1 ELSE 0 END),0) loss_count
        FROM margin_by_sku{w}""", p)[0]
    r["margin_pct"] = round(r["net"] / r["revenue"] * 100, 1) if r["revenue"] else None
    return r


@app.get("/api/sku")
def sku(platform: str = "", account: str = "", period: str = "",
        problem: bool = False, sort: str = "revenue_buyer", order: str = "desc",
        q: str = "", limit: int = 300):
    """SKU-уровень (drill-down). problem=true → только убыточные. q → поиск по nm_id."""
    # nm_id='0' — служебный bucket WB (нераспределённая логистика), не SKU.
    # Без real-активности (qty>0 или выручка>0) — это возвраты/удержания по товарам других
    # периодов, не продажи; в SKU-список не показываем.
    extra = ("article<>'0' AND net_profit<0 AND qty>0" if problem
             else "article<>'0' AND (qty>0 OR revenue_buyer>0)")
    w, p = _where(platform, account, period, extra)
    if q:
        w = (w + " AND " if w else " WHERE ") + "article ILIKE %s"
        p.append(f"%{q}%")
    sort = sort if sort in ("net_profit", "revenue_buyer", "cogs", "qty", "margin_pct") else "net_profit"
    order = "DESC" if order.lower() == "desc" else "ASC"
    rows = db.query(f"""SELECT article,
        qty::float, revenue_buyer::float, cogs::float, commission::float,
        logistics::float, net_profit::float, round(margin_pct,1)::float margin_pct
        FROM margin_by_sku{w} ORDER BY {sort} {order} NULLS LAST LIMIT %s""", p + [limit])
    return {"rows": rows, "count": len(rows)}


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
    return {"captured_at": cap, "total": tot, "by_subject": by}
