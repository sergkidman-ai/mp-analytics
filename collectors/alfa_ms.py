# поток: inv
"""collectors/alfa_ms.py — проводки выписки Альфа-Банка → МойСклад (paymentin/paymentout).

CREDIT (приход) → paymentin, DEBIT (расход) → paymentout. Источник — нормализованные
операции из collectors/alfa_statement.fetch_statement().

Идемпотентность: банковский `uuid` операции — готовый GUID, кладём его в МС `syncId` и
пишем через create-or-update `PUT /entity/{type}/syncid/{uuid}` — повторный прогон за тот
же период НЕ плодит дублей (правило 3). Если uuid нет — детерминированный uuid5 от
transactionId.

Контрагент: по ИНН (payer для CREDIT, payee для DEBIT), иначе по имени; не нашли — создаём
карточку (name + inn при наличии). Организация — владелец счёта (ALFA_ORG_INN, по умолч.
Цифровой Квадрат 7807355364).

БЕЗОПАСНОСТЬ: по умолчанию DRY-RUN (только чтение МС + план). `--apply` реально пишет в МС —
запускать ТОЛЬКО на настоящих выписках; данные песочницы фейковые, в боевой МС их не льём.

Запуск:
    ./venv/bin/python collectors/alfa_ms.py <accountNumber> [YYYY-MM-DD]           # dry-run
    ./venv/bin/python collectors/alfa_ms.py <accountNumber> [YYYY-MM-DD] --apply   # запись
"""
import os
import sys
import uuid as _uuid
import pathlib
import urllib.parse

HERE = pathlib.Path(__file__).resolve().parent           # каталог collectors/
BASE_DIR = HERE.parent
sys.path.insert(0, str(HERE))                            # сосед alfa_statement.py
sys.path.insert(0, "/opt/mp-analytics/invoice_bot")
from ms import get, post, put, MS                        # noqa: E402  invoice_bot/ms.py
from alfa_statement import fetch_statement               # noqa: E402  сосед по каталогу

ORG_INN = os.getenv("ALFA_ORG_INN", "7807355364")        # Цифровой Квадрат
_NS = _uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # namespace для uuid5


def _meta(ent, i, t=None):
    return {"meta": {"href": f"{MS}/entity/{ent}/{i}", "type": t or ent,
                     "mediaType": "application/json"}}


def _norm(s):
    return " ".join((s or "").lower().split())


def _ms_dt(iso):
    # "2026-07-22T00:00:00Z" → "2026-07-22 00:00:00"
    return (iso or "").replace("T", " ").replace("Z", "")[:19] or None


def _sync_id(op):
    if op.get("uuid"):
        return op["uuid"]
    seed = op.get("transaction_id") or f"{op.get('operation_date')}|{op.get('amount')}"
    return str(_uuid.uuid5(_NS, seed))


def _day_bounds(day):
    return f"{day} 00:00:00", f"{day} 23:59:59"


def existing_index(typ, day):
    """Индекс платежей, УЖЕ лежащих в МС за этот день.

    Зачем: выписку по счёту в МС годами заводили руками (загрузка банк-файла), у таких
    документов нет нашего `syncId` — по нему мы их не увидим и завели бы вторые копии.
    Поэтому перед записью сверяемся ещё и «по-человечески»: сумма + номер банковского
    документа + день. Ключ `sync` — наши собственные документы, их PUT просто обновит.
    """
    lo, hi = _day_bounds(day)
    flt = urllib.parse.quote(f"moment>={lo};moment<={hi}")
    idx = {"by_num": {}, "by_sum": {}, "sync": set(), "total": 0}
    offset = 0
    while True:
        r = get(f"/entity/{typ}?filter={flt}&limit=100&offset={offset}")
        rows = r.get("rows", [])
        for d in rows:
            s = d.get("sum")
            if d.get("syncId"):
                idx["sync"].add(d["syncId"])
            for num in {str(d.get("name") or "").strip(),
                        str(d.get("incomingNumber") or "").strip()}:
                if num:
                    idx["by_num"].setdefault((s, num), []).append(d)
            idx["by_sum"].setdefault(s, []).append(d)
        idx["total"] += len(rows)
        if len(rows) < 100:
            break
        offset += 100
    return idx


def find_existing(idx, op, sync_id):
    """→ (документ|None, причина). None = такого платежа в МС ещё нет."""
    if sync_id in idx["sync"]:
        return None, "ours"                       # наш же документ, PUT его обновит
    s = round((op.get("amount") or 0) * 100)
    num = str(op.get("document_number") or "").strip()
    if num and (s, num) in idx["by_num"]:
        return idx["by_num"][(s, num)][0], "номер+сумма"
    same_sum = idx["by_sum"].get(s) or []
    if same_sum:
        # Номер не сошёлся, но сумма за тот же день уже есть. Осознанно считаем дублем:
        # пропущенный платёж виден при сверке, задвоенный — портит учёт молча.
        return same_sum[0], "сумма+день"
    return None, ""


def resolve_org():
    rows = get("/entity/organization")["rows"]
    for o in rows:
        if o.get("inn") == ORG_INN:
            return o
    raise SystemExit(f"организация с ИНН {ORG_INN} не найдена в МС")


def resolve_agent(inn, name, apply):
    """→ (agent_dict|None, статус). Матч по ИНН → по имени → создание (в apply)."""
    if inn:
        rows = get(f"/entity/counterparty?filter=inn={inn}")["rows"]
        if rows:
            return rows[0], "inn"
    if name:
        q = urllib.parse.quote(name)
        for r in get(f"/entity/counterparty?search={q}&limit=5")["rows"]:
            if _norm(r["name"]) == _norm(name):
                return r, "name"
    # не нашли
    if not apply:
        return None, "would-create"
    body = {"name": name or "Без наименования"}
    if inn:
        body["inn"] = inn
    st, resp = post("/entity/counterparty", body)
    if st not in (200, 201):
        return None, f"create-fail:{st}"
    return resp, "created"


def build_payment(op, org, agent):
    typ = "paymentin" if op["direction"] == "CREDIT" else "paymentout"
    body = {
        "organization": _meta("organization", org["id"]),
        "agent": _meta("counterparty", agent["id"], "counterparty"),
        "sum": round((op["amount"] or 0) * 100),           # МС хранит в копейках
        "moment": _ms_dt(op["operation_date"]),
        "paymentPurpose": op["purpose"] or "",
        "syncId": _sync_id(op),
    }
    if typ == "paymentin":                                  # у paymentout нет incoming*
        if op.get("document_number"):
            body["incomingNumber"] = str(op["document_number"])
        idate = _ms_dt(op.get("document_date") or op["operation_date"])
        if idate:
            body["incomingDate"] = idate
    return typ, body


def sync(normalized, apply=False):
    org = resolve_org()
    stats = {"paymentin": 0, "paymentout": 0, "matched": 0, "created": 0,
             "would_create": 0, "errors": 0, "existing": 0, "before_cutoff": 0}
    plan = []
    idx_cache = {}
    # дата отсечки: операции раньше неё не пишем (до неё документы заводились руками)
    since = os.getenv("ALFA_MS_SINCE") or None
    for op in normalized:
        typ = "paymentin" if op["direction"] == "CREDIT" else "paymentout"
        stats[typ] += 1                                    # намеченный тип всегда
        day = (op.get("operation_date") or "")[:10]
        sid = _sync_id(op)

        if since and day and day < since:
            stats["before_cutoff"] += 1
            plan.append({"dir": op["direction"], "sum": op["amount"], "typ": typ,
                         "agent": op["counterparty_name"], "agent_status": "до отсечки",
                         "written": False, "syncId": sid})
            continue

        if day:                                            # антидубль с ручной загрузкой
            key = (typ, day)
            if key not in idx_cache:
                idx_cache[key] = existing_index(typ, day)
            dup, why = find_existing(idx_cache[key], op, sid)
            if dup is not None:
                stats["existing"] += 1
                plan.append({"dir": op["direction"], "sum": op["amount"], "typ": typ,
                             "agent": op["counterparty_name"],
                             "agent_status": f"уже есть ({why})", "written": False,
                             "syncId": sid})
                continue

        agent, ast = resolve_agent(op["counterparty_inn"], op["counterparty_name"], apply)
        if agent is None:
            # без агента платёж в МС не создать — фиксируем в плане, но не пишем
            stats["would_create" if ast == "would-create" else "errors"] += 1
            plan.append({"dir": op["direction"], "sum": op["amount"],
                         "agent": op["counterparty_name"], "agent_status": ast,
                         "typ": typ, "written": False, "syncId": _sync_id(op)})
            continue
        stats["created" if ast == "created" else "matched"] += 1
        _, body = build_payment(op, org, agent)
        written = False
        if apply:
            st, resp = put(f"/entity/{typ}/syncid/{body['syncId']}", body)
            if st in (200, 201):
                written = True
            else:
                stats["errors"] += 1
        plan.append({"dir": op["direction"], "sum": op["amount"],
                     "agent": agent["name"], "agent_status": ast, "typ": typ,
                     "written": written, "syncId": body["syncId"]})
    return stats, plan


def main(argv):
    apply = "--apply" in argv
    pos = [a for a in argv if not a.startswith("--")]
    if not pos:
        sys.exit("usage: alfa_ms.py <accountNumber> [YYYY-MM-DD] [--apply]")
    account = pos[0]
    date = pos[1] if len(pos) > 1 else None
    res = fetch_statement(account, date)
    stats, plan = sync(res["normalized"], apply=apply)
    mode = "APPLY (запись в МС)" if apply else "DRY-RUN (только план)"
    print(f"[{mode}] счёт {account} дата {res['date']} — операций {len(res['normalized'])}")
    print(f"paymentin(приход) {stats['paymentin']}  paymentout(расход) {stats['paymentout']}  "
          f"уже в МС {stats['existing']}  до отсечки {stats['before_cutoff']}  "
          f"агент: matched {stats['matched']} / created {stats['created']} / "
          f"would-create {stats['would_create']}  ошибок {stats['errors']}")
    print("--- план (напр, сумма, контрагент, тип, агент-статус, записан) ---")
    for p in plan[:20]:
        print(f"{p['dir']:6} {str(p['sum']):>8} {(p['agent'] or '—')[:22]:22} "
              f"{p['typ']:11} {p['agent_status']:12} {'да' if p['written'] else '—'}")


if __name__ == "__main__":
    main(sys.argv[1:])
