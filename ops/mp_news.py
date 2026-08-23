# поток: ev — новости площадок: важное из официальных каналов в дневник и в бот
"""Приёмник новостей маркетплейсов: Ozon (чаты ЛК) и WB (лента новостей).

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

У WB, в отличие от Ozon, новостное API есть: `communications/v2/news`. Отдаёт до 100 записей
за запрос, листается сдвигом `from`. Токен нужен только `wb_acc1` — новости общие для площадки,
а у токена Дисквэра нет нужной категории (404, см. память wb-token-scopes). Лента WB и есть
тот самый «заранее»: и тарифы, и пожары на складах приходят в неё раньше, чем доедут до цифр.

Запуск: ./venv/bin/python -m ops.mp_news              обе площадки, новое -> дневник + бот
        ./venv/bin/python -m ops.mp_news --dry        показать, ничего не писать
        ./venv/bin/python -m ops.mp_news --reclassify пересчитать важность по сырью
        ./venv/bin/python -m ops.mp_news --platform wb --days 90 --diary-days 90 --diary-rule склад/ЧП
                                                      разовый добор истории по одному правилу
"""
import argparse
import email
import email.utils
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv

load_dotenv("/opt/mp-analytics/.env")

import requests

from core import db
from collectors.ozon import _headers as ozon_headers
from ops import biz_diary
from ops import news_digest as digest

BASE = "https://api-seller.ozon.ru"
ACCOUNTS = ("oz_acc1", "oz_acc2")
ACC_NAME = {"oz_acc1": "Цифровой", "oz_acc2": "Дисквэр"}
# Переписка с покупателями и тикеты поддержки — не новости. Их пропуск режет обход
# с 1478 чатов до ~46 на аккаунте: рассылки живут в UNSPECIFIED и SELLER_*.
SKIP_CHATS = {"BUYER_SELLER", "SELLER_SUPPORT"}
NOTIFIER = "NotificationUser"

# WB: новости общие для площадки, поэтому один токен и один «аккаунт» в сырье.
WB_NEWS = "https://common-api.wildberries.ru/api/communications/v2/news"
WB_ACCOUNT = "wb_acc1"
# common-api WB держит жёсткий лимит на ленту новостей — листаем неспешно.
WB_PAGE_PAUSE = 8      # пауза между страницами, с
WB_RETRY_PAUSE = 30    # база ожидания после 429, с
PLATFORMS = ("ozon", "wb", "yandex")

# Маркет новостного API не даёт, зато шлёт письма. Наталья отключила лишние уведомления
# в ЛК и настроила пересылку нужных на рабочий ящик — читаем ту папку, куда они падают.
# Подпапку «Маркет. Не важное» не трогаем: она для того и заведена.
YA_FOLDER = "Яндекс.Маркет"
YA_ACCOUNT = "ya_mail"
YA_BODY_MAX = 12000  # письмо о тарифах на 8 тыс. знаков: главное часто в конце

SOURCE_OF = {"ozon": "ozon_chat", "wb": "wb_news", "yandex": "ya_mail"}
AUTHOR_OF = {"ozon": "Ozon", "wb": "WB", "yandex": "Маркет"}

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
    # WB о пожарах пишет казённо: «Работа склада «Котовск» временно приостановлена»,
    # «на складе произошла нештатная ситуация». Слова «пожар» в заголовке чаще нет —
    # ловим по остановке приёмки и по эвакуации, иначе июльские пожары уходят в info.
    # Новость про НАШ пострадавший остаток — самая денежная в этой теме, а по заголовку
    # («Остатки на складе „Коледино" отобразим в отдельном столбце») ни одно правило про ЧП
    # не срабатывало: ни пожара, ни эвакуации, ни остановки приёмки в тексте нет.
    (r"остатк\w*[\s\S]{0,60}(пострадал|отдельн\w+\s+столб)|пострадал\w*[\s\S]{0,40}остатк",
     "склад/ЧП"),
    (r"пожар|возгоран|задымлен|затоплен|нештатн\w*\s+ситуац|эвакуац|"
     r"(склад\w*|\bсц\b|\bск\b|сортировочн\w+\s+центр)\W[^.]{0,80}(приостанов|закрыт|не принима)|"
     r"приостанов\w*\s+(приём|работ)", "склад/ЧП"),
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


def _strip_html(text):
    """Разметка -> чистый текст. Нужен и для новостей WB, и для писем Маркета.

    У письма Маркета две особенности: <style> с версткой (его текст попал бы в тело новости)
    и «невидимая набивка» — сотни символов U+2800 (пустой шрифт Брайля), которыми в рассылках
    растягивают preheader. И то и другое надо снимать до правил, иначе тело новости — мусор.
    """
    text = re.sub(r"<(style|script)[^>]*>.*?</\1>", " ", text or "", flags=re.I | re.S)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (text.replace("&nbsp;", " ").replace("&mdash;", "—").replace("&laquo;", "«")
                .replace("&raquo;", "»").replace("&amp;", "&").replace("&quot;", '"'))
    text = re.sub(r"[\u2800\u200b\u00a0\ufeff]+", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _wb_page(token, cursor, tries=4):
    """Одна страница ленты WB. 429 у common-api — норма, а не сбой: ждём и повторяем."""
    for attempt in range(tries):
        r = requests.get(WB_NEWS, params={"from": cursor},
                         headers={"Authorization": token}, timeout=60)
        if r.status_code == 429:
            if attempt == tries - 1:
                r.raise_for_status()
            time.sleep(WB_RETRY_PAUSE * (attempt + 1))
            continue
        r.raise_for_status()
        return (r.json() or {}).get("data") or []
    return []


def wb_news(since):
    """Лента новостей WB с даты `since`. -> список записей (id/date/header/content/types).

    Листаем сдвигом `from`: за раз WB отдаёт максимум 100 и молча обрезает хвост. Признак
    конца — страница, не принёсшая НИ ОДНОГО нового id: на дату-границу опираться нельзя,
    последняя новость приезжает дважды.
    """
    token = os.getenv("WB_TOKEN_ACC1", "").strip()
    if not token:
        raise RuntimeError("нет WB_TOKEN_ACC1")
    seen, out, cursor = set(), [], since
    for page in range(50):                                 # предохранитель от вечного цикла
        if page:
            time.sleep(WB_PAGE_PAUSE)
        items = _wb_page(token, cursor)
        new = [x for x in items if x.get("id") not in seen]
        if not new:
            return out
        seen.update(x["id"] for x in new)
        out += new
        if len(items) < 100:
            return out
        cursor = max(x["date"] for x in items)[:10]
    return out


def collect_wb(dry=False, days=90):
    """Новости WB -> mp_notices. -> список свежих строк (тех, что раньше не видели)."""
    have = {r["message_id"] for r in db.query(
        "SELECT message_id FROM mp_notices WHERE platform='wb'")}
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    fresh = []
    for n in wb_news(since):
        mid = str(n.get("id"))
        if mid in have:
            continue
        title = (n.get("header") or "").strip()[:200]
        body = _strip_html(n.get("content"))
        imp, rule = classify(title, body)
        try:
            when = datetime.fromisoformat(n["date"])
        except (KeyError, ValueError):
            continue
        # types приходит списком словарей [{"id":79,"name":"Товары"}] — кладём в chat_type
        # человеческие названия рубрик: по ним потом видно, какого рода была новость.
        types = ", ".join(t.get("name", "") for t in (n.get("types") or []))[:100]
        row = {"account": WB_ACCOUNT, "message_id": mid, "chat_id": None,
               "chat_type": types, "created_at": when, "title": title,
               "body": body[:4000], "importance": imp, "matched": rule}
        fresh.append(row)
        if not dry:
            db.execute("""
                INSERT INTO mp_notices (platform, account, message_id, chat_id, chat_type,
                                        created_at, title, body, importance, matched)
                VALUES ('wb',%s,%s,NULL,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (platform, account, message_id) DO NOTHING
            """, (WB_ACCOUNT, mid, types, when, title, body[:4000], imp, rule))
    return fresh


MONTHS_EN = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# Заголовок пересылки Яндекс-почты: «20.08.2026, 13:17, Новости Маркета (seller@market.yandex.ru):»
FWD_HEAD = re.compile(r"(\d{2})\.(\d{2})\.(\d{4}),\s*(\d{2}):(\d{2})"
                      r"(?:,\s*([^(<\n]{0,60}?)\s*[(<]([\w.\-]+@[\w.\-]+)[)>])?")
MSK = timezone(timedelta(hours=3))


def _mail_text(msg):
    """Тело письма -> чистый текст. text/plain, если есть; иначе html без разметки."""
    parts = msg.walk() if msg.is_multipart() else [msg]
    plain, html = "", ""
    for part in parts:
        if part.get_content_maintype() != "text" or part.get("Content-Disposition", "").startswith("attach"):
            continue
        try:
            body = part.get_payload(decode=True).decode(
                part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if part.get_content_subtype() == "plain":
            plain += body
        else:
            html += body
    return _strip_html(plain if plain.strip() else html)


def _fwd_trim(text):
    """Хвост шапки пересылки и подвал рассылки убрать — в теле должна остаться новость.

    После заголовка «дата, отправитель» у пересылки идут служебные строки «Кому/Тема/Копия»
    и голые адреса в скобках, а в конце письма — «отписаться» и реквизиты. Ни то ни другое
    в дневнике не нужно.
    """
    lines = text.split("\n")
    while lines:
        head = lines[0].strip()
        if head and not re.match(r"^[\W_]*$|^(кому|тема|копия|to|cc|subject)\b|"
                                 r"^[\W_]*[\w.\-]+@[\w.\-]+[\W_]*$", head, flags=re.I):
            break
        lines.pop(0)
    body = "\n".join(lines)
    cut = re.search(r"отписаться|чтобы (?:больше )?не получать|"
                    r"вы получили это письмо|настроить уведомлени", body, flags=re.I)
    return (body[:cut.start()] if cut else body).strip()


def collect_yandex(dry=False, days=90, folder=YA_FOLDER):
    """Письма Маркета из почтовой папки -> mp_notices. -> список свежих строк.

    Наталья пересылает нужные письма в папку «Яндекс.Маркет» (мусорные уведомления в ЛК
    отключены). Письмо — пересылка, поэтому дата ПИСЬМА (когда переслали) для дневника
    не годится: берём дату ОРИГИНАЛА из шапки «Пересылаемое сообщение», и только если её
    нет — дату письма. Ящик открываем readonly: флаг «прочитано» — дело человека, не наше.
    """
    from prices.mailbox import connect, imap_utf7, _hdr

    have = {r["message_id"] for r in db.query(
        "SELECT message_id FROM mp_notices WHERE platform='yandex'")}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    crit = f"{since.day:02d}-{MONTHS_EN[since.month - 1]}-{since.year}"
    fresh = []
    box = connect()
    try:
        box.select(imap_utf7(folder), readonly=True)
        uids = (box.uid("search", None, "SINCE", crit)[1][0] or b"").split()
        for uid in uids:
            raw = box.uid("fetch", uid, "(RFC822)")[1][0][1]
            msg = email.message_from_bytes(raw)
            mid = (msg.get("Message-ID") or "").strip("<> ") or f"uid:{uid.decode()}"
            if mid in have:
                continue
            title = re.sub(r"^\s*(?:fwd|fw|re)\s*:\s*", "", _hdr(msg.get("Subject")),
                           flags=re.I).strip()[:200]
            text = _mail_text(msg)
            m = FWD_HEAD.search(text[:600])
            if m:
                when = datetime(int(m[3]), int(m[2]), int(m[1]),
                                int(m[4]), int(m[5]), tzinfo=MSK)
                sender = (m[7] or m[6] or "").strip()[:100]
                text = text[m.end():]          # шапку пересылки в тело новости не тащим
            else:
                when = email.utils.parsedate_to_datetime(msg.get("Date"))
                sender = _hdr(msg.get("From"))[:100]
            body = _fwd_trim(text)
            imp, rule = classify(title, body)
            row = {"account": YA_ACCOUNT, "message_id": mid, "chat_id": folder,
                   "chat_type": sender, "created_at": when, "title": title,
                   "body": body[:YA_BODY_MAX], "importance": imp, "matched": rule}
            fresh.append(row)
            if not dry:
                db.execute("""
                    INSERT INTO mp_notices (platform, account, message_id, chat_id, chat_type,
                                            created_at, title, body, importance, matched)
                    VALUES ('yandex',%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (platform, account, message_id) DO NOTHING
                """, (YA_ACCOUNT, mid, folder, sender, when, title, body[:YA_BODY_MAX], imp, rule))
    finally:
        try:
            box.logout()
        except Exception:
            pass
    return fresh


# --- Ущерб складам ВБ (атаки с 18.07.2026) -------------------------------------------------
# Со слов Натальи 21.08.2026. Площадка о судьбе ТОВАРА не пишет вовсе — новость сообщает лишь
# «работа склада приостановлена», а для нас это списание остатка и претензия. Поэтому статус
# склада живёт рядом с новостью и подставляется в событие дневника.
# Чего тут НЕТ: складов, по которым данных нет. Молчание — это «неизвестно», а не «цело».
WH_STATUS = (
    (r"Электростал", "lost", "Электросталь"),
    (r"Котовск", "lost", "Котовск"),
    (r"Новосемейкино", "lost", "Новосемейкино"),
    (r"Чехов|Новосёлк|Новоселк", "lost", "Чехов / Новосёлки"),
    (r"Алексин", "lost", "Алексин"),
    (r"Владимир|Воршин", "lost", "Владимир: Воршинское"),
    (r"Северн\w*\s+Домодедово", "lost", "Северное Домодедово"),  # просто Домодедово — не отсюда
    (r"Шушар", "hit", "Шушары"),
    (r"Коледино", "hit", "Коледино"),
    (r"Краснодар", "hit", "Краснодар"),
    (r"Невинномысск", "hit", "Невинномысск"),
)
WH_ICON = {"lost": "🔥", "hit": "💥"}
WH_PHRASE = {
    "lost": "уничтожен полностью — весь наш товар, лежавший там на хранении, утрачен",
    "hit": "серьёзно повреждён — высокая вероятность, что весь наш товар утрачен",
}
WH_ACTION = {
    "lost": "Считать остаток на этом складе утраченным на дату удара: списать в учёте "
            "и заявить компенсацию ВБ.",
    "hit": "Запросить у ВБ судьбу остатка на складе на дату удара и готовить списание "
           "с претензией.",
}
# Значок ставим ОДИН раз на склад — в день удара. Дальше по тому же складу идёт хвост новостей
# («не принимает товары», «возобновили работу»), и повтор значка сбивал бы дату, на которую
# считать остатки. Решение Натальи 21.08.2026.
WH_MARKS = "".join(WH_ICON.values()) + "⚠️"

# В теле новости ВБ перечисляет и ЦЕЛЫЕ склады — куда перенаправить поставки («можно
# отгрузить на склад „Чехов 2“»). Слепой поиск по телу метил такие склады как пострадавшие:
# «Рязань» получала статус от «Владимир: Воршинское» из строки перенаправления. Поэтому
# смотрим заголовок и только те предложения тела, где описан САМ инцидент.
WH_INCIDENT = re.compile(r"нештатн|произошл|пострадал|поврежд|приостановлен", re.I)
WH_REDIRECT = re.compile(r"отгруз|перенаправ|перенес|примут|вместо|запланированн", re.I)


def wh_hits(title, body=""):
    """Какие пострадавшие склады названы в новости -> [(имя, 'lost'|'hit'), ...]."""
    parts = [title or ""]
    for sent in re.split(r"(?<=[.!?])\s+|\n+", (body or "")[:1500]):
        if WH_INCIDENT.search(sent) and not WH_REDIRECT.search(sent):
            parts.append(sent)
    text = "\n".join(parts)
    return [(name, st) for pat, st, name in WH_STATUS if re.search(pat, text, re.I)]


def _wh_clean(details):
    """Снять прежние пометки об ударе: правила уточняются, старая метка уходить обязана."""
    keep = [ln for ln in (details or "").splitlines() if not ln[:2].strip().startswith(tuple(WH_MARKS))]
    return "\n".join(keep).strip()


def annotate_damage():
    """Значок удара — ОДИН раз на склад, в день его первой новости. Идемпотентно."""
    rows = db.query("""SELECT b.id, b.event_date, b.title, b.details, b.expect,
                              n.body, n.digest
                       FROM biz_events b LEFT JOIN mp_notices n ON n.event_id = b.id
                       WHERE b.platform = 'wb' AND b.kind = 'mp'
                       ORDER BY b.event_date, b.id""")
    first = {}                                   # склад -> (id события, статус)
    for r in rows:
        for name, st in wh_hits(r["title"] or "", r["body"] or ""):
            first.setdefault(name, (r["id"], st))
    own = {}                                     # id события -> [(склад, статус), ...]
    for name, (eid, st) in first.items():
        own.setdefault(eid, []).append((name, st))

    upd = 0
    for r in rows:
        hits = sorted(own.get(r["id"], []))
        note = "\n".join(f"{WH_ICON[st]} «{name}» {WH_PHRASE[st]}. Остатки считать "
                          f"на {r['event_date'].strftime('%d.%m.%Y')}."
                          for name, st in hits)
        mark = (WH_ICON["lost"] if any(st == "lost" for _, st in hits)
                else WH_ICON["hit"] if hits else None)
        action = (WH_ACTION["lost"] if any(st == "lost" for _, st in hits)
                  else WH_ACTION["hit"] if hits else None)
        details = _wh_clean(r["details"])
        details = f"{note}\n\n{details}".strip() if note else details
        expect = action or (r["digest"] or {}).get("action")
        if details == (r["details"] or "") and expect == r["expect"] and mark == r.get("mark"):
            continue
        db.execute("UPDATE biz_events SET details=%s, expect=%s, mark=%s WHERE id=%s",
                   (details, expect, mark, r["id"]))
        upd += 1
    print(f"склады ВБ: значок удара стоит у {len(own)} событий из {len(rows)}; обновлено {upd}")


# Паузу склада ВБ закрывает следующая же новость: «СЦ «Воронеж СГТ» временно не принимает» в
# 20:17 и «СЦ «Воронеж СГТ» возобновил работу» в 04:23. Это штатная остановка по воздушной
# тревоге — людей вывели, товару ничего не сделалось, наших поставок туда нет. Правило Сергея
# 23.08.2026: в ленте остаётся только то, что реально бьёт по бизнесу, а не всякая остановка.
REOPEN_RE = re.compile(r"возобнов|снова\s+принима|снова\s+работа|снова\s+открыт", re.I)
# Маркеры настоящего ЧП. Если склад горел или по нему прилетело — событие остаётся в ленте
# даже после открытия: там наш товар мог сгореть, и это деньги (случай «Котовск», июль).
DAMAGE_RE = re.compile(r"пожар|возгоран|бпла|беспилотн|дрон|атак|повреж|утрач|сгорел|обрушен|"
                       r"чрезвычайн|\bмчс\b|подтоплен|затоплен", re.I)
PAUSE_WINDOW_H = 72


def _places(text):
    """Склады из заголовка — они у ВБ всегда в «ёлочках»: «Воронеж СГТ», «Коледино»."""
    return {p.strip().lower() for p in re.findall(r"«([^»]+)»", text or "")}


def _wh_base(name):
    """«Тула КГТ+» → «тула»: у ВБ один физический куст носит десяток имён-приставок."""
    s = (name or "").lower().replace("\xa0", " ")
    s = re.split(r"[:(]", s)[0]
    s = re.sub(r"\b(сгт|кгт\+?|кгт|фбс|fbs|—?\s*питание|склад|сц|ск)\b", " ", s)
    return re.sub(r"[^а-яёa-z0-9 ]+", " ", s).strip()


def our_warehouses():
    """Склады ВБ, где ЛЕЖИТ наш товар — по последнему снимку `wb_stocks`.

    Зачем: «СЦ «Воронеж СГТ» не принимает товары» — новость для тех, у кого там товар.
    У нас в Воронеже нет ни штуки, значит и события нет. Список берём из данных, а не руками:
    товар переезжает по складам, а зашитый перечень протухает молча.
    """
    rows = db.query("""SELECT DISTINCT warehouse FROM wb_stocks
                        WHERE captured_at = (SELECT max(captured_at) FROM wb_stocks)
                          AND quantity > 0""")
    return {b for b in (_wh_base(r["warehouse"]) for r in rows) if b}


def close_pauses(dry=False, window_hours=PAUSE_WINDOW_H):
    """Пауза склада, закрытая следующей новостью, — не событие дневника.

    Ищем пары «склад приостановлен» → «склад возобновил работу» по одному и тому же складу
    в окне 72 ч и решаем судьбу события:
      • был удар или в тексте ЧП (пожар, БПЛА, повреждения) — событие ОСТАВЛЯЕМ и дописываем,
        что склад открылся: висящий без развязки инцидент читается как незакрытая проблема;
      • ничего не горело — новость гасим до `watch`, событие из дневника убираем совсем.

    Считаем ДО `to_diary`: если пауза и открытие приехали одним прогоном (у ВБ обычная
    картина — остановка вечером, открытие ночью), событие не заводится вовсе.
    """
    pauses = db.query("""SELECT platform, account, message_id, created_at, title, body, event_id
                           FROM mp_notices
                          WHERE matched = 'склад/ЧП' AND importance = 'alert'
                          ORDER BY created_at""")
    reopens = db.query("""SELECT platform, created_at, title FROM mp_notices
                           WHERE title ~* 'возобнов|снова принима|снова работа'
                           ORDER BY created_at""")
    ours = our_warehouses()
    dropped = closed = alien = 0
    for p in pauses:
        names = _places(p["title"])
        if not names:
            continue
        ev = db.query("SELECT id, mark, details FROM biz_events WHERE id = %s",
                      (p["event_id"],)) if p["event_id"] else []
        damage = bool(DAMAGE_RE.search((p["title"] or "") + " " + (p["body"] or "")[:1500]))
        hit = bool(ev and ev[0]["mark"])          # значок удара уже стоит — это наш убыток
        # Чужой склад: нашего товара там нет и поставок туда мы не возим. Не новость —
        # даже если склад стоит третий день (правило Сергея 23.08.2026).
        if p["platform"] == "wb" and not damage and not hit \
                and not any(_wh_base(n) and _wh_base(n) in ours for n in names):
            alien += 1
            if not dry:
                _drop_pause(p, ev, "склад не наш (товара там нет)")
            continue
        end = p["created_at"] + timedelta(hours=window_hours)
        back = next((r for r in reopens
                     if r["platform"] == p["platform"]
                     and p["created_at"] < r["created_at"] <= end
                     and names & _places(r["title"])), None)
        if not back:
            continue
        if damage or hit:
            line = f"✅ Закрыто {back['created_at']:%d.%m %H:%M}: {back['title']}"
            if ev and line[:12] not in (ev[0]["details"] or ""):
                closed += 1
                if not dry:
                    db.execute("UPDATE biz_events SET details = %s WHERE id = %s",
                               ((ev[0]["details"] or "").rstrip() + "\n\n" + line, ev[0]["id"]))
            continue
        dropped += 1
        if not dry:
            _drop_pause(p, ev, "штатная пауза склада (открыт снова)")
    print(f"паузы складов: погашено {dropped} (открылись), {alien} (склад не наш), "
          f"закрыто пометкой {closed}" + (" (сухой прогон)" if dry else ""))
    return dropped + alien, closed


def _drop_pause(p, ev, why):
    """Новость гасим до watch (сырьё остаётся), событие из дневника убираем."""
    db.execute("""UPDATE mp_notices SET importance = 'watch', matched = %s, event_id = NULL
                   WHERE platform=%s AND account=%s AND message_id=%s""",
               (why, p["platform"], p["account"], p["message_id"]))
    if ev:
        biz_diary.delete(ev[0]["id"])


def to_diary(dry=False, since_days=14, platform=None, only_rule=None):
    """Только ALERT из сырья -> дневник (kind='mp'). Идемпотентно по dedup_key.

    `only_rule` — добрать историю по ОДНОМУ правилу, не поднимая всё остальное: пожары на
    складах за июль нужны в дневнике задним числом, а июльские тарифы уже неактуальны.

    Watch в дневник НЕ пускаем сознательно: за 90 дней его набирается столько же, сколько
    тревог, и наши собственные решения — ради которых дневник и заводился — утонут в
    «обновили методы Seller API». Watch лежит в `mp_notices` и поднимается запросом.
    """
    where, params = ["importance = 'alert'", "event_id IS NULL",
                     "created_at >= current_date - %s::int"], [since_days]
    if platform:
        where.append("platform = %s")
        params.append(platform)
    if only_rule:
        where.append("matched = %s")
        params.append(only_rule)
    rows = db.query(f"""SELECT platform, account, message_id, created_at::date AS d,
                               title, body, matched, digest
                        FROM mp_notices WHERE {' AND '.join(where)}
                        ORDER BY created_at""", tuple(params))
    added = 0
    for r in rows:
        if dry:
            added += 1
            continue
        # У WB и Маркета новости общие для площадки — аккаунт в дневнике не проставляем, иначе
        # событие сядет на график одного аккаунта, а касается оно обоих.
        eid = biz_diary.add(
            event_date=r["d"], kind="mp", platform=r["platform"],
            account=r["account"] if r["platform"] == "ozon" else None,
            title=r["title"],
            # Если выжимка уже посчитана — в дневник идёт она: человеку нужен денежный смысл
            # новости, а не три экрана текста площадки. Сырьё остаётся в mp_notices.
            details=(digest.render(r["digest"]) if r["digest"]
                     else (r["body"] or "")[:1500] + (f"\n\nПравило: {r['matched']}"
                                                     if r["matched"] else "")),
            # В `expect` у события площадки лежит рекомендация «что делать» — она есть только
            # там, где посчитана выжимка. Название правила туда не кладём: на главной оно
            # читалось бы как «что делать: склад/ЧП».
            expect=(r["digest"] or {}).get("action"),
            source=SOURCE_OF.get(r["platform"], r["platform"]),
            author=AUTHOR_OF.get(r["platform"], r["platform"]),
            dedup_key=f"{r['platform']}:{r['account']}:{r['message_id']}")
        db.execute("UPDATE mp_notices SET event_id = %s WHERE platform=%s "
                   "AND account=%s AND message_id=%s",
                   (eid, r["platform"], r["account"], r["message_id"]))
        added += 1
    return added


def reclassify():
    """Пересчёт важности по сырью — после правки правил, без похода в API."""
    rows = db.query("SELECT platform, account, message_id, title, body FROM mp_notices")
    changed = 0
    for r in rows:
        imp, rule = classify(r["title"] or "", r["body"] or "")
        n = db.query("""UPDATE mp_notices SET importance=%s, matched=%s
                        WHERE platform=%s AND account=%s AND message_id=%s
                          AND (importance <> %s OR matched IS DISTINCT FROM %s)
                        RETURNING message_id""",
                     (imp, rule, r["platform"], r["account"], r["message_id"], imp, rule))
        changed += len(n)
    # Правила поменялись — значит, часть уже заведённых событий больше не важна.
    # Оставить их в дневнике нельзя: он потеряет доверие, а вычищать руками никто не станет.
    stale = db.query("""SELECT platform, account, message_id, event_id FROM mp_notices
                        WHERE event_id IS NOT NULL AND importance <> 'alert'""")
    for r in stale:
        biz_diary.delete(r["event_id"])
        db.execute("UPDATE mp_notices SET event_id = NULL WHERE platform=%s "
                   "AND account=%s AND message_id=%s",
                   (r["platform"], r["account"], r["message_id"]))
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
    lines = ["🏪 Важные изменения площадок", ""]
    tail = len(alerts) - 12
    for a in alerts[:12]:
        # У WB новость общая для площадки, у Ozon — своя на каждый аккаунт.
        who = "WB" if a.get("platform") == "wb" else ACC_NAME.get(a["account"], a["account"])
        lines.append(f"• [{who}] {a['title']}")
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
    ap.add_argument("--close-pauses", action="store_true",
                    help="погасить паузы складов, закрытые следующей новостью")
    ap.add_argument("--wh-damage", action="store_true",
                    help="проставить статус складов ВБ (уничтожен/повреждён) в событиях дневника")
    ap.add_argument("--platform", choices=PLATFORMS, help="только одна площадка")
    ap.add_argument("--diary-rule", metavar="ПРАВИЛО",
                    help="в дневник только по этому правилу (разовый добор истории)")
    ap.add_argument("--digest", action="store_true",
                    help="считать выжимку по денежным новостям (ПЛАТНЫЕ запросы к модели)")
    ap.add_argument("--diary-only", action="store_true",
                    help="не ходить в API: поднять в дневник то, что уже лежит в сырье")
    args = ap.parse_args(argv)

    if args.reclassify:
        reclassify()
        return 0

    if args.close_pauses:
        close_pauses(dry=args.dry)
        return 0

    if args.wh_damage:
        annotate_damage()
        return 0

    fresh = []
    sources = []
    if args.diary_only:
        sources = []
    elif args.platform in (None, "ozon"):
        sources += [(acc, "ozon", lambda a=acc: collect(a, dry=args.dry, days=args.days))
                    for acc in ACCOUNTS]
    if not args.diary_only and args.platform in (None, "wb"):
        sources.append(("WB", "wb", lambda: collect_wb(dry=args.dry, days=args.days)))
    if not args.diary_only and args.platform in (None, "yandex"):
        sources.append(("Маркет (почта)", "yandex",
                        lambda: collect_yandex(dry=args.dry, days=args.days)))
    for name, plat, fetch in sources:
        try:
            got = fetch()
        except Exception as exc:                    # один источник не должен ронять остальные
            print(f"{name}: СБОЙ {type(exc).__name__}: {exc}")
            continue
        by = {}
        for r in got:
            r["platform"] = plat
            by[r["importance"]] = by.get(r["importance"], 0) + 1
        print(f"{name}: новых уведомлений {len(got)} | {by or '—'}")
        fresh += got

    # Выжимку считаем ДО дневника: тогда событие сразу заводится с разбором, а не с сырым
    # текстом, который потом пришлось бы переписывать.
    if args.digest and not args.dry:
        digest.run(days=args.diary_days, dry=False)

    # Сначала гасим паузы, закрытые следующей новостью, потом заводим события: иначе в ленту
    # успевает попасть остановка склада, которую тот же прогон уже видит закрытой.
    close_pauses(dry=args.dry)

    added = to_diary(dry=args.dry, since_days=args.diary_days,
                     platform=args.platform, only_rule=args.diary_rule)
    print(f"в дневник: {added}" + (" (сухой прогон)" if args.dry else ""))
    if not args.dry:
        # Значок удара считается по ВСЕЙ ленте разом: «первый раз на склад» видно только
        # в общей хронологии, событие в одиночку про это не знает.
        annotate_damage()

    alerts = [r for r in fresh if r["importance"] == "alert"]
    if alerts and not args.quiet and not args.dry:
        print("телеграм: " + tg(message(alerts)))
    elif alerts:
        print(f"тревог: {len(alerts)} (не отправлено)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
