# поток: ret
"""Бот возвратов: long-polling, команды по запросу.

    /vozvraty   — что лежит и ждёт забора (стадии из pending.SHOW_STAGES)
    /pvz        — короткий список точек: куда ехать и сколько там
    /shtrihkod  — штрихкод получения возвратов Ozon картинкой
    /obnovit    — сходить в API площадок и пересобрать данные
    /help

Только чтение API площадок: бот ничего не согласовывает и не отправляет в кабинеты.
"""
import sys
import time
import traceback

from returns_bot import codes, collect as collector, render, tg
from returns_bot.sources import ozon

HELP = (
    "📦 <b>Бот возвратов</b>\n\n"
    "/vozvraty — что лежит и ждёт забора\n"
    "/pvz — точки: куда ехать и сколько там\n"
    "/shtrihkod — штрихкод получения возвратов Ozon\n"
    "/obnovit — обновить данные из кабинетов\n\n"
    "Под сводкой есть кнопка «Обновить» — то же самое, но не набирая команду."
)

# Кнопка под сводкой: сходить в кабинеты и перерисовать список. То же, что /obnovit + /vozvraty.
CB_REFRESH = "ret:refresh"
KB_REFRESH = {"inline_keyboard": [[{"text": "🔄 Обновить", "callback_data": CB_REFRESH}]]}

# Сбор идёт минутами и блокирует long-polling: пока он идёт, вторая нажатая кнопка должна
# получить отказ, а не встать в очередь и не сходить в API площадок второй раз.
_busy = False


def send_summaries(chat_id):
    """Сводка по юрлицам; кнопка «Обновить» — на последнем сообщении."""
    letters = render.summaries() or [(None, render.summary())]
    for n, (_, text) in enumerate(letters):
        tg.send(chat_id, text, reply_markup=KB_REFRESH if n == len(letters) - 1 else None)


def refresh(chat_id):
    """Сходить в API площадок за свежим окном, пересчитать и показать сводку.

    Быстрый сбор: у WB одно окно вместо трёх, у вывоза FBO одно вместо двух. Старую закрытую
    историю (это 3900 из 4000 строк) кнопка не перечитывает — её освежает ночной полный прогон.
    """
    global _busy
    _busy = True
    try:
        before = collector.pickup_keys()
        rows, errors = collector.gather(quick=True)
        if rows:
            # mark_gone=False обязателен: при узком окне «нет в выдаче» ≠ «пропал»
            collector.store(rows, mark_gone=False)
            got = collector.received_since(before)
            note = (f"🔄 Обновлено. Получено нами с прошлого обновления: <b>{len(got)}</b>."
                    if got else "🔄 Обновлено. С прошлого обновления ничего не получили.")
        else:
            note = "🔄 Площадки ничего не отдали, данные прежние."
        if errors:
            note += "\n⚠️ " + "\n⚠️ ".join(str(e)[:200] for e in errors)
        tg.send(chat_id, note)
        send_summaries(chat_id)
    finally:
        _busy = False


def handle_callback(chat_id, cq):
    """Нажатие кнопки. «Часики» гасим сразу: Telegram ждёт ответ ~10 с, а сбор идёт минутами."""
    if cq.get("data") != CB_REFRESH:
        tg.answer_callback(cq["id"])
        return
    if _busy:
        tg.answer_callback(cq["id"], "Уже обновляю, подожди")
        return
    tg.answer_callback(cq["id"], "Иду в кабинеты, это займёт пару минут")
    refresh(chat_id)


def handle(chat_id, text):
    cmd = (text or "").strip().split()[0].lower().split("@")[0]

    if cmd in ("/start", "/help"):
        tg.send(chat_id, HELP)
    elif cmd in ("/vozvraty", "/zabrat"):
        send_summaries(chat_id)
    elif cmd == "/pvz":
        tg.send(chat_id, render.pvz_digest(), reply_markup=KB_REFRESH)
    elif cmd == "/shtrihkod":
        sent = 0
        for account in ozon.CRED_ENV:
            try:
                value, png_b64 = ozon.giveout_barcode(account)
            except Exception as e:
                tg.send(chat_id, f"Ozon {account}: {type(e).__name__} {str(e)[:150]}")
                continue
            png = codes.from_base64(png_b64) or codes.code128(value)
            if png:
                title = ozon.ACCOUNT_TITLE.get(account, account)
                tg.send_photo(chat_id, png, f"🔵 Штрихкод получения возвратов Ozon · {title}"
                                            + (f"\n<code>{value}</code>" if value else ""))
                sent += 1
        if not sent:
            tg.send(chat_id, "Площадки не отдали штрихкод.")
    elif cmd == "/obnovit":
        if _busy:
            tg.send(chat_id, "Уже обновляю, подожди.")
        else:
            refresh(chat_id)
    else:
        tg.send(chat_id, HELP)


def loop():
    allowed = tg.allowed_ids()
    offset = None
    print("бот возвратов запущен" + (f", доступ у {len(allowed)} чатов" if allowed else
                                     ", TG_RETURNS_ALLOWED_IDS пуст — отвечаем всем"))
    while True:
        try:
            for upd in tg.get_updates(offset):
                offset = upd["update_id"] + 1
                cq = upd.get("callback_query")
                if cq:
                    chat_id = str((cq.get("message") or {}).get("chat", {}).get("id", ""))
                    if allowed and chat_id not in allowed:
                        tg.answer_callback(cq["id"])
                        continue
                    try:
                        handle_callback(chat_id, cq)
                    except Exception:
                        traceback.print_exc()
                        tg.send(chat_id, "Обновление сломалось, смотрю журнал.")
                    continue
                msg = upd.get("message") or upd.get("edited_message")
                if not msg or "text" not in msg:
                    continue
                chat_id = str(msg["chat"]["id"])
                if allowed and chat_id not in allowed:
                    continue
                try:
                    handle(chat_id, msg["text"])
                except Exception:
                    traceback.print_exc()
                    tg.send(chat_id, "Сломалось на этой команде, смотрю журнал.")
        except KeyboardInterrupt:
            return 0
        except Exception:
            traceback.print_exc()
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(loop())
