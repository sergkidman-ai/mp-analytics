# -*- coding: utf-8 -*-
"""collectors/feedback_send.py — ОТПРАВКА ответов на ВОПРОСЫ покупателей в WB/Ozon.

Первый write-путь движка автоответов. Вызывается ботом-модератором после ручного
подтверждения. По умолчанию — dry-run (FEEDBACK_LIVE_SEND=0): ничего реально не шлём,
только логируем «что бы ушло». FEEDBACK_LIVE_SEND=1 включает живую отправку.

Тела запросов (сверено 2026-07-19):
  WB:   PATCH https://feedbacks-api.wildberries.ru/api/v1/questions
        {"id": <ext_id>, "answer": {"text": <text>}, "state": "wbRu"}   header Authorization=<token>
        (state=wbRu — ответ публикуется на витрине; лимит 1 rps на категорию «Вопросы и отзывы»)
  Ozon: POST  https://api-seller.ozon.ru/v1/question/answer/create
        {"question_id": <payload.id=ext_id>, "sku": <payload.sku=item_id, int>, "text": <text>}
        headers Client-Id + Api-Key (collectors.ozon._headers)
  Яндекс: POST https://api.partner.market.yandex.ru/v1/businesses/{biz}/goods-questions/update
        {"operationType": "CREATE", "parentEntityId": <ext_id = id вопроса>, "text": <text>}
        header Api-Key (у ВОПРОСОВ версия /v1 в пути обязательна, у отзывов путь /v2)

Постинг фиксируется в raw_feedback.posted_at/posted_ok/answer_text (только в live-режиме).
Охват — вопросы аккаунтов wb_acc1 / oz_acc1 (только у них доступ к ответам).
"""
import os
import sys
import time
import json
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv                       # noqa: E402
load_dotenv(BASE_DIR / ".env")
from core import db                                  # noqa: E402
from collectors.wb import _token as _wb_token        # noqa: E402
from collectors.ozon import _headers as _oz_headers  # noqa: E402

WB_QUESTIONS_URL = "https://feedbacks-api.wildberries.ru/api/v1/questions"
WB_FEEDBACK_ANSWER_URL = "https://feedbacks-api.wildberries.ru/api/v1/feedbacks/answer"
OZ_ANSWER_URL = "https://api-seller.ozon.ru/v1/question/answer/create"
OZ_REVIEW_COMMENT_URL = "https://api-seller.ozon.ru/v1/review/comment/create"
YA_API = "https://api.partner.market.yandex.ru"


def _reload_env():
    """Перечитать .env ПЕРЕД каждой проверкой гейта.

    Зачем: бот модерации — ДОЛГОЖИВУЩИЙ процесс (systemd, недели аптайма), а load_dotenv отрабатывает
    один раз при импорте. Реальный инцидент 27.07: бот стартовал 23.07, живые флаги дописаны в .env
    27.07 — процесс их не увидел, `_live()` в нём остался False, и кнопка ✅ молча уходила в dry-run
    (карточка помечалась state='sent', а на площадку НИЧЕГО не уходило: posted_at=NULL). Циклы этого
    не показывали — каждый цикл это НОВЫЙ процесс со свежим .env, поэтому автоотправка работала живьём,
    а модерация нет. override=True обязателен: без него dotenv не перезаписывает уже загруженные ключи."""
    load_dotenv(BASE_DIR / ".env", override=True)


def _live():
    _reload_env()
    return os.environ.get("FEEDBACK_LIVE_SEND", "0") == "1"


def _live_accounts():
    """Аллоу-лист аккаунтов для поэтапного включения (FEEDBACK_LIVE_ACCOUNTS=wb_acc1,oz_acc1).
    Пусто при FEEDBACK_LIVE_SEND=1 = ничего не льём нигде (безопасный дефолт, не «все аккаунты»)."""
    _reload_env()
    raw = os.environ.get("FEEDBACK_LIVE_ACCOUNTS", "")
    return {a.strip() for a in raw.split(",") if a.strip()}


def _backlog_cap():
    """Дневной лимит НА КАНАЛ и ТОЛЬКО для авто-ответов на СТАРЫЙ бэклог отзывов (решение Сергея
    28.07). Вопросы, ответы оператора из Telegram и авто-ответы на свежие отзывы лимитом не режутся —
    они уходят сразу в ближайшем цикле. Анти-залповая механика (порции по 5 + паузы 3–5 с) остаётся
    для всех: лимит защищает от массовой заливки старья, а не от нормальной работы."""
    _reload_env()
    raw = os.environ.get("FEEDBACK_BACKLOG_DAILY_CAP", "")
    return int(raw) if raw.strip() else 150


def backlog_days():
    """Граница «свежий отзыв / бэклог» в днях (по created_at отзыва)."""
    _reload_env()
    raw = os.environ.get("FEEDBACK_BACKLOG_DAYS", "")
    return int(raw) if raw.strip() else 7


def is_backlog(row):
    """True для отзыва старше backlog_days() — только такие тратят дневной лимит канала."""
    if row.get("kind") != "review":
        return False
    ts = row.get("created_at")
    if not ts:
        return False
    return (time.time() - ts.timestamp()) > backlog_days() * 86400


# Календарные сутки МОСКВЫ, а не сервера. Таймзона БД = Etc/UTC, поэтому `posted_at::date =
# current_date` сбрасывал дневной лимит в 03:00 МСК, а отправки с 21:00 до 24:00 МСК списывались
# с квоты УЖЕ ПРОШЕДШЕГО дня. posted_at — timestamptz, так что AT TIME ZONE переводит корректно.
MSK_TODAY_SQL = ("(posted_at AT TIME ZONE 'Europe/Moscow')::date"
                 " = (now() AT TIME ZONE 'Europe/Moscow')::date")


def _sent_today():
    """Сколько ответов РЕАЛЬНО опубликовано за сегодня (Москва) — ФАКТ для баннеров и сводок.

    Считаем строго успехи: posted_ok=true. Неудачные попытки пишут posted_ok=false и сюда не
    попадают; dry-run вообще ничего не пишет в raw_feedback (_mark_posted вызывается только после
    реального вызова API) — то есть ни отбойные вызовы, ни холостой режим счётчик не двигают."""
    r = db.query(f"""SELECT count(*) AS n FROM raw_feedback
        WHERE posted_ok=true AND {MSK_TODAY_SQL}""")
    return r[0]["n"] if r else 0


def backlog_sent_today(platform, account):
    """Сколько авто-ответов по бэклогу отзывов ушло сегодня (Москва) в ЭТОМ канале — база лимита.
    Считаем по факту публикации: успех + отзыв + старше границы + маршрут auto."""
    r = db.query(f"""SELECT count(*) AS n FROM raw_feedback
        WHERE posted_ok=true AND {MSK_TODAY_SQL} AND kind='review' AND draft_route='auto'
          AND platform=%s AND account=%s
          AND created_at < now() - make_interval(days => %s)""",
        (platform, account, backlog_days()))
    return r[0]["n"] if r else 0


def sent_today_by_channel():
    """{(platform, account): n} — опубликовано сегодня (Москва) по каналам. Для баннера карточки
    и суточной сводки: показываем ФАКТ публикаций, а не только остаток лимита."""
    rows = db.query(f"""SELECT platform, account, count(*) AS n FROM raw_feedback
        WHERE posted_ok=true AND {MSK_TODAY_SQL}
        GROUP BY platform, account ORDER BY platform, account""")
    return {(r["platform"], r["account"]): r["n"] for r in rows}


def _log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] feedback_send: {msg}", flush=True)


def send_wb_question(account, question_id, text):
    """PATCH ответа на вопрос WB. Бросает исключение при не-2xx. Возвращает True."""
    h = {"Authorization": _wb_token(account), "Content-Type": "application/json"}
    body = {"id": question_id, "answer": {"text": text}, "state": "wbRu"}
    for _ in range(4):
        r = requests.patch(WB_QUESTIONS_URL, headers=h, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 1)
            continue
        r.raise_for_status()
        return True
    raise RuntimeError("WB questions: исчерпаны ретраи по 429")


def send_ozon_question(account, question_id, sku, text):
    """POST ответа на вопрос Ozon. Бросает исключение при не-2xx. Возвращает True."""
    h = _oz_headers(account)
    body = {"question_id": question_id, "sku": int(sku), "text": text}
    for _ in range(4):
        r = requests.post(OZ_ANSWER_URL, headers=h, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "3")) + 1)
            continue
        r.raise_for_status()
        return True
    raise RuntimeError("Ozon question/answer: исчерпаны ретраи по 429")


def send_wb_review(account, feedback_id, text):
    """POST первичного ответа на ОТЗЫВ WB (POST /feedbacks/answer, тело {id,text}; успех 204;
    лимит 1 rps). Бросает исключение при не-2xx. Возвращает True."""
    h = {"Authorization": _wb_token(account), "Content-Type": "application/json"}
    body = {"id": feedback_id, "text": text}
    for _ in range(4):
        r = requests.post(WB_FEEDBACK_ANSWER_URL, headers=h, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "2")) + 1)
            continue
        r.raise_for_status()
        return True
    raise RuntimeError("WB feedbacks/answer: исчерпаны ретраи по 429")


def send_ozon_review(account, review_id, text):
    """POST комментария-ответа на ОТЗЫВ Ozon (Premium) + пометка отзыва обработанным. Бросает
    исключение при не-2xx. Возвращает True."""
    h = _oz_headers(account)
    body = {"review_id": review_id, "text": text, "mark_review_as_processed": True}
    for _ in range(4):
        r = requests.post(OZ_REVIEW_COMMENT_URL, headers=h, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "3")) + 1)
            continue
        r.raise_for_status()
        return True
    raise RuntimeError("Ozon review/comment: исчерпаны ретраи по 429")


def send_yandex_review(account, feedback_id, text):
    """POST комментария-ответа на ОТЗЫВ Яндекс.Маркета (updateGoodsFeedbackComment, новый коммент =
    без id/parentId). Бросает исключение при не-2xx. Возвращает True."""
    key = os.environ["YANDEX_API_KEY_ACC1"]
    biz = os.environ["YANDEX_BUSINESS_ID_ACC1"]
    h = {"Api-Key": key, "Content-Type": "application/json"}
    url = f"{YA_API}/v2/businesses/{biz}/goods-feedback/comments/update"
    body = {"feedbackId": int(feedback_id), "comment": {"text": text}}
    for _ in range(4):
        r = requests.post(url, headers=h, json=body, timeout=60)
        if r.status_code in (420, 429):
            time.sleep(int(r.headers.get("Retry-After", "3")) + 1)
            continue
        r.raise_for_status()
        return True
    raise RuntimeError("Yandex goods-feedback/comments: исчерпаны ретраи по лимиту")


def send_yandex_question(account, question_id, text):
    """POST ответа продавца на ВОПРОС о товаре Яндекс.Маркета.

    /v1/businesses/{biz}/goods-questions/update, operationType=CREATE, parentEntityId = id вопроса
    (у вопросов версия в пути обязательна — без /v1 путь отдаёт 404, в отличие от отзывов).
    Бросает исключение при не-2xx. Возвращает True."""
    key = os.environ["YANDEX_API_KEY_ACC1"]
    biz = os.environ["YANDEX_BUSINESS_ID_ACC1"]
    h = {"Api-Key": key, "Content-Type": "application/json"}
    url = f"{YA_API}/v1/businesses/{biz}/goods-questions/update"
    body = {"operationType": "CREATE", "parentEntityId": int(question_id), "text": text}
    for _ in range(4):
        r = requests.post(url, headers=h, json=body, timeout=60)
        if r.status_code in (420, 429):
            time.sleep(int(r.headers.get("Retry-After", "3")) + 1)
            continue
        r.raise_for_status()
        return True
    raise RuntimeError("Yandex goods-questions/update: исчерпаны ретраи по лимиту")


def _mark_posted(row, text, ok, err=None):
    """Зафиксировать результат живой отправки в raw_feedback (posted_*/answer_text)."""
    db.execute("""UPDATE raw_feedback SET posted_at=now(), posted_ok=%s,
        answer_text=CASE WHEN %s THEN %s ELSE answer_text END
        WHERE platform=%s AND account=%s AND kind=%s AND ext_id=%s""",
        (ok, ok, text, row["platform"], row["account"], row["kind"], row["ext_id"]))


def post_answer(row, text, apply_cap=False):
    """Диспетчер отправки ответа. row — строка raw_feedback (dict с platform/account/kind/ext_id/
    item_id/payload). text — финальный текст (одобренный/исправленный). apply_cap=True ставит
    вызов под дневной лимит канала — его передаёт ТОЛЬКО авто-отправка бэклога отзывов
    (collectors/feedback_autosend.py). Ручные ответы оператора и свежие отзывы идут без лимита.
    → (ok: bool, detail: str). В dry-run реально не шлёт и НЕ помечает posted."""
    text = (text or "").strip()
    if not text:
        return False, "пустой текст"
    if text.startswith("⚠️"):                       # маркер «на человека» — не ответ покупателю
        return False, "маркер-заглушка (на человека), не отправляется — ответьте вручную"
    kind = row["kind"]
    if kind not in ("question", "review"):
        return False, f"kind={kind} вне охвата (вопросы и отзывы)"
    plat, acc, ext = row["platform"], row["account"], row["ext_id"]

    if not _live():
        _log(f"DRY-RUN {plat}/{acc} {kind}={ext}: ушло бы «{text[:80]}»")
        return True, "dry-run"
    if acc not in _live_accounts():
        _log(f"DRY-RUN(acct) {plat}/{acc} {kind}={ext}: аккаунт вне FEEDBACK_LIVE_ACCOUNTS")
        return True, "dry-run:acct-not-live"
    if apply_cap:
        cap = _backlog_cap()
        used = backlog_sent_today(plat, acc)
        if used >= cap:
            _log(f"DRY-RUN(cap) {plat}/{acc} {kind}={ext}: лимит бэклога канала {used}/{cap} исчерпан")
            return True, "dry-run:daily-cap"

    try:
        payload = row.get("payload") or {}
        if plat == "wb":
            if kind == "question":
                send_wb_question(acc, ext, text)          # ext_id = id вопроса
            else:
                send_wb_review(acc, ext, text)            # ext_id = id отзыва (payload.id)
        elif plat == "ozon":
            if kind == "question":
                sku = payload.get("sku") or row.get("item_id")
                if not sku:
                    return False, "нет sku для Ozon-вопроса"
                send_ozon_question(acc, payload.get("id") or ext, sku, text)
            else:
                send_ozon_review(acc, payload.get("id") or ext, text)  # review_id = payload.id
        elif plat == "yandex":
            if kind == "question":
                # ext_id = questionIdentifiers.id (см. collectors/yandex_questions.py)
                send_yandex_question(acc, ext, text)
            else:
                send_yandex_review(acc, payload.get("feedbackId") or ext, text)
        else:
            return False, f"неизвестная площадка {plat}"
    except Exception as e:
        detail = f"{type(e).__name__}: {str(e)[:200]}"
        # тело ответа площадки — часто полезно для разбора формата
        if isinstance(e, requests.HTTPError) and e.response is not None:
            detail += f" | {e.response.text[:200]}"
        _log(f"FAIL {plat}/{acc} {kind}={ext}: {detail}")
        _mark_posted(row, text, False, detail)
        return False, detail

    _log(f"SENT {plat}/{acc} {kind}={ext}: «{text[:80]}»")
    _mark_posted(row, text, True)
    return True, "sent"


if __name__ == "__main__":
    # Ручная проверка формата (dry-run по умолчанию): подставить реальные ext_id вопроса.
    r = db.query("""SELECT platform,account,kind,ext_id,item_id,payload FROM raw_feedback
        WHERE kind='question' AND account IN ('wb_acc1','oz_acc1') LIMIT 1""")
    if r:
        ok, detail = post_answer(r[0], "Тестовый ответ (не отправлять).")
        print(json.dumps({"ok": ok, "detail": detail}, ensure_ascii=False))
    else:
        print("нет вопросов для проверки")
