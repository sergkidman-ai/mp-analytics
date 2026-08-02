# поток: inv
"""collectors/sber_statement.py — выписка по счетам ООО «ДИСКВЭР» (SberBusinessAPI H2H).

    GET {API_HOST}/fintech/api/v2/statement/transactions
        ?accountNumber=<20 цифр>&statementDate=YYYY-MM-DD&page=N
    Authorization: Bearer <access_token>  + mTLS   (всё это даёт sber_auth.api)

ВАЖНО, проверено на живом ПРОМе 2026-08-02: путь из официальной спецификации
(`/fintech/api/v1/statement/...`) отвечает 404 «Не найден указанный urlPath» —
боевой контур обслуживает **v2**. Спецификация в этом месте устарела.
`/v1/client-info` при этом жив, версии эндпоинтов независимы.

Счета берём не из .env, а из `client-info` (поле `accounts`): у Дисквэра их 8, но
действующий расчётный — один (`state=OPEN`, `type=calculated`), остальные 7 — закрытые
депозиты, по ним банк отвечает 400 WORKFLOW_FAULT «Счёт не является действующим».
Поэтому по умолчанию ходим только по OPEN-счетам.

Формат операции (v2): `uuid`, `operationId`, `amount{amount,currencyName}`, `amountRub`,
`direction` CREDIT/DEBIT, `operationDate`, `documentDate`, `number`, `operationCode`,
`priority`, `correspondingAccount`, `paymentPurpose`, `hashAbc` + блок `rurTransfer`
(payer*/payee*, valueDate, receiptDate, purposeCode, deliveryKind).
Поля `transactionId` у Сбера НЕТ (в отличие от Альфы) — ключ операции = `uuid`,
запасной — `operationId`.

Гигиена контекста (правило 11): сырьё пишется на диск (incoming/sber/), в чат — только
агрегаты. Никогда не дампить сырой JSON и не печатать токены.

Запуск:
    ./venv/bin/python collectors/sber_statement.py --accounts          # счета организации
    ./venv/bin/python collectors/sber_statement.py 2026-07-31          # выписка за дату
    ./venv/bin/python collectors/sber_statement.py 2026-07-28 2026-07-31   # период
    ./venv/bin/python collectors/sber_statement.py --account <20 цифр> 2026-07-31
"""
import sys
import json
import pathlib
import datetime as dt
from decimal import Decimal, InvalidOperation

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
CANON = pathlib.Path("/opt/mp-analytics")          # канонический чекаут проекта
# .env и incoming/ gitignored → в git-worktree их нет; сырьё копим в ОДНОМ месте.
PROJECT_ROOT = BASE_DIR if (BASE_DIR / ".env").exists() else CANON
sys.path.insert(0, str(PROJECT_ROOT))

from collectors import sber_auth as sa                            # noqa: E402

STATEMENT_PATH = "/fintech/api/v2/statement/transactions"
CLIENT_INFO_PATH = "/fintech/api/v1/client-info"
RAW_DIR = PROJECT_ROOT / "incoming" / "sber"
MAX_PAGES = 100                      # предохранитель от бесконечной пагинации
PAGE_TIMEOUT = 60                    # сек на страницу


# ── счета организации ────────────────────────────────────────────────────────
def accounts(only_open=True):
    """Список счетов из client-info. only_open → только действующие расчётные
    (по закрытым депозитам выписка отвечает 400 WORKFLOW_FAULT)."""
    r = sa.api("GET", CLIENT_INFO_PATH, timeout=PAGE_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"client-info HTTP {r.status_code}: {r.text[:200]!r}")
    accs = r.json().get("accounts") or []
    if only_open:
        accs = [a for a in accs if (a.get("state") or "").upper() == "OPEN"]
    return accs


def _num(v):
    """Суммы Сбер отдаёт СТРОКОЙ ("12345.67"), Альфа — числом. Приводим к Decimal:
    копейка не должна страдать от float, а МС всё равно умножает на 100 и округляет."""
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _has_next(payload):
    for lnk in (payload.get("_links") or []):
        if lnk.get("rel") == "next":
            return True
    return False


# ── нормализация операции (плоская запись для стыковки с МС) ──────────────────
def normalize(txn, account=None):
    """Плоская запись из операции выписки. Рубли → rurTransfer; поля контрагента
    зависят от направления: для CREDIT контрагент = плательщик (payer*), для DEBIT
    = получатель (payee*). Совместима по ключам с collectors/alfa_statement.normalize,
    чтобы дальнейшие слои (МС, привязка к приёмкам) были банконезависимы."""
    rur = txn.get("rurTransfer") or {}
    direction = txn.get("direction")            # CREDIT (приход) / DEBIT (расход)
    amt = txn.get("amount") or {}
    if direction == "CREDIT":
        cp_name, cp_inn = rur.get("payerName"), rur.get("payerInn")
        cp_acc, cp_kpp = rur.get("payerAccount"), rur.get("payerKpp")
        cp_bic = rur.get("payerBankBic")
    else:
        cp_name, cp_inn = rur.get("payeeName"), rur.get("payeeInn")
        cp_acc, cp_kpp = rur.get("payeeAccount"), rur.get("payeeKpp")
        cp_bic = rur.get("payeeBankBic")
    return {
        "bank": "sber",
        "account": account,
        "uuid": txn.get("uuid"),
        # у Сбера нет transactionId — держим ключ ради совместимости с Альфой
        "transaction_id": txn.get("operationId"),
        "direction": direction,
        "amount": _num(amt.get("amount")),
        "currency": amt.get("currencyName"),
        "amount_rub": _num(txn.get("amountRub")),
        "operation_date": txn.get("operationDate"),
        "document_date": txn.get("documentDate"),
        "document_number": txn.get("number"),
        "operation_code": txn.get("operationCode"),
        "corresponding_account": txn.get("correspondingAccount"),
        "purpose": txn.get("paymentPurpose"),
        "counterparty_name": cp_name,
        "counterparty_inn": cp_inn,
        "counterparty_kpp": cp_kpp,
        "counterparty_account": cp_acc,
        "counterparty_bic": cp_bic,
    }


# ── основной вызов: выписка за дату (со всеми страницами) ─────────────────────
def fetch_statement(account, statement_date=None, save_raw=True, strict=True):
    """Возвращает {'account','date','transactions':[...сырьё...],'normalized':[...],
    'status': 'ok'|'not_active'}. Сырьё каждой страницы пишется на диск (incoming/sber/).

    strict=False → закрытый/недействующий на дату счёт (400 WORKFLOW_FAULT) не роняет
    прогон, а возвращает status='not_active' с пустым списком."""
    date = statement_date or dt.date.today().isoformat()
    if save_raw:
        RAW_DIR.mkdir(parents=True, exist_ok=True)

    txns, page = [], 1
    while page <= MAX_PAGES:
        params = {"accountNumber": account, "statementDate": date, "page": page}
        r = sa.api("GET", STATEMENT_PATH, params=params, timeout=PAGE_TIMEOUT)
        if r.status_code != 200:
            cause = ""
            try:
                cause = (r.json() or {}).get("cause") or ""
            except ValueError:
                pass
            if cause == "WORKFLOW_FAULT" and not strict:
                return {"account": account, "date": date, "transactions": [],
                        "normalized": [], "status": "not_active"}
            raise RuntimeError(f"HTTP {r.status_code} на стр.{page} "
                               f"(счёт …{account[-6:]}, {date}): {r.text[:200]!r}")
        payload = r.json()
        if save_raw:
            fn = RAW_DIR / f"stmt_{account}_{date}_p{page}.json"
            fn.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        chunk = payload.get("transactions") or []
        txns.extend(chunk)
        if not chunk or not _has_next(payload):
            break
        page += 1

    return {"account": account, "date": date, "transactions": txns,
            "normalized": [normalize(t, account) for t in txns], "status": "ok"}


def fetch_period(date_from, date_to, accs=None, save_raw=True):
    """Выписка за период по списку счетов (по умолчанию — все действующие).
    Банк отдаёт выписку строго за один день, поэтому идём по дням."""
    accs = accs or [a["number"] for a in accounts(only_open=True)]
    d0 = dt.date.fromisoformat(date_from)
    d1 = dt.date.fromisoformat(date_to)
    out = []
    d = d0
    while d <= d1:
        for acc in accs:
            res = fetch_statement(acc, d.isoformat(), save_raw=save_raw, strict=False)
            out.extend(res["normalized"])
        d += dt.timedelta(days=1)
    return out


# ── CLI: короткая сводка, без сырого дампа ───────────────────────────────────
def main(argv):
    if "--accounts" in argv:
        accs = accounts(only_open=False)
        print(f"счетов у организации: {len(accs)}")
        for a in accs:
            print(f"  …{a['number'][-6:]}  {a.get('state','?'):7} {str(a.get('type',''))[:12]:12} "
                  f"вал.{a.get('currencyCode','')}  {str(a.get('name',''))[:34]}")
        return 0

    acc = None
    if "--account" in argv:
        i = argv.index("--account")
        acc = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]

    dates = [a for a in argv if not a.startswith("--")]
    d_from = dates[0] if dates else dt.date.today().isoformat()
    d_to = dates[1] if len(dates) > 1 else d_from
    accs = [acc] if acc else [a["number"] for a in accounts(only_open=True)]

    rows = fetch_period(d_from, d_to, accs=accs)
    tot_cr = sum(x["amount"] or 0 for x in rows if x["direction"] == "CREDIT")
    tot_db = sum(x["amount"] or 0 for x in rows if x["direction"] == "DEBIT")
    print(f"период {d_from}…{d_to}, счетов {len(accs)}, операций: {len(rows)}")
    print(f"приход (CREDIT): {tot_cr:.2f}   расход (DEBIT): {tot_db:.2f}")
    print(f"сырьё: {RAW_DIR}/stmt_<счёт>_<дата>_p*.json")
    print("--- первые операции (напр, сумма, дата, контрагент, назначение[:32]) ---")
    for x in rows[:15]:
        print(f"{x['direction']:6} {str(x['amount']):>12} {(x['operation_date'] or '')[:10]} "
              f"{(x['counterparty_name'] or '—')[:22]:22} {(x['purpose'] or '')[:32]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
