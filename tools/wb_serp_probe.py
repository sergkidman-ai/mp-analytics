#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# поток: mkt
"""Разведка выдачи Wildberries: наша позиция по ключу и обстановка вокруг неё.

ЗАЧЕМ. Джем отвечает раз в неделю и средней позицией. Выдача отвечает сейчас и фактом: на каком
месте мы стоим по конкретному запросу, кто выше, почём он и сколько у него остатка.

ЧЕГО ЗДЕСЬ НЕТ И БОЛЬШЕ НЕ БУДЕТ (проверено 08.08.2026). Ставок конкурентов (`cpm`) в выдаче
не осталось: в ответе v18 нет ни `log`, ни `cpm`, ни `promoPosition` — среди 66 полей их просто
нет, а старый рекламный хост `catalog-ads.wb.ru` снят с DNS. «Почём стоит место» из выдачи
теперь не читается ничем; свою фактическую цену клика считаем из `wb_ad_nm_daily` (расход/клики).

ЧТО ДЕЛАЕТ: только GET-запросы на публичный адрес поиска. Ключей, паролей и токенов не читает
и не использует, никуда ничего не отправляет, на маркетплейсе ничего не меняет.

ЗАПУСК — с нашего сервера, релей и домашний интернет не нужны (v18 отдаёт 200 и 100 товаров):
    ./venv/bin/python tools/wb_serp_probe.py             # все запросы из файла, 2 страницы
    ./venv/bin/python tools/wb_serp_probe.py --pages 1   # быстрее: только первая сотня
    ./venv/bin/python tools/wb_serp_probe.py --limit 5   # проба на пяти запросах

РЕЗУЛЬТАТ — два CSV рядом со скриптом (Excel, разделитель «;»):
    serp_summary.csv — строка на запрос: где мы, кто топ-1, его цена
    serp_detail.csv  — строка на карточку из топа: позиция, цена, отзывы, продавец, остаток
"""
import argparse
import csv
import io
import json
import pathlib
import random
import sys
import time

try:
    import requests
except ImportError:
    sys.exit("Не установлена библиотека requests. Выполни:  pip install requests")

HERE = pathlib.Path(__file__).resolve().parent
SEARCH = "https://search.wb.ru/exactmatch/ru/common/v18/search"
# Версия пути обязательна: v13 и старше отдают обрезанный ответ на 740 байт (не блокировка —
# просто мёртвая версия). dest — новый положительный формат, старый -1257786 не работает.
DEST = 1259570991        # Москва и область: позиция зависит от региона, фиксируем один
PAUSE = (4, 7)           # пауза между запросами, секунд (случайная — ведём себя как человек)
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}
# Наши бренды — на случай, если файла our_nm.txt рядом не окажется
OUR_BRANDS = ("цифровой квадрат", "дисквэр")


def load_lines(name):
    p = HERE / name
    if not p.exists():
        return []
    return [s.strip() for s in p.read_text(encoding="utf-8-sig").splitlines() if s.strip()]


def fetch(session, query, page):
    """Одна страница выдачи. Возвращает список товаров (может быть пустым)."""
    params = {"ab_testid": "old_cb_and_poly", "appType": 1, "curr": "rub", "dest": DEST,
              "hide_dtype": 15, "hide_vflags": 4294967296, "inheritFilters": "true",
              "lang": "ru", "locale": "ru", "query": query, "resultset": "catalog",
              "sort": "popular", "spp": 30, "suppressSpellcheck": "false", "page": page}
    for attempt in range(4):
        try:
            r = session.get(SEARCH, params=params, headers=HEADERS, timeout=30)
        except Exception as e:
            print(f"    сеть: {type(e).__name__}, повтор", flush=True)
            time.sleep(10)
            continue
        if r.status_code == 429:
            # Отдых кратен рабочей паузе: на ночном прогоне 15 с не хватает, чтобы лимит отпустил.
            wait = max(15, PAUSE[1] * 3) * (attempt + 1)
            print(f"    429 (лимит), ждём {wait}с", flush=True)
            time.sleep(wait)
            continue
        if r.status_code != 200:
            print(f"    HTTP {r.status_code}", flush=True)
            return None
        try:
            j = json.loads(r.content.decode("utf-8"))
        except Exception:
            print(f"    ответ обрезан ({len(r.content)} байт) — это тоже блокировка", flush=True)
            return None
        prods = (j.get("data") or {}).get("products") or j.get("products") or []
        # Первый удачный ответ сохраняем как есть: если ВБ переименовал поля, поправлю разбор
        # по этому файлу, не гоняя тебя за вторым прогоном.
        sample = HERE / "serp_raw_sample.json"
        if prods and not sample.exists():
            sample.write_bytes(r.content)
        return prods
    return None


def price_of(p):
    """Цена покупателя в рублях (в ответе — копейки)."""
    for s in (p.get("sizes") or []):
        pr = (s.get("price") or {})
        v = pr.get("product") or pr.get("total") or pr.get("basic")
        if v:
            return round(v / 100, 2)
    return None


def _median(vals):
    v = sorted(x for x in vals if x)
    return v[len(v) // 2] if v else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=2, help="страниц выдачи на запрос (100 товаров = 1 стр.)")
    ap.add_argument("--limit", type=int, default=0, help="взять только первые N запросов")
    ap.add_argument("--top", type=int, default=20, help="сколько карточек с каждого запроса писать в детали")
    ap.add_argument("--file", default="wb_serp_queries.txt", help="файл со списком запросов")
    ap.add_argument("--out", default="serp", help="префикс имён выходных CSV")
    # Лимитер ВБ жёсткий: 30 запросов подряд с паузой 4-7 с проходят, следующая партия ловит 429
    # и IP уходит в отказ надолго. Для ночных прогонов ставить --pause 30 60.
    ap.add_argument("--pause", type=int, nargs=2, default=[4, 7], metavar=("МИН", "МАКС"),
                    help="пауза между запросами, секунд (по умолчанию 4 7)")
    a = ap.parse_args()

    global PAUSE
    PAUSE = tuple(a.pause)

    queries = load_lines(a.file)
    if not queries:
        sys.exit(f"Рядом со скриптом нет файла {a.file} (список запросов, по одному в строке)")
    if a.limit:
        queries = queries[:a.limit]
    our = {int(x) for x in load_lines("our_nm.txt") if x.isdigit()}
    print(f"Запросов: {len(queries)} | страниц на запрос: {a.pages} | наших артикулов в списке: {len(our)}")
    print(f"Ожидаемое время: ~{len(queries) * a.pages * 6 // 60 + 1} мин\n", flush=True)

    session = requests.Session()
    summary, detail = [], []
    for i, q in enumerate(queries, 1):
        products = []
        for page in range(1, a.pages + 1):
            chunk = fetch(session, q, page)
            if chunk is None:
                break
            products.extend(chunk)
            if len(chunk) < 100:
                break
            time.sleep(random.uniform(*PAUSE))
        if not products:
            print(f"[{i}/{len(queries)}] «{q}» — пусто (см. сообщение выше)", flush=True)
            summary.append({"запрос": q, "товаров": 0})
            time.sleep(random.uniform(*PAUSE))
            continue

        our_pos, our_nm, our_all = None, None, []
        for pos, p in enumerate(products, 1):
            brand = (p.get("brand") or "")
            mine = (p.get("id") in our) if our else (brand.lower() in OUR_BRANDS)
            if mine:
                our_all.append(pos)
                if our_pos is None:
                    our_pos, our_nm = pos, p.get("id")
            if pos <= a.top or mine:
                detail.append({
                    "запрос": q, "позиция": pos, "nm_id": p.get("id"), "бренд": brand,
                    "название": (p.get("name") or "")[:80], "цена": price_of(p),
                    "отзывов": p.get("feedbacks"), "рейтинг": p.get("reviewRating"),
                    "продавец": p.get("supplier"), "остаток": p.get("totalQuantity"),
                    "наш": "да" if mine else "",
                })
        top = products[0]
        summary.append({
            "запрос": q, "товаров": len(products),
            "наша_позиция": our_pos, "наш_nm": our_nm, "наших_в_выдаче": len(our_all),
            "топ1_продавец": (top.get("supplier") or "")[:40], "топ1_цена": price_of(top),
            "топ1_отзывов": top.get("feedbacks"),
            "медиана_цены_топ20": _median([price_of(x) for x in products[:20]]),
        })
        print(f"[{i}/{len(queries)}] «{q}»: товаров {len(products)}"
              f"{f', мы на {our_pos} (всего наших {len(our_all)})' if our_pos else ', нас нет'}",
              flush=True)
        time.sleep(random.uniform(*PAUSE))

    for name, rows in ((f"{a.out}_summary.csv", summary), (f"{a.out}_detail.csv", detail)):
        if not rows:
            continue
        cols = list({k: None for r in rows for k in r})
        with io.open(HERE / name, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
            w.writeheader()
            w.writerows(rows)
        print(f"записан {name}: строк {len(rows)}")
    print("\nГотово.")


if __name__ == "__main__":
    main()
