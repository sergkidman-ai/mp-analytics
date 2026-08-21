# поток: ev — новости площадок: важное из официальных каналов в дневник и в бот
"""Приёмник новостей маркетплейсов. Первый канал — уведомления Ozon в чатах ЛК.

Зачем именно чаты: у Ozon нет API новостей, сайт закрыт антиботом, а почта до нас доносит
не всё. Зато в ЛК есть официальный вещатель `NotificationUser` (`o3_notification_user_sc`) —
через него приходят и тарифы, и изменения договора, и главное: «Подключили вам „Сбор первых
отзывов“», «Для части ваших товаров подключили доупаковку».
Это ровно те новости, где деньги списываются, пока их не отключишь руками.

Отбираем по АВТОРУ сообщения, а не по типу чата: `chat_type` у Ozon для рассылок сплошь
`UNSPECIFIED`, и фильтр списка по типу сервер молча игнорирует (проверено 21.08.2026).

Из 549 накопленных уведомлений важных ~15 %: остальное — «Курьер приехал за заказом»,
«Как прошла отгрузка». Поэтому сырьё копим целиком (`mp_notices`), а в дневник и в бот
пускаем только то, что прошло правила. Список правил менять безопасно: переклассификация
идёт по сырью, без похода в API (`--reclassify`).

Запуск: ./venv/bin/python -m ops.mp_news              оба аккаунта, новое -> дневник + бот
        ./venv/bin/python -m ops.mp_news --dry        показать, ничего не писать
        ./venv/bin/python -m ops.mp_news --reclassify пересчитать важность по сырью
"""
import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv

load_dotenv("/opt/mp-analytics/.env")

import requests

from core import db
from collectors.ozon import _headers as ozon_headers
from ops import biz_diary

BASE = "https://api-seller.ozon.ru"
ACCOUNTS = ("oz_acc1", "oz_acc2")
ACC_NAME = {"oz_acc1": "Цифровой", "oz_acc2": "Дисквэр"}
# Переписка с покупателями и тикеты поддержки — не новости. Их пропуск режет обход
# с 1478 чатов до ~46 на аккаунте: рассылки живут в UNSPECIFIED и SELLER_*.
SKIP_CHATS = {"BUYER_SELLER", "SELLER_SUPPORT"}
NOTIFIER = "NotificationUser"

NOTIFY_IDS = [x.strip() for x in os.getenv("TG_PRC_NOTIFY_ID", "1031321444").split(",") if x.strip()]
TG_TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "").strip()

# --- правила важности -------------------------------------------------------------------
# ALERT — деньги: тариф, комиссия, договор, подписка и услуги, которые ПОДКЛЮЧАЮТ САМИ
# и которые надо успеть отключить. Именно про них просила Наталья.
ALERT = [
    (r"тариф", "тарифы"),
    (r"комисси", "комиссии"),
    (r"договор", "договор"),
    # Просто упоминание Premium — это реклама подписки, а не изменение её условий:
    # тревожим только когда рядом деньги или сроки.
    (r"(подписк|premium|премиум)[\s\S]{0,80}(цен|тариф|услови|стоимост|продл|отключ|повыш|сохраня|подорожа)|(цен|тариф|услови|стоимост|продл|отключ|повыш)[\s\S]{0,80}(подписк|premium|премиум)", "подписки"),
    (r"подключил[иа]\s+(вам|для|часть|некотор)|подключили\s+доупаковк", "автоподключение услуги"),
    (r"эквайринг|стоимость\s+услуг|повышени[ея]\s+цен|индексаци", "цены на услуги"),
    (r"пожар|затоплен|склад\s+(закрыт|приостанов)|приостанов\w*\s+(приём|работ)", "склад/ЧП"),
    (r"скрыли\s+.*товар|заблокирова|нарушени\w*\s+в\s+договоре", "блокировки"),
]
# WATCH — важно знать, но деньги не трогает прямо сейчас: изменения API, правила, лимиты.
WATCH = [
    (r"api\b|интеграци", "API"),
    (r"измен\w+\s+характеристик|обновили\s+раздел|новые\s+правил|изменени[яе]\s+в\s+услови", "правила"),
    (r"лимит|квот", "лимиты"),
    # Решение Натальи 21.08.2026: автодобавление в акцию из тревог убрать. Акции Ozon
    # включает постоянно, деньги это не списывает, а ленту дневника забивает.
    (r"добавили\s+ваши\s+товары\s+в\s+акци|включили\s+ваши\s+товары", "автодобавление в акцию"),
    (r"возврат\w*\s+.*(правил|услови)|новые\s+услови", "условия возвратов"),
]
# Операционка: курьеры, отгрузки, акты. Проверяется ПОСЛЕ alert/watch — заголовок
# «Про тарифы на отгрузку курьеру» содержит и «тариф», и «отгрузк», и он важный.
NOISE = (r"курьер|как прошла отгрузк|водител|заказ \d|акт по возвратам|вебинар|онлайн-встреч|"
         r"интенсив|конференц|заберите\s+их|возвраты\s+в\s+точке|узнаете|подборка\s+стат")


# По телу письма тревожим только там, где Ozon прячет факт подключения услуги в текст.
# Остальные денежные слова в теле встречаются сплошь и рядом: у операционного письма
# «У вас есть возвраты в точке выдачи» в тексте есть «тариф хранения», и по телу оно
# ложно уезжало в тревоги (проверено на 1068 уведомлениях 21.08.2026).
BODY_ALERT = [
    (r"подключил[иа]\s+(вам|для|часть|некотор)", "автоподключение услуги"),
]


def classify(title, body=""):
    """-> (importance, сработавшее правило). Решает ЗАГОЛОВОК: Ozon пишет его внятно."""
    t = (title or "").lower()
    for pat, name in ALERT:
        if re.search(pat, t):
            return "alert", name
    # Операционный заголовок закрывает вопрос — в тело таких писем лезть нельзя.
    if re.search(NOISE, t):
        return "info", "операционка"
    for pat, name in WATCH:
        if re.search(pat, t):
            return "watch", name
    b = (body or "")[:800].lower()
    for pat, name in BODY_ALERT:
        if re.search(pat, b):
            return "alert", name + " (в тексте)"
    return "info", ""


# --- сбор -------------------------------------------------------------------------------
def _post(path, headers, body, timeout=60):
    r = requests.post(BASE + path, headers=headers, json=body, timeout=timeout)
    r.raise_for_status()
    return r.json()


def chat_list(account):
    """Все чаты аккаунта, кроме покупательских и тикетов поддержки."""
    headers, out, cursor = ozon_headers(account), [], None
    while True:
        body = {"limit": 100, "filter": {"chat_status": "All"}}
        if cursor:
            body["cursor"] = cursor
        d = _post("/v3/chat/list", headers, body)
        got = d.get("chats", [])
        out += [c for c in got
                if (c.get("chat") or {}).get("chat_type") not in SKIP_CHATS]
        cursor = d.get("cursor")
        if not got or not cursor or len(got) < 100:
            return out


def seen_max(account):
    """Максимальный разобранный message_id по каждому чату — чтобы не тянуть историю зря."""
    rows = db.query("""SELECT chat_id, max(message_id::numeric) AS mx FROM mp_notices
                       WHERE platform = 'ozon' AND account = %s GROUP BY chat_id""", (account,))
    return {r["chat_id"]: r["mx"] for r in rows}


def history(account, chat_id, limit=100):
    d = _post("/v3/chat/history", ozon_headers(account),
              {"chat_id": chat_id, "direction": "Backward", "limit": limit})
    return d.get("messages", [])


def title_of(text):
    """Заголовок уведомления. Ozon шлёт его первой строкой в **звёздочках**."""
    m = re.match(r"\s*\*\*(.+?)\*\*", text)
    return (m.group(1) if m else text.split("\n")[0])[:200].strip()


def collect(account, dry=False, days=90):
    """Новые уведомления аккаунта -> mp_notices. -> список свежих строк."""
    seen = seen_max(account)
    edge = datetime.now(timezone.utc) - timedelta(days=days)
    fresh = []
    for c in chat_list(account):
        info = c.get("chat") or {}
        chat_id = str(info.get("chat_id"))
        last = c.get("last_message_id")
        # Чат без новых сообщений пропускаем, не открывая историю.
        if last is not None and chat_id in seen and seen[chat_id] is not None:
            try:
                if int(last) <= int(seen[chat_id]):
                    continue
            except (TypeError, ValueError):
                pass
        for m in history(account, chat_id):
            if (m.get("user") or {}).get("type") != NOTIFIER:
                continue
            mid = str(m.get("message_id"))
            if chat_id in seen and seen[chat_id] is not None and int(mid) <= int(seen[chat_id]):
                continue
            created = m.get("created_at") or ""
            try:
                when = datetime.fromisoformat(created.replace("Z", "+00:00"))
            except ValueError:
                continue
            if when < edge:                       # старую историю первым прогоном не тянем
                continue
            text = "\n".join(m.get("data", []))
            title = title_of(text)
            imp, rule = classify(title, text)
            row = {"account": account, "message_id": mid, "chat_id": chat_id,
                   "chat_type": info.get("chat_type"), "created_at": when,
                   "title": title, "body": text[:4000], "importance": imp, "matched": rule}
            fresh.append(row)
            if not dry:
                db.execute("""
                    INSERT INTO mp_notices (platform, account, message_id, chat_id, chat_type,
                                            created_at, title, body, importance, matched)
                    VALUES ('ozon',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (platform, account, message_id) DO NOTHING
                """, (account, mid, chat_id, info.get("chat_type"), when, title,
                      text[:4000], imp, rule))
    return fresh


def to_diary(dry=False, since_days=14):
    """Только ALERT из сырья -> дневник (kind='mp'). Идемпотентно по dedup_key.

    Watch в дневник НЕ пускаем сознательно: за 90 дней его набирается столько же, сколько
    тревог, и наши собственные решения — ради которых дневник и заводился — утонут в
    «обновили методы Seller API». Watch лежит в `mp_notices` и поднимается запросом.
    """
    rows = db.query("""SELECT account, message_id, created_at::date AS d, title, body, matched
                       FROM mp_notices
                       WHERE platform='ozon' AND importance = 'alert'
                         AND event_id IS NULL
                         AND created_at >= current_date - %s::int
                       ORDER BY created_at""", (since_days,))
    added = 0
    for r in rows:
        if dry:
            added += 1
            continue
        eid = biz_diary.add(
            event_date=r["d"], kind="mp", platform="ozon", account=r["account"],
            title=r["title"], details=(r["body"] or "")[:1500],
            expect=f"Правило: {r['matched']}" if r["matched"] else None,
            source="ozon_chat", author="Ozon",
            dedup_key=f"ozon:{r['account']}:{r['message_id']}")
        db.execute("UPDATE mp_notices SET event_id = %s WHERE platform='ozon' "
                   "AND account=%s AND message_id=%s", (eid, r["account"], r["message_id"]))
        added += 1
    return added


def reclassify():
    """Пересчёт важности по сырью — после правки правил, без похода в API."""
    rows = db.query("SELECT account, message_id, title, body FROM mp_notices WHERE platform='ozon'")
    changed = 0
    for r in rows:
        imp, rule = classify(r["title"] or "", r["body"] or "")
        n = db.query("""UPDATE mp_notices SET importance=%s, matched=%s
                        WHERE platform='ozon' AND account=%s AND message_id=%s
                          AND (importance <> %s OR matched IS DISTINCT FROM %s)
                        RETURNING message_id""",
                     (imp, rule, r["account"], r["message_id"], imp, rule))
        changed += len(n)
    # Правила поменялись — значит, часть уже заведённых событий больше не важна.
    # Оставить их в дневнике нельзя: он потеряет доверие, а вычищать руками никто не станет.
    stale = db.query("""SELECT account, message_id, event_id FROM mp_notices
                        WHERE platform='ozon' AND event_id IS NOT NULL AND importance <> 'alert'""")
    for r in stale:
        biz_diary.delete(r["event_id"])
        db.execute("UPDATE mp_notices SET event_id = NULL WHERE platform='ozon' "
                   "AND account=%s AND message_id=%s", (r["account"], r["message_id"]))
    print(f"переклассифицировано: {changed} из {len(rows)}; "
          f"снято с дневника: {len(stale)}")


def tg(text):
    if not TG_TOKEN:
        return "нет TG_PRC_BOT_TOKEN"
    out = []
    for chat in NOTIFY_IDS:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              data={"chat_id": chat, "text": text[:3900],
                                    "disable_web_page_preview": "true"}, timeout=60)
            out.append("ok" if r.ok else f"{r.status_code} {r.text[:100]}")
        except Exception as exc:
            out.append(f"{type(exc).__name__}: {exc}")
    return "; ".join(out)


def message(alerts):
    """Одно сообщение за прогон. Деньги вперёд, подробности — в Пульте."""
    lines = ["🏪 Ozon — важные изменения", ""]
    tail = len(alerts) - 12
    for a in alerts[:12]:
        lines.append(f"• [{ACC_NAME.get(a['account'], a['account'])}] {a['title']}")
        if a["matched"]:
            lines.append(f"  ↳ {a['matched']}")
    if tail > 0:
        lines.append(f"…и ещё {tail}")
    lines += ["", "Полный список — «Что мы меняли» на главной Пульта."]
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Новости площадок -> дневник + бот")
    ap.add_argument("--dry", action="store_true", help="показать, ничего не писать")
    ap.add_argument("--quiet", action="store_true", help="не слать телеграм")
    ap.add_argument("--days", type=int, default=90, help="как глубоко брать историю")
    ap.add_argument("--diary-days", type=int, default=14,
                    help="за сколько дней тревоги заводить в дневник (сырьё копится глубже)")
    ap.add_argument("--reclassify", action="store_true", help="пересчитать важность по сырью")
    args = ap.parse_args(argv)

    if args.reclassify:
        reclassify()
        return 0

    fresh = []
    for acc in ACCOUNTS:
        try:
            got = collect(acc, dry=args.dry, days=args.days)
        except Exception as exc:                       # один аккаунт не должен ронять второй
            print(f"{acc}: СБОЙ {type(exc).__name__}: {exc}")
            continue
        by = {}
        for r in got:
            by[r["importance"]] = by.get(r["importance"], 0) + 1
        print(f"{acc}: новых уведомлений {len(got)} | {by or '—'}")
        fresh += got

    added = to_diary(dry=args.dry, since_days=args.diary_days)
    print(f"в дневник: {added}" + (" (сухой прогон)" if args.dry else ""))

    alerts = [r for r in fresh if r["importance"] == "alert"]
    if alerts and not args.quiet and not args.dry:
        print("телеграм: " + tg(message(alerts)))
    elif alerts:
        print(f"тревог: {len(alerts)} (не отправлено)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
