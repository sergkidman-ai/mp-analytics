# поток: inv
"""collectors/alfa_statement.py — выписка по расчётному счёту Альфа-Банка (Alfa API H2H).

Тянет операции по счёту за дату методом «Получение выписки»:

    GET {ALFA_API_BASE}/jp/v1/statement/transactions
        ?accountNumber=<счёт>&statementDate=YYYY-MM-DD[&page=N]
    Authorization: ApiKey <key>   + mTLS,  Accept: application/json
    Scope: transactions

Авторизация — упрощённая (API Key), плюс обязательный mTLS: клиентский серт + ЗАШИФРОВАННЫЙ
ключ (пароль в ALFA_KEY_PASSWORD). requests не умеет пароль на ключе напрямую, поэтому
поднимаем свой SSLContext через HTTPAdapter (load_cert_chain(..., password=...)).

Серверный серт песочницы подписан CA Минцифры (не APICA), поэтому в sandbox серверную
проверку отключаем (как `curl -k`); для ПРОМА добавить корни Минцифры в доверенные и
включить verify. Управляется ALFA_ENV (sandbox → verify off) / ALFA_VERIFY_SERVER.

Гигиена контекста (правило 11): сырой ответ ПИШЕТСЯ НА ДИСК (incoming/alfa/), в чат —
только агрегаты. Никогда не дампить сырой JSON и не печатать ключ/пароль.

Запуск:
    ./venv/bin/python collectors/alfa_statement.py <accountNumber> [YYYY-MM-DD]
    # без даты — сегодня; в песочнице дата игнорируется (фиксированный тестовый ответ).
"""
import os
import ssl
import sys
import json
import pathlib
import datetime as dt

import requests
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
# .env gitignored → в git-worktree его нет; фолбэк на канонический чекаут проекта.
_ENV = BASE_DIR / ".env"
load_dotenv(_ENV if _ENV.exists() else pathlib.Path("/opt/mp-analytics/.env"))

STATEMENT_PATH = "/jp/v1/statement/transactions"
RAW_DIR = BASE_DIR / "incoming" / "alfa"
MAX_PAGES = 100                      # предохранитель от бесконечной пагинации
PAGE_TIMEOUT = 60                    # сек на страницу


# ── конфиг из .env ───────────────────────────────────────────────────────────
def _cfg():
    base = os.getenv("ALFA_API_BASE", "").rstrip("/")
    key = os.getenv("ALFA_API_KEY", "")
    cert = os.getenv("ALFA_CERT_PATH", "")
    pkey = os.getenv("ALFA_KEY_PATH", "")
    pwd = os.getenv("ALFA_KEY_PASSWORD") or None
    ca = os.getenv("ALFA_CA_BUNDLE") or None
    env = (os.getenv("ALFA_ENV") or "sandbox").lower()
    # sandbox: серверный серт от Минцифры, APICA-бандлом не проверяется → verify off.
    verify_server = os.getenv("ALFA_VERIFY_SERVER")
    verify = (verify_server == "1") if verify_server is not None else (env != "sandbox")
    missing = [n for n, v in [("ALFA_API_BASE", base), ("ALFA_API_KEY", key),
                              ("ALFA_CERT_PATH", cert), ("ALFA_KEY_PATH", pkey)] if not v]
    if missing:
        sys.exit(f"нет в .env: {', '.join(missing)}")
    return dict(base=base, key=key, cert=cert, pkey=pkey, pwd=pwd, ca=ca,
                env=env, verify=verify)


# ── mTLS-адаптер (клиентский серт + зашифрованный ключ с паролем) ────────────
class _MTLSAdapter(HTTPAdapter):
    def __init__(self, cert, key, password, ca=None, verify=True, **kw):
        self._cert, self._key, self._pwd = cert, key, password
        self._ca, self._verify = ca, verify
        super().__init__(**kw)

    def _ctx(self):
        ctx = ssl.create_default_context()
        if not self._verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        elif self._ca:
            ctx.load_verify_locations(self._ca)
        ctx.load_cert_chain(self._cert, self._key, password=self._pwd)
        return ctx

    def init_poolmanager(self, *a, **kw):
        kw["ssl_context"] = self._ctx()
        return super().init_poolmanager(*a, **kw)

    def proxy_manager_for(self, *a, **kw):
        kw["ssl_context"] = self._ctx()
        return super().proxy_manager_for(*a, **kw)


def _session(cfg):
    s = requests.Session()
    ad = _MTLSAdapter(cfg["cert"], cfg["pkey"], cfg["pwd"], ca=cfg["ca"],
                      verify=cfg["verify"])
    s.mount("https://", ad)
    # requests на уровне send() заново применяет verify поверх ssl_context —
    # задаём и на сессии: sandbox → False, пром → путь к CA (Минцифры).
    s.verify = (cfg["ca"] or True) if cfg["verify"] else False
    s.headers.update({"Authorization": f"ApiKey {cfg['key']}",
                      "Accept": "application/json"})
    if not cfg["verify"]:
        # заглушаем InsecureRequestWarning только для песочницы
        from urllib3.exceptions import InsecureRequestWarning
        requests.packages.urllib3.disable_warnings(InsecureRequestWarning)
    return s


def _has_next(payload):
    for lnk in (payload.get("_links") or []):
        if lnk.get("rel") == "next":
            return True
    return False


# ── нормализация операции (плоская запись для стыковки с МС) ──────────────────
def normalize(txn):
    """Плоская запись из операции выписки. Рубли → rurTransfer; поля контрагента
    зависят от направления: для CREDIT контрагент = плательщик (payer*), для DEBIT
    = получатель (payee*)."""
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
        "uuid": txn.get("uuid"),
        "transaction_id": txn.get("transactionId"),
        "direction": direction,
        "amount": amt.get("amount"),
        "currency": amt.get("currencyName"),
        "operation_date": txn.get("operationDate"),
        "document_date": txn.get("documentDate"),
        "document_number": txn.get("number"),
        "corresponding_account": txn.get("correspondingAccount"),
        "purpose": txn.get("paymentPurpose"),
        "counterparty_name": cp_name,
        "counterparty_inn": cp_inn,
        "counterparty_kpp": cp_kpp,
        "counterparty_account": cp_acc,
        "counterparty_bic": cp_bic,
    }


# ── основной вызов: выписка за дату (со всеми страницами) ─────────────────────
def fetch_statement(account, statement_date=None, save_raw=True, session=None, cfg=None):
    """Возвращает {'account','date','transactions':[...сырьё...],'normalized':[...]}.
    Сырьё каждой страницы пишется на диск (incoming/alfa/), в память кладём только
    список операций (не HTTP-обёртку). Ключ/пароль в чат/лог не попадают."""
    cfg = cfg or _cfg()
    s = session or _session(cfg)
    date = statement_date or dt.date.today().isoformat()
    url = f"{cfg['base']}{STATEMENT_PATH}"

    txns, page = [], 1
    if save_raw:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
    while page <= MAX_PAGES:
        params = {"accountNumber": account, "statementDate": date, "page": page}
        r = s.get(url, params=params, timeout=PAGE_TIMEOUT)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code} на стр.{page}: "
                               f"{r.text[:200]!r}")
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
            "normalized": [normalize(t) for t in txns]}


# ── CLI: короткая сводка, без сырого дампа ───────────────────────────────────
def main(argv):
    if not argv:
        sys.exit("usage: alfa_statement.py <accountNumber> [YYYY-MM-DD]")
    account = argv[0]
    date = argv[1] if len(argv) > 1 else None
    res = fetch_statement(account, date)
    n = res["normalized"]
    tot_cr = sum(x["amount"] or 0 for x in n if x["direction"] == "CREDIT")
    tot_db = sum(x["amount"] or 0 for x in n if x["direction"] == "DEBIT")
    print(f"счёт {res['account']}  дата {res['date']}  операций: {len(n)}")
    print(f"приход (CREDIT): {tot_cr:.2f}   расход (DEBIT): {tot_db:.2f}")
    print(f"сырьё: {RAW_DIR}/stmt_{res['account']}_{res['date']}_p*.json")
    print("--- первые операции (напр, сумма, дата, контрагент, назначение[:35]) ---")
    for x in n[:15]:
        print(f"{x['direction']:6} {str(x['amount']):>10} {(x['operation_date'] or '')[:10]} "
              f"{(x['counterparty_name'] or '—')[:20]:20} {(x['purpose'] or '')[:35]}")


if __name__ == "__main__":
    main(sys.argv[1:])
