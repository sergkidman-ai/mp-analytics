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
import json
import fcntl
from concurrent.futures import ThreadPoolExecutor
import base64
import io
import os
import re
import sys
import time
import zipfile
from datetime import date, timedelta, datetime, timezone
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
# Используем только ключи этого кабинета с категорией «Документы» (бит12 JWT).
TOKEN_ENV = {"wb_acc1": ("WB_TOKEN_DOCS_ACC1", "WB_TOKEN_PRICES_ACC1", "WB_TOKEN_ACC1", "WB_TOKEN_FEEDBACK_ACC1"),
             "wb_acc2": ("WB_TOKEN_DOCS_ACC2", "WB_TOKEN_PRICES_ACC2", "WB_TOKEN_ACC2")}
STATE_DIR = BASE_DIR / ".private" / "wb_redeem_sources"
PAUSE = 11          # лимит 1 запрос / 10 с на продавца, берём с запасом
NAME_RE = re.compile(r"№\s*(\d+)\s*от\s*(\d{4}-\d{2}-\d{2})")


def _token(account):
    for env in TOKEN_ENV[account]:
        token = (os.getenv(env) or "").strip()
        if not token:
            continue
        try:
            claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        except (ValueError, IndexError):
            continue
        if claims.get("s", 0) & (1 << 12) and claims.get("exp", 0) > time.time():
            return token
    raise RuntimeError(f"Нет действующего токена категории «Документы» для {account}")


def _get(account, method, params):
    """Общий межпроцессный лимит документов: один запрос/11с на кабинет."""
    token = _token(account)
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (STATE_DIR / f"{account}.rate").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        lock.seek(0)
        previous = float(lock.read() or 0)
        time.sleep(max(0, PAUSE - (time.time() - previous)))
        try:
            response = requests.get(f"{BASE}/{method}", headers={"Authorization": token},
                                    params=params, timeout=120)
        finally:
            lock.seek(0); lock.truncate(); lock.write(str(time.time())); lock.flush()
        response.raise_for_status()
        return response.json()


def _num(v):
    """«2 700,18» / «5%» / None → Decimal. Прочерк и пустое → 0."""
    s = str(v or "").replace("\xa0", "").replace(" ", "").replace(",", ".").rstrip("%")
    if s in ("", "-", "—", "None"):
        return Decimal(0)
    return Decimal(s)


def list_notices(account, d_from, d_to):
    documents, seen = [], set()
    for offset in range(0, 10000, 50):
        body = _get(account, "list", {"category": CATEGORY, "beginTime": d_from.isoformat(),
                    "endTime": d_to.isoformat(), "locale": "ru", "limit": 50, "offset": offset,
                    "sort": "date", "order": "desc"})
        data = body.get("data", body)
        page = data.get("documents")
        if not isinstance(page, list):
            raise RuntimeError("ВБ вернул неполный список документов")
        for document in page:
            key = document.get("serviceName")
            if not key or key in seen:
                raise RuntimeError("Некорректная пагинация документов ВБ")
            seen.add(key); documents.append(document)
        if len(page) < 50:
            STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
            (STATE_DIR / f"{account}_list_{d_from}_{d_to}.json").write_text(json.dumps(documents, ensure_ascii=False))
            return documents
    raise RuntimeError("Превышен предел страниц документов ВБ")


def download(account, service_name):
    """Сохраняем оригинальный ZIP: повторный разбор не требует API."""
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", service_name):
        raise RuntimeError("Некорректный идентификатор документа ВБ")
    path = STATE_DIR / account / (service_name + ".zip")
    if path.exists():
        return path.read_bytes()
    body = _get(account, "download", {"serviceName": service_name, "extension": "zip"})
    raw = base64.b64decode(body.get("data", body)["document"], validate=True)
    if not zipfile.is_zipfile(io.BytesIO(raw)):
        raise RuntimeError("Документ ВБ не является ZIP")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(".tmp"); temp.write_bytes(raw); temp.replace(path)
    return raw


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
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    state_file = STATE_DIR / f"{account}.json"
    try:
        previous_state = json.loads(state_file.read_text())
    except (OSError, ValueError):
        previous_state = {}
    known_count = max(previous_state.get("stored_count", 0), previous_state.get("listed", 0))
    state = dict(status="running", account=account, date_from=str(d_from), date_to=str(d_to),
                 started_at=datetime.now(timezone.utc).isoformat())
    state_file.write_text(json.dumps(state))
    have = {r["doc_number"] for r in db.query(
        "SELECT doc_number FROM wb_redeem_notice WHERE account=%s", (account,))}
    notices = list_notices(account, d_from, d_to)
    new = 0
    for n in notices:
        m = NAME_RE.search(n.get("name") or "")
        if not m:
            raise RuntimeError("Не разобран номер/дата уведомления о выкупе ВБ")
        number, doc_date = m.group(1), date.fromisoformat(m.group(2))
        if number in have and not force:
            continue
        pos, totals = parse_zip(download(account, n["serviceName"]))

        with_vat = sum(Decimal(p["sum_with_vat"]) for p in pos)
        vat = sum(Decimal(p["vat_sum"]) for p in pos)
        if totals is None or with_vat != totals[0]:
            raise RuntimeError(f"{number}: сумма позиций не совпадает с итогами уведомления")
        if abs(vat - totals[1]) > Decimal("0.01") * len(pos):
            raise RuntimeError(f"{number}: НДС строк не совпадает с итогом уведомления")
        if vat != totals[1]:
            print(f"  {number}: округление НДС строк/итога {vat}/{totals[1]}", flush=True)
        vat = totals[1]
        if any(Decimal(p["qty"]) <= 0 for p in pos):
            raise RuntimeError(f"{number}: неположительное количество товара")
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
    actual = db.query("SELECT count(*) AS n FROM wb_redeem_notice WHERE account=%s", (account,))[0]["n"]
    state.update(status="complete", listed=len(notices), new=new, stored_count=max(known_count, actual),
                 completed_at=datetime.now(timezone.utc).isoformat())
    temp = state_file.with_suffix(".tmp"); temp.write_text(json.dumps(state)); temp.replace(state_file)
    return new


def main_range(d_from, d_to, accounts=ACCOUNTS):
    # У кабинетов независимые лимиты и ключи таблицы; ошибку любого не скрываем.
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(collect, account, d_from, d_to) for account in accounts]
        for future in futures:
            future.result()


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
