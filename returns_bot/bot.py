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
    "📦 <b>Бот возвратов FBS</b>\n\n"
    "/vozvraty — что лежит и ждёт забора\n"
    "/pvz — точки: куда ехать и сколько там\n"
    "/shtrihkod — штрихкод получения возвратов Ozon\n"
    "/obnovit — обновить данные из кабинетов\n"
)


def handle(chat_id, text):
    cmd = (text or "").strip().split()[0].lower().split("@")[0]

    if cmd in ("/start", "/help"):
        tg.send(chat_id, HELP)
    elif cmd in ("/vozvraty", "/zabrat"):
        tg.send(chat_id, render.summary())
    elif cmd == "/pvz":
        tg.send(chat_id, render.pvz_digest())
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
        rows, errors = collector.gather()
        if rows:
            st = collector.store(rows)
            tg.send(chat_id, f"Обновлено: возвратов {st['heads']}, позиций {st['items']}."
                             + ("\n⚠️ " + "\n⚠️ ".join(errors) if errors else ""))
        else:
            tg.send(chat_id, "Ничего не получено.\n" + "\n".join(errors))
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
