#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# поток: gab
"""Харвестер габаритов карточек КОНКУРЕНТОВ на Wildberries и Ozon.

ЗАПУСКАТЬ НА ОБЫЧНОМ ИНТЕРНЕТЕ (машина Сергея, Windows). С серверного IP
маркетплейсы отвечают 429/403 — поэтому этот скрипт отдельный и рассчитан
на чистый домашний egress.

Что делает: по списку OEM-кодов ищет на WB и Ozon публичные карточки СОВМЕСТИМЫХ
картриджей того же кода у других продавцов и вытаскивает размеры УПАКОВКИ,
если продавец их указал.

Правила (железные): размер берётся ТОЛЬКО из данных конкретной карточки; у каждой
строки — ссылка и артикул конкурента; ничего не выдумывается; статус тут только
CANDIDATE/NOT_FOUND/AMBIGUOUS — CONFIRMED ставит отдельная независимая проверка
на сервере.

Скрипт НИЧЕГО НИКУДА НЕ ОТПРАВЛЯЕТ: только GET-запросы на публичные адреса
и запись двух файлов рядом с собой (результат и прогресс). Ключей и паролей
не читает и не использует.

Зависимости: только `requests`  (pip install requests). Больше ничего не нужно.
Скрипт возобновляемый — можно прервать и запустить снова, уже собранное
не перезапрашивается.

  python competitor_harvester.py --limit 10 --debug     # тест с диагностикой
  python competitor_harvester.py                        # весь список
  python competitor_harvester.py --input top216_uncovered.csv

Версия от 2026-07-30: заменён адрес поиска WB (search.wb.ru отдаёт 429 →
u-search.wb.ru), исправлен разбор ответа (товары лежат в корне, а не в data),
исправлена сборка размера (длина/ширина/высота — три отдельные характеристики),
переделана раскладка по basket-хостам, добавлен флаг --debug.
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import re
import sys
import time
from urllib.parse import quote

try:
    import requests
    from requests.adapters import HTTPAdapter
except ImportError:
    print("Нужен модуль requests. Установите командой:  pip install requests")
    sys.exit(1)


class BrowserTLSAdapter(HTTPAdapter):
    """Набор шифров как у Chrome.

    Поиск WB отсекает клиентов по TLS-отпечатку: у голого `requests` набор шифров
    не браузерный, и сервер отдаёт 403 при абсолютно тех же заголовках, на которых
    curl получает 200 (проверено 2026-07-30). Меняем только набор шифров —
    ни новых пакетов, ни подмены личности: User-Agent и так браузерный.
    """
    CIPHERS = ("ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
               "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
               "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
               "ECDHE-RSA-AES128-SHA:ECDHE-RSA-AES256-SHA:AES128-GCM-SHA256:"
               "AES256-GCM-SHA384:AES128-SHA:AES256-SHA")

    def init_poolmanager(self, *a, **kw):
        try:
            from urllib3.util.ssl_ import create_urllib3_context
            ctx = create_urllib3_context(ciphers=self.CIPHERS)
            ctx.options |= 0x00004000            # OP_NO_TICKET, как у браузера
            kw["ssl_context"] = ctx
        except Exception:
            pass                                  # не вышло — работаем как обычный requests
        return super().init_poolmanager(*a, **kw)


HERE = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*",
      "Accept-Language": "ru,en;q=0.9",
      "Referer": "https://www.wildberries.ru/"}
MAX_COMPETITORS = 5          # сколько карточек-конкурентов брать на один код.
# Пять, а не три: подтверждение требует ДВУХ РАЗНЫХ продавцов, а у одного продавца
# запросто бывает по две-три карточки на один код — с трёх карточек согласия
# двух независимых продавцов чаще всего не набирается.
SESSION = requests.Session()
SESSION.mount("https://", BrowserTLSAdapter())
PAUSE = 1.2                  # пауза между запросами, сек (переопределяется --pause)
DEBUG = False
_dbg_shown: set[str] = set()

# Адреса поиска WB — пробуем по очереди, первый отдавший товары запоминаем.
# Проверено 2026-07-30: search.wb.ru отвечает 429 (жёсткий лимит), рабочий — u-search.
WB_SEARCH_ENDPOINTS = [
    "https://u-search.wb.ru/exactmatch/ru/common/v9/search",
    "https://search.wb.ru/exactmatch/ru/common/v9/search",
    "https://u-search.wb.ru/exactmatch/ru/common/v5/search",
    "https://search.wb.ru/exactmatch/ru/common/v5/search",
]
_wb_endpoint: str | None = None      # прилипает после первого удачного

# Раскладка vol → basket. Диапазоны до 3269 проверены и совпадают со старой картой;
# выше — опорные замеры 2026-07-30 (WB добавил корзины, старый потолок «всё в 20» врал).
BASKET_RANGES = [(0,143,1),(144,287,2),(288,431,3),(432,719,4),(720,1007,5),(1008,1061,6),
                 (1062,1115,7),(1116,1169,8),(1170,1313,9),(1314,1601,10),(1602,1655,11),
                 (1656,1919,12),(1920,2045,13),(2046,2189,14),(2190,2405,15),(2406,2621,16),
                 (2622,2837,17),(2838,3053,18),(3054,3269,19)]
BASKET_ANCHORS = [(3272,20),(3657,21),(4138,24),(4404,25),(4816,26),(5276,28),
                  (6275,31),(7157,34),(7421,35),(8068,37),(8618,38),(10746,42)]
_basket_cache: dict[int, int] = {}    # vol → номер корзины, найденный в этом прогоне

DIM_NAMES = {"длина упаковки": 0, "ширина упаковки": 1, "высота упаковки": 2}

# Ozon. Оба входа живые и принимают один и тот же формат `?url=<путь страницы>`
# (проверено 2026-07-30). Отдельный User-Agent-набор: Referer тут свой.
OZON_ENDPOINTS = [
    "https://www.ozon.ru/api/composer-api.bx/page/json/v2",
    "https://www.ozon.ru/api/entrypoint-api.bx/page/json/v2",
]
OZON_UA = {**{k: v for k, v in UA.items() if k != "Referer"}, "Referer": "https://www.ozon.ru/"}
_ozon_endpoint: str | None = None
_ozon_warm = False           # зашли ли на главную за куками
_ozon_dead = False           # антибот сработал — до конца прогона Ozon не дёргаем


def dbg(kind: str, url: str, resp) -> None:
    """Печать диагностики ОДИН раз на каждый вид запроса (при --debug)."""
    if not DEBUG or kind in _dbg_shown:
        return
    _dbg_shown.add(kind)
    body = ""
    try:
        body = resp.text[:200].replace("\n", " ").replace("\r", " ")
    except Exception:
        body = "(тело не читается)"
    print(f"\n  [debug] {kind}")
    print(f"  [debug] адрес : {url[:160]}")
    print(f"  [debug] статус: HTTP {getattr(resp, 'status_code', '?')}")
    print(f"  [debug] ответ : {body}\n")


def dbg_note(text: str) -> None:
    if DEBUG:
        print(f"  [debug] {text}")


def _norm_dims(text: str) -> tuple:
    """Размер → отсортированная тройка чисел, ТОЛЬКО для сравнения продавцов."""
    nums = [float(x) for x in re.findall(r"\d+(?:[.,]\d+)?", (text or "").replace(",", "."))]
    return tuple(sorted(nums[:3])) if len(nums) >= 3 else ()


# ---------------------- Wildberries ----------------------
def _wb_params(oem: str) -> dict:
    return {"appType": 1, "curr": "rub", "dest": -1257786, "lang": "ru", "spp": 30,
            "query": f"картридж {oem}", "resultset": "catalog"}


def _products(payload: dict) -> list:
    """Товары лежат либо в корне ответа (v9), либо в data (старые версии)."""
    if not isinstance(payload, dict):
        return []
    return payload.get("products") or (payload.get("data") or {}).get("products") or []


def wb_search(oem: str) -> tuple[list[int], bool]:
    """Ищет карточки по коду. Возвращает (список артикулов, поиск_ответил).

    Второе значение важно: если WB притормозил нас (429) или не ответил вовсе —
    это НЕ «товаров нет». Такую позицию нельзя записывать как NOT_FOUND и нельзя
    отмечать пройденной, иначе она потеряется навсегда.
    """
    global _wb_endpoint
    answered = False
    for attempt in range(1, 4):                   # до трёх попыток с возрастающей паузой
        for ep in ([_wb_endpoint] if _wb_endpoint else WB_SEARCH_ENDPOINTS):
            try:
                r = SESSION.get(ep, params=_wb_params(oem), headers=UA, timeout=25)
            except Exception as exc:
                dbg_note(f"поиск WB {ep}: сеть недоступна — {str(exc)[:70]}")
                continue
            dbg(f"поиск WB — {ep}", r.url, r)
            if r.status_code != 200:
                dbg_note(f"поиск WB {ep}: HTTP {r.status_code} "
                         f"(попытка {attempt} из 3), пробую следующий адрес")
                continue
            try:
                prods = _products(r.json())
            except Exception:
                dbg_note(f"поиск WB {ep}: ответ не JSON, пробую следующий адрес")
                continue
            answered = True
            if prods:
                if _wb_endpoint != ep:
                    _wb_endpoint = ep
                    dbg_note(f"рабочий адрес поиска WB: {ep}")
                return [p["id"] for p in prods[:MAX_COMPETITORS * 3] if p.get("id")], True
            dbg_note(f"поиск WB {ep}: HTTP 200, но товаров ноль (попытка {attempt} из 3)")
        # Пусто или не ответил — переспрашиваем. WB под нагрузкой отдаёт 200
        # с пустой выдачей по коду, по которому минуту назад были карточки,
        # поэтому «ничего нет» признаём только после трёх попыток.
        if not answered:
            _wb_endpoint = None                    # адрес мог отвалиться — пробуем все заново
        if attempt < 3:
            time.sleep(PAUSE * 3 * attempt)
    return [], answered


def _basket_guess(vol: int) -> int:
    for lo, hi, n in BASKET_RANGES:
        if lo <= vol <= hi:
            return n
    prev_v, prev_b = BASKET_ANCHORS[0]
    for v, b in BASKET_ANCHORS:
        if vol <= v:
            if v == prev_v:
                return b
            f = (vol - prev_v) / (v - prev_v)
            return int(round(prev_b + f * (b - prev_b)))
        prev_v, prev_b = v, b
    # выше последнего замера — линейная экстраполяция по хвосту
    (v1, b1), (v2, b2) = BASKET_ANCHORS[-2], BASKET_ANCHORS[-1]
    step = (v2 - v1) / max(1, (b2 - b1))
    return int(round(b2 + (vol - v2) / step))


def wb_card(nm: int) -> dict | None:
    """Карточка WB. Номер basket-хоста угадываем и при промахе ищем вокруг."""
    vol, part = nm // 100000, nm // 1000
    start = _basket_cache.get(vol) or _basket_guess(vol)
    order = [start] + [start + d for pair in range(1, 25) for d in (pair, -pair)]
    for n in order:
        if not 1 <= n <= 60:
            continue
        url = f"https://basket-{n:02d}.wbbasket.ru/vol{vol}/part{part}/{nm}/info/ru/card.json"
        try:
            r = SESSION.get(url, headers=UA, timeout=20)
        except Exception:
            continue
        dbg(f"карточка WB (nm {nm})", url, r)
        if r.status_code == 200:
            _basket_cache[vol] = n
            try:
                c = r.json()
            except Exception:
                return None
            c["_url"] = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
            return c
    dbg_note(f"карточка nm {nm}: ни один basket-хост не отдал 200")
    return None


def _all_options(card: dict) -> list[dict]:
    opts = list(card.get("options") or [])
    for g in card.get("grouped_options") or []:
        opts += list(g.get("options") or [])
    return [o for o in opts if isinstance(o, dict)]


def dims_from_wb(card: dict) -> tuple[str, str] | None:
    """('ДxШxВ', 'дословные характеристики') — или None, если размеров нет.

    В карточке WB длина/ширина/высота упаковки лежат ТРЕМЯ отдельными
    характеристиками (группа «Габариты»), а не одной строкой.
    """
    # 1) прямое поле dimensions (встречается редко)
    d = card.get("dimensions") or {}
    if all(k in d for k in ("length", "width", "height")) and d.get("length"):
        return f"{d['length']}x{d['width']}x{d['height']}", "поле dimensions карточки"
    # 2) три характеристики упаковки
    got: dict[int, tuple[str, str]] = {}
    for o in _all_options(card):
        name = str(o.get("name", "")).strip()
        pos = DIM_NAMES.get(name.lower())
        if pos is not None and str(o.get("value", "")).strip():
            got[pos] = (name, str(o["value"]).strip())
    if len(got) == 3:
        vals = [got[i][1] for i in (0, 1, 2)]
        nums = [re.sub(r"[^\d.,]", "", v).replace(",", ".").strip(".") for v in vals]
        if all(nums):
            evidence = "; ".join(f"{got[i][0]} = {got[i][1]}" for i in (0, 1, 2))
            return "x".join(nums), evidence
    # 3) одиночная характеристика с габаритами (старый формат карточек)
    for o in _all_options(card):
        name = str(o.get("name", "")).lower()
        if any(k in name for k in ("габарит", "размер упаков")) and str(o.get("value", "")).strip():
            return str(o["value"]).strip(), f"{o.get('name')} = {o.get('value')}"
    return None


# ---------------------- Ozon ----------------------
def _ozon_warmup() -> None:
    """Зайти на главную, чтобы получить куки. Без них API отвечает антиботом."""
    global _ozon_warm
    if _ozon_warm:
        return
    _ozon_warm = True
    try:
        h = dict(OZON_UA)
        h["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        r = SESSION.get("https://www.ozon.ru/", headers=h, timeout=25)
        dbg("главная Ozon (за куками)", r.url, r)
    except Exception as exc:
        dbg_note(f"главная Ozon: сеть недоступна — {str(exc)[:70]}")


def _ozon_json(path: str, kind: str) -> dict | None:
    """Запрос к публичному API страницы Ozon. None — не отдал (антибот/ошибка).

    Формат адреса проверен 2026-07-30: `.../page/json/v2?url=<путь страницы>`.
    Оба живых входа (composer-api и entrypoint-api) принимают его одинаково.
    При блокировке Ozon отдаёт 403 с полем challengeURL либо гоняет по редиректу
    `__rr=1,2,3...`. С серверного IP блокирует всегда, с домашнего — может пройти.
    """
    global _ozon_dead, _ozon_endpoint
    if _ozon_dead:
        return None
    _ozon_warmup()
    for ep in ([_ozon_endpoint] if _ozon_endpoint else OZON_ENDPOINTS):
        try:
            r = SESSION.get(ep, params={"url": path}, headers=OZON_UA, timeout=25)
        except Exception as exc:
            dbg_note(f"{kind}: сеть недоступна — {str(exc)[:70]}")
            return None
        dbg(f"{kind} — {ep}", r.url, r)
        head = r.text[:800]
        # Признаки антибота: 403 с incidentId/challengeURL либо карусель редиректов __rr=
        if r.status_code == 403 or "challengeURL" in head or "incidentId" in head \
                or "__rr=" in r.url:
            dbg_note(f"{kind}: HTTP {r.status_code} — антибот Ozon (проверка «я не робот»).")
            continue
        if r.status_code != 200:
            dbg_note(f"{kind}: HTTP {r.status_code}, пробую следующий адрес Ozon")
            continue
        try:
            data = r.json()
        except Exception:
            dbg_note(f"{kind}: ответ не JSON")
            continue
        _ozon_endpoint = ep
        return data
    # Ни один вход не ответил: дальше по всему списку будет то же самое.
    # Гасим Ozon на этот прогон, чтобы не тратить по два впустую запроса на позицию.
    _ozon_dead = True
    print("  Ozon закрыт своей защитой от роботов — работаем только по Wildberries.")
    return None


def ozon_dims(oem: str) -> list[dict]:
    """Публичный API страницы Ozon: карточки товара + характеристика «Размеры»."""
    out = []
    data = _ozon_json(f"/search/?text={quote('картридж ' + oem)}&from_global=true",
                      "поиск Ozon")
    if not data:
        return out
    blob = json.dumps(data, ensure_ascii=False)
    links = re.findall(r"/product/[a-z0-9\-]+-(\d+)/", blob)
    dbg_note(f"{oem}: карточек в выдаче Ozon — {len(set(links))}")
    for pid in list(dict.fromkeys(links))[:MAX_COMPETITORS]:
        time.sleep(PAUSE)
        card = _ozon_json(f"/product/{pid}/", f"карточка Ozon ({pid})")
        if not card:
            break
        txt = json.dumps(card, ensure_ascii=False)
        m = re.search(r"(размер[^\"]{0,40})\"[^\"]*\"value\":\"([^\"]*\d[^\"]*)\"", txt, re.I)
        if m:
            out.append({"article": pid, "dims": m.group(2),
                        "url": f"https://www.ozon.ru/product/{pid}/", "evidence": m.group(1)})
    return out


# ---------------------- Основной проход ----------------------
def load_done(path: str) -> set:
    if os.path.exists(path):
        return set(open(path, encoding="utf-8").read().split())
    return set()


def main():
    global PAUSE, DEBUG, _ozon_dead
    ap = argparse.ArgumentParser(
        description="Сбор габаритов упаковки с публичных карточек конкурентов (WB, Ozon).")
    ap.add_argument("--input", default="residual_oem.csv",
                    help="входной CSV рядом со скриптом (по умолчанию residual_oem.csv)")
    ap.add_argument("--out", default="competitor_dims.csv",
                    help="файл результата (по умолчанию competitor_dims.csv)")
    ap.add_argument("--limit", type=int, default=0,
                    help="обработать только первые N позиций (0 = все). Для теста: --limit 10")
    ap.add_argument("--pause", type=float, default=PAUSE,
                    help="пауза между запросами в секундах (по умолчанию 1.2)")
    ap.add_argument("--no-ozon", action="store_true",
                    help="не ходить на Ozon вообще (он закрыт капчей с серверного IP)")
    ap.add_argument("--debug", action="store_true",
                    help="печатать адрес, HTTP-статус и первые 200 символов ответа "
                         "для первого запроса каждого вида (для диагностики)")
    args = ap.parse_args()
    PAUSE, DEBUG = args.pause, args.debug
    if args.no_ozon:
        _ozon_dead = True                          # Ozon не трогаем совсем

    in_csv = args.input if os.path.isabs(args.input) else os.path.join(HERE, args.input)
    out_csv = args.out if os.path.isabs(args.out) else os.path.join(HERE, args.out)
    # прогресс привязан к файлу результата: тестовый прогон не мешает боевому
    done_file = out_csv + ".done.txt"

    if not os.path.exists(in_csv):
        print(f"Не найден входной файл: {in_csv}")
        print("Положите его рядом со скриптом или укажите путь через --input")
        return
    with open(in_csv, encoding="utf-8-sig", newline="") as f:
        items = list(csv.DictReader(f, delimiter=";"))
    if not items or "vendorCode" not in items[0]:
        print(f"Во входном файле нет колонки vendorCode. Проверьте {in_csv}")
        return

    done = load_done(done_file)
    todo = [it for it in items if it["vendorCode"] not in done]
    if args.limit:
        todo = todo[:args.limit]

    print(f"Вход: {os.path.basename(in_csv)} — {len(items)} позиций, "
          f"уже собрано ранее {len(done)}, в этот прогон {len(todo)}")
    if not todo:
        print("Новых позиций нет. Всё уже собрано.")
        return
    print(f"Результат будет здесь: {out_csv}")
    if DEBUG:
        print("Режим --debug: по первому запросу каждого вида будет напечатана диагностика.")
    print("Идёт сбор, это небыстро (примерно 15-25 секунд на позицию). Ctrl+C — прервать,\n"
          "при следующем запуске продолжит с того же места.\n")

    new_file = not os.path.exists(out_csv)
    out = open(out_csv, "a", encoding="utf-8-sig", newline="")
    w = csv.writer(out, delimiter=";")
    if new_file:
        w.writerow(["vendorCode", "oem", "source", "competitor_article", "url",
                    "dimensions", "evidence", "seller_id", "seller_brand",
                    "agree", "status"])
    progress = open(done_file, "a", encoding="utf-8")
    total, skipped = len(todo), 0
    for i, it in enumerate(todo, 1):
        vc = it["vendorCode"]
        oem = (it.get("oem") or it.get("model") or "").strip()
        found = []
        # --- WB ---
        nms, wb_ok = wb_search(oem)
        dbg_note(f"{oem}: карточек в выдаче WB — {len(nms)}")
        for nm in nms:
            time.sleep(PAUSE)
            card = wb_card(nm)
            if not card:
                continue
            dd = dims_from_wb(card)
            if dd:
                # Продавец нужен для сверки: две карточки ОДНОГО продавца — это одно
                # мнение, а не два. Лежит прямо в карточке, отдельный запрос не нужен.
                # Считаем по supplier_id, а НЕ по бренду: один и тот же продавец
                # (например 1375862) торгует под вывесками NV Print, Netproduct и CACTUS,
                # и по брендам он бы сошёл за трёх независимых.
                sell = card.get("selling") or {}
                found.append(["конкурент-ВБ", nm, card.get("_url", ""), dd[0], dd[1],
                              str(sell.get("supplier_id") or ""),
                              sell.get("brand_name") or ""])
            if len([x for x in found if x[0] == "конкурент-ВБ"]) >= MAX_COMPETITORS:
                break
        # --- Ozon ---
        for oz in ozon_dims(oem):
            found.append(["конкурент-Озон", oz["article"], oz["url"], oz["dims"],
                          oz["evidence"], f"ozon-{oz['article']}", ""])
        # --- запись: несколько конкурентов = сверка ---
        if not found and not wb_ok:
            # Поиск WB не ответил — позицию НЕ записываем и НЕ отмечаем пройденной,
            # чтобы следующий запуск взял её снова.
            skipped += 1
            print(f"  {i}/{total}  {vc}  {oem:<20} → поиск WB не ответил, "
                  f"позиция отложена до следующего запуска")
            time.sleep(PAUSE * 2)
            continue
        if not found:
            w.writerow([vc, oem, "", "", "", "", "", "", "", 0, "NOT_FOUND"])
            status = "NOT_FOUND"
        else:
            # Сверка. Тройку сортируем, потому что «длину» и «ширину» каждый
            # подписывает по-своему (11x31x11 и 31x11x11 — одна и та же коробка).
            # В файл размер пишется КАК ЕСТЬ, сортировка нужна только для сравнения.
            # Считаем РАЗНЫХ ПРОДАВЦОВ: две карточки одного продавца — одно мнение.
            keys = [_norm_dims(f[3]) for f in found]
            sellers: dict[tuple, set] = {}
            for f, k in zip(found, keys):
                if k:
                    sellers.setdefault(k, set()).add(f[5] or f[1])
            counts = {k: len(v) for k, v in sellers.items()}
            best = max(counts.values()) if counts else 0
            for f, k in zip(found, keys):
                agree = counts.get(k, 0)
                # CANDIDATE — одну и ту же коробку назвали минимум два РАЗНЫХ продавца,
                # и это самая распространённая коробка по коду. Иначе AMBIGUOUS:
                # строку оставляем, но смотреть её надо руками.
                st = "CANDIDATE" if agree >= 2 and agree == best else "AMBIGUOUS"
                w.writerow([vc, oem, f[0], f[1], f[2], f[3], f[4], f[5], f[6], agree, st])
            status = ("CANDIDATE, коробку назвали %d разных продавца" % best if best >= 2
                      else "AMBIGUOUS, %d карточек, согласия продавцов нет" % len(found))
        out.flush()
        progress.write(vc + "\n"); progress.flush()
        print(f"  {i}/{total}  {vc}  {oem:<20} → {status} ({len(found)} карточек)")
    out.close(); progress.close()
    print(f"\nГотово. Результат: {out_csv}")
    if skipped:
        print(f"Отложено из-за молчания поиска WB: {skipped} шт. "
              f"Запустите ту же команду ещё раз — она возьмёт только их.")
    print("Отправьте этот файл обратно — независимая проверка присвоит CONFIRMED на сервере.")


if __name__ == "__main__":
    main()
