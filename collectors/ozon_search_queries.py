"""collectors/ozon_search_queries.py — поисковые запросы Ozon по товарам (недельная история).

POST /v1/analytics/product-queries          — сводка по SKU: искали / показали / позиция / GMV.
POST /v1/analytics/product-queries/details  — конкретные фразы по каждому SKU.

Раздел кабинета «Аналитика → Поисковые запросы». Работает на ОБЕИХ подписках Ozon
(проверено A/B по двум аккаунтам 30.07.2026, docs/ozon-recon-report.md, раздел 9).

Зачем свой сбор: у Ozon глубина всего ~6 недель, дальше 400 «There is no data for the
specified period». История живёт только у нас в ozon_search_query / ozon_search_product.

Ограничения API, снятые с живых вызовов (не из документации — она недоступна):
  * skus обязателен, 1..1000 штук. На коротком списке сводка молча отдаёт items: [] —
    слать надо пачками в сотни SKU, иначе метод выглядит нерабочим;
  * limit_by_sku ∈ (0, 15]  — больше 15 фраз на товар не отдаёт;
  * page_size ∈ (0, 100];
  * date_to ИСКЛЮЧАЮЩИЙ: неделя = [понедельник, следующий понедельник);
  * page считается С НУЛЯ. Начав с page=1, молча теряешь первые page_size строк — то есть
    самые частые запросы, ради которых всё и затевалось. Сторож в ozon_search_run.tail_dropped;
  * лимит 2 запроса/сек на клиента, при превышении 429 → пауза.

Стратегия: сводку берём по ВСЕМ товарам в продаже, фразы — только по тем, кого реально
искали (порог MIN_SEARCH), иначе длинный хвост из десятков тысяч SKU растягивает прогон
на часы, а данных не несёт (как MIN_OPENS в wb_funnel).

Запуск:
    ./venv/bin/python collectors/ozon_search_queries.py                 # обе площадки, прошлая неделя
    ./venv/bin/python collectors/ozon_search_queries.py oz_acc1         # один аккаунт
    ./venv/bin/python collectors/ozon_search_queries.py all --backfill  # вся доступная глубина
    ./venv/bin/python collectors/ozon_search_queries.py all --force     # перезабрать уже собранное
"""
import datetime
import os
import pathlib
import sys
import time

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

load_dotenv(BASE_DIR / ".env")
API = "https://api-seller.ozon.ru"
CRED_ENV = {"oz_acc1": ("OZON_CLIENT_ID_ACC1", "OZON_API_KEY_ACC1"),
            "oz_acc2": ("OZON_CLIENT_ID_ACC2", "OZON_API_KEY_ACC2")}
ACCOUNTS = ["oz_acc1", "oz_acc2"]

SUMMARY_BATCH = 500     # SKU в одном вызове сводки (потолок API — 1000)
DETAILS_BATCH = 500     # SKU в одном вызове деталей
LIMIT_BY_SKU = 15       # потолок API
PAGE_SIZE = 100         # потолок API
MIN_SEARCH = 5          # ниже этого числа искавших фразы не тянем — длинный хвост
MAX_DEPTH_WEEKS = 10    # предохранитель при поиске границы глубины
PAUSE = 0.55            # 2 запроса/сек


def _headers(account):
    cid_env, key_env = CRED_ENV[account]
    cid, key = os.getenv(cid_env), os.getenv(key_env)
    if not cid or not key:
        raise RuntimeError(f"{cid_env}/{key_env} не заданы в .env")
    return {"Client-Id": cid, "Api-Key": key, "Content-Type": "application/json"}


class Api:
    """Клиент с лимитером и счётчиком вызовов (счётчик уходит в журнал прогонов)."""

    def __init__(self, account):
        self.account = account
        self.headers = _headers(account)
        self.calls = 0

    def post(self, path, body, tries=4):
        for attempt in range(tries):
            r = requests.post(API + path, headers=self.headers, json=body, timeout=120)
            self.calls += 1
            time.sleep(PAUSE)
            if r.status_code == 429:            # лимитер Ozon: 2 запроса/сек
                time.sleep(2 + attempt * 2)
                continue
            if r.status_code >= 500:
                time.sleep(3 + attempt * 3)
                continue
            return r.status_code, (r.json() if r.content else {})
        return r.status_code, (r.json() if r.content else {})


def _iso(d):
    return d.isoformat() + "T00:00:00Z"


def _monday(d):
    return d - datetime.timedelta(days=d.weekday())


def _weeks_available(api, probe_sku, today=None):
    """Список недель [Пн, Пн), которые Ozon ещё отдаёт: от самой старой к свежей.

    Старую границу ищем перебором назад — на слишком давнюю дату Ozon отвечает 400
    «There is no data for the specified period».

    Свежая граница: ДАННЫЕ ОТСТАЮТ НА ДВА ДНЯ. Проверено 30.07.2026 (четверг):
    date_to=30.07 → 400, date_to=29.07 → 200. date_to исключающий, значит последний
    посчитанный день = позавчера. Из-за этого неделю, закончившуюся в понедельник,
    в тот же понедельник забрать НЕЛЬЗЯ — cron стоит на среду.
    """
    today = today or datetime.date.today()
    max_to = today - datetime.timedelta(days=1)   # предельный date_to (исключающий)
    cur_mon = _monday(today)
    weeks, wk = [], cur_mon
    for _ in range(MAX_DEPTH_WEEKS):
        wk -= datetime.timedelta(days=7)
        we = wk + datetime.timedelta(days=7)
        if we > max_to:                           # неделя закрылась, но ещё не досчитана
            continue
        code, _j = api.post("/v1/analytics/product-queries",
                            {"date_from": _iso(wk), "date_to": _iso(we),
                             "skus": probe_sku, "page": 0, "page_size": 3})
        if code != 200:
            break
        weeks.append((wk, we))
    weeks.reverse()
    if cur_mon < max_to:                          # текущая неделя, добранная по позавчера
        weeks.append((cur_mon, max_to))
    return weeks


def _all_skus(api):
    """Все SKU в продаже: /v3/product/list постранично через last_id."""
    skus, last_id = [], ""
    while True:
        code, j = api.post("/v3/product/list",
                           {"filter": {"visibility": "IN_SALE"}, "limit": 1000, "last_id": last_id})
        if code != 200:
            raise RuntimeError(f"product/list вернул {code}: {str(j)[:200]}")
        res = j.get("result") or {}
        items = res.get("items") or []
        skus += [str(i["sku"]) for i in items if i.get("sku")]
        last_id = res.get("last_id") or ""
        if not items or not last_id or len(items) < 1000:
            break
    return sorted(set(skus))


def _paginate(api, path, body, rows_key):
    """Постраничный обход. ВНИМАНИЕ: page у Ozon считается С НУЛЯ.

    Проверено на query_index: page=0 отдаёт строки 1..page_size, page=1 — 16..30 и т.д.
    Если начать с page=1 (как подсказывает интуиция), молча теряются первые page_size строк —
    а это ровно верхушка выдачи, самые частые запросы. Сортировка по убыванию частоты.

    Возвращает (строки, сколько строк недобрали против total). Второе — сторож целостности:
    в норме 0, ненулевое значение означает, что Ozon поменял поведение.
    """
    rows, page, total = [], 0, None
    while True:
        code, j = api.post(path, dict(body, page=page, page_size=PAGE_SIZE))
        if code != 200:
            print(f"    [{path}] стр.{page} → {code}: {str(j)[:160]}", flush=True)
            break
        if total is None:
            total = j.get("total") or 0
        chunk = j.get(rows_key) or []
        if not chunk:                          # пустая страница = данные кончились
            break
        rows += chunk
        page += 1
        if page > (j.get("page_count") or 0):   # страницы 0..page_count-1
            break
    return rows, max(0, (total or 0) - len(rows))


def _num(x):
    """Ozon отдаёт часть чисел строками (unique_view_users приходит как "23")."""
    if x is None or x == "":
        return 0
    try:
        return int(x)
    except (TypeError, ValueError):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0


def collect_week(api, account, ws, we, skus):
    """Одна неделя по одному аккаунту: сводка по всем SKU + фразы по значимым."""
    print(f"  неделя {ws}..{we} (исключая {we}), SKU: {len(skus)}", flush=True)
    base = {"date_from": _iso(ws), "date_to": _iso(we)}
    prod_rows, dropped = [], 0

    # 1) сводка по всем товарам
    for i in range(0, len(skus), SUMMARY_BATCH):
        batch = skus[i:i + SUMMARY_BATCH]
        items, lost = _paginate(api, "/v1/analytics/product-queries", dict(base, skus=batch), "items")
        dropped += lost
        for it in items:
            prod_rows.append({
                "account": account, "period_start": ws, "period_end": we,
                "sku": str(it.get("sku")), "offer_id": it.get("offer_id"),
                "name": it.get("name"), "category": it.get("category"),
                "position": _num(it.get("position")),
                "unique_search_users": _num(it.get("unique_search_users")),
                "unique_view_users": _num(it.get("unique_view_users")),
                "view_conversion": _num(it.get("view_conversion")),
                "order_count": _num(it.get("order_count")),
                "gmv": _num(it.get("gmv")), "currency": it.get("currency") or "RUB",
            })
        print(f"    сводка {i + len(batch)}/{len(skus)} SKU → всего строк {len(prod_rows)}", flush=True)
    if prod_rows:
        db.upsert("ozon_search_product", prod_rows,
                  conflict_cols=["account", "period_start", "sku"])

    # 2) фразы — только по товарам с заметным спросом
    hot = sorted({r["sku"] for r in prod_rows if r["unique_search_users"] >= MIN_SEARCH})
    print(f"    искали ≥{MIN_SEARCH} человек: {len(hot)} SKU → тянем фразы", flush=True)
    q_rows = []
    for i in range(0, len(hot), DETAILS_BATCH):
        batch = hot[i:i + DETAILS_BATCH]
        qs, lost = _paginate(api, "/v1/analytics/product-queries/details",
                             dict(base, skus=batch, limit_by_sku=LIMIT_BY_SKU), "queries")
        dropped += lost
        seen = set()
        chunk = []
        for q in qs:
            key = (str(q.get("sku")), (q.get("query") or "").strip())
            if not key[1] or key in seen:      # одна фраза может прийти дважды на стыке страниц
                continue
            seen.add(key)
            chunk.append({
                "account": account, "period_start": ws, "period_end": we,
                "sku": key[0], "query": key[1],
                "position": _num(q.get("position")),
                "unique_search_users": _num(q.get("unique_search_users")),
                "unique_view_users": _num(q.get("unique_view_users")),
                "view_conversion": _num(q.get("view_conversion")),
                "order_count": _num(q.get("order_count")),
                "gmv": _num(q.get("gmv")), "currency": q.get("currency") or "RUB",
            })
        if chunk:
            db.upsert("ozon_search_query", chunk,
                      conflict_cols=["account", "period_start", "sku", "query"])
            q_rows += chunk
        print(f"    фразы {i + len(batch)}/{len(hot)} SKU → всего строк {len(q_rows)}", flush=True)

    db.upsert("ozon_search_run", [{
        "account": account, "period_start": ws, "period_end": we,
        "skus_total": len(skus), "skus_with_data": len(prod_rows), "skus_detailed": len(hot),
        "queries_rows": len(q_rows), "api_calls": api.calls, "tail_dropped": dropped,
        "finished_at": datetime.datetime.now(),
    }], conflict_cols=["account", "period_start"])
    print(f"  итог недели: товаров {len(prod_rows)}, фраз {len(q_rows)}, "
          f"недобрано хвостов {dropped}", flush=True)
    return len(prod_rows), len(q_rows)


def main(account="oz_acc1", backfill=False, force=False):
    api = Api(account)
    skus = _all_skus(api)
    if not skus:
        print(f"[{account}] нет товаров в продаже — пропуск", flush=True)
        return
    weeks = _weeks_available(api, skus[:3])
    if not backfill:
        weeks = weeks[-3:]           # две последние закрытые недели + текущая неполная
    print(f"[{account}] товаров в продаже {len(skus)}, недель к сбору {len(weeks)}: "
          f"{[str(w[0]) for w in weeks]}", flush=True)

    done = set()
    if not force:
        # «Собрано» считается только то, что закрылось больше 16 дней назад. Свежие недели
        # перезабираем каждый прогон, и не потому что Ozon «досчитывает»: на повторном заборе
        # он присылает НЕМНОГО ДРУГОЙ срез фраз той же недели. Замерено 30.07 на неделе
        # 13–20.07: пришло 80 491 строка против 91 524 в первом заборе, но 289 из них были
        # новыми — то есть перезабор добирает объединение, а не уточняет старое.
        # Дублей не будет, upsert идёт по PK (аккаунт, неделя, sku, фраза).
        settled_before = datetime.date.today() - datetime.timedelta(days=16)
        done = {r["period_start"] for r in db.query(
            "SELECT period_start FROM ozon_search_run WHERE account = %s "
            "AND queries_rows > 0 AND period_end < %s", (account, settled_before))}
    for ws, we in weeks:
        if ws in done:
            print(f"  неделя {ws} уже собрана — пропуск (--force чтобы перезабрать)", flush=True)
            continue
        collect_week(api, account, ws, we, skus)
    print(f"[{account}] готово, вызовов к API: {api.calls}", flush=True)


if __name__ == "__main__":
    args = sys.argv[1:]
    backfill = "--backfill" in args
    force = "--force" in args
    positional = [a for a in args if not a.startswith("--")]
    target = positional[0] if positional else "all"
    for acc in (ACCOUNTS if target == "all" else [target]):
        main(acc, backfill=backfill, force=force)
