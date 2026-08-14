# поток: fin
"""collectors/oz_cogs_demand.py — кэш себестоимости Ozon ПО ОТГРУЗКАМ (demand), два юрлица.

Зеркало collectors/ya_cogs_demand.py для раздела «Себестоимость», но дешевле по API: FIFO-себест
озоновских отгрузок УЖЕ собран в ms_demand_cogs (агенты «Покупатель Озон» / «Озон Экспресс», юрлицо
в поле org), позиции — в ms_demand_pos. Поэтому в МойСклад ходим только за списком отгрузок ради
суммы документа (наша цена); report/stock/byoperation дёргаем лишь для отгрузок, которых нет в кэше.

  наша цена  = demand.sum из МС;
  себест     = ms_demand_cogs.cogs (FIFO конкретной отгрузки) → фолбэк импутация cost_seb × qty;
  статус     = raw_ozon_posting.status (мост posting_number = demand.name)
               + МС salesreturn через ms_return_cogs (склад возврата решает, сторнируем или нет).

Пишет oz_cogs_demand (идемпотентный upsert по account+demand_name).

Запуск:  ./venv/bin/python -m collectors.oz_cogs_demand [2026-01-01] [oz_acc1|oz_acc2]
"""
import sys
import pathlib
import urllib.parse
from collections import defaultdict

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from collectors import ms_demand_cogs as MDC  # noqa: E402  (throttled MS get + byoperation_cogs)
from reports import fifo_fallback  # noqa: E402  (FIFO товара МС вместо cost_seb)

ACCOUNTS = ("oz_acc1", "oz_acc2")          # oz_acc1 — Цифровой квадрат, oz_acc2 — Дисквэр
AGENTS = MDC.PLATFORM["ozon"]["agents"]    # «Покупатель Озон», «Озон Экспресс»
ACC_ORG = MDC.OZ_ACC_ORG                   # {account: имя юрлица в МС}

DEFECT_STORES = {"Брак"}
OZON_STORES = {"Озон"}                     # склад FBO: товар остался у площадки, но продаваем

# статусы отправления Ozon, при которых товар к покупателю не доехал (деньги не реализованы)
CANCELLED_RAW = {"cancelled"}
IN_FLIGHT_RAW = {"delivering", "awaiting_packaging", "awaiting_deliver", "awaiting_registration",
                 "awaiting_approve", "acceptance_in_progress", "arbitration", "not_accepted"}


def _cost_seb_map():
    """{ms_id: cost_seb} — справочная закупочная для фолбэк-импутации (когда FIFO нет)."""
    return {r["ms_id"]: float(r["cost_seb"] or 0) for r in db.query(
        "SELECT ms_id, cost_seb FROM products WHERE cost_seb>0")}


def _manual_map(account):
    """{demand_name: cogs} — ручной себест сотрудника (побеждает FIFO/импутацию)."""
    return {r["demand_name"]: float(r["cogs"] or 0) for r in db.query(
        "SELECT demand_name, cogs FROM oz_cogs_manual WHERE account=%s", (account,))}


def _known_map(account):
    """{demand_name: (cogs, qty, method)} — себест, посчитанный прошлыми прогонами.

    FIFO уже состоявшейся отгрузки не меняется, поэтому второй раз идти за ним в МойСклад незачем.
    Без этого кэша каждый прогон заново дёргал report/stock/byoperation по всем отгрузкам, которых
    нет в ms_demand_cogs (Ozon ~0.7 тыс. запросов, и так КАЖДЫЙ раз): byoperation_cogs результат
    никуда не пишет. Нужно пересчитать конкретную отгрузку — кнопка «пересчитать» в детализации
    (одна отгрузка = один запрос), месяц целиком — разморозка + прогон."""
    return {r["demand_name"]: (float(r["cogs"] or 0), float(r["qty"] or 0), r["method"])
            for r in db.query(
                """SELECT demand_name, cogs, qty, method FROM oz_cogs_demand
                   WHERE account=%s AND coalesce(cogs,0) > 0""", (account,))}


def _frozen_set(account):
    """{'YYYY-MM', …} — закрытые месяцы: коллектор их не пересобирает (МС не дёргает)."""
    return {r["ym"].strftime("%Y-%m") for r in db.query(
        "SELECT ym FROM oz_cogs_frozen WHERE account=%s", (account,))}


def _eff_since(account, since, frozen):
    """Поднять since до первого дня самого раннего НЕзакрытого месяца (сплошной закрытый префикс
    от min(ym) пропускаем — чтобы не листать в МС заведомо закрытые месяцы).

    Досбор истории: если since РАНЬШЕ самого раннего собранного месяца — это осознанный бэкфилл
    (добираем месяцы, которых в таблице нет вовсе), пропуск префикса не применяем. Закрытые месяцы
    всё равно не перезапишутся: их отсекает фильтр по frozen при записи.
    """
    if not frozen:
        return since
    row = db.query("SELECT min(ym) mn FROM oz_cogs_demand WHERE account=%s", (account,))
    mn = row[0]["mn"] if row else None
    if not mn:
        return since
    if since < mn.strftime("%Y-%m-01"):
        return since                            # бэкфилл истории — префикс не пропускаем
    y, m = mn.year, mn.month
    while f"{y:04d}-{m:02d}" in frozen:
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return max(since, f"{y:04d}-{m:02d}-01")


def _fifo_map(org_id, since):
    """{demand_id: (cogs, qty)} — готовый FIFO-себест отгрузок из кэша ms_demand_cogs."""
    return {r["demand_id"]: (float(r["cogs"] or 0), float(r["qty"] or 0)) for r in db.query(
        """SELECT demand_id, cogs, qty FROM ms_demand_cogs
           WHERE org=%s AND agent = ANY(%s) AND moment >= %s""",
        (org_id, list(AGENTS), since))}


def _pos_map(demand_ids):
    """{demand_id: [{ms_id, qty}]} — позиции отгрузок из кэша ms_demand_pos (для импутации)."""
    out = defaultdict(list)
    if not demand_ids:
        return out
    for r in db.query("SELECT demand_id, ms_id, qty FROM ms_demand_pos WHERE demand_id = ANY(%s)",
                      (list(demand_ids),)):
        out[r["demand_id"]].append({"ms_id": r["ms_id"], "qty": float(r["qty"] or 0)})
    return out


def _posting_status_map(account):
    """{posting_number: status} из raw_ozon_posting (delivered/cancelled/delivering/…)."""
    return {r["posting_number"]: r["status"] for r in db.query(
        "SELECT posting_number, status FROM raw_ozon_posting WHERE account=%s", (account,))}


def _return_store_map(org_id, since):
    """{demand_name: store_name} по возвратам МС — склад возврата решает судьбу себеста.

    На одну отгрузку возвратов может быть несколько (21 случай) — берём склад возврата с наибольшим
    числом штук: он определяет, куда физически ушёл товар."""
    out = {}
    best = defaultdict(float)
    for r in db.query("""SELECT demand_name, store, coalesce(ret_qty,0)::float q FROM ms_return_cogs
                         WHERE org=%s AND agent = ANY(%s) AND moment >= %s AND demand_name IS NOT NULL""",
                      (org_id, list(AGENTS), since)):
        nm = r["demand_name"]
        if r["q"] >= best[nm] or nm not in out:
            best[nm] = r["q"]
            out[nm] = r["store"]
    return out


def _list_demands(org_href, agent_href, since):
    """[{name, id, moment, sum}] — отгрузки org+agent с moment>=since (страницами по 1000)."""
    flt = urllib.parse.quote(
        f"organization={org_href};agent={agent_href};moment>={since} 00:00:00")
    out, offset = [], 0
    while True:
        j = MDC.get(f"/entity/demand?limit=1000&offset={offset}&filter={flt}")
        rows = j.get("rows", [])
        for d in rows:
            out.append({"name": d.get("name"), "id": d.get("id"),
                        "moment": (d.get("moment") or "")[:10],
                        "sum": (d.get("sum") or 0) / 100.0})
        offset += 1000
        if not rows or offset >= j.get("meta", {}).get("size", 0):
            break
    return out


def _classify(posting_status, ret_store):
    """Статус отгрузки для раздела «Себестоимость».

    Возврат в МС — сильнейший сигнал (товар физически у нас или на складе FBO). Дальше — статус
    отправления Ozon. Отсутствие постинга в сырье (raw_ozon_posting собирается с 2026-05) не значит
    отмену: отгрузка есть → товар отгружен и продан → 'done'."""
    if ret_store is not None:
        if ret_store in DEFECT_STORES:
            return "return_defect"          # брак: перепродать нельзя → себест остаётся убытком
        if ret_store in OZON_STORES:
            return "return_ozon"            # вернулось на склад FBO: продаваемо → себест сторнируется
        return "return_stock"               # вернулось в наш сток → себест сторнируется
    if posting_status in CANCELLED_RAW:
        return "unredeemed"                 # отменено/не выкуплено, возврат в МС не проведён
    if posting_status in IN_FLIGHT_RAW:
        return "other"                      # в пути / в обработке — деньги ещё не реализованы
    return "done"                            # delivered либо постинга нет в сырье → продано


def collect_account(account, since="2026-01-01"):
    if not MDC.TOK:
        print("[oz-cogs] нет MOYSKLAD_TOKEN")
        return 0
    org_name = ACC_ORG[account]
    frozen = _frozen_set(account)
    since = _eff_since(account, since, frozen)
    if frozen:
        print(f"[oz-cogs][{account}] закрыто: {len(frozen)} мес, собираю с {since}")
    org_href = MDC._resolve_href("organization", org_name)
    org_id = MDC._hid(org_href)

    ff = fifo_fallback.load()            # FIFO тех же товаров МС — импутация без cost_seb
    cost_seb = _cost_seb_map()          # аварийный хвост: товар в МС ни разу не отгружался
    manual = _manual_map(account)
    known = _known_map(account)
    fifo = _fifo_map(org_id, since)
    post_st = _posting_status_map(account)
    ret_store = _return_store_map(org_id, since)

    demands = []
    for agent in AGENTS:
        agent_href = MDC._resolve_href("counterparty", agent)
        demands += _list_demands(org_href, agent_href, since)

    live = [d for d in demands
            if d["name"] and len(d["moment"]) == 10 and d["moment"][:7] not in frozen]
    skipped = len(demands) - len(live)
    pos = _pos_map({d["id"] for d in live
                    if (d["id"] not in fifo or fifo[d["id"]][0] <= 0) and d["name"] not in known})

    recs, stats = [], defaultdict(int)
    for d in live:
        did, nm = d["id"], d["name"]
        cogs, qty = fifo.get(did, (0.0, 0.0))
        method = "ms_fifo"
        if cogs <= 0 and nm in known:                   # уже посчитан прошлым прогоном → в МС не идём
            cogs, kq, method = known[nm]
            qty = qty or kq
            stats["из кэша"] += 1
        if cogs <= 0:                                   # нет FIFO → импутация по справочной закупочной
            p = pos.get(did) or []
            if not p:                                   # нет и позиций в кэше → добираем из МС
                try:
                    cogs, qty, p = MDC.byoperation_cogs(did)
                except Exception as e:
                    print(f"[oz-cogs][{account}] byoperation {nm}: {e}")
                    p = []
            if cogs <= 0:
                # Себест списания — только FIFO (решение Сергея 2026-08-13). Своего FIFO у
                # документа нет → берём FIFO ТЕХ ЖЕ товаров МС по ближайшей отгрузке до этой
                # даты; cost_seb (средняя по остатку из карточки) — лишь аварийный хвост.
                cogs, method = ff.impute(p, d["moment"])
                if not cogs:
                    # FIFO не существует НИГДЕ — цифра из карточки не себест списания.
                    # `need_manual` = строка ждёт ручного ввода (решение Сергея 2026-08-14).
                    cogs = sum(cost_seb.get(x["ms_id"], 0) * x["qty"] for x in p)
                    method = "need_manual"
            if not qty:
                qty = sum(x["qty"] for x in p)
        if nm in manual:                                # ручной себест — истина, побеждает всё
            cogs, method = manual[nm], "manual"
        st_raw = post_st.get(nm)
        stats[method] += 1
        recs.append({
            "account": account, "demand_name": nm, "demand_id": did,
            "ym": d["moment"][:7] + "-01", "demand_date": d["moment"],
            "our_sum": round(d["sum"], 2), "qty": qty,
            "cogs": round(cogs, 2), "method": method,
            "status": _classify(st_raw, ret_store.get(nm)), "status_raw": st_raw})
    if recs:
        db.upsert("oz_cogs_demand", recs, conflict_cols=["account", "demand_name"])
    print(f"[oz-cogs][{account}] отгрузок записано: {len(recs)} "
          f"(FIFO {stats['ms_fifo']}, нужна ручная {stats['need_manual']}, ручной {stats['manual']}"
          f"; из кэша без запроса в МС {stats['из кэша']}; пропущено закрытых {skipped})")
    return len(recs)


def collect(since="2026-01-01", accounts=ACCOUNTS):
    return sum(collect_account(a, since) for a in accounts)


def main(since="2026-01-01", account=None):
    return collect(since, (account,) if account else ACCOUNTS)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-01-01",
         sys.argv[2] if len(sys.argv) > 2 else None)
