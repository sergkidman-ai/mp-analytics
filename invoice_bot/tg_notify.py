# поток: inv
"""invoice_bot/tg_notify.py — доставка платёжных сводок в Telegram с повторами.

Один адресат на два движка (`payment_autosend` и `rent_core`): суммы и получатели идут в
ОТДЕЛЬНЫЙ платёжный бот (`TG_PAY_BOT_TOKEN` / `TG_PAY_NOTIFY_ID`), а не в общий канал
invoice-bot, который читают несколько человек.

Зачем повторы (09.09.2026): одиночный `urlopen` без ретрая терял сводку на любом сетевом
чихе — в `payment_autosend.log` пять `URLError` при живом канале и валидных токенах. Платежи
при этом уходили в банк, а человек об этом не узнавал: автоотправка работала вслепую.
Повторы — тот же приём, что в `returns_bot/tg.py`: 4 попытки, пауза 1-2-4 с, у 429 слушаем
`retry_after` самого Telegram.

Отправка НЕ роняет вызывающий код: не дошло — пишем в лог с причиной и идём дальше.
"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 20
TRIES = 4


def _post(api, uid, msg):
    """→ (ok, причина). Причина заполнена, только если не доставили."""
    data = urllib.parse.urlencode({"chat_id": uid, "text": msg}).encode()
    reason = "неизвестно"
    for n in range(TRIES):
        try:
            urllib.request.urlopen(api, data=data, timeout=TIMEOUT).read()
            return True, None
        except urllib.error.HTTPError as e:
            body = e.read(500).decode("utf-8", "replace")
            try:
                desc = (json.loads(body) or {}).get("description") or body
                wait = ((json.loads(body) or {}).get("parameters") or {}).get("retry_after")
            except ValueError:
                desc, wait = body, None
            reason = f"HTTP {e.code}: {desc[:150]}"
            # 4xx кроме 429 — наша ошибка (не тот chat_id, отозванный токен), повтор не поможет
            if e.code != 429 and e.code < 500:
                return False, reason
            time.sleep(min(wait or 2 ** n, 30))
        except Exception as e:                                   # noqa: BLE001
            reason = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            if n < TRIES - 1:
                time.sleep(2 ** n)
    return False, f"{reason} (после {TRIES} попыток)"


def tg(msg):
    """Сводка в платёжный бот. Бот не настроен → пишем в лог и выходим."""
    token = os.getenv("TG_PAY_BOT_TOKEN", "").strip()
    ids = [x.strip() for x in os.getenv("TG_PAY_NOTIFY_ID", "").split(",") if x.strip()]
    if not (token and ids):
        print("TG: платёжный бот не настроен (TG_PAY_BOT_TOKEN/TG_PAY_NOTIFY_ID) — сводка "
              "не отправлена", flush=True)
        return
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    for uid in ids:
        ok, reason = _post(api, uid, msg)
        if not ok:
            print(f"TG {uid}: НЕ ДОСТАВЛЕНО — {reason}", flush=True)
