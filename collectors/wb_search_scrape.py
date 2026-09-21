"""collectors/wb_search_scrape.py — публичная поисковая выдача WB по нашим моделям (поток mkt).

Этап 1 из docs/prompts/mkt_wb_scraper_start.md. Первая страница выдачи (до 100 товаров) на
запрос/регион → wb_search_snapshot (append-only, один прогон = один run_id). Публичный адрес,
токен не нужен, ничего на ВБ не пишем.

ПРОВЕРЕНО 19.09.2026 (см. BRIEF_MKT.md): `search.wb.ru/exactmatch/ru/common/v14/search` — 200,
v7 — 429, `u-search.wb.ru` v18 (старый крон `serp_core.log`) — 403 с 12–18.09 включительно, тот
крон молча пишет пустые CSV с этой даты. v14 подтверждён живым запросом перед началом этой работы,
но версии у ВБ уже трижды менялись за полтора месяца (v9→v18→v14) — при первом же 429/403 на v14
не менять версию наугад, сверить сначала руками (как здесь), потом чинить.

Темп: 1 запрос не чаще, чем раз в PAUSE секунд (по умолчанию 3.5 — с запасом от «не выше 1/3с»
из промта), суточный лимит DAILY_LIMIT запросов (общий на все регионы, считается по строке в
wb_search_scrape_run за сегодня). 429 → пауза и до RETRIES попыток, затем запрос пропускается
(регион/запрос не помечаются «пусто» — следующий прогон повторит).

Список запросов — файл, один на строку (по умолчанию tools/wb_serp_core_keys.txt, уже
курированный вручную; полноценный «топ-200 прибыльных моделей» — отдельная задача, см. бриф).
Регионы по умолчанию — Москва/СПб/Екатеринбург (ответ Сергея 19.09), dest СПб/Екб сняты живым
запросом к geo API, не угаданы; свои --dest переопределяют список целиком.

Запуск:
    ./venv/bin/python collectors/wb_search_scrape.py --limit 5              # проба, в БД не пишет
    ./venv/bin/python collectors/wb_search_scrape.py --limit 5 --write      # проба, пишет в БД
    ./venv/bin/python collectors/wb_search_scrape.py --write                # весь файл запросов
    ./venv/bin/python collectors/wb_search_scrape.py --write --file f.txt --dest msk:-1257786
"""
import argparse
import pathlib
import sys
import time
import uuid

import psycopg2.extras
import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")

SEARCH_URL = "https://search.wb.ru/exactmatch/ru/common/v14/search"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}
DEFAULT_QUERIES_FILE = BASE_DIR / "tools" / "wb_serp_core_keys.txt"
# Регионы — ответ Сергея 19.09 на вопрос 2 промта: Москва, СПб, Екатеринбург/Урал.
# dest СПб/Екб получены живым запросом к user-geo-data.wildberries.ru/get-geo-info по
# координатам городов (не угаданы) и сверены — Екб на «картридж 718» даёт другой топ-3
# (локальный продавец «Принтбург»), СПб пока совпадает с Москвой на этом запросе.
DEFAULT_REGIONS = [(-1257786, "msk"), (-1198055, "spb"), (-5818883, "ekb")]
PAUSE = 3.5
RETRIES = 3
DAILY_LIMIT = 1500          # общий потолок запросов/сутки на все регионы, с запасом от ~1200 из промта


def daily_count_today():
    row = db.query(
        "SELECT count(DISTINCT (query, region, captured_at)) AS n FROM wb_search_snapshot "
        "WHERE captured_at::date = now()::date")[0]
    return row["n"] or 0


def fetch(session, query, dest):
    """Одна страница выдачи. (products, ok) — ok=False значит «не ответил», не «пусто»."""
    params = {"appType": 1, "curr": "rub", "dest": dest, "query": query,
              "resultset": "catalog", "sort": "popular", "spp": 30, "page": 1}
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(SEARCH_URL, params=params, headers=HEADERS, timeout=20)
        except Exception as e:
            print(f"    сеть: {type(e).__name__}, повтор", flush=True)
            time.sleep(PAUSE * 2)
            continue
        if r.status_code == 429:
            wait = max(15, PAUSE * 3) * attempt
            print(f"    429, ждём {wait:.0f}с (попытка {attempt}/{RETRIES})", flush=True)
            time.sleep(wait)
            continue
        if r.status_code != 200:
            print(f"    HTTP {r.status_code} на «{query}»", flush=True)
            return [], False
        try:
            j = r.json()
        except Exception:
            print(f"    ответ не JSON на «{query}»", flush=True)
            return [], False
        prods = j.get("products") or (j.get("data") or {}).get("products") or []
        return prods, True
    return [], False


def row_of(p, run_id, query, region, dest, pos):
    sz = (p.get("sizes") or [{}])[0]
    price = sz.get("price") or {}
    product = price.get("product")
    basic = price.get("basic")
    qty = p.get("totalQuantity") or 0
    return (run_id, query, region, dest, pos, p.get("id"), p.get("supplierId"),
            (p.get("supplier") or "")[:200], (p.get("brand") or "")[:200], (p.get("name") or "")[:300],
            (product / 100.0 if product else None), (basic / 100.0 if basic else None),
            p.get("reviewRating"), p.get("feedbacks"), qty > 0, qty)


COLS = ("run_id", "query", "region", "dest", "position", "nm_id", "supplier_id", "supplier",
        "brand", "name", "price", "price_basic", "rating", "feedbacks", "in_stock", "total_qty")


def write_rows(rows):
    if not rows:
        return 0
    with db.get_conn() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur, f"INSERT INTO wb_search_snapshot ({', '.join(COLS)}) VALUES %s", rows)
    return len(rows)


def main():
    global PAUSE
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(DEFAULT_QUERIES_FILE), help="файл запросов, по строке")
    ap.add_argument("--limit", type=int, default=0, help="взять только первые N запросов (проба)")
    ap.add_argument("--dest", action="append", default=[],
                    help="регион вида метка:dest (порядок важен — dest отрицательный, argparse "
                         "ломается на «--dest -123:msk», а «--dest msk:-123» ок), можно несколько раз")
    ap.add_argument("--pause", type=float, default=PAUSE)
    ap.add_argument("--write", action="store_true", help="писать в БД (без флага — только печать)")
    a = ap.parse_args()
    PAUSE = a.pause

    qpath = pathlib.Path(a.file)
    if not qpath.exists():
        sys.exit(f"нет файла запросов: {qpath}")
    queries = [s.strip() for s in qpath.read_text(encoding="utf-8-sig").splitlines() if s.strip()]
    if a.limit:
        queries = queries[:a.limit]

    regions = []
    for d in a.dest:
        label, sep, dest = d.partition(":")
        if not sep:            # без метки — сам dest, отрицательный, парсится нормально
            label, dest = d, d
        regions.append((int(dest), label))
    if not regions:
        regions = DEFAULT_REGIONS

    already = daily_count_today()
    budget = DAILY_LIMIT - already
    planned = len(queries) * len(regions)
    if a.write and budget <= 0:
        sys.exit(f"суточный лимит исчерпан: сегодня уже {already} запросов из {DAILY_LIMIT}")
    if a.write and planned > budget:
        print(f"план {planned} запросов больше остатка лимита {budget} — обрежу список", flush=True)
        queries = queries[:max(1, budget // len(regions))]

    print(f"запросов: {len(queries)} | регионов: {len(regions)} | режим: "
          f"{'ЗАПИСЬ в БД' if a.write else 'только печать (dry-run)'}", flush=True)

    run_id = str(uuid.uuid4())
    session = requests.Session()
    total_rows, total_ok, total_fail = 0, 0, 0
    for i, q in enumerate(queries, 1):
        for dest, region in regions:
            prods, ok = fetch(session, q, dest)
            if not ok:
                total_fail += 1
                print(f"[{i}/{len(queries)}] «{q}» ({region}) — не ответил", flush=True)
                time.sleep(PAUSE)
                continue
            total_ok += 1
            rows = [row_of(p, run_id, q, region, dest, pos) for pos, p in enumerate(prods, 1)]
            if a.write:
                write_rows(rows)
            total_rows += len(rows)
            print(f"[{i}/{len(queries)}] «{q}» ({region}): товаров {len(prods)}", flush=True)
            time.sleep(PAUSE)

    print(f"\nГотово. run_id={run_id} | запросов ок {total_ok}, не ответили {total_fail}, "
          f"строк {'записано' if a.write else 'собрано (не записано, нет --write)'} {total_rows}")


if __name__ == "__main__":
    main()
