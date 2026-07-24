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
import html
import urllib.request
import urllib.error
import traceback

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


def _pending(limit=5, days=None):
    """Карточки, готовые к показу: свежие 'queued' + проснувшиеся 'snoozed', ТОЛЬКО за последние
    `days` дней (по дате отзыва/вопроса). Отдаём порцией (limit) — рассылка только по кнопке."""
    days = WINDOW_DAYS if days is None else days
    return db.query("""SELECT m.id, m.platform, m.account, m.kind, m.ext_id,
        f.product_name, f.body, f.pros, f.cons, f.rating, f.created_at, f.draft_text, f.draft_grounding
        FROM feedback_moderation m
        JOIN raw_feedback f ON f.platform=m.platform AND f.account=m.account
             AND f.kind=m.kind AND f.ext_id=m.ext_id
        WHERE ((m.state='queued' AND m.tg_msg_id IS NULL)
               OR (m.state='snoozed' AND m.snooze_until <= now()))
          AND f.created_at >= now() - make_interval(days => %s)
        ORDER BY f.created_at DESC LIMIT %s""", (days, limit))


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


def _kb(mod_id):
    return {"inline_keyboard": [
        [{"text": "✅ Отправить", "callback_data": f"snd:{mod_id}"},
         {"text": "✏️ Править", "callback_data": f"edt:{mod_id}"}],
        [{"text": "🕒 Позже", "callback_data": f"lat:{mod_id}"},
         {"text": "🚫 Пропустить", "callback_data": f"skp:{mod_id}"}],
    ]}


def _card(row):
    e = html.escape
    note = ""
    g = row.get("draft_grounding") or {}
    if isinstance(g, dict):
        src = g.get("source") or ("веб" if g.get("web") else "")
        if src or g.get("note"):
            note = f"\n<i>источник: {e(src or '—')}{'; ' + e((g.get('note') or ''))[:120] if g.get('note') else ''}</i>"
    banner = "" if fs._live() else "🧪 <b>DRY-RUN</b> (реальной отправки нет)\n"
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
    return (f"{banner}{head} · {e(row['platform'])} · 📅 {ds} · {e(row.get('product_name') or '')[:70]}\n\n"
            f"<b>Покупатель:</b> {e(txt)[:600]}\n\n"
            f"<b>Наш ответ:</b>\n{e((row.get('draft_text') or '').strip())[:1500]}{note}")


def send_batch(limit=5, days=None):
    """Разослать ПОРЦИЮ карточек за окно `days` (по кнопке). Возвращает число реально отправленных."""
    sent = 0
    for row in _pending(limit, days):
        card, kb = _card(row), _kb(row["id"])
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
    lines = [f"📊 <b>Сводка за {WINDOW_DAYS} дней</b> · режим " + ("LIVE" if fs._live() else "DRY-RUN"), "",
             "<b>Неотвечено на площадках:</b>"]
    for (plat, acc), a in sorted(agg.items()):
        lines.append(f"• {e(plat)} ({e(acc)}): вопросов <b>{a['question']}</b> · "
                     f"отзывов с текстом <b>{a['review_txt']}</b> (всего отзывов {a['review']})")
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
              f"пропущено: {st.get('skipped', 0)} · отложено: {st.get('snoozed', 0)}",
              "", f"«Показать всё» пришлёт все {ready} карточек за {WINDOW_DAYS} дней (по одной, с датой)."]
    kb = {"inline_keyboard": [[
        {"text": f"📥 Показать всё за {WINDOW_DAYS} дн.", "callback_data": "more:all"}],
        [{"text": "📥 5", "callback_data": "more:5"},
         {"text": "📥 10", "callback_data": "more:10"}]]}
    return "\n".join(lines), kb


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
    ok, detail = fs.post_answer(fr, text)
    if ok:
        _set(mod_id, "sent", final_text=text, error=None, decided_at="now()", decided_by=int(from_id))
        tail = "🧪 (dry-run) ушло бы" if detail == "dry-run" else "✅ Отправлено"
        edit_text(ec, em,
                  f"{tail}\n\n<b>Вопрос:</b> {html.escape((m.get('body') or '')[:300])}\n"
                  f"<b>Ответ:</b> {html.escape(text[:800])}")
        return tail
    _set(mod_id, "failed", error=detail, decided_at="now()", decided_by=int(from_id))
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
        res = _do_send(mod_id, from_id, text, chat_id, None)
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
    log(f"bot @{me.get('username')} запущен. live={fs._live()} allowed={sorted(ALLOWED) or 'ПУСТО'} "
        f"notify={NOTIFY} · доставка карточек ТОЛЬКО по кнопке (авто-рассылки нет)")
    offset = None
    while True:
        try:
            params = {"timeout": 20, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            upd = api("getUpdates", params, timeout=30)
            res = upd.get("result", [])
            if res:
                log(f"получено апдейтов: {len(res)}")
            for u in res:
                offset = u["update_id"] + 1
                if "message" in u:
                    log(f"  msg от {u['message'].get('from',{}).get('id')}: {(u['message'].get('text') or '')[:40]!r}")
                elif "callback_query" in u:
                    log(f"  cb от {u['callback_query'].get('from',{}).get('id')}: {u['callback_query'].get('data')!r}")
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
