#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# поток: ops
"""Разовое напоминание перевыпустить WB-токен Дисквэра (WB_TOKEN_ACC2) в бот @MC_invoicebot.

Токен ВБ Дисквэра выпущен 2026-07-29, действует 182 дня → истекает ~2027-01-27.
Напоминание — на 170-й день (2027-01-15), за ~12 дней до истечения.

Механика (устойчивая к простою сервера): скрипт запускается cron-ом ЕЖЕДНЕВНО и
делает быстрый no-op, пока не наступит дата. На/после FIRE_ON шлёт одно сообщение
всем TG_ALLOWED_IDS, ставит маркер и САМ удаляет свою строку из crontab (тег
WB_TOKEN_REMINDER) — больше не срабатывает. Секреты (.env) не печатает.

Время в crontab сервера = UTC (CRON_TZ не поддерживается — проверено 2026-07-28).
"""
import os
import sys
import json
import datetime
import subprocess
import urllib.request
import urllib.parse
import pathlib

FIRE_ON = datetime.date(2027, 1, 15)          # 170-й день от выпуска (29.07.2026)
BASE = pathlib.Path("/opt/mp-analytics")
ENV = BASE / ".env"
MARKER = BASE / "ops" / ".wb_token_reminder_sent"
CRON_TAG = "WB_TOKEN_REMINDER"

MSG = (
    "🔑 Обновить токен ВБ *Дисквэр* (для Пульта):\n"
    "Статистика, Аналитика, Контент, Продвижение, Вопросы и отзывы\n\n"
    "Выпущен 29.07.2026, действует 182 дня → истекает ~27.01.2027 (через ~12 дней).\n"
    "Если не перевыпустить — снова встанут по Дисквэру: финотчёт, остатки "
    "(«Распродажа остатков ВБ»), карточки и реклама.\n\n"
    "Что сделать:\n"
    "1) ЛК ВБ (Дисквэр) → новый токен со скоупами:\n"
    "   • Статистика\n"
    "   • Аналитика\n"
    "   • Контент\n"
    "   • Продвижение\n"
    "   • Вопросы и отзывы\n"
    "2) Вписать значение в /opt/mp-analytics/.env → WB_TOKEN_ACC2\n"
    "3) run_daily доберёт пропущенное скользящим окном — вручную гонять не нужно.\n\n"
    "Не трогать: WB_TOKEN_PRICES_ACC2 (Цены — отдельный токен) и WB_TOKEN_CONTENT_ACC2 (Контент)."
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
        # регистронезависимо: комментарии содержат WB_TOKEN_REMINDER, строка задания —
        # путь wb_token_reminder.py; оба ловятся по подстроке "wb_token_reminder"
        kept = [ln for ln in cur.stdout.splitlines()
                if "wb_token_reminder" not in ln.lower()]
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
