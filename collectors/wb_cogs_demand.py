# поток: fin
"""collectors/wb_cogs_demand.py — кэш себестоимости WB ПО ОТГРУЗКАМ (demand), два юрлица.

Зеркало collectors/oz_cogs_demand.py. FIFO-себест вбшных отгрузок уже собран в ms_demand_cogs
(агент «Покупатель ВБ», юрлицо в поле org), позиции — в ms_demand_pos, поэтому в МойСклад ходим
только за списком отгрузок ради суммы документа (наша цена); report/stock/byoperation дёргаем лишь
для отгрузок, которых нет в кэше.

  наша цена  = demand.sum из МС;
  себест     = ms_demand_cogs.cogs (FIFO конкретной отгрузки) → фолбэк импутация cost_seb × qty;
  статус     = финотчёт raw_wb_report (мост assembly_id = demand.name, supplier_oper_name
               «Продажа» / «Возврат») + МС salesreturn через ms_return_cogs (склад возврата).

Чем WB отличается от Ozon:
  * статуса отправления в реальном времени нет — есть только финотчёт с лагом до 7 недель, поэтому
    «нет строки в отчёте» у свежей отгрузки = «ждём отчёт» (other);
  * не-сток при возврате только «Брак» (аналога озоновского склада «Озон» у ВБ нет);
  * продажи в отчёте нет, но есть логистика туда-обратно = 'unredeemed' (невыкуп): денег за товар
    не было, товар вернулся к нам → строка net-neutral, флаг на разбор (в МС возврат не проведён);
  * «Возврат» в финотчёте БЕЗ возврата в МС = 'return_wb': ВБ сторнировал продажу, товар в МС не
    оприходован → тоже net-neutral + флаг;
  * отгрузки нет в отчёте совсем и она старше лага = 'unreported' — дыра, на разбор.

Раздел покрывает FBS: FBO-продажи ВБ документа «Отгрузка» в МС не создают (~11 % оборота).

Пишет wb_cogs_demand (идемпотентный upsert по account+demand_name).

Запуск:  ./venv/bin/python -m collectors.wb_cogs_demand [2026-01-01] [wb_acc1|wb_acc2]
"""
import sys
import pathlib
import urllib.parse
import datetime as dt
from collections import defaultdict

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from collectors import ms_demand_cogs as MDC  # noqa: E402  (throttled MS get + byoperation_cogs)
from reports import fifo_fallback  # noqa: E402  (FIFO товара МС вместо cost_seb)

ACCOUNTS = ("wb_acc1", "wb_acc2")          # wb_acc1 — Цифровой квадрат, wb_acc2 — Дисквэр
AGENTS = MDC.PLATFORM["wb"]["agents"]      # «Покупатель ВБ»
ACC_ORG = MDC.ACC_ORG                      # {account: имя юрлица в МС}

DEFECT_STORES = {"Брак"}                   # не-сток ВБ (ms_return_cogs.NON_STOCK['wb'])

OP_SALE = "Продажа"
OP_RETURN = "Возврат"
# сколько ждать появления отгрузки в финотчёте, прежде чем считать её «не попавшей в отчёт».
# Лаг ВБ доходит до 7 недель (память project_mp_wb_data_model), берём 60 дней с запасом.
REPORT_LAG_DAYS = 60


def _cost_seb_map():
    """{ms_id: cost_seb} — справочная закупочная для фолбэк-импутации (когда FIFO нет)."""
    return {r["ms_id"]: float(r["cost_seb"] or 0) for r in db.query(
        "SELECT ms_id, cost_seb FROM products WHERE cost_seb>0")}


def _manual_map(account):
    """{demand_name: cogs} — ручной себест сотрудника (побеждает FIFO/импутацию)."""
    return {r["demand_name"]: float(r["cogs"] or 0) for r in db.query(
        "SELECT demand_name, cogs FROM wb_cogs_manual WHERE account=%s", (account,))}


def _known_map(account):
    """{demand_name: (cogs, qty, method)} — себест, посчитанный прошлыми прогонами.

    FIFO уже состоявшейся отгрузки не меняется, поэтому второй раз идти за ним в МойСклад незачем.
    Без этого кэша каждый прогон заново дёргал report/stock/byoperation по всем отгрузкам, которых
    нет в ms_demand_cogs (WB ~2.2 тыс. запросов, и так КАЖДЫЙ раз): byoperation_cogs результат
    никуда не пишет. Нужно пересчитать конкретную отгрузку — кнопка «пересчитать» в детализации
    (одна отгрузка = один запрос), месяц целиком — разморозка + прогон."""
    return {r["demand_name"]: (float(r["cogs"] or 0), float(r["qty"] or 0), r["method"])
            for r in db.query(
                """SELECT demand_name, cogs, qty, method FROM wb_cogs_demand
                   WHERE account=%s AND coalesce(cogs,0) > 0""", (account,))}


def _frozen_set(account):
    """{'YYYY-MM', …} — закрытые месяцы: коллектор их не пересобирает (МС не дёргает)."""
    return {r["ym"].strftime("%Y-%m") for r in db.query(
        "SELECT ym FROM wb_cogs_frozen WHERE account=%s", (account,))}


def _eff_since(account, since, frozen):
    """Поднять since до первого дня самого раннего НЕзакрытого месяца (сплошной закрытый префикс
    от min(ym) пропускаем — чтобы не листать в МС заведомо закрытые месяцы).

    Досбор истории: если since РАНЬШЕ самого раннего собранного месяца — это осознанный бэкфилл
    (добираем месяцы, которых в таблице нет вовсе), пропуск префикса не применяем. Закрытые месяцы
    всё равно не перезапишутся: их отсекает фильтр по frozen при записи.
    """
    if not frozen:
        return since
    row = db.query("SELECT min(ym) mn FROM wb_cogs_demand WHERE account=%s", (account,))
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


def _report_op_map(account):
    """{assembly_id: 'Продажа' | 'Возврат'} по финотчёту ВБ.

    Возврат сильнее продажи: у вернувшегося заказа в отчёте есть ОБЕ строки (сначала продажа,
    потом сторно-возврат), и итоговое состояние заказа — возврат."""
    out = {}
    for r in db.query("""SELECT payload->>'assembly_id' a, payload->>'supplier_oper_name' op
                         FROM raw_wb_report
                         WHERE account=%s AND payload->>'supplier_oper_name' IN (%s, %s)
                           AND coalesce(payload->>'assembly_id','') NOT IN ('', '0')""",
                      (account, OP_SALE, OP_RETURN)):
        if r["op"] == OP_RETURN or r["a"] not in out:
            out[r["a"]] = r["op"]
    return out


def _report_seen_set(account):
    """{assembly_id, …} — все сборочные задания, вообще встреченные в финотчёте (любая операция).

    Нужен, чтобы отличить НЕВЫКУП от дыры в отчёте: у невыкупа строки «Продажа» нет, но есть
    логистика туда-обратно («Логистика» ×2, «Возмещение за выдачу и возврат товаров на ПВЗ»),
    то есть отгрузка отчёту известна — просто денег за товар не было."""
    return {r["a"] for r in db.query(
        """SELECT DISTINCT payload->>'assembly_id' a FROM raw_wb_report
           WHERE account=%s AND coalesce(payload->>'assembly_id','') NOT IN ('', '0')""",
        (account,))}


def _return_store_map(org_id, since):
    """{demand_name: store_name} по возвратам МС — склад возврата решает судьбу себеста.

    На одну отгрузку возвратов может быть несколько — берём склад возврата с наибольшим числом
    штук: он определяет, куда физически ушёл товар."""
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


def _classify(report_op, seen, ret_store, demand_date, today):
    """Статус отгрузки для раздела «Себестоимость».

    Возврат в МС — сильнейший сигнал: товар физически у нас, склад решает сторно/убыток. Дальше —
    финотчёт ВБ. Если продажи в отчёте нет, но отгрузка отчёту известна (только логистика) — это
    невыкуп; если её нет в отчёте совсем и она старше лага — дыра, на разбор."""
    if ret_store is not None:
        if ret_store in DEFECT_STORES:
            return "return_defect"          # брак: перепродать нельзя → себест остаётся убытком
        return "return_stock"               # вернулось в наш сток → себест сторнируется
    if report_op == OP_RETURN:
        return "return_wb"                  # ВБ сторнировал продажу, в МС возврата нет → разбор
    if report_op == OP_SALE:
        return "done"                       # продано и попало в финотчёт
    if demand_date and (today - demand_date).days > REPORT_LAG_DAYS:
        if seen:
            return "unredeemed"             # в отчёте только логистика → невыкуп, денег за товар нет
        return "unreported"                 # отгрузки нет в отчёте совсем → дыра, на разбор
    return "other"                          # свежая: ждём финотчёт (лаг до 7 недель)


def collect_account(account, since="2026-01-01"):
    if not MDC.TOK:
        print("[wb-cogs] нет MOYSKLAD_TOKEN")
        return 0
    org_name = ACC_ORG[account]
    frozen = _frozen_set(account)
    since = _eff_since(account, since, frozen)
    if frozen:
        print(f"[wb-cogs][{account}] закрыто: {len(frozen)} мес, собираю с {since}")
    org_href = MDC._resolve_href("organization", org_name)
    org_id = MDC._hid(org_href)
    today = dt.date.today()

    ff = fifo_fallback.load()            # FIFO тех же товаров МС — импутация без cost_seb
    cost_seb = _cost_seb_map()          # аварийный хвост: товар в МС ни разу не отгружался
    manual = _manual_map(account)
    known = _known_map(account)
    fifo = _fifo_map(org_id, since)
    report_op = _report_op_map(account)
    report_seen = _report_seen_set(account)
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
                    print(f"[wb-cogs][{account}] byoperation {nm}: {e}")
                    p = []
            if cogs <= 0:
                # Себест списания — только FIFO (решение Сергея 2026-08-13). Своего FIFO у
                # документа нет → берём FIFO ТЕХ ЖЕ товаров МС по ближайшей отгрузке до этой
                # даты; cost_seb (средняя по остатку из карточки) — лишь аварийный хвост.
                cogs, method = ff.impute(p, d["moment"])
                if not cogs:
                    # FIFO не существует НИГДЕ (товар ни разу не отгружался до этой даты) —
                    # цифра из карточки МС себестоимостью списания не является. Помечаем
                    # `need_manual`: строка ждёт ручного ввода человеком (решение Сергея
                    # 2026-08-14), виден флаг в провале «Отчёты МП» → «Себестоимость».
                    cogs = sum(cost_seb.get(x["ms_id"], 0) * x["qty"] for x in p)
                    method = "need_manual"
            if not qty:
                qty = sum(x["qty"] for x in p)
        if nm in manual:                                # ручной себест — истина, побеждает всё
            cogs, method = manual[nm], "manual"
        op = report_op.get(nm)
        stats[method] += 1
        recs.append({
            "account": account, "demand_name": nm, "demand_id": did,
            "ym": d["moment"][:7] + "-01", "demand_date": d["moment"],
            "our_sum": round(d["sum"], 2), "qty": qty,
            "cogs": round(cogs, 2), "method": method,
            "status": _classify(op, nm in report_seen, ret_store.get(nm),
                                dt.date.fromisoformat(d["moment"]), today),
            "status_raw": op})
    if recs:
        db.upsert("wb_cogs_demand", recs, conflict_cols=["account", "demand_name"])
    print(f"[wb-cogs][{account}] отгрузок записано: {len(recs)} "
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
