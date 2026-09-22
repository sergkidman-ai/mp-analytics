# поток: cf
"""cf/yandex_netting.py — невыплаченное Маркетом по дням доставки → cf_ya_netting_day.

Метода «Финансы → Предстоящие выплаты» в Partner API нет. Замена — отчёт по платежам:
    POST /v2/reports/united-netting/generate?format=CSV  {businessId, dateFrom, dateTo}
    GET  /v2/reports/info/{id} → DONE → ZIP с transaction_date.csv
Строки «Будет переведён по графику выплат» / «Будет удержан из платежей покупателей» — ещё без
платёжки, это и есть предстоящая выплата; «Переведён»/«Удержан» — уже выплачено (сверено с банком
до копейки 22.09.2026). TRANSACTION_SUM уже со знаком (удержания отрицательные). Строки без даты
доставки — недоставленные заказы, в выплату ЛК не входят → пропускаем. Сверка со скрином ЛК
22.09: 1–7.09 335,9 тыс. против 329,8, 8–14.09 130,8 против 122,9. Дату выплаты ставит
cf/mp_forecast.py по графику Маркета (конец периода + 28 дней).
Лимит: 1 генерация в 2 минуты, окно ≤ 3 месяцев. Зип кладём в incoming/yandex_netting/.

Запуск:  ./venv/bin/python -m cf.yandex_netting
"""
import csv
import datetime as dt
import io
import os
import pathlib
import sys
import time
import zipfile
from collections import defaultdict

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import db  # noqa: E402

API = "https://api.partner.market.yandex.ru"
ACCOUNT = "ya_acc1"
WINDOW_DAYS = 70
RAW_DIR = ROOT / "incoming" / "yandex_netting"
KIND = {"Будет переведён по графику выплат": "pending",
        "Будет удержан из платежей покупателей": "pending",
        "Переведён по графику выплат": "paid",
        "Удержан из платежей покупателей": "paid"}


def _date(s):
    return dt.datetime.strptime(s[:10], "%d.%m.%Y").date() if s else None


def fetch(date_from, date_to):
    key, biz = os.getenv("YANDEX_API_KEY_ACC1"), os.getenv("YANDEX_BUSINESS_ID_ACC1")
    if not key or not biz:
        raise RuntimeError("нет YANDEX_API_KEY_ACC1 / YANDEX_BUSINESS_ID_ACC1 в .env")
    h = {"Api-Key": key, "Content-Type": "application/json"}
    r = requests.post(f"{API}/v2/reports/united-netting/generate?format=CSV", headers=h,
                      json={"businessId": int(biz), "dateFrom": date_from.isoformat(),
                            "dateTo": date_to.isoformat()}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"generate HTTP {r.status_code}: {r.text[:200]!r}")
    rid = r.json()["result"]["reportId"]
    for _ in range(60):
        time.sleep(10)
        info = requests.get(f"{API}/v2/reports/info/{rid}", headers=h, timeout=30).json()
        res = info.get("result") or {}
        if res.get("status") == "DONE":
            return requests.get(res["file"], timeout=120).content
        if res.get("status") == "FAILED":
            raise RuntimeError(f"отчёт FAILED: {res.get('subStatus')}")
    raise RuntimeError("отчёт не готов за 10 минут")


def parse(blob):
    days = defaultdict(lambda: {"pending": 0.0, "paid": 0.0, "rows_cnt": 0})
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        name = next(n for n in z.namelist() if n.endswith(".csv"))
        text = z.read(name).decode("utf-8-sig")
    for row in csv.DictReader(io.StringIO(text)):
        kind = KIND.get(row["PAYMENT_STATUS"])
        d = _date(row["ORDER_DELIVERY_DATE"])
        if not kind or not d:
            continue
        days[d][kind] += float(row["TRANSACTION_SUM"] or 0)
        days[d]["rows_cnt"] += 1
    return days


def main():
    today = dt.date.today()
    blob = fetch(today - dt.timedelta(days=WINDOW_DAYS), today)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    (RAW_DIR / f"netting_{today}.zip").write_bytes(blob)
    days = parse(blob)
    rows = [dict(account=ACCOUNT, day=d, pending=round(v["pending"], 2),
                 paid=round(v["paid"], 2), rows_cnt=v["rows_cnt"]) for d, v in days.items()]
    with db.get_conn() as conn, conn.cursor() as cur:
        cur.execute("delete from cf_ya_netting_day where account=%s", (ACCOUNT,))
    db.upsert("cf_ya_netting_day", rows, ["account", "day"])
    pend = sum(r["pending"] for r in rows)
    print(f"[ok] Маркет: {len(rows)} дней доставки, ждёт выплаты "
          + f"{pend:,.0f} ₽".replace(",", " "))
    return rows


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    main()
