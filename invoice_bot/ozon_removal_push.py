# -*- coding: utf-8 -*-
"""ozon_removal_push.py — еженедельная авторассылка кандидатов на вывоз со склада Ozon FBO.

Пересобирает список (DB-only, по данным свежего run_daily) и шлёт всем из TG_ALLOWED_IDS.
Запуск из cron (еженедельно). Переиспользует send_long/ozon_removal_report/ALLOWED из tg_bot.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tg_bot  # noqa: E402  (грузит .env, TOKEN, ALLOWED)


def main():
    if not tg_bot.ALLOWED:
        print("нет TG_ALLOWED_IDS — некому слать", flush=True)
        return
    rep = tg_bot.ozon_removal_report()
    header = "🗓️ Еженедельный список на вывоз со склада Ozon\n\n"
    for uid in tg_bot.ALLOWED:
        try:
            tg_bot.send_long(int(uid), header + rep)
        except Exception as e:
            print(f"send {uid} error: {e}", flush=True)
    print(f"разослано {len(tg_bot.ALLOWED)} пользователям", flush=True)


if __name__ == "__main__":
    main()
