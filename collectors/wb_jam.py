"""collectors/wb_jam.py — WB Джем: позиции в выдаче + поисковые запросы по товару.

Раздел «Аналитика → Поисковые запросы» (подписка «Джем»).
  POST /api/v2/search-report/report               — сводка + позиции/воронка по товарам
  POST /api/v2/search-report/product/search-texts — топ поисковых запросов конкретного товара

Поля current/dynamics: dynamics = % изменения к прошлому сопоставимому периоду (pastPeriod).
positionCluster ∈ {"all","firstHundred"}. Пагинация report — limit/offset.

Запуск:  ./venv/bin/python collectors/wb_jam.py [wb_acc1|wb_acc2|all] [текущих_дней=7]
         (search-texts тянутся для товаров с упавшими позициями/заказами + заметным трафиком)
"""
import os
import sys
import time
import datetime
import pathlib

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
BASE = "https://seller-analytics-api.wildberries.ru/api/v2/search-report"
TOKEN_ENV = {"wb_acc1": "WB_TOKEN_ACC1", "wb_acc2": "WB_TOKEN_ACC2"}
PAUSE = 5            # пауза между запросами (лимитер WB)
MIN_OPEN = 5        # порог трафика, ниже которого по товару не тянем запросы (длинный хвост)


class NoJam(Exception):
    """Аккаунт без подписки «Джем» (403) — пропускаем, не валим прогон."""


def _token(account):
    t = os.getenv(TOKEN_ENV[account])
    if not t:
        raise RuntimeError(f"{TOKEN_ENV[account]} не задан в .env")
    return t


def _periods(cur_days=7):
    """Текущая неделя vs предыдущая сопоставимая (для dynamics)."""
    today = datetime.date.today()
    cur_end = today - datetime.timedelta(days=1)              # вчера (сегодня неполный)
    cur_start = cur_end - datetime.timedelta(days=cur_days - 1)
    past_end = cur_start - datetime.timedelta(days=1)
    past_start = past_end - datetime.timedelta(days=cur_days - 1)
    return ({"start": cur_start.isoformat(), "end": cur_end.isoformat()},
            {"start": past_start.isoformat(), "end": past_end.isoformat()})


def _post(account, path, body, tries=6):
    H = {"Authorization": _token(account), "Content-Type": "application/json"}
    for _ in range(tries):
        r = requests.post(f"{BASE}/{path}", headers=H, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "20")) + 2)
            continue
        if r.status_code == 403:
            raise NoJam(f"{path}: 403 — подписка «Джем» не подключена на аккаунте")
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"{path}: исчерпаны попытки (429)")


def _cd(o, k):
    """вернуть (current, dynamics) из {current,dynamics}."""
    v = o.get(k) or {}
    return v.get("current"), v.get("dynamics")


def fetch_report(account, cur, past):
    """Полный отчёт по выдаче: сводка + позиции/воронка по всем товарам. Пагинация limit/offset."""
    piso = cur["start"]
    LIM, offset, total = 100, 0, 0
    saved_summary = False
    while True:
        body = {"currentPeriod": cur, "pastPeriod": past, "nmIds": [],
                "positionCluster": "all", "orderBy": {"field": "openCard", "mode": "desc"},
                "limit": LIM, "offset": offset}
        data = (_post(account, "report", body).get("data") or {})
        if not saved_summary:
            ci, pi, vi = data.get("commonInfo") or {}, data.get("positionInfo") or {}, data.get("visibilityInfo") or {}
            ap, mp = _cd(pi, "average"), _cd(pi, "median")
            vis, oc = _cd(vi, "visibility"), _cd(vi, "openCard")
            fh = ((pi.get("clusters") or {}).get("firstHundred") or {})
            db.upsert("wb_search_summary", [{
                "account": account, "period_start": piso, "period_end": cur["end"],
                "supplier_rating": (ci.get("supplierRating") or {}).get("current"),
                "advertised": (ci.get("advertisedProducts") or {}).get("current"),
                "total_products": ci.get("totalProducts"),
                "avg_position": ap[0], "avg_position_dyn": ap[1],
                "median_position": mp[0], "median_position_dyn": mp[1],
                "visibility": vis[0], "visibility_dyn": vis[1],
                "open_card": oc[0], "open_card_dyn": oc[1],
                "first_hundred": fh.get("current"), "first_hundred_dyn": fh.get("dynamics"),
            }], conflict_cols=["account", "period_start"])
            saved_summary = True
        items = []
        for g in (data.get("groups") or []):
            items += g.get("items") or []
        recs = []
        for it in items:
            pr = it.get("price") or {}
            ap, oc = _cd(it, "avgPosition"), _cd(it, "openCard")
            atc, otc = _cd(it, "addToCart"), _cd(it, "openToCart")
            od, cto = _cd(it, "orders"), _cd(it, "cartToOrder")
            vis = _cd(it, "visibility")
            recs.append({
                "account": account, "period_start": piso, "nm_id": it.get("nmId"),
                "name": it.get("name"), "vendor_code": it.get("vendorCode"),
                "subject_name": it.get("subjectName"), "brand": it.get("brandName"),
                "is_advertised": it.get("isAdvertised"), "rating": it.get("rating"),
                "feedback_rating": it.get("feedbackRating"),
                "min_price": pr.get("minPrice"), "max_price": pr.get("maxPrice"),
                "avg_position": ap[0], "avg_position_dyn": ap[1],
                "open_card": oc[0], "open_card_dyn": oc[1],
                "add_to_cart": atc[0], "add_to_cart_dyn": atc[1],
                "open_to_cart": otc[0], "open_to_cart_dyn": otc[1],
                "orders": od[0], "orders_dyn": od[1],
                "cart_to_order": cto[0], "cart_to_order_dyn": cto[1],
                "visibility": vis[0], "visibility_dyn": vis[1],
            })
        recs = [r for r in recs if r["nm_id"]]
        if recs:
            db.upsert("wb_search_report", recs, conflict_cols=["account", "period_start", "nm_id"])
            total += len(recs)
        print(f"  [jam report {account}] offset {offset}: +{len(items)} (всего {total})", flush=True)
        if len(items) < LIM:
            break
        offset += LIM
        time.sleep(PAUSE)
    print(f"[jam report {account}] записано товаров: {total}", flush=True)
    return total


def fetch_search_texts(account, nm_ids, cur, past):
    """Поисковые запросы по списку товаров (по одному nmId за запрос — стабильнее)."""
    piso = cur["start"]
    total = 0
    for i, nm in enumerate(nm_ids, 1):
        body = {"currentPeriod": cur, "pastPeriod": past, "nmIds": [nm],
                "topOrderBy": "openCard", "orderBy": {"field": "openCard", "mode": "desc"},
                "limit": 30, "offset": 0}
        items = ((_post(account, "product/search-texts", body).get("data") or {}).get("items")) or []
        recs = []
        for it in items:
            fr = _cd(it, "frequency")
            mp, ap = _cd(it, "medianPosition"), _cd(it, "avgPosition")
            oc = it.get("openCard") or {}
            atc, otc = _cd(it, "addToCart"), _cd(it, "openToCart")
            od, vis = _cd(it, "orders"), _cd(it, "visibility")
            recs.append({
                "account": account, "period_start": piso, "nm_id": it.get("nmId") or nm,
                "text": it.get("text"),
                "frequency": fr[0], "frequency_dyn": fr[1], "week_frequency": it.get("weekFrequency"),
                "median_position": mp[0], "median_position_dyn": mp[1],
                "avg_position": ap[0], "avg_position_dyn": ap[1],
                "open_card": oc.get("current"), "open_card_dyn": oc.get("dynamics"),
                "open_card_pct": oc.get("percentile"),
                "add_to_cart": atc[0], "add_to_cart_dyn": atc[1],
                "open_to_cart": otc[0], "open_to_cart_dyn": otc[1],
                "orders": od[0], "orders_dyn": od[1],
                "cart_to_order": (it.get("cartToOrder") or {}).get("current"),
                "visibility": vis[0], "visibility_dyn": vis[1],
            })
        recs = [r for r in recs if r["text"]]
        if recs:
            db.upsert("wb_search_text", recs, conflict_cols=["account", "period_start", "nm_id", "text"])
            total += len(recs)
        if i % 10 == 0:
            print(f"  [jam texts {account}] {i}/{len(nm_ids)} товаров, запросов {total}", flush=True)
        time.sleep(PAUSE)
    print(f"[jam texts {account}] товаров {len(nm_ids)}, запросов записано: {total}", flush=True)
    return total


def _target_nmids(account, piso):
    """Товары, по которым тянем запросы: заметный трафик И (упала позиция ИЛИ просели заказы)."""
    rows = db.query("""
        SELECT nm_id FROM wb_search_report
        WHERE account=%s AND period_start=%s AND open_card >= %s
          AND (avg_position_dyn > 0 OR orders_dyn < 0 OR visibility_dyn < 0)
        ORDER BY open_card DESC LIMIT 120
    """, (account, piso, MIN_OPEN))
    return [r["nm_id"] for r in rows]


def main(account="wb_acc1", cur_days=7):
    cur, past = _periods(cur_days)
    print(f"WB Джем {account}: текущий {cur['start']}..{cur['end']} vs прошлый {past['start']}..{past['end']}", flush=True)
    try:
        fetch_report(account, cur, past)
        nmids = _target_nmids(account, cur["start"])
        print(f"[jam {account}] товаров для разбора запросов: {len(nmids)}", flush=True)
        if nmids:
            fetch_search_texts(account, nmids, cur, past)
    except NoJam as e:
        print(f"[jam {account}] пропуск: {e}", flush=True)


if __name__ == "__main__":
    acc = sys.argv[1] if len(sys.argv) > 1 else "wb_acc1"
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    accounts = ["wb_acc1", "wb_acc2"] if acc == "all" else [acc]
    for a in accounts:
        main(a, days)
