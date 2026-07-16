"""collectors/ms_demand_cogs.py — себестоимость КОНКРЕТНЫХ отгрузок МойСклад.

Источник правды себеста проданного — сама отгрузка МС: `report/stock/byoperation?operation.id=`
(«Остатки по документу») возвращает positions[].cost (КОПЕЙКИ, итог с учётом quantity) — FIFO-себест
на `moment` документа отгрузки. Σ cost/100 = себест всей отгрузки = ровно то, что МС показывает в
документе (не усреднение, не приближение report/stock на дату). Подтверждено на эталоне 4747758355 = 284 ₽.

Кэш в `ms_demand_cogs` (миграция 027), натуральный ключ demand_id → идемпотентно и резюмируемо
(повторный/прерванный прогон не плодит дублей и не перекачивает уже собранное). Витрина матчит по
demand_name = WB assembly_id напрямую (см. reports/margin_by_sku.py).

Запуск:  ./venv/bin/python -m collectors.ms_demand_cogs            # оба юрлица, нужные отгрузки
"""
import os
import sys
import time
import json
import gzip
import pathlib
import urllib.request
import urllib.parse

from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
TOK = os.getenv("MOYSKLAD_TOKEN")
MS = "https://api.moysklad.ru/api/remap/1.2"

MIN_INTERVAL = 0.09          # ~11 req/s (лимит МС 45/3с)
_last = [0.0]

# Юрлицо МС по WB-аккаунту (совпадает с reports/margin_by_sku.ACC_ORG).
ACC_ORG = {"wb_acc1": 'ООО "ЦИФРОВОЙ КВАДРАТ"', "wb_acc2": 'ООО "ДИСКВЭР"'}


def _norm_posting(p):
    return "-".join((p or "").split("-")[:2])


OZ_ACC_ORG = {"oz_acc1": 'ООО "ЦИФРОВОЙ КВАДРАТ"', "oz_acc2": 'ООО "ДИСКВЭР"'}
PLATFORM = {
    "wb": {"agents": ["Покупатель ВБ"], "org_map": ACC_ORG},
    "ozon": {"agents": ["Покупатель Озон", "Озон Экспресс"], "org_map": OZ_ACC_ORG},
}


def _throttle():
    dt = time.monotonic() - _last[0]
    if dt < MIN_INTERVAL:
        time.sleep(MIN_INTERVAL - dt)
    _last[0] = time.monotonic()


def get(path, _tries=0):
    _throttle()
    req = urllib.request.Request(MS + path, headers={
        "Authorization": f"Bearer {TOK}", "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            d = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                d = gzip.decompress(d)
            return json.loads(d)
    except urllib.error.HTTPError as e:
        if e.code == 429 and _tries < 8:
            wait = int(e.headers.get("X-Lognex-Retry-TimeInterval", "1000")) / 1000.0 + 0.2
            time.sleep(wait)
            return get(path, _tries + 1)
        if e.code in (500, 502, 503, 504) and _tries < 4:
            time.sleep(1.0 + _tries)
            return get(path, _tries + 1)
        raise


def _hid(h):
    return (h or "").split("/")[-1].split("?")[0]


def _resolve_href(entity, name):
    j = get(f"/entity/{entity}?filter=" + urllib.parse.quote(f"name={name}") + "&limit=1")
    rows = j.get("rows", [])
    if not rows:
        raise RuntimeError(f"{entity} '{name}' не найден")
    return rows[0]["meta"]["href"]


def list_demand_ids(org_href, agent_href, moment_from):
    """{demand_name: (demand_id, moment)} для org+agent с moment>=moment_from."""
    flt = urllib.parse.quote(
        f"organization={org_href};agent={agent_href};moment>={moment_from} 00:00:00")
    out, offset = {}, 0
    while True:
        j = get(f"/entity/demand?limit=1000&offset={offset}&filter={flt}")
        rows = j.get("rows", [])
        for d in rows:
            out[d.get("name")] = (d.get("id"), d.get("moment"))
        offset += 1000
        if not rows or offset >= j.get("meta", {}).get("size", 0):
            break
    return out


def byoperation_cogs(demand_id):
    """(cogs_руб, qty, positions) по отчёту «Остатки по документу». cost — копейки, итог с учётом
    quantity. positions: [{ms_id, cost(₽ итог), qty}] — для ms_demand_pos (импутация FBO по ms_id)."""
    j = get(f"/report/stock/byoperation?operation.id={demand_id}")
    rows = j.get("rows", [])
    if not rows:
        return 0.0, 0.0, []
    pos = rows[0].get("positions", []) or []
    out = [{"ms_id": _hid((p.get("meta", {}) or {}).get("href")),
            "cost": (p.get("cost", 0) or 0) / 100.0,
            "qty": p.get("quantity", 0) or 0} for p in pos]
    cogs = sum(p["cost"] for p in out)
    qty = sum(p["qty"] for p in out)
    return cogs, qty, out


def needed_assembly_ids(account):
    """distinct WB assembly_id (FBS-продажи) этого аккаунта — что вообще нужно собрать."""
    rows = db.query("""SELECT DISTINCT payload->>'assembly_id' a FROM raw_wb_report
        WHERE account=%s AND payload->>'supplier_oper_name'='Продажа'
          AND coalesce(payload->>'assembly_id','0')<>'0'""", (account,))
    return {r["a"] for r in rows if r["a"]}


def needed_ozon_norms(account):
    """distinct нормализованные posting_number доставленных отправлений Ozon."""
    rows = db.query("""SELECT DISTINCT payload->'posting'->>'posting_number' posting_number
        FROM raw_ozon_transaction WHERE account=%s
          AND payload->>'operation_type'='OperationAgentDeliveredToCustomer'""", (account,))
    return {_norm_posting(r["posting_number"]) for r in rows if r["posting_number"]}


def collect(account="wb_acc1", platform="wb", moment_from="2025-09-01", batch=200,
            progress_every=500):
    config = PLATFORM[platform]
    org_name = config["org_map"][account]
    agents = config["agents"]
    print(f"[{account}] юрлицо {org_name}; резолв org/agent…", flush=True)
    org_href = _resolve_href("organization", org_name)
    org_id = _hid(org_href)

    cached = {r["demand_id"] for r in db.query(
        "SELECT demand_id FROM ms_demand_cogs WHERE org=%s AND agent = ANY(%s)",
        (org_id, agents))}
    todo = []
    all_names = set()
    if platform == "wb":
        need = needed_assembly_ids(account)
        agent = agents[0]
        agent_href = _resolve_href("counterparty", agent)
        print(f"[{account}] список отгрузок МС (org+agent, moment>={moment_from})…", flush=True)
        name2id = list_demand_ids(org_href, agent_href, moment_from)
        all_names.update(name2id)
        print(f"[{account}] отгрузок в списке МС: {len(name2id)}", flush=True)
        todo = [(n, did, moment, agent) for n, (did, moment) in name2id.items()
                if n in need and did not in cached]
        missing = [n for n in need if n not in name2id]
    else:
        need = needed_ozon_norms(account)
        for agent in agents:
            agent_href = _resolve_href("counterparty", agent)
            print(f"[{account}] список отгрузок МС ({agent}, moment>={moment_from})…", flush=True)
            name2id = list_demand_ids(org_href, agent_href, moment_from)
            all_names.update(_norm_posting(n) for n in name2id)
            print(f"[{account}] отгрузок в списке МС ({agent}): {len(name2id)}", flush=True)
            todo += [(n, did, moment, agent) for n, (did, moment) in name2id.items()
                     if _norm_posting(n) in need and did not in cached]
        missing = [n for n in need if n not in all_names]
    print(f"[{account}] нужно отгрузок {len(need)}, уже в кэше {len(need) - len(todo) - len(missing)}",
          flush=True)
    print(f"[{account}] к сбору {len(todo)}; нет в списке МС (вне окна?) {len(missing)}", flush=True)
    if missing[:5]:
        print(f"[{account}]   примеры отсутствующих: {missing[:5]}", flush=True)

    buf, pbuf, done = [], [], 0

    def flush():
        # порядок важен: сперва документы (FK), затем позиции
        if buf:
            db.upsert("ms_demand_cogs", buf, conflict_cols=["demand_id"])
        if pbuf:
            db.upsert("ms_demand_pos", pbuf, conflict_cols=["demand_id", "ms_id"])

    for name, did, moment, agent in todo:
        try:
            cogs, qty, positions = byoperation_cogs(did)
        except Exception as e:
            print(f"[{account}] ОШИБКА byoperation {name} ({did}): {e}", flush=True)
            continue
        buf.append({"demand_id": did, "demand_name": name, "org": org_id,
                    "agent": agent, "moment": moment, "cogs": cogs, "qty": qty,
                    "npos": len(positions)})
        pbuf += [{"demand_id": did, "ms_id": p["ms_id"], "cost": p["cost"], "qty": p["qty"]}
                 for p in positions if p["ms_id"]]
        done += 1
        if len(buf) >= batch:
            flush()
            buf, pbuf = [], []
        if done % progress_every == 0:
            print(f"[{account}]   собрано {done}/{len(todo)}…", flush=True)
    flush()
    print(f"[{account}] ГОТОВО: собрано {done}, пропущено-без-id {len(missing)}", flush=True)
    return done, len(missing)


def main():
    args = sys.argv[1:]
    if args and args[0] in PLATFORM:
        platform = args.pop(0)
    else:
        platform = "wb"
    defaults = ["oz_acc1", "oz_acc2"] if platform == "ozon" else ["wb_acc1", "wb_acc2"]
    accounts = args or defaults
    for acc in accounts:
        collect(acc, platform=platform)


if __name__ == "__main__":
    main()
