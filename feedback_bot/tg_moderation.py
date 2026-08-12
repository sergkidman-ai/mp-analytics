# -*- coding: utf-8 -*-
"""feedback_bot/tg_moderation.py — Telegram-модерация ответов на ВОПРОСЫ покупателей.

Боевой режим с ручным подтверждением. Движок (reports.feedback_today, FEEDBACK_MODERATION=1)
кладёт вопросы в очередь feedback_moderation (state='queued'); текст предложенного ответа —
в raw_feedback.draft_text. Этот бот:
  1) периодически шлёт карточку по каждому 'queued' вопросу в TG_NOTIFY_ID (state→'carded');
  2) по inline-кнопке: ✅ Отправить (уходит draft_text) / ✏️ Править (пришли свой текст → уходит он) /
     🚫 Пропустить. Отправка идёт через collectors.feedback_send.post_answer (dry-run/live по
     FEEDBACK_LIVE_SEND). Ничего не публикуется без нажатия человека.

Зависимостей нет — long-polling на urllib (как invoice_bot/tg_bot.py).

ВАЖНО про токен: invoice_bot уже держит getUpdates на TG_BOT_TOKEN. Два бота на ОДНОМ токене
конфликтуют (Telegram 409). Заведи ОТДЕЛЬНОГО бота у @BotFather и положи его токен в
TG_FEEDBACK_BOT_TOKEN. Если он не задан — берём TG_BOT_TOKEN и предупреждаем (тогда invoice-bot
надо остановить). Из .env:
    TG_FEEDBACK_BOT_TOKEN=123456:AA...     # отдельный бот модерации (рекомендуется)
    TG_ALLOWED_IDS=11111111,22222222       # кто может подтверждать
    TG_NOTIFY_ID=11111111                  # куда слать карточки
    FEEDBACK_LIVE_SEND=0|1                  # 0 = dry-run (по умолчанию)

Запуск: ./venv/bin/python feedback_bot/tg_moderation.py   (в бою — под systemd)
"""
import os
import sys
import json
import time
import re
import html
import urllib.request
import urllib.error
import traceback
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv
load_dotenv("/opt/mp-analytics/.env")
from core import db
import collectors.feedback_send as fs

TOKEN = (os.getenv("TG_FEEDBACK_BOT_TOKEN") or os.getenv("TG_BOT_TOKEN") or "").strip()
_DEDICATED = bool(os.getenv("TG_FEEDBACK_BOT_TOKEN"))
# Свои списки доступа/адресатов (фолбэк на общие). Отдельные — потому что у бота-модератора
# другой Start-relationship, чем у invoice_bot: там свои чаты, здесь свои. Общие TG_*_IDS не трогаем.
ALLOWED = {x.strip() for x in (os.getenv("TG_FEEDBACK_ALLOWED_IDS") or os.getenv("TG_ALLOWED_IDS", "")).split(",") if x.strip()}
NOTIFY = (os.getenv("TG_FEEDBACK_NOTIFY_ID") or os.getenv("TG_NOTIFY_ID") or "").strip()
NOTIFY_IDS = [x.strip() for x in NOTIFY.split(",") if x.strip()]   # может быть списком
API = f"https://api.telegram.org/bot{TOKEN}"
POLL_QUEUE_SEC = int(os.getenv("FEEDBACK_QUEUE_POLL_SEC", "15"))

# from_id -> mod_id, ожидание исправленного текста после «✏️ Править»
PENDING_EDIT = {}


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def api(method, params=None, timeout=60):
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(f"{API}/{method}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def send(chat_id, text, reply_markup=None):
    text = text if len(text) <= 4000 else text[:3990] + "\n…(обрезано)"
    p = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
    if reply_markup:
        p["reply_markup"] = reply_markup
    try:
        r = api("sendMessage", p)
        return r.get("result", {}).get("message_id")
    except Exception as e:
        log(f"sendMessage error: {e}")
        return None


def edit_text(chat_id, message_id, text):
    p = {"chat_id": chat_id, "message_id": message_id, "text": text[:4000],
         "parse_mode": "HTML", "disable_web_page_preview": True}
    try:
        api("editMessageText", p)
    except Exception as e:
        log(f"editMessageText error: {e}")


def answer_cb(cb_id, text=""):
    try:
        api("answerCallbackQuery", {"callback_query_id": cb_id, "text": text[:190]})
    except Exception as e:
        log(f"answerCallbackQuery error: {e}")


# ---------- очередь / БД ----------

# Окно модерации: показываем/шлём только фидбек за последние N дней (старьё не выводим).
WINDOW_DAYS = int(os.getenv("FEEDBACK_MOD_WINDOW_DAYS", "30"))
BATCH_CAP = int(os.getenv("FEEDBACK_MOD_BATCH_CAP", "60"))   # предохранитель от флуда на «показать всё»


def _pending(limit=5, days=None, kind=None):
    """Карточки, готовые к показу: свежие 'queued' + проснувшиеся 'snoozed', ТОЛЬКО за последние
    `days` дней (по дате отзыва/вопроса). Отдаём порцией (limit) — рассылка только по кнопке.
    `kind` ('question'/'review') сужает выборку — чтобы можно было прогнать очередь вопросов
    отдельно от отзывов (вопросы срочнее: покупатель ждёт ответа до покупки)."""
    days = WINDOW_DAYS if days is None else days
    return db.query("""SELECT m.id, m.platform, m.account, m.kind, m.ext_id,
        f.product_name, f.body, f.pros, f.cons, f.rating, f.created_at,
        f.item_id, f.article, f.draft_text, f.draft_route, f.draft_grounding
        FROM feedback_moderation m
        JOIN raw_feedback f ON f.platform=m.platform AND f.account=m.account
             AND f.kind=m.kind AND f.ext_id=m.ext_id
        WHERE ((m.state='queued' AND m.tg_msg_id IS NULL)
               OR (m.state='snoozed' AND m.snooze_until <= now()))
          -- уже отвеченное на площадке (или отправленное нами, но ещё не подтверждённое площадкой)
          -- в карточки не тянем: очередь модерации живёт дольше, чем актуальность вопроса
          AND COALESCE(f.is_answered, false) = false AND f.posted_at IS NULL
          AND f.created_at >= now() - make_interval(days => %s)
          AND (%s IS NULL OR m.kind = %s)
        ORDER BY f.created_at DESC LIMIT %s""", (days, kind, kind, limit))


def _mod(mod_id):
    r = db.query("""SELECT m.id, m.platform, m.account, m.kind, m.ext_id, m.state,
        m.tg_chat_id, m.tg_msg_id, f.product_name, f.body, f.draft_text
        FROM feedback_moderation m
        JOIN raw_feedback f ON f.platform=m.platform AND f.account=m.account
             AND f.kind=m.kind AND f.ext_id=m.ext_id
        WHERE m.id=%s""", (mod_id,))
    return r[0] if r else None


def _fr(m):
    """Строка raw_feedback для post_answer (нужен payload/item_id)."""
    r = db.query("""SELECT platform,account,kind,ext_id,item_id,payload FROM raw_feedback
        WHERE platform=%s AND account=%s AND kind=%s AND ext_id=%s""",
        (m["platform"], m["account"], m["kind"], m["ext_id"]))
    return r[0] if r else None


def _set(mod_id, state, **f):
    cols = ["state=%s"]
    vals = [state]
    for k, v in f.items():
        if v == "now()":                       # сентинел: серверное время, не литерал
            cols.append(f"{k}=now()")
        else:
            cols.append(f"{k}=%s")
            vals.append(v)
    vals.append(mod_id)
    db.execute(f"UPDATE feedback_moderation SET {', '.join(cols)} WHERE id=%s", tuple(vals))


def _kb(mod_id, allow_send=True):
    # Для route=human (домен-фильтр / ошибка парсинга) кнопки ✅ НЕТ — черновик это маркер, не ответ;
    # оператор отвечает только через ✏️ Править.
    top = ([{"text": "✅ Отправить", "callback_data": f"snd:{mod_id}"},
            {"text": "✏️ Править", "callback_data": f"edt:{mod_id}"}]
           if allow_send else
           [{"text": "✏️ Ответить вручную", "callback_data": f"edt:{mod_id}"}])
    return {"inline_keyboard": [top,
        [{"text": "🕒 Позже", "callback_data": f"lat:{mod_id}"},
         {"text": "🚫 Пропустить", "callback_data": f"skp:{mod_id}"}]]}


def _published_today_line(account):
    """«📤 Опубликовано сегодня: N (этот канал) / M всего» — календарные сутки МОСКВЫ, только успехи."""
    by = fs.sent_today_by_channel()
    total = sum(by.values())
    mine = sum(n for (_p, a), n in by.items() if a == account) if account else 0
    if account:
        return f"📤 Опубликовано сегодня: <b>{mine}</b> ({html.escape(str(account))}) / {total} всего\n"
    return f"📤 Опубликовано сегодня: <b>{total}</b>\n"


def _mode_banner(account):
    """ЧЕСТНЫЙ статус режима ДЛЯ ЭТОЙ карточки: что реально произойдёт по кнопке ✅.

    Раньше баннер смотрел только на глобальный fs._live() и врал в обе стороны: показывал «DRY-RUN»
    на живом канале (бот держал устаревший снимок .env — инцидент 27.07) и НЕ показывал dry-run, когда
    аккаунт вне FEEDBACK_LIVE_ACCOUNTS. Проверяем те же гейты, что и сам post_answer().

    Дневного лимита здесь БОЛЬШЕ НЕТ и в баннере он не упоминается (решение Сергея 28.07): лимит
    действует только на авто-ответы по бэклогу отзывов, а всё, что одобрил оператор кнопкой ✅ или
    прислал руками, уходит сразу в ближайшем цикле. Показывать «⏸ лимит дня» на такой карточке было бы
    прямой ложью. Второй строкой — ФАКТ публикаций за сегодня (Москва) по этому каналу и всего."""
    fact = _published_today_line(account)
    if not fs._live():
        return "🧪 <b>DRY-RUN</b> (реальной отправки нет: FEEDBACK_LIVE_SEND выключен)\n" + fact
    if account and account not in fs._live_accounts():
        return f"🧪 <b>DRY-RUN</b> (канал {html.escape(str(account))} вне списка живых)\n" + fact
    return "🔴 <b>БОЕВОЙ РЕЖИМ</b> — по ✅ ответ уйдёт покупателю сразу (без дневного лимита)\n" + fact


# ИДЕНТИФИКАЦИЯ ТОВАРА В КАРТОЧКЕ (правило Сергея 12.08.2026). Первая строка блока о товаре —
# НАШ внутренний артикул (external_code МойСклада): по нему владелец сразу узнаёт товар, тогда как
# площадочный номер (nmID/SKU/offerId) ему ни о чём не говорит. Дальше — площадочный артикул и имя.
_PLAT_ART_LABEL = {"wb": "артикул ВБ", "ozon": "Ozon SKU", "yandex": "offerId Яндекса"}
_PLAT_SUFFIX_RX = re.compile(r"^(\d{3,6})[A-Za-z0-9]{6,10}$")


def _internal_art(platform, article, item_id):
    """Наш внутренний артикул. WB vendorCode и offerId Яндекса — это он и есть, иногда с площадочным
    случайным хвостом ('00281LR4TANV' → '00281'); у Ozon в raw_feedback артикула нет вовсе, берём
    offer_id по sku. Срез хвоста неоднозначен по длине — кандидаты сверяем с ms_product и выбираем
    самый длинный известный; не нашли — отдаём как есть, лучше приблизительный код, чем пусто."""
    raw = str(article or "").strip()
    if not raw and platform == "ozon" and item_id:
        r = db.query("SELECT offer_id FROM ozon_product WHERE sku::text=%s LIMIT 1", (str(item_id),))
        raw = str(r[0]["offer_id"]).strip() if r else ""
    if not raw:
        return None
    cands = [raw]
    m = _PLAT_SUFFIX_RX.match(raw)
    if m:
        d = m.group(1)
        cands += [d[:k] for k in range(len(d), 2, -1)]
    try:
        known = {x["external_code"] for x in
                 db.query("SELECT DISTINCT external_code FROM ms_product WHERE external_code = ANY(%s)",
                          (cands,))}
    except Exception:
        known = set()
    for c in cands:
        if c in known:
            return c
    return cands[1] if len(cands) > 1 else raw


def _product_block(row):
    """Блок о товаре: внутренний артикул → площадочный → название."""
    e = html.escape
    ours = _internal_art(row.get("platform"), row.get("article"), row.get("item_id"))
    plat = str(row.get("item_id") or "—")
    label = _PLAT_ART_LABEL.get(row.get("platform"), "артикул площадки")
    head = f"🏷 <b>Наш артикул: {e(ours)}</b>" if ours else "🏷 <b>Наш артикул: не сшит</b>"
    return f"{head} · {label} {e(plat)}\n📦 {e(row.get('product_name') or '')[:90]}"


def _card(row):
    e = html.escape
    note = ""
    g = row.get("draft_grounding") or {}
    if isinstance(g, dict):
        src = g.get("source") or ("веб" if g.get("web") else "")
        if src or g.get("note"):
            note = f"\n<i>источник: {e(src or '—')}{'; ' + e((g.get('note') or ''))[:120] if g.get('note') else ''}</i>"
    banner = _mode_banner(row.get("account"))
    if row.get("draft_route") == "human":          # домен-фильтр / ошибка парсинга — только вручную
        banner += "⚠️ <b>НА ЧЕЛОВЕКА</b> — авто-ответа нет, ответьте через «✏️ Ответить вручную»\n"
    elif isinstance(g, dict) and g.get("no_card"):  # профильный товар, но карточка пустая
        banner += "🔍 <b>БЕЗ ДАННЫХ КАРТОЧКИ</b> — ответ собран по каталогу/вебу, проверьте внимательнее\n"
    dt = row.get("created_at")
    ds = dt.strftime("%d.%m.%Y") if dt else "—"
    if row.get("kind") == "review":
        head = f"⭐ <b>Отзыв {e(str(row.get('rating') or ''))}★</b>"
        txt = " · ".join(x for x in [(row.get('body') or '').strip(),
                                     (row.get('pros') or '').strip(),
                                     (row.get('cons') or '').strip()] if x) or "(без текста)"
    else:
        head = "❓ <b>Вопрос</b>"
        txt = (row.get('body') or '').strip()
    return (f"{banner}{head} · {e(row['platform'])} · 📅 {ds}\n"
            f"{_product_block(row)}\n\n"
            f"<b>Покупатель:</b> {e(txt)[:600]}\n\n"
            f"<b>Наш ответ:</b>\n{e((row.get('draft_text') or '').strip())[:1500]}{note}")


def flush_deferred(limit=20):
    """ХВОСТ СТАРОЙ СХЕМЫ: дослать одобренные ответы, застрявшие в state='deferred'.

    С 28.07.2026 дневной лимит применяется ТОЛЬКО к авто-ответам на старый бэклог отзывов, решения
    оператора им не режутся — новые карточки в 'deferred' не попадают. Функция осталась как слив
    остатка (её ещё зовёт цикл, шаг 3b): что было отложено при старой логике, уходит без лимита.
    Пустой 'deferred' = no-op. Возвращает число реально ушедших."""
    rows = db.query("""SELECT m.id, m.final_text, m.tg_chat_id, m.tg_msg_id,
        f.platform, f.account, f.kind, f.ext_id, f.item_id, f.payload, f.body
        FROM feedback_moderation m
        JOIN raw_feedback f ON f.platform=m.platform AND f.account=m.account
             AND f.kind=m.kind AND f.ext_id=m.ext_id
        WHERE m.state='deferred' AND m.final_text IS NOT NULL
        ORDER BY m.decided_at LIMIT %s""", (limit,))
    sent = 0
    for r in rows:
        ok, detail = fs.post_answer(dict(r), r["final_text"])   # без apply_cap — лимит тут не при чём
        if ok:
            _set(r["id"], "sent", error=None)
            sent += 1
            log(f"deferred → отправлено mod={r['id']} {r['platform']} {r['ext_id']} ({detail})")
            if r["tg_chat_id"] and r["tg_msg_id"]:  # закрываем ту же карточку в TG, чтобы не гадать
                edit_text(r["tg_chat_id"], r["tg_msg_id"],
                          "✅ Отправлено (отложенное с прежней схемы лимита)\n\n"
                          f"<b>Вопрос:</b> {html.escape((r.get('body') or '')[:300])}\n"
                          f"<b>Ответ:</b> {html.escape((r['final_text'] or '')[:800])}")
        else:
            _set(r["id"], "failed", error=detail)
            log(f"deferred → ОШИБКА mod={r['id']} {r['platform']} {r['ext_id']}: {detail[:150]}")
        time.sleep(1.2)                            # лимитер площадок (WB — 1 rps на категорию)
    return sent


def send_batch(limit=5, days=None, kind=None):
    """Разослать ПОРЦИЮ карточек за окно `days` (по кнопке). Возвращает число реально отправленных."""
    sent = 0
    for row in _pending(limit, days, kind):
        card, kb = _card(row), _kb(row["id"], allow_send=(row.get("draft_route") != "human"))
        canon = None                              # первый успешный (chat_id,msg_id) — канонический для правок
        for cid in NOTIFY_IDS:
            mid = send(cid, card, reply_markup=kb)
            if mid and canon is None:
                canon = (cid, mid)
        if canon:
            _set(row["id"], "carded", tg_chat_id=int(canon[0]), tg_msg_id=canon[1], carded_at="now()")
            log(f"card sent mod={row['id']} {row['platform']} {row['kind']} q={row['ext_id']} msg={canon[1]}")
            sent += 1
    return sent


def _dashboard():
    """Текст сводки + клавиатура. Всё в ОКНЕ последних WINDOW_DAYS дней: неотвечено на площадках
    (содержательное) + сколько таких карточек ждёт показа в очереди."""
    e = html.escape
    rf = db.query("""SELECT platform, account, kind,
        count(*) FILTER (WHERE COALESCE(is_answered,false)=false) un,
        count(*) FILTER (WHERE COALESCE(is_answered,false)=false AND COALESCE(body,pros,cons,'')<>'') un_txt
        FROM raw_feedback
        WHERE account IN ('wb_acc1','wb_acc2','oz_acc1','oz_acc2','ya_acc1')
          AND created_at >= now() - make_interval(days => %s)
        GROUP BY 1,2,3""", (WINDOW_DAYS,))
    agg = {}
    for r in rf:
        a = agg.setdefault((r["platform"], r["account"]), {"question": 0, "review": 0, "review_txt": 0})
        if r["kind"] == "question":
            a["question"] = r["un"]
        elif r["kind"] == "review":
            a["review"] = r["un"]; a["review_txt"] = r["un_txt"]
    if fs._live():
        _la = sorted(fs._live_accounts())
        mode = "🔴 БОЕВОЙ: " + (", ".join(_la) if _la else "нет живых каналов")
    else:
        mode = "🧪 DRY-RUN"
    lines = [f"📊 <b>Сводка за {WINDOW_DAYS} дней</b> · режим {e(mode)}", "",
             "<b>Неотвечено на площадках:</b>"]
    for (plat, acc), a in sorted(agg.items()):
        lines.append(f"• {e(plat)} ({e(acc)}): вопросов <b>{a['question']}</b> · "
                     f"отзывов с текстом <b>{a['review_txt']}</b> (всего отзывов {a['review']})")
    # ФАКТ публикаций за сегодня (Москва) по каналам — квота показывает остаток, а не то, сколько
    # ответов реально увидели покупатели. Для бэклога рядом — расход дневного лимита канала.
    pub = fs.sent_today_by_channel()
    lines += ["", f"<b>Опубликовано сегодня (МСК): {sum(pub.values())}</b>"]
    cap = fs._backlog_cap()
    for (p, a), n in sorted(pub.items()):
        lines.append(f"• {e(p)} ({e(a)}): <b>{n}</b> · бэклог {fs.backlog_sent_today(p, a)}/{cap}")
    if not pub:
        lines.append("• пока ничего")
    # сколько СОДЕРЖАТЕЛЬНЫХ карточек ждёт показа в окне
    ready = db.query("""SELECT count(*) c FROM feedback_moderation m
        JOIN raw_feedback f ON f.platform=m.platform AND f.account=m.account
             AND f.kind=m.kind AND f.ext_id=m.ext_id
        WHERE ((m.state='queued' AND m.tg_msg_id IS NULL) OR (m.state='snoozed' AND m.snooze_until<=now()))
          AND f.created_at >= now() - make_interval(days => %s)""", (WINDOW_DAYS,))[0]["c"]
    st = {r["state"]: r["n"] for r in db.query(
        "SELECT state, count(*) n FROM feedback_moderation GROUP BY state")}
    lines += ["", f"<b>Очередь модерации (за {WINDOW_DAYS} дней):</b>",
              f"• ждут показа: <b>{ready}</b>",
              f"• уже показано: {st.get('carded', 0)} · отправлено: {st.get('sent', 0)} · "
              f"пропущено: {st.get('skipped', 0)} · отложено: {st.get('snoozed', 0)}"
              # 'deferred' — наследие старой схемы (лимит резал и ручные ответы). Новые карточки в
              # него не попадают, показываем строку только пока хвост не дошлётся flush_deferred.
              + (f" · хвост старого лимита: {st['deferred']}" if st.get("deferred") else ""),
              "", f"«Показать всё» пришлёт все {ready} карточек за {WINDOW_DAYS} дней (по одной, с датой)."]
    kb = {"inline_keyboard": [[
        {"text": f"📥 Показать всё за {WINDOW_DAYS} дн.", "callback_data": "more:all"}],
        [{"text": "📥 5", "callback_data": "more:5"},
         {"text": "📥 10", "callback_data": "more:10"}]]}
    return "\n".join(lines), kb


# Служебные маркеры карточки модерации. Оператор правит ответ, копируя карточку целиком, и
# служебная шапка уезжает покупателю (инцидент 31.07: на вопрос Ozon 019fb444 опубликовался весь
# текст карточки — «🔴 БОЕВОЙ РЕЖИМ», счётчик «Опубликовано сегодня», «Покупатель:», «Наш ответ:»).
_CARD_MARK_RX = re.compile(
    r"^\s*(?:[\U0001F300-\U0001FAFF☀-➿️]+\s*)?"
    r"(?:БОЕВОЙ РЕЖИМ|DRY-RUN|Опубликовано сегодня|Вопрос\s*·|Отзыв\s*·|Покупатель:|Наш ответ:)",
    re.IGNORECASE)
_ANSWER_MARK_RX = re.compile(r"^\s*(?:[\U0001F300-\U0001FAFF☀-➿️]+\s*)?Наш ответ:\s*",
                             re.IGNORECASE)


def clean_operator_text(text):
    """Вырезать служебную разметку карточки из текста, присланного оператором.

    Возвращает (clean, None) либо (None, причина-переспроса). Три случая:
      * маркеров нет — текст и есть ответ, отдаём как прислали (обычная правка);
      * есть «Наш ответ:» — ответ это всё, что ПОСЛЕ последнего такого маркера;
      * маркеры есть, а «Наш ответ:» нет — где именно ответ, неизвестно; НЕ угадываем и НЕ шлём.
    Отправлять «что осталось после вырезания» вслепую опаснее, чем переспросить: покупателю
    уходит живой текст, отозвать его на площадке нельзя."""
    raw = (text or "").strip()
    if not raw:
        return None, "пустой текст"
    lines = raw.splitlines()
    if not any(_CARD_MARK_RX.match(ln) for ln in lines):
        return raw, None
    idx = [i for i, ln in enumerate(lines) if _ANSWER_MARK_RX.match(ln)]
    if not idx:
        return None, "в тексте служебная разметка карточки, но строки «Наш ответ:» нет"
    i = idx[-1]
    body = [_ANSWER_MARK_RX.sub("", lines[i])] + lines[i + 1:]
    body = [ln for ln in body if not _CARD_MARK_RX.match(ln)]
    clean = "\n".join(body).strip()
    if len(clean) < 15:
        return None, "после вырезания служебных строк ответа не осталось"
    return clean, None


def _do_send(mod_id, from_id, text, chat_id, message_id):
    """Общий путь отправки (кнопка ✅ или присланный правленый текст)."""
    m = _mod(mod_id)
    if not m:
        return "запись не найдена"
    if m["state"] in ("sent", "skipped"):
        return f"уже {m['state']}"
    fr = _fr(m)
    if not fr:
        _set(mod_id, "failed", error="raw_feedback не найден", decided_at="now()", decided_by=int(from_id))
        return "raw_feedback не найден"
    # правим ту карточку, по кнопке которой пришло решение; для потока правки (message_id=None) —
    # каноническую сохранённую карточку.
    ec = chat_id if message_id else (m["tg_chat_id"] or chat_id)
    em = message_id or m["tg_msg_id"]
    # apply_cap НЕ передаём: решение оператора дневным лимитом не режется (лимит — только для
    # авто-ответов на старый бэклог отзывов, см. collectors/feedback_send.py). Поэтому ветки
    # 'deferred' здесь больше нет: одобренное уходит в этом же вызове либо честно падает в 'failed'.
    ok, detail = fs.post_answer(fr, text)
    if ok:
        _set(mod_id, "sent", final_text=text, error=None,
             decided_at="now()", decided_by=int(from_id))
        tail = "🧪 (dry-run) ушло бы" if detail.startswith("dry-run") else "✅ Отправлено"
        edit_text(ec, em,
                  f"{tail}\n\n<b>Вопрос:</b> {html.escape((m.get('body') or '')[:300])}\n"
                  f"<b>Ответ:</b> {html.escape(text[:800])}")
        return tail
    # final_text сохраняем и при провале: иначе правленый оператором текст теряется и досыл после
    # починки причины воспроизвести его уже не может (инцидент 03.08, вопрос ЯМ 28227084).
    _set(mod_id, "failed", final_text=text, error=detail, decided_at="now()", decided_by=int(from_id))
    edit_text(ec, em, f"❌ Ошибка отправки: {html.escape(detail[:300])}")
    return f"ошибка: {detail[:120]}"


# ---------- обработка апдейтов ----------

def handle_callback(cb):
    from_id = str(cb.get("from", {}).get("id", ""))
    data = cb.get("data", "")
    msg = cb.get("message", {})
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    if from_id not in ALLOWED:
        answer_cb(cb["id"], "Нет доступа")
        return
    action, _, sid = data.partition(":")
    if action == "more":                          # «Показать N/всё» — подтянуть карточки за окно
        n = BATCH_CAP if sid == "all" else (int(sid) if sid.isdigit() else 5)
        cnt = send_batch(n)
        answer_cb(cb["id"], f"Отправлено: {cnt}" if cnt else "За окно нет новых карточек")
        return
    try:
        mod_id = int(sid)
    except Exception:
        answer_cb(cb["id"], "?")
        return

    if action == "snd":
        m = _mod(mod_id)
        if not m:
            answer_cb(cb["id"], "нет записи"); return
        res = _do_send(mod_id, from_id, (m.get("draft_text") or ""), chat_id, message_id)
        answer_cb(cb["id"], res)
    elif action == "edt":
        PENDING_EDIT[from_id] = mod_id
        answer_cb(cb["id"], "Пришли исправленный текст ответа")
        send(chat_id, "✏️ Пришли исправленный текст ответа одним сообщением — отправлю его.")
    elif action == "lat":
        m = _mod(mod_id)
        if m and m["state"] in ("sent", "skipped", "failed"):
            answer_cb(cb["id"], f"уже {m['state']}"); return
        # tg_msg_id=NULL, чтобы при пробуждении ушла новая карточка; старую гасим (убираем кнопки)
        db.execute("""UPDATE feedback_moderation
            SET state='snoozed', snooze_until=now()+interval '5 hours', tg_msg_id=NULL WHERE id=%s""",
            (mod_id,))
        edit_text(chat_id, message_id, "🕒 Отложено на 5 часов — напомню позже.")
        answer_cb(cb["id"], "Отложено на 5 часов")
    elif action == "skp":
        m = _mod(mod_id)
        if m and m["state"] in ("sent", "skipped", "failed"):
            answer_cb(cb["id"], f"уже {m['state']}"); return
        _set(mod_id, "skipped", decided_at="now()", decided_by=int(from_id))
        edit_text(chat_id, message_id, "🚫 Пропущено")
        answer_cb(cb["id"], "Пропущено")
    else:
        answer_cb(cb["id"], "?")


def handle_message(msg):
    from_id = str(msg.get("from", {}).get("id", ""))
    chat_id = msg["chat"]["id"]
    if from_id not in ALLOWED:
        send(chat_id, f"⛔ Нет доступа. Твой Telegram ID: {from_id}\n"
                      f"Добавь его в TG_ALLOWED_IDS в /opt/mp-analytics/.env.")
        return
    text = (msg.get("text") or "").strip()
    mod_id = PENDING_EDIT.pop(from_id, None)
    if mod_id is not None:
        if not text:
            send(chat_id, "Пустой текст — правка отменена.")
            return
        clean, why = clean_operator_text(text)
        if clean is None:
            PENDING_EDIT[from_id] = mod_id          # правка НЕ отменена — ждём текст ещё раз
            send(chat_id, f"⚠️ Не отправил: {why}.\n"
                          f"Пришли, пожалуйста, только сам текст ответа покупателю — "
                          f"без шапки карточки, счётчиков и строк «Покупатель:» / «Наш ответ:».")
            return
        if clean != text:
            log(f"правка mod={mod_id}: вырезана служебная разметка карточки "
                f"({len(text)} → {len(clean)} симв.)")
        res = _do_send(mod_id, from_id, clean, chat_id, None)
        send(chat_id, f"Правка: {res}")
        return
    if text == "/next":
        cnt = send_batch(5)
        send(chat_id, f"Отправлено карточек: {cnt}." if cnt else "За окно нет новых карточек.")
        return
    if text == "/all":
        cnt = send_batch(BATCH_CAP)
        send(chat_id, f"Отправлено карточек: {cnt}." if cnt else "За окно нет новых карточек.")
        return
    if text.startswith("/"):                       # /menu, /start, /stats и прочее — показать сводку
        t, kb = _dashboard()
        send(chat_id, t, reply_markup=kb)


def main():
    if not TOKEN:
        raise SystemExit("Нет TG_FEEDBACK_BOT_TOKEN/TG_BOT_TOKEN в /opt/mp-analytics/.env")
    if not NOTIFY:
        raise SystemExit("Нет TG_NOTIFY_ID в .env — некуда слать карточки")
    me = api("getMe")["result"]
    if not _DEDICATED:
        log("⚠️  TG_FEEDBACK_BOT_TOKEN не задан — использую TG_BOT_TOKEN (конфликт с invoice-bot! "
            "останови invoice-bot или заведи отдельного бота).")
    try:                                           # кнопка-меню в клиенте Telegram
        api("setMyCommands", {"commands": [
            {"command": "menu", "description": "Сводка: неотвечено и очередь"},
            {"command": "next", "description": "Показать 5 следующих карточек"}]})
    except Exception as e:
        log(f"setMyCommands: {e}")
    # ревизию пишем в лог осознанно: процесс держит модули в памяти с момента старта, и после
    # `git pull` без restart бот молча исполняет СТАРЫЙ код (инцидент 03.08: фикс parentEntityId
    # лежал на диске с 30.07, а бот работал с 28.07 и продолжал ловить 400). Теперь версию,
    # которая реально выполняется, видно в journalctl без раскопок по mtime и ps.
    try:
        rev = subprocess.run(["git", "-C", os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=5).stdout.strip() or "?"
    except Exception:
        rev = "?"
    log(f"bot @{me.get('username')} запущен. rev={rev} live={fs._live()} "
        f"allowed={sorted(ALLOWED) or 'ПУСТО'} "
        f"notify={NOTIFY} · карточки этот бот шлёт только по кнопке; авто-порции — отдельный цикл "
        f"feedback_cycle.py (send_batch по таймеру)")
    offset = None
    while True:
        try:
            params = {"timeout": 20, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            upd = api("getUpdates", params, timeout=30)
            res = upd.get("result", [])
            for u in res:
                offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        handle_callback(u["callback_query"])
                    elif "message" in u:
                        handle_message(u["message"])
                except Exception:
                    log("update error: " + traceback.format_exc())
        except urllib.error.HTTPError as e:
            log(f"HTTP {e.code} на getUpdates; пауза 5с"); time.sleep(5)
        except Exception as e:
            log(f"loop error: {e}; пауза 5с"); time.sleep(5)


if __name__ == "__main__":
    main()
