"""Уведомления о выкупе Wildberries → wb_redeem_notice.

Выкупая наш товар, ВБ выпускает «Уведомление о выкупе»; на каждое такое уведомление
продавец выставляет РВБ УПД и отправляет через ЭДО. Здесь — только забор и разбор
уведомлений, сборка XML живёт в reports/upd_form.py.

Особенности API документов ВБ:
  * хост отдельный — documents-api.wildberries.ru, и токен нужен со скоупом
    «Документы»: обычные WB_TOKEN_ACC* отвечают 403;
  * единственное доступное расширение — zip; внутри XLSX с таблицей, .sig (УКЭП) и
    МЧД. PDF/XML ВБ не отдаёт, поэтому позиции разбираем из XLSX и храним у себя;
  * лимит 1 запрос в 10 секунд на продавца — поэтому скачиваем только то, чего ещё
    нет в базе, и спим между запросами;
  * в списке две даты: creationTime — когда документ выложили в ЛК, а дата в имени —
    дата самого уведомления. В УПД идёт вторая.

Запуск:  venv/bin/python -m collectors.wb_documents --from 2026-01-01
"""
import argparse
import base64
import io
import os
import re
import sys
import time
import zipfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import openpyxl
import psycopg2.extras
import requests
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")

BASE = "https://documents-api.wildberries.ru/api/v1/documents"
CATEGORY = "redeem-notification"
ACCOUNTS = ("wb_acc1", "wb_acc2")
# Скоуп «Документы» у Цифрового живёт в токене цен, у Дисквэра — в отдельном.
TOKEN_ENV = {"wb_acc1": ("WB_TOKEN_DOCS_ACC1", "WB_TOKEN_PRICES_ACC1"),
             "wb_acc2": ("WB_TOKEN_DOCS_ACC2",)}
PAUSE = 11          # лимит 1 запрос / 10 с на продавца, берём с запасом
NAME_RE = re.compile(r"№\s*(\d+)\s*от\s*(\d{4}-\d{2}-\d{2})")


def _token(account):
    for env in TOKEN_ENV[account]:
        t = os.getenv(env)
        if t:
            return t.strip()
    raise RuntimeError(f"нет токена со скоупом «Документы» для {account}: "
                       f"ожидались {TOKEN_ENV[account]}")


def _num(v):
    """«2 700,18» / «5%» / None → Decimal. Прочерк и пустое → 0."""
    s = str(v or "").replace("\xa0", "").replace(" ", "").replace(",", ".").rstrip("%")
    if s in ("", "-", "—", "None"):
        return Decimal(0)
    return Decimal(s)


def list_notices(account, d_from, d_to):
    r = requests.get(f"{BASE}/list", headers={"Authorization": _token(account)},
                     params={"category": CATEGORY, "beginTime": d_from.isoformat(),
                             "endTime": d_to.isoformat(), "locale": "ru"}, timeout=60)
    r.raise_for_status()
    body = r.json()
    return (body.get("data") or body).get("documents") or []


def download(account, service_name):
    """zip уведомления → bytes."""
    r = requests.get(f"{BASE}/download", headers={"Authorization": _token(account)},
                     params={"serviceName": service_name, "extension": "zip"}, timeout=120)
    r.raise_for_status()
    body = r.json()
    doc = (body.get("data") or body)["document"]
    return base64.b64decode(doc)


def parse_zip(raw):
    """zip с XLSX → (позиции, итоги файла).

    Шапку ищем по строке с «Артикул»: ВБ может сдвинуть таблицу, а порядок колонок
    у них стабилен только относительно заголовков.
    """
    z = zipfile.ZipFile(io.BytesIO(raw))
    name = next(n for n in z.namelist() if n.lower().endswith(".xlsx"))
    ws = openpyxl.load_workbook(io.BytesIO(z.read(name)), data_only=True).active

    rows = list(ws.iter_rows(values_only=True))
    head_i = next(i for i, r in enumerate(rows)
                  if any("артикул" in str(c or "").lower() for c in r))
    head = [str(c or "").replace("\n", " ").strip().lower() for c in rows[head_i]]

    def col(*keys):
        for i, h in enumerate(head):
            if all(k in h for k in keys):
                return i
        raise RuntimeError(f"в уведомлении нет колонки {keys}: {head}")

    c_art, c_name, c_qty = col("артикул"), col("наименование"), col("количество")
    # «Сумма выкупа, руб., (вкл. НДС)» тоже содержит и «сумма», и «ндс», поэтому
    # колонку налога ищем как «сумма НДС» без слова «выкуп».
    c_sum, c_rate = col("сумма", "выкупа"), col("ставка")
    c_vat = next(i for i, h in enumerate(head)
                 if "сумма" in h and "ндс" in h and "выкуп" not in h)

    pos, totals = [], None
    for r in rows[head_i + 1:]:
        first = str(r[0] or "").strip().lower()
        if first.startswith("итого"):
            totals = (_num(r[c_sum]), _num(r[c_vat]))
            break
        if not str(r[c_art] or "").strip():
            continue
        pos.append({
            "n": len(pos) + 1,
            "article": str(r[c_art]).strip(),
            "name": str(r[c_name] or "").strip(),
            "qty": str(_num(r[c_qty])),
            "sum_with_vat": str(_num(r[c_sum])),
            "vat_rate": str(r[c_rate] or "").strip(),
            "vat_sum": str(_num(r[c_vat])),
        })
    if not pos:
        raise RuntimeError("в уведомлении нет товарных строк")
    return pos, totals


def collect(account, d_from, d_to, force=False):
    have = {r["doc_number"] for r in db.query(
        "SELECT doc_number FROM wb_redeem_notice WHERE account=%s", (account,))}
    notices = list_notices(account, d_from, d_to)
    new = 0
    for n in notices:
        m = NAME_RE.search(n.get("name") or "")
        if not m:
            print(f"  пропуск, не разобрано имя: {n.get('name')}", flush=True)
            continue
        number, doc_date = m.group(1), date.fromisoformat(m.group(2))
        if number in have and not force:
            continue
        time.sleep(PAUSE)
        pos, totals = parse_zip(download(account, n["serviceName"]))

        with_vat = sum(Decimal(p["sum_with_vat"]) for p in pos)
        vat = sum(Decimal(p["vat_sum"]) for p in pos)
        if totals and (with_vat, vat) != totals:
            # Расхождение со строкой «Итого» — повод посмотреть глазами, а не молча
            # выставить УПД: в ЭДО уходит юридически значимый документ.
            print(f"  ⚠ {number}: сумма позиций {with_vat}/{vat} ≠ «Итого» {totals[0]}/{totals[1]}",
                  flush=True)
        db.upsert("wb_redeem_notice", [{
            "account": account, "doc_number": number, "doc_date": doc_date,
            "service_name": n["serviceName"], "created_at": n.get("creationTime"),
            "total_wo_vat": with_vat - vat, "total_vat": vat, "total_with_vat": with_vat,
            "positions": psycopg2.extras.Json(pos),
        }], ["account", "doc_number"],
            update_cols=["doc_date", "service_name", "created_at", "total_wo_vat",
                         "total_vat", "total_with_vat", "positions"])
        new += 1
        print(f"  {number} от {doc_date}: позиций {len(pos)}, {with_vat} ₽", flush=True)
    print(f"{account}: в списке {len(notices)}, загружено новых {new}", flush=True)
    return new


def main_range(d_from, d_to, accounts=ACCOUNTS):
    for acc in accounts:
        print(f"{acc}: уведомления о выкупе {d_from}…{d_to}", flush=True)
        collect(acc, d_from, d_to)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="d_from", help="с какой даты (по умолчанию 90 дней назад)")
    ap.add_argument("--to", dest="d_to")
    ap.add_argument("--account", choices=ACCOUNTS)
    args = ap.parse_args()
    d_to = date.fromisoformat(args.d_to) if args.d_to else date.today()
    d_from = date.fromisoformat(args.d_from) if args.d_from else d_to - timedelta(days=90)
    main_range(d_from, d_to, [args.account] if args.account else ACCOUNTS)


if __name__ == "__main__":
    main()
