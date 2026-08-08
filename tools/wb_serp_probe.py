#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# поток: mkt
"""Разведка выдачи Wildberries: наша позиция, реклама и ЦЕНА ВХОДА (cpm) по нашим ключам.

ЗАЧЕМ. Ставку мы поднимаем вслепую: не знаем, почём вообще стоит рекламное место по нашим
запросам. Если вход стоит 60 ₽, лестница с 9.9 ₽ — театр; если 12 ₽ — надо прыгать, а не ползти.
Ответ есть только в самой выдаче: у каждой карточки в ответе поиска лежит блок `log` со ставкой
(`cpm`) и рекламной позицией (`promoPosition`).

ЗАПУСКАТЬ НА ДОМАШНЕМ ИНТЕРНЕТЕ (машина Сергея). С нашего сервера search.wb.ru отдаёт 429
в 80 % случаев, а прошедшие ответы обрезаны на 740 байтах — товаров в них нет. Проверено 08.08.2026.

ЧТО ДЕЛАЕТ: только GET-запросы на публичный адрес поиска. Ключей, паролей и токенов не читает
и не использует, никуда ничего не отправляет, на маркетплейсе ничего не меняет.

УСТАНОВКА (один раз):
    pip install requests

ЗАПУСК (положить рядом три файла: этот скрипт, wb_serp_queries.txt, our_nm.txt):
    python wb_serp_probe.py                 # все запросы из файла, 2 страницы выдачи
    python wb_serp_probe.py --pages 1       # быстрее: только первая страница
    python wb_serp_probe.py --limit 5       # проба на пяти запросах

РЕЗУЛЬТАТ — два CSV рядом со скриптом (открываются Excel, разделитель «;»):
    serp_summary.csv — строка на запрос: сколько рекламных мест, почём они, где мы
    serp_detail.csv  — строка на карточку из топа: позиция, цена, отзывы, ставка, наша/чужая
Оба файла прислать мне — дальше считаю я.
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
SEARCH = "https://search.wb.ru/exactmatch/ru/common/v13/search"
DEST = -1257786          # Москва и область: позиция зависит от региона, фиксируем один
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
    params = {"appType": 1, "curr": "rub", "dest": DEST, "lang": "ru", "query": query,
              "resultset": "catalog", "sort": "popular", "spp": 30, "page": page}
    for attempt in range(4):
        try:
            r = session.get(SEARCH, params=params, headers=HEADERS, timeout=30)
        except Exception as e:
            print(f"    сеть: {type(e).__name__}, повтор", flush=True)
            time.sleep(10)
            continue
        if r.status_code == 429:
            wait = 15 * (attempt + 1)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=2, help="страниц выдачи на запрос (100 товаров = 1 стр.)")
    ap.add_argument("--limit", type=int, default=0, help="взять только первые N запросов")
    ap.add_argument("--top", type=int, default=20, help="сколько карточек с каждого запроса писать в детали")
    a = ap.parse_args()

    queries = load_lines("wb_serp_queries.txt")
    if not queries:
        sys.exit("Рядом со скриптом нет файла wb_serp_queries.txt (список запросов, по одному в строке)")
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

        ad_cpms, our_pos, our_nm, our_cpm = [], None, None, None
        for pos, p in enumerate(products, 1):
            log = p.get("log") or {}
            cpm = log.get("cpm")
            is_ad = bool(cpm)
            if is_ad:
                ad_cpms.append(cpm)
            brand = (p.get("brand") or "")
            mine = (p.get("id") in our) if our else (brand.lower() in OUR_BRANDS)
            if mine and our_pos is None:
                our_pos, our_nm, our_cpm = pos, p.get("id"), cpm
            if pos <= a.top or mine:
                detail.append({
                    "запрос": q, "позиция": pos, "nm_id": p.get("id"), "бренд": brand,
                    "название": (p.get("name") or "")[:80], "цена": price_of(p),
                    "отзывов": p.get("feedbacks"), "рейтинг": p.get("reviewRating"),
                    "продавец": p.get("supplier"), "реклама": "да" if is_ad else "",
                    "ставка_cpm": cpm, "реклам_позиция": log.get("promoPosition"),
                    "наш": "да" if mine else "",
                })
        ad_cpms.sort()
        summary.append({
            "запрос": q, "товаров": len(products), "реклам_мест": len(ad_cpms),
            "ставка_мин": ad_cpms[0] if ad_cpms else None,
            "ставка_медиана": ad_cpms[len(ad_cpms) // 2] if ad_cpms else None,
            "ставка_макс": ad_cpms[-1] if ad_cpms else None,
            "наша_позиция": our_pos, "наш_nm": our_nm, "наша_ставка": our_cpm,
            "топ1_продавец": (products[0].get("supplier") or "")[:40],
            "топ1_цена": price_of(products[0]),
        })
        print(f"[{i}/{len(queries)}] «{q}»: товаров {len(products)}, реклама {len(ad_cpms)} мест"
              f"{f' (ставки {ad_cpms[0]}…{ad_cpms[-1]})' if ad_cpms else ''}"
              f"{f', мы на {our_pos}' if our_pos else ', нас нет'}", flush=True)
        time.sleep(random.uniform(*PAUSE))

    for name, rows in (("serp_summary.csv", summary), ("serp_detail.csv", detail)):
        if not rows:
            continue
        cols = list({k: None for r in rows for k in r})
        with io.open(HERE / name, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=cols, delimiter=";")
            w.writeheader()
            w.writerows(rows)
        print(f"записан {name}: строк {len(rows)}")
    print("\nГотово. Пришли оба CSV.")


if __name__ == "__main__":
    main()
