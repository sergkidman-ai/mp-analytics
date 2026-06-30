"""collectors/set_cost.py — себестоимость наборов = Σ закупочных компонентов.

Состав набора → thecartridge.ru POST /api/catalog/mix_data {external_code} (отдаёт список
external_code компонентов; для простого артикула — {"error":"not_mix"}). Цена компонента →
МойСклад (ms_product.buy_price, min по external_code). Себест набора = Σ цен компонентов.

Состав КЕШИРУЕТСЯ в set_cost (резолвим через API только новые наборы), цена пересчитывается
из МС каждый прогон без API. Кандидаты — наборы (по названию), заведённые на МП (ВБ/Озон).

Запуск:  ./venv/bin/python collectors/set_cost.py [--refresh-all]
"""
import os
import sys
import json
import time
import datetime
import urllib.request
import pathlib

from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
API = os.getenv("CARTRIDGE_API_URL", "https://thecartridge.ru/api/catalog/mix_data")
PAUSE = 0.15


def _mix(ec):
    """Состав набора: список external_code компонентов или None (не набор/ошибка)."""
    key = os.getenv("CARTRIDGE_API_KEY")
    body = json.dumps({"external_code": str(ec)}).encode()
    req = urllib.request.Request(API, data=body, method="POST", headers={
        "Api-Key": key, "Content-Type": "application/json"})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read())
            if isinstance(d, list) and d:
                return [str(x) for x in d]
            return None       # {"error":"not_mix"} или пусто
        except Exception:
            time.sleep(1)
    return None


def main(refresh_all=False):
    # 1) кандидаты: наборы (по названию), заведённые на ВБ или Озон
    cands = [r["ec"] for r in db.query("""
        SELECT DISTINCT p.external_code ec FROM ms_product p
        WHERE (p.name ILIKE '%набор%' OR p.name ILIKE '%комплект%') AND p.external_code IS NOT NULL
          AND (EXISTS(SELECT 1 FROM wb_cards w WHERE w.vendor_code=p.external_code)
            OR EXISTS(SELECT 1 FROM ozon_product o WHERE o.offer_id=p.external_code))""")]
    # 2) цена компонентов из МС (min buy_price по external_code)
    price = {r["external_code"]: float(r["bp"]) for r in db.query(
        "SELECT external_code, min(buy_price) bp FROM ms_product WHERE buy_price>0 AND external_code IS NOT NULL GROUP BY external_code")}
    # 3) кешированный состав
    cached = {r["external_code"]: r["components"] for r in db.query(
        "SELECT external_code, components FROM set_cost WHERE components IS NOT NULL")}

    recs, resolved, api_calls = [], 0, 0
    for ec in cands:
        comps = None if refresh_all else cached.get(ec)
        if comps is None:
            comps = _mix(ec); api_calls += 1; time.sleep(PAUSE)
            if comps:
                resolved += 1
        if not comps:
            continue   # не набор / состав не получен
        covered = [c for c in comps if c in price]
        cost = round(sum(price[c] for c in covered), 2)
        recs.append({"external_code": ec, "components": comps, "n_components": len(comps),
                     "cost": cost, "covered": len(covered),
                     "resolved_at": datetime.datetime.now(datetime.timezone.utc).isoformat()})
        if api_calls % 100 == 0 and api_calls:
            print(f"  [set_cost] API-запросов {api_calls}, наборов собрано {len(recs)}", flush=True)
    if recs:
        db.upsert("set_cost", recs, conflict_cols=["external_code"],
                  update_cols=["components", "n_components", "cost", "covered", "resolved_at"])
    full = sum(1 for r in recs if r["covered"] == r["n_components"])
    print(f"Себест наборов: {len(recs)} наборов | API-запросов {api_calls} (новых составов {resolved}) | "
          f"полное покрытие компонентов: {full}/{len(recs)}", flush=True)


if __name__ == "__main__":
    main(refresh_all="--refresh-all" in sys.argv)
