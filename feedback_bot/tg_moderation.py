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
ALLOWED = {x.strip() for x in os.getenv("TG_ALLOWED_IDS", "").split(",") if x.strip()}
NOTIFY = (os.getenv("TG_NOTIFY_ID") or "").strip()
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

def _pending():
    """'queued' вопросы, ещё без карточки — с текстом вопроса и предложенным ответом."""
    return db.query("""SELECT m.id, m.platform, m.account, m.ext_id,
        f.product_name, f.body, f.draft_text, f.draft_grounding
        FROM feedback_moderation m
        JOIN raw_feedback f ON f.platform=m.platform AND f.account=m.account
             AND f.kind=m.kind AND f.ext_id=m.ext_id
        WHERE m.state='queued' AND m.tg_msg_id IS NULL
        ORDER BY m.enqueued_at LIMIT 20""")


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
    return {"inline_keyboard": [[
        {"text": "✅ Отправить", "callback_data": f"snd:{mod_id}"},
        {"text": "✏️ Править", "callback_data": f"edt:{mod_id}"},
        {"text": "🚫 Пропустить", "callback_data": f"skp:{mod_id}"},
    ]]}


def _card(row):
    e = html.escape
    note = ""
    g = row.get("draft_grounding") or {}
    if isinstance(g, dict):
        src = g.get("source") or ("веб" if g.get("web") else "")
        if src or g.get("note"):
            note = f"\n<i>источник: {e(src or '—')}{'; ' + e((g.get('note') or ''))[:120] if g.get('note') else ''}</i>"
    banner = "" if fs._live() else "🧪 <b>DRY-RUN</b> (реальной отправки нет)\n"
    return (f"{banner}❓ <b>Вопрос</b> · {e(row['platform'])} · {e(row.get('product_name') or '')[:70]}\n\n"
            f"<b>Покупатель:</b> {e((row.get('body') or '').strip())[:600]}\n\n"
            f"<b>Наш ответ:</b>\n{e((row.get('draft_text') or '').strip())[:1500]}{note}")


def post_cards():
    for row in _pending():
        mid = send(NOTIFY, _card(row), reply_markup=_kb(row["id"]))
        if mid:
            _set(row["id"], "carded", tg_chat_id=int(NOTIFY), tg_msg_id=mid, carded_at="now()")
            log(f"card sent mod={row['id']} {row['platform']} q={row['ext_id']} msg={mid}")


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
    ok, detail = fs.post_answer(fr, text)
    if ok:
        _set(mod_id, "sent", final_text=text, error=None, decided_at="now()", decided_by=int(from_id))
        tail = "🧪 (dry-run) ушло бы" if detail == "dry-run" else "✅ Отправлено"
        edit_text(m["tg_chat_id"] or chat_id, m["tg_msg_id"] or message_id,
                  f"{tail}\n\n<b>Вопрос:</b> {html.escape((m.get('body') or '')[:300])}\n"
                  f"<b>Ответ:</b> {html.escape(text[:800])}")
        return tail
    _set(mod_id, "failed", error=detail, decided_at="now()", decided_by=int(from_id))
    edit_text(m["tg_chat_id"] or chat_id, m["tg_msg_id"] or message_id,
              f"❌ Ошибка отправки: {html.escape(detail[:300])}")
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
    try:
        action, sid = data.split(":", 1)
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
    elif action == "skp":
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
    if text.startswith("/"):
        n = db.query("SELECT count(*) c FROM feedback_moderation WHERE state='queued'")[0]["c"]
        send(chat_id, f"Бот модерации ответов на вопросы. В очереди: {n}. "
                      f"Режим: {'LIVE' if fs._live() else 'DRY-RUN'}. "
                      f"Карточки приходят автоматически; жми кнопки под ними.")


def main():
    if not TOKEN:
        raise SystemExit("Нет TG_FEEDBACK_BOT_TOKEN/TG_BOT_TOKEN в /opt/mp-analytics/.env")
    if not NOTIFY:
        raise SystemExit("Нет TG_NOTIFY_ID в .env — некуда слать карточки")
    me = api("getMe")["result"]
    if not _DEDICATED:
        log("⚠️  TG_FEEDBACK_BOT_TOKEN не задан — использую TG_BOT_TOKEN (конфликт с invoice-bot! "
            "останови invoice-bot или заведи отдельного бота).")
    log(f"bot @{me.get('username')} запущен. live={fs._live()} allowed={sorted(ALLOWED) or 'ПУСТО'} "
        f"notify={NOTIFY} queue_poll={POLL_QUEUE_SEC}s")
    offset = None
    last_poll = 0.0
    while True:
        try:
            params = {"timeout": 20, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            upd = api("getUpdates", params, timeout=30)
            for u in upd.get("result", []):
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
        # периодически выкладываем карточки по новым 'queued'
        if time.time() - last_poll >= POLL_QUEUE_SEC:
            last_poll = time.time()
            try:
                post_cards()
            except Exception:
                log("post_cards error: " + traceback.format_exc())


if __name__ == "__main__":
    main()
