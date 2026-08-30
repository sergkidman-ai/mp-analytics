#!/usr/bin/env python3
# поток: ev
"""Сторож автоакций WB: предупредить Наталью НАКАНУНЕ старта.

Зачем. Автоакция WB стартует ночью и сама режет цены по товарам, которые в неё попали;
узнать об этом постфактум — значит увидеть провал маржи в отчёте через неделю. Просьба
Натальи (24.08.2026): акция начинается 28.08 → уведомление приходит 27.08, чтобы был день
на решение «входим / выводим товары».

Источник — календарь акций WB `dp-calendar-api`. Токен нужен со скоупом «Цены и скидки»
(`WB_TOKEN_PRICES_ACC*`): обычные `WB_TOKEN_ACC*` этот ресурс отдают 403.

Даты WB отдаёт в UTC (`2026-08-27T21:00:00Z`), а человек живёт в МСК — это ровно
28.08 00:00. Считать «за сколько дней до старта» по UTC-дате нельзя: вечерние старты
уехали бы на день назад и уведомление пришло бы в день начала акции.

Дедуп в `wb_promo_notice`: у acc1 и acc2 календарь почти одинаковый, поэтому сообщение
одно на акцию со списком аккаунтов, а отметка «уведомили» ставится каждому аккаунту.

    ./venv/bin/python -m ops.wb_promo_watch --list        # календарь на 45 дней
    ./venv/bin/python -m ops.wb_promo_watch --dry         # что бы отправил сегодня
    ./venv/bin/python -m ops.wb_promo_watch               # боевой прогон (крон, утро)
    ./venv/bin/python -m ops.wb_promo_watch --lead 2      # предупреждать за 2 дня
"""
import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, "/opt/mp-analytics")
load_dotenv("/opt/mp-analytics/.env")

API = "https://dp-calendar-api.wildberries.ru/api/v1/calendar/promotions"
MSK = timezone(timedelta(hours=3))
HORIZON_DAYS = 45

# Бот потока prc — там же сторож остатков; в TG_NOTIFY_ID другие люди (память telegram-channels).
NOTIFY_IDS = [x.strip() for x in os.getenv("TG_PRC_NOTIFY_ID", "1031321444").split(",") if x.strip()]
TG_TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "").strip()

ACCOUNTS = {"wb_acc1": "WB_TOKEN_PRICES_ACC1", "wb_acc2": "WB_TOKEN_PRICES_ACC2"}
ACC_NAME = {"wb_acc1": "Цифровой квадрат", "wb_acc2": "Дисквэр"}

# «(автоматические скидки)» в конце названия — шум: в шапке уже сказано, что акция автоматическая.
AUTO_SUFFIX = re.compile(r"\s*\((?:автоматическ|автоматическая акция)[^)]*\)\s*$", re.I)


def db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def tg(text):
    if not TG_TOKEN:
        return "нет TG_PRC_BOT_TOKEN"
    out = []
    for chat in NOTIFY_IDS:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              data={"chat_id": chat, "text": text[:3900],
                                    "disable_web_page_preview": "true"}, timeout=60)
            out.append(f"{chat}: " + ("ok" if r.ok else f"{r.status_code} {r.text[:120]}"))
        except Exception as exc:
            out.append(f"{chat}: {type(exc).__name__}: {exc}")
    return "; ".join(out)


def fetch(account):
    """Календарь акций аккаунта на горизонт. allPromo=false — только то, что доступно нам."""
    token = os.getenv(ACCOUNTS[account], "").strip()
    if not token:
        return None, f"нет {ACCOUNTS[account]}"
    now = datetime.now(timezone.utc)
    r = requests.get(API, headers={"Authorization": token}, timeout=60, params={
        "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endDateTime": (now + timedelta(days=HORIZON_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "allPromo": "false", "limit": 100})
    if not r.ok:
        return None, f"{r.status_code} {r.text[:120]}"
    return r.json().get("data", {}).get("promotions", []) or [], None


def msk_date(iso):
    """'2026-08-27T21:00:00Z' → date(2026-08-28) по МСК."""
    if not iso:
        return None
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(MSK).date()


def collect():
    """Сырьё по всем аккаунтам: [(account, promo dict)], + ошибки доступа."""
    rows, errors = [], []
    for acc in ACCOUNTS:
        promos, err = fetch(acc)
        if err:
            errors.append(f"{acc}: {err}")
            continue
        rows += [(acc, p) for p in promos]
    return rows, errors


# Событие в дневник (biz_events) сторож НЕ пишет: решение Сергея 30.08.2026. Автоакции WB
# идут сплошным потоком, и лента на главной, заведённая ради наших решений и ЧП на складах,
# забивалась строками «— старт». Канал предупреждения один — телеграм накануне старта.


def run(lead=1, dry=False, show_all=False):
    today = datetime.now(MSK).date()
    target = today + timedelta(days=lead)
    rows, errors = collect()
    for e in errors:
        print("ОШИБКА ДОСТУПА:", e)
    if not rows and errors:
        # Молчать нельзя: «уведомлений нет» и «нас не пустили в API» — разные вещи.
        if not dry:
            tg("⚠️ Сторож автоакций WB не смог прочитать календарь:\n" + "\n".join(errors))
        return

    conn = db()
    cur = conn.cursor()
    fresh = {}          # promo_id -> (name, start, end, [accounts, которым ещё не слали])
    for acc, p in rows:
        pid, ptype = p.get("id"), (p.get("type") or "").lower()
        start, end = msk_date(p.get("startDateTime")), msk_date(p.get("endDateTime"))
        name = (p.get("name") or "").strip()
        cur.execute("""INSERT INTO wb_promo_notice (account, promo_id, promo_type, name,
                                                    starts_at, ends_at)
                       VALUES (%s,%s,%s,%s,%s,%s)
                       ON CONFLICT (account, promo_id) DO UPDATE
                          SET name=EXCLUDED.name, starts_at=EXCLUDED.starts_at,
                              ends_at=EXCLUDED.ends_at
                       RETURNING notified_at""", (acc, pid, ptype, name,
                                                  p.get("startDateTime"), p.get("endDateTime")))
        already = cur.fetchone()[0]
        if show_all:
            print(f"  {acc} {pid:>5} {ptype:8s} {start:%d.%m}→{end:%d.%m} "
                  f"{'уведомлено' if already else '':10s} {name[:60]}")
        if ptype != "auto" or start != target or already:
            continue
        item = fresh.setdefault(pid, [name, start, end, []])
        item[3].append(acc)
    conn.commit()

    if show_all:
        conn.close()
        return

    if not fresh:
        print(f"сегодня {today:%d.%m}: автоакций со стартом {target:%d.%m} нет")
        conn.close()
        return

    # Одна строка на акцию, без разбивки по аккаунтам: календарь acc1 и acc2 совпадает,
    # а у одной и той же акции бывают разные id — для читателя это была бы пустая копия.
    uniq = {}
    for pid, (name, start, end, _accs) in fresh.items():
        uniq.setdefault((AUTO_SUFFIX.sub("", name).strip(), start, end), None)
    body = [f"📣 Завтра ({target:%d.%m}) стартует автоакция WB", ""]
    for name, start, end in sorted(uniq, key=lambda k: (k[1], k[0])):
        body.append(f"• {name}")
        body.append(f"    {start:%d.%m} → {end:%d.%m}")
    text = "\n".join(body)

    if dry:
        print("--- НЕ ОТПРАВЛЕНО (--dry) ---")
        print(text)
        conn.close()
        return

    print("telegram:", tg(text))
    for pid, (name, start, end, accs) in fresh.items():
        cur.execute("UPDATE wb_promo_notice SET notified_at=now() "
                    "WHERE promo_id=%s AND account = ANY(%s)", (pid, accs))
    conn.commit()
    conn.close()
    print(f"уведомлено об акциях: {len(fresh)}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Сторож автоакций WB (уведомление накануне старта)")
    ap.add_argument("--lead", type=int, default=1, help="за сколько дней предупреждать (по умолч. 1)")
    ap.add_argument("--dry", action="store_true", help="показать сообщение, не отправлять")
    ap.add_argument("--list", action="store_true", help="весь календарь на 45 дней")
    a = ap.parse_args(argv)
    run(lead=a.lead, dry=a.dry, show_all=a.list)


if __name__ == "__main__":
    main()
