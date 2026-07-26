# поток: rev
"""collectors/ya_cogs_demand.py — кэш себестоимости Яндекс.Маркета ПО ОТГРУЗКАМ (demand).

Для раздела «Себестоимость»: себест каждой отгрузки = FIFO report/stock/byoperation (как WB/Ozon),
наша цена = demand.sum из МС, статус = raw_yandex_stats_order (+ МС salesreturn для «наш склад/брак»).
Мост к заказу/возврату — demand.name (= номер заказа ЯМ). Пишет ya_cogs_demand (идемпотентный upsert),
переиспользуя throttled MS-клиент и byoperation_cogs из collectors.ms_demand_cogs.

Запуск:  ./venv/bin/python -m collectors.ya_cogs_demand [2026-01-01]
"""
import os
import sys
import pathlib
from collections import defaultdict

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from collectors import ms_demand_cogs as MDC  # noqa: E402  (throttled MS get + byoperation_cogs)

load_dotenv(BASE_DIR / ".env")
MS_TOK = os.getenv("MOYSKLAD_TOKEN")
MS = "https://api.moysklad.ru/api/remap/1.2"
H = {"Authorization": f"Bearer {MS_TOK}", "Accept-Encoding": "gzip"}

ACCOUNT = "ya_acc1"
AGENTS = ("Покупатель Маркет", "Я.Маркет Экспресс")
DEFECT_STORES = {"Брак"}


def _agent_href(name):
    rr = requests.get(f"{MS}/entity/counterparty", headers=H,
                      params={"filter": f"name={name}"}, timeout=60).json().get("rows", [])
    return rr[0]["meta"]["href"] if rr else None


def _cost_seb_map():
    """{ms_id: cost_seb} — справочная закупочная для фолбэк-импутации (когда FIFO нет)."""
    return {r["ms_id"]: float(r["cost_seb"] or 0) for r in db.query(
        "SELECT ms_id, cost_seb FROM products WHERE cost_seb>0")}


def _order_status_map():
    """{order_id(str): status} из raw_yandex_stats_order (DELIVERED/RETURNED/CANCELLED_*/...)."""
    rows = db.query("SELECT order_id, payload->>'status' st FROM raw_yandex_stats_order WHERE account=%s",
                    (ACCOUNT,))
    return {str(r["order_id"]): r["st"] for r in rows}


def _ya_fullprice_map():
    """{order_id: полная цена заказа} = Σ по позициям Σ price.total (BUYER+CASHBACK+MARKETPLACE).
    Нужна для заказов, где demand.sum в МС номинальный (оплата в осн. баллами/промо → в МС попал
    только кэш-платёж покупателя, напр. 1 ₽). Полная цена по данным Маркета — сумма всех типов оплаты."""
    out = {}
    for r in db.query("SELECT order_id, payload FROM raw_yandex_stats_order WHERE account=%s", (ACCOUNT,)):
        p = r["payload"] or {}
        tot = 0.0
        for it in (p.get("items") or []):
            for pr in (it.get("prices") or []):
                tot += pr.get("total") or 0
        if tot:
            out[str(r["order_id"])] = round(tot, 2)
    return out


def _return_store_map(href, since):
    """{demand_name: store_name} по возвратам агента — для «наш склад (сток) / брак»."""
    out = {}
    offset = 0
    while True:
        rows = requests.get(f"{MS}/entity/salesreturn", headers=H, timeout=90, params={
            "filter": f"agent={href};moment>={since} 00:00:00", "limit": 100, "offset": offset,
            "expand": "store,demand"}).json().get("rows", [])
        if not rows:
            break
        for d in rows:
            nm = (d.get("demand") or {}).get("name") or d.get("name")
            store = ((d.get("store") or {}).get("name")) or None
            if nm:
                out[nm] = store
        offset += 100
        if len(rows) < 100:
            break
    return out


def _classify(order_status, ret_store):
    """Статус отгрузки на стороне Маркета для раздела «Себестоимость».

    Отгрузка (demand) существует → товар отгружен и продан. Поэтому НЕ-выполненными считаем только
    явные сигналы: возврат в МС (наш склад/брак), невыкуп (CANCELLED_IN_DELIVERY) и возврат по данным
    Маркета (RETURNED). Всё прочее, включая отсутствие заказа в стат-сырье (raw неполон — часть
    доставленных заказов туда не попадает), — «Выполнен»."""
    if ret_store is not None:
        return "return_defect" if ret_store in DEFECT_STORES else "return_stock"
    if order_status == "CANCELLED_IN_DELIVERY":
        return "unredeemed"                    # невыкуп — вернулось на склад Маркета (не наш сток)
    if order_status == "RETURNED":
        return "return_stock"                  # возврат по данным Маркета (МС salesreturn не найден)
    return "done"                              # отгрузка есть → доставлен/продан


def collect(since="2026-01-01"):
    if not MS_TOK:
        print("[ya-cogs] нет MOYSKLAD_TOKEN")
        return 0
    cost_seb = _cost_seb_map()
    order_st = _order_status_map()
    fullprice = _ya_fullprice_map()   # для заказов с номинальной суммой в МС (<100 ₽)
    recs = []
    stats = defaultdict(int)
    for name in AGENTS:
        href = _agent_href(name)
        if not href:
            print(f"[ya-cogs] агент «{name}» не найден")
            continue
        ret_store = _return_store_map(href, since)
        offset = 0
        while True:
            rows = requests.get(f"{MS}/entity/demand", headers=H, timeout=90, params={
                "filter": f"agent={href};moment>={since} 00:00:00",
                "limit": 100, "offset": offset}).json().get("rows", [])
            if not rows:
                break
            for d in rows:
                did = d["meta"]["href"].rsplit("/", 1)[-1]
                nm = d.get("name")
                moment = (d.get("moment") or "")[:10]
                if len(moment) != 10 or not nm:
                    continue
                our_sum = (d.get("sum") or 0) / 100.0
                if our_sum < 100:                       # номинал в МС → полная цена по данным Маркета
                    our_sum = fullprice.get(str(nm), our_sum)
                try:
                    cogs, qty, pos = MDC.byoperation_cogs(did)
                except Exception as e:
                    print(f"[ya-cogs] byoperation {nm}: {e}")
                    continue
                if cogs > 0:
                    method = "ms_fifo"
                else:
                    cogs = sum(cost_seb.get(p["ms_id"], 0) * p["qty"] for p in pos)
                    method = "imputed"
                    if not qty:
                        qty = sum(p["qty"] for p in pos)
                st_raw = order_st.get(str(nm))
                status = _classify(st_raw, ret_store.get(nm))
                stats[method] += 1
                recs.append({
                    "account": ACCOUNT, "demand_name": nm, "demand_id": did,
                    "ym": moment[:7] + "-01", "demand_date": moment,
                    "our_sum": round(our_sum, 2), "qty": qty,
                    "cogs": round(cogs, 2), "method": method,
                    "status": status, "status_raw": st_raw})
            offset += 100
            if len(rows) < 100:
                break
    if recs:
        db.upsert("ya_cogs_demand", recs, conflict_cols=["account", "demand_name"])
    print(f"[ya-cogs] отгрузок записано: {len(recs)} "
          f"(FIFO {stats['ms_fifo']}, импутация {stats['imputed']})")
    return len(recs)


def main(since="2026-01-01"):
    return collect(since)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-01-01")
