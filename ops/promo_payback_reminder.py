#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# поток: ops
"""Разовое напоминание вернуться к окупаемости акций — когда накопится фактический флаг.

С 23.09.2026 prices/mp_price_promo.py пишет prc_mp_price_day (цена + участие в акции по дням,
три площадки, обе фирмы). К 18.11.2026 накопится ~8 недель — хватит, чтобы повторить расчёт
не по косвенному признаку (падение цены), а по факту. Постановка задачи и цифры ретро-оценки —
в docs/BRIEF_PRC.md, раздел «ОТКРЫТО: окупаемость акций».

Механика (как у ops/wb_token_reminder.py): cron гоняет ежедневно, до даты — тихий no-op;
на/после FIRE_ON шлёт одно сообщение в TG_ALLOWED_IDS, ставит маркер и САМ снимает свою
строку из crontab. Секреты не печатает. Время в crontab сервера = UTC.
"""
import os
import sys
import json
import datetime
import subprocess
import urllib.request
import urllib.parse
import pathlib

FIRE_ON = datetime.date(2026, 11, 18)         # ~8 недель сбора prc_mp_price_day
BASE = pathlib.Path("/opt/mp-analytics")
ENV = BASE / ".env"
MARKER = BASE / "ops" / ".promo_payback_reminder_sent"
CRON_TAG = "PROMO_PAYBACK_REMINDER"

MSG = (
    "📊 Вернуться к вопросу: *окупаются ли акции*\n\n"
    "Накопилось ~8 недель фактического флага акций (prc_mp_price_day пишется с 23.09.2026, "
    "три площадки, обе фирмы). Теперь это измерение, а не реконструкция по падению цены.\n\n"
    "Что сделать (подробно — docs/BRIEF_PRC.md, раздел «ОТКРЫТО: окупаемость акций»):\n"
    "1) Повторить расчёт на in_promo вместо падения цены.\n"
    "2) Считать отдельно недели со ставкой рекламы и без — в ретро реклама давала почти весь прирост.\n"
    "3) Проверить: окупается ли скидка только на ходовых карточках и только с рекламой.\n"
    "4) Решить по дешёвому терцилю ВБ — по ретро он в акциях убыточен (−11 % штук).\n\n"
    "Ретро июнь–сентябрь 2026 (оценка, не факт): ВБ −108,8 тыс ₽, Ozon −128,6 тыс ₽. "
    "На Ozon акция ценой не проявляется вовсе — там рычаг ставки, а не скидка."
)


def _load_env():
    d = {}
    with open(ENV) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            d[k.strip()] = v.strip()
    return d


def _self_remove_cron():
    """Удалить свою строку из crontab (по тегу), чтобы больше не срабатывать."""
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        if cur.returncode != 0:
            return
        # и комментарий (PROMO_PAYBACK_REMINDER), и путь скрипта ловятся
        # по подстроке "promo_payback_reminder"
        kept = [ln for ln in cur.stdout.splitlines()
                if "promo_payback_reminder" not in ln.lower()]
        subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True)
    except Exception as e:
        print(f"self-remove cron err: {type(e).__name__}: {e}", flush=True)


def main():
    if datetime.date.today() < FIRE_ON or MARKER.exists():
        return  # ещё рано или уже отправлено — тихий no-op
    env = _load_env()
    token = env.get("TG_BOT_TOKEN", "")
    allowed = [x.strip() for x in env.get("TG_ALLOWED_IDS", "").split(",") if x.strip()]
    if not token or not allowed:
        print("нет TG_BOT_TOKEN / TG_ALLOWED_IDS — пропуск", flush=True)
        return
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    ok_any = False
    for uid in allowed:
        data = urllib.parse.urlencode(
            {"chat_id": uid, "text": MSG, "parse_mode": "Markdown"}).encode()
        try:
            with urllib.request.urlopen(urllib.request.Request(api, data=data), timeout=30) as r:
                resp = json.load(r)
            ok_any = ok_any or bool(resp.get("ok"))
            print(f"uid …{uid[-4:]}: ok={resp.get('ok')}", flush=True)
        except Exception as e:
            print(f"uid …{uid[-4:]}: ERR {type(e).__name__}: {e}", flush=True)
    if ok_any:
        MARKER.write_text(datetime.datetime.now().isoformat())
        _self_remove_cron()
        print("напоминание отправлено, cron-строка снята", flush=True)


if __name__ == "__main__":
    main()
