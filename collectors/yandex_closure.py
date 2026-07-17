"""collectors/yandex_closure.py — выручка и возвраты Яндекса из детализированного
отчёта о схождении с закрывающими документами (эталон ЛК → Финансы → Закрывающие
документы, файл period_closure_income_...).

API: POST /v2/reports/closure-documents/detalization/generate (contractType=INCOME),
опрос /reports/info/{id} до DONE, ZIP с CSV-листами. Денежная колонка TRANSACTION_SUM.
  period_closure_income_payments  → revenue (Получено от потребителей);
  period_closure_income_refunds   → returns (Возвращено потребителям, знак −);
  + *_sold_refunds / *_sold_defect_refunds → returns (брак; в наших данных пусто).

Проверено на январе 2026 до копейки: revenue 955629, returns −62018 (одна кампания
87623061 даёт консолидированный эталон account'а). При сборе всех campaignId одного
договора — дедуп по TRANSACTION_ID. Лимит API без подписки: 1 запрос / 2 мин (HTTP 420).

Пишет в raw_yandex_closure идемпотентно: снапшот на (account, ym).

Запуск:  ./venv/bin/python collectors/yandex_closure.py [2026-01] [2026-06]
"""
import os
import io
import csv
import sys
import time
import zipfile
import calendar
import datetime
import pathlib

import requests
import psycopg2.extras

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

API = "https://api.partner.market.yandex.ru"
ACCOUNT = "ya_acc1"
RATE_SLEEP = 125          # лимит 1 запрос / 2 мин на generate
_last_gen = [None]        # монотонная метка последнего generate (None → первый вызов не спит)

SHEET_CAT = {
    "period_closure_income_payments": "revenue",
    "period_closure_income_refunds": "returns",
    "period_closure_income_sold_refunds": "returns",
    "period_closure_income_sold_defect_refunds": "returns",
}


def _cfg():
    key = os.getenv("YANDEX_API_KEY_ACC1")
    camps = [c.strip() for c in (os.getenv("YANDEX_CAMPAIGN_ID_ACC1") or "").split(",") if c.strip()]
    if not key or not camps:
        raise RuntimeError("YANDEX_API_KEY_ACC1 / YANDEX_CAMPAIGN_ID_ACC1 не заданы")
    return key, camps


def _fnum(v):
    if v is None:
        return 0.0
    s = str(v).replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def _date(v):
    """TRANSACTION_DATE → date; принимает DD.MM.YYYY и YYYY-MM-DD (возможно со временем)."""
    if not v:
        return None
    s = str(v).strip()[:10]
    try:
        if "." in s:                       # DD.MM.YYYY
            d, m, y = s.split(".")
            return datetime.date(int(y), int(m), int(d))
        return datetime.date.fromisoformat(s)   # YYYY-MM-DD
    except (ValueError, IndexError):
        return None


def _closure_csvs(key, campaign_id, year, month, timeout=300):
    """Генерирует INCOME-отчёт и возвращает {sheet_name: [row_dict, ...]}."""
    H = {"Api-Key": key, "Content-Type": "application/json"}
    body = {"campaignId": int(campaign_id),
            "monthOfYear": {"year": year, "month": month},
            "contractType": "INCOME"}
    # соблюдаем лимит 1/2мин между generate (первый вызов — без ожидания)
    if _last_gen[0] is not None:
        wait = RATE_SLEEP - (time.monotonic() - _last_gen[0])
        if wait > 0:
            time.sleep(wait)
    rid = None
    for attempt in range(6):
        r = requests.post(f"{API}/v2/reports/closure-documents/detalization/generate",
                          headers=H, params={"format": "CSV"}, json=body, timeout=60)
        _last_gen[0] = time.monotonic()
        if r.status_code in (420, 429):
            time.sleep(RATE_SLEEP)
            continue
        r.raise_for_status()
        rid = r.json()["result"]["reportId"]
        break
    if not rid:
        raise RuntimeError(f"closure generate: rate-limit не отпустил (camp={campaign_id})")
    t0 = time.time()
    while time.time() - t0 < timeout:
        i = requests.get(f"{API}/reports/info/{rid}", headers=H, timeout=30).json().get("result", {})
        st = i.get("status")
        if st == "DONE":
            f = requests.get(i["file"], timeout=180)
            out = {}
            with zipfile.ZipFile(io.BytesIO(f.content)) as z:
                for name in z.namelist():
                    if name.endswith(".csv"):
                        out[name] = list(csv.DictReader(
                            io.TextIOWrapper(z.open(name), encoding="utf-8")))
            return out
        if st == "FAILED":
            raise RuntimeError(f"closure FAILED: {i.get('subStatus')}")
        time.sleep(3)
    raise RuntimeError(f"closure таймаут {timeout}с (camp={campaign_id})")


def _months(m_from, m_to):
    """Список (year, month, 'YYYY-MM') от m_from до m_to включительно."""
    y, m = int(m_from[:4]), int(m_from[5:7])
    ey, em = int(m_to[:4]), int(m_to[5:7])
    out = []
    while (y, m) <= (ey, em):
        out.append((y, m, f"{y:04d}-{m:02d}"))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return out


_COLS = ["account", "ym", "category", "transaction_id", "transaction_date", "order_id",
         "offer_id", "offer_name", "count", "amount", "campaign_id", "source"]


def _replace_month(account, ym, rows):
    """Атомарный снапшот месяца: DELETE прежних строк + INSERT новых в ОДНОЙ транзакции
    (get_conn коммитит при успехе, откатывает при ошибке — читатели не видят пустой месяц)."""
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM raw_yandex_closure WHERE account=%s AND ym=%s", (account, ym))
            if rows:
                sql = (f"INSERT INTO raw_yandex_closure ({', '.join(_COLS)}) "
                       f"VALUES ({', '.join(['%s'] * len(_COLS))})")
                psycopg2.extras.execute_batch(cur, sql, [[r[c] for c in _COLS] for r in rows])


def _parse_csvs(csvs, account, ym, cid):
    """Листы одного отчёта → строки для raw_yandex_closure.
    TRANSACTION_ID в closure-CSV пуст → ключ строки синтетический (ym+cid+лист+индекс);
    снапшот режется по (account, ym), поэтому уникальность в пределах месяца достаточна."""
    rows = []
    for name, recs in csvs.items():
        base = name[:-4] if name.endswith(".csv") else name
        cat = SHEET_CAT.get(base)
        if not cat:
            continue
        for rec in recs:
            amt = _fnum(rec.get("TRANSACTION_SUM"))
            if amt == 0:
                continue
            tid = rec.get("TRANSACTION_ID") or f"{ym}:{cid}:{base}:{len(rows)}"
            rows.append({
                "account": account, "ym": ym, "category": cat,
                "transaction_id": tid,
                "transaction_date": _date(rec.get("TRANSACTION_DATE")),
                "order_id": rec.get("ORDER_ID") or None,
                "offer_id": rec.get("OFFER_ID") or None,
                "offer_name": rec.get("OFFER_NAME") or None,
                "count": int(_fnum(rec.get("COUNT"))) if rec.get("COUNT") else None,
                "amount": round(amt, 2),
                "campaign_id": str(cid),
                "source": "api",
                "payload": None,
            })
    return rows


def collect(m_from=None, m_to=None, account=ACCOUNT):
    """Собирает выручку/возвраты по месяцам [m_from..m_to] ('YYYY-MM').
    По умолчанию — прошлый и текущий месяц. Идемпотентно: снапшот на (account, ym).

    Closure-отчёт консолидирован на уровне ДОГОВОРА: любой campaignId одного договора
    возвращает идентичные данные (проверено — 3 кампании дают один и тот же отчёт, TID пуст,
    дедуп невозможен). Поэтому берём ПЕРВУЮ кампанию, вернувшую выручку, и останавливаемся
    (это и корректно — иначе тройной счёт, — и втрое быстрее по лимиту API)."""
    key, camps = _cfg()
    today = datetime.date.today()
    if not m_from:
        cur = today.replace(day=1)
        prev = (cur - datetime.timedelta(days=1)).replace(day=1)
        m_from, m_to = prev.strftime("%Y-%m"), cur.strftime("%Y-%m")
    m_to = m_to or m_from
    total = 0
    for year, month, ym in _months(m_from, m_to):
        rows, used_cid, fetched = [], None, False
        for cid in camps:
            try:
                csvs = _closure_csvs(key, cid, year, month)
            except Exception as e:
                print(f"  [closure] {ym} camp={cid}: {e}", flush=True)
                continue
            fetched = True
            crows = _parse_csvs(csvs, account, ym, cid)
            if any(r["category"] == "revenue" for r in crows):
                rows, used_cid = crows, cid
                break                      # консолидированный отчёт получен — остальные идентичны
            # эта кампания без выручки (мёртвый магазин?) — пробуем следующую
        if not fetched:
            print(f"  [closure] {ym}: ПРОПУСК — ни одна кампания не ответила; "
                  "прежние данные месяца целы", flush=True)
            continue
        _replace_month(account, ym, rows)   # атомарно: DELETE + INSERT (rows=[] → пустой месяц)
        total += len(rows)
        rev = sum(r["amount"] for r in rows if r["category"] == "revenue")
        ret = sum(r["amount"] for r in rows if r["category"] == "returns")
        print(f"  [closure] {ym}: выручка {rev:,.2f} | возвраты {ret:,.2f} | "
              f"строк {len(rows)} | кампания {used_cid}".replace(",", " "), flush=True)
    print(f"Closure Яндекс: {total} строк за {m_from}..{m_to}", flush=True)
    return total


def closure_monthly(account=ACCOUNT):
    """{ym: {'revenue': x, 'returns': y}} — агрегат для yandex_monthly.
    returns — модуль (в БД знак −, выручка/возвраты витрины положительные)."""
    out = {}
    for r in db.query("""
        SELECT ym,
               sum(amount) FILTER (WHERE category='revenue')::float rev,
               sum(amount) FILTER (WHERE category='returns')::float ret
        FROM raw_yandex_closure WHERE account=%s GROUP BY ym""", (account,)):
        out[r["ym"]] = {"revenue": r["rev"] or 0.0, "returns": abs(r["ret"] or 0.0)}
    return out


def main():
    a = sys.argv
    m_from = a[1] if len(a) > 1 else None
    m_to = a[2] if len(a) > 2 else m_from
    collect(m_from, m_to)


if __name__ == "__main__":
    main()
