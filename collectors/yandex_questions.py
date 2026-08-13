# поток: rev
"""collectors/yandex_questions.py — ВОПРОСЫ о товарах Яндекс.Маркета → raw_feedback.

Partner API (сверено 2026-07-28, ya_acc1): у вопросов СВОЙ путь с версией в URL —
  список:  POST /v1/businesses/{businessId}/goods-questions      (тело {}, ?limit=&page_token=)
  ответы:  POST /v1/businesses/{businessId}/goods-questions/answers   (тело {"questionId": id})
  ответ продавца: POST /v1/businesses/{businessId}/goods-questions/update
                  {"operationType": "CREATE",
                   "parentEntityId": {"id": <id вопроса>, "type": "QUESTION"}, "text": ...}
                  parentEntityId — ОБЪЕКТ, не голое число (иначе 400 «Illegal input at
                  parentEntityId»); единственная реализация — feedback_send.send_yandex_question.
Ключ и businessId — те же, что у отзывов (goods-feedback), но БЕЗ префикса /v1 отзывы, а вопросы
только с ним: `/businesses/{biz}/goods-questions` отдаёт 404, `/v1/...` — 200.

Признак «отвечен» площадка в списке НЕ отдаёт (полей answered/answersCount нет, фильтр
`{"answered": false}` тело принимает, но игнорирует — отдаёт те же записи). Поэтому по каждому
вопросу дочитываем /answers и считаем отвеченным наличие НАШЕГО ответа (author.name = имя магазина
из ответов, статус не DELETED). Вопросов на аккаунте единицы, лишних запросов это не создаёт.

Окно — 30 дней (как у остальных каналов): вопросы старше площадка ещё отдаёт, но отвечать на них
поздно, и в очередь модерации они не попадают (движок фильтрует по created_at).

Маршрут ВСЕГДА через модерацию: вопросы никогда не уходят в auto (reports/feedback_today.py
форсит route='review' для kind='question'), отправка — только по ✅ оператора.

Запуск:  ./venv/bin/python collectors/yandex_questions.py
"""
import os
import sys
import time
import pathlib

import requests
from psycopg2.extras import Json

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                 # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                            # noqa: E402

ACCOUNT = "ya_acc1"
API = "https://api.partner.market.yandex.ru"
WINDOW_DAYS = int(os.environ.get("FEEDBACK_MOD_WINDOW_DAYS", "30"))


def _cfg():
    return os.environ["YANDEX_API_KEY_ACC1"], os.environ["YANDEX_BUSINESS_ID_ACC1"]


def _post(url, key, body, params=None, tries=4):
    """POST с ретраями по лимитам площадки (420/429 — как в goods-feedback)."""
    h = {"Api-Key": key, "Content-Type": "application/json"}
    for _ in range(tries):
        r = requests.post(url, headers=h, json=body, params=params or {}, timeout=60)
        if r.status_code in (420, 429):
            time.sleep(int(r.headers.get("Retry-After", "3")) + 1)
            continue
        r.raise_for_status()
        return r.json().get("result", {})
    raise RuntimeError(f"Yandex {url}: исчерпаны ретраи по лимиту")


def _pages(key, biz, limit=50):
    """Пагинация goods-questions по nextPageToken (как у отзывов)."""
    token = None
    while True:
        params = {"limit": limit}
        if token:
            params["page_token"] = token
        res = _post(f"{API}/v1/businesses/{biz}/goods-questions", key, {}, params)
        qs = res.get("questions") or []
        for q in qs:
            yield q
        token = (res.get("paging") or {}).get("nextPageToken")
        if not qs or not token:
            break
        time.sleep(0.3)


def answers(key, biz, question_id):
    """Ответы к вопросу (наши и чужие). Пусто = никто не отвечал."""
    res = _post(f"{API}/v1/businesses/{biz}/goods-questions/answers", key,
                {"questionId": int(question_id)})
    return res.get("answers") or []


def _our_answer(ans, shop_names):
    """Наш ли это ответ. Признак — автор из числа наших имён магазина и не удалён."""
    for a in ans:
        if (a.get("status") or "").upper() == "DELETED":
            continue
        name = ((a.get("author") or {}).get("name") or "").strip()
        if name and name in shop_names:
            return a
    return None


def _shop_names():
    """Имена, под которыми площадка показывает НАШИ ответы. Берём из уже собранных отзывов
    (goods-feedback: comments[].author.name у наших комментариев) + явный дефолт."""
    names = {"Цифровой квадрат"}
    rows = db.query("""SELECT DISTINCT payload->'comments'->0->'author'->>'name' AS n
        FROM raw_feedback WHERE platform='yandex' AND kind='review'
          AND payload->'comments'->0->'author'->>'name' IS NOT NULL LIMIT 20""")
    for r in rows:
        if r["n"]:
            names.add(r["n"].strip())
    return names


def _names():
    """offerId Яндекса → название товара. ОСНОВНОЙ источник — карточка Маркета, МС только запасной.

    `raw_yandex_offer.payload->mapping->marketSkuName` — имя НАШЕГО оффера на витрине Маркета
    (при пустом берём `offer.name`, это то же название, которым мы оффер и заводили). Именно его
    видит покупатель, задавая вопрос.

    МойСклад оставлен запасным ключом (`external_code`, затем `article` — сверено 28.07: у offer
    '0996'/'23937' article пустой). Почему не первым: `external_code` НЕ уникален — на код 6806
    висит восемь позиций (6806ct Colortek, 6806oem, 6806sf, …), и `ORDER BY name` даёт произвольный
    бренд по алфавиту. 13.08.2026 из-за этого вопрос про наш W1510A показывался оператору как
    «Картридж Colortek HP W1510A». Без названия домен-фильтр движка считает товар «не расходником
    печати» и гонит вопрос на человека, поэтому запасной источник нужен."""
    m = {}
    for r in db.query("SELECT article, name FROM ms_product WHERE article IS NOT NULL ORDER BY name"):
        m.setdefault(r["article"], r["name"])
    ext = {}                                   # external_code не уникален — берём первое по алфавиту
    for r in db.query("""SELECT external_code, name FROM ms_product
                         WHERE external_code IS NOT NULL ORDER BY name"""):
        ext.setdefault(r["external_code"], r["name"])
    m.update(ext)                              # external_code приоритетнее article
    for r in db.query("""SELECT offer_id,
                                COALESCE(NULLIF(payload->'mapping'->>'marketSkuName', ''),
                                         NULLIF(payload->'offer'->>'name', '')) AS n
                         FROM raw_yandex_offer"""):
        if r["n"]:
            m[str(r["offer_id"])] = r["n"]     # витрина Маркета важнее любого имени МС
    return m


def main():
    key, biz = _cfg()
    print(f"Яндекс вопросы {ACCOUNT}", flush=True)
    prod, shops = _names(), _shop_names()
    cutoff = time.time() - WINDOW_DAYS * 86400
    recs, un, skipped_old = [], 0, 0
    n = 0
    for q in _pages(key, biz):
        ident = q.get("questionIdentifiers") or {}
        qid = str(ident.get("id"))
        created = q.get("createdAt")
        offer = str(ident.get("offerId") or "")
        n += 1
        # окно 30 дней — старьё в БД не тянем (в модерацию оно всё равно не попадёт)
        if created:
            try:
                ts = time.mktime(time.strptime(created[:19], "%Y-%m-%dT%H:%M:%S"))
                if ts < cutoff:
                    skipped_old += 1
                    continue
            except ValueError:
                pass
        ans = answers(key, biz, qid)
        mine = _our_answer(ans, shops)
        un += 0 if mine else 1
        payload = dict(q)
        payload["answers"] = ans                      # сырьё ответов рядом с вопросом
        recs.append({"platform": "yandex", "account": ACCOUNT, "kind": "question",
                     "ext_id": qid, "item_id": offer, "article": offer or None,
                     "product_name": prod.get(offer), "rating": None,
                     "body": (q.get("text") or "").strip(), "pros": None, "cons": None,
                     "created_at": created,
                     "is_answered": bool(mine),
                     "answer_text": (mine or {}).get("text"),
                     "status": None, "payload": Json(payload)})
        time.sleep(0.2)                               # вежливый темп к API вопросов
    cols = ["item_id", "article", "product_name", "rating", "body", "pros", "cons",
            "created_at", "is_answered", "answer_text", "status", "payload"]
    w = db.upsert("raw_feedback", recs,
                  conflict_cols=["platform", "account", "kind", "ext_id"], update_cols=cols) if recs else 0
    print(f"  вопросов: {n} (в окне {WINDOW_DAYS} дн. {len(recs)}, старых пропущено {skipped_old}, "
          f"без нашего ответа {un}) | записано {w}", flush=True)
    return {"total": n, "in_window": len(recs), "unanswered": un, "written": w}


if __name__ == "__main__":
    main()
