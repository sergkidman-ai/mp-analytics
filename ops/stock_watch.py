#!/usr/bin/env python3
# поток: ev
"""Сторож остатков поставщика: «скажи, когда закончится товар по коду 3804at».

Зачем отдельный сторож, а не глазами в МойСкладе. Остаток по коду поставщика падает молча:
карточка на площадке продолжает висеть, продажи идут, а взять товар уже негде — и узнаём мы
об этом по провалу выручки через неделю. Сторож переводит «узнать постфактум» в «узнать заранее».

Почему следим не за всеми остатками. Просьба Натальи (21.08.2026) — четыре кода HP 913A CMYK
у ООО «ОДИССЕЙ». Следить за всем каталогом бессмысленно: это тысячи строк ежедневного шума без
адресата. Поэтому подписка (таблица `stock_watch`) — добавить код должно быть строкой в БД,
а не правкой исходника: такие просьбы будут повторяться.

Ключ слежки — `ms_product.code` («Код» в МС, напр. `3804at`). Это товар КОНКРЕТНОГО поставщика,
у него один остаток. Джойн по `external_code` (наша карточка `3804`) дал бы 9–11 строк разных
поставщиков и отвечал бы на другой вопрос — «товар негде взять вообще».

Данные берём из `supplier_stock` (ежедневные снимки), в API МойСклада не ходим: снимок уже
собран основным прогоном, и второй поход за теми же числами только тратил бы лимиты.

    ./venv/bin/python -m ops.stock_watch --seed   # завести 4 кода из записки
    ./venv/bin/python -m ops.stock_watch --dry    # показать, что бы отправил
    ./venv/bin/python -m ops.stock_watch          # боевой прогон
    ./venv/bin/python -m ops.stock_watch --list   # текущие подписки
"""
import argparse
import os
import sys
from datetime import datetime, timezone

import requests
import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, "/opt/mp-analytics")
load_dotenv("/opt/mp-analytics/.env")

# Свой бот потока prc — @ds_prc_bot, как и просили («туда же в бот PRC»).
# В TG_NOTIFY_ID лежат ДРУГИЕ люди, туда слать нельзя (память telegram-channels).
NOTIFY_IDS = [x.strip() for x in os.getenv("TG_PRC_NOTIFY_ID", "1031321444").split(",") if x.strip()]
TG_TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "").strip()

# Коды из записки Натальи 21.08.2026 — HP 913A PageWide, поставщик ООО «ОДИССЕЙ» WB.
SEED = [("3801at", 5, "HP 913A чёрный (AT-L0R95AE Bk)"),
        ("3802at", 5, "HP 913A голубой (AT-F6T77AE C)"),
        ("3803at", 5, "HP 913A пурпурный (AT-F6T78AE M)"),
        ("3804at", 5, "HP 913A жёлтый (AT-F6T79AE Y)")]

STATE_WORD = {"zero": "🔴 ЗАКОНЧИЛСЯ", "low": "🟡 на исходе", "ok": "🟢 восстановился",
              "nodata": "⚪️ нет данных"}


def db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))


def tg(text):
    """Молчать при сбое телеграма нельзя — но и ронять прогон из-за него тоже."""
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


def classify(stock, threshold):
    """Состояние подписки по остатку. Порядок проверок важен: 0 всегда 'zero', даже при пороге 0."""
    if stock is None:
        return "nodata"
    if stock <= 0:
        return "zero"
    if stock <= threshold:
        return "low"
    return "ok"


def read_current(cur):
    """Остаток по каждой активной подписке на ПОСЛЕДНЮЮ дату снимка.

    LEFT JOIN, а не INNER: товар может пропасть из снимка совсем (сняли с продажи у поставщика),
    и это тоже новость — иначе подписка молча перестала бы наблюдаться.
    """
    cur.execute("""
        SELECT w.id, w.ms_code, w.store, w.threshold, w.last_state, w.chat_id, w.note,
               p.name, s.stock, s.supplier, s.in_transit
          FROM stock_watch w
          LEFT JOIN ms_product p ON p.code = w.ms_code
          LEFT JOIN supplier_stock s
                 ON s.ms_id = p.ms_id
                AND s.store = w.store
                AND s.captured_at = (SELECT max(captured_at) FROM supplier_stock)
         WHERE w.active
         ORDER BY w.ms_code
    """)
    return cur.fetchall()


def log_event(cur, row, state, stock, snap_date):
    """Срабатывание сторожа = строка в дневнике (kind=supply).

    Смысл: провал продаж по карточке 913A через неделю будет объяснён прямо в отчёте, а не
    останется загадкой. dedup_key держит одно событие на переход — повторный прогон не плодит дубли.
    """
    _id, code, store, _thr, _last, _chat, note, name, *_ = row
    key = f"stock:{code}:{store}:{state}:{snap_date}"
    cur.execute("""
        INSERT INTO biz_events (event_date, kind, scope_kind, scope_ref, title, details,
                                source, dedup_key, author)
        VALUES (%s, 'supply', 'sku_list', %s, %s, %s, 'auto_stock', %s, 'сторож остатков')
        ON CONFLICT (dedup_key) DO NOTHING
    """, (snap_date, code,
          f"Остаток поставщика {code}: {STATE_WORD[state]}",
          f"{note or name or ''} — на складе «{store}» осталось {stock if stock is not None else '—'} шт",
          key))


def run(dry=False):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT max(captured_at) FROM supplier_stock")
    snap_date = cur.fetchone()[0]
    rows = read_current(cur)
    if not rows:
        print("подписок нет — заведите: --seed или INSERT в stock_watch")
        return

    changed, lines = [], []
    for row in rows:
        wid, code, store, thr, last_state, _chat, note, name, stock, supplier, in_transit = row
        state = classify(stock, thr)
        mark = "→ СМЕНА" if state != last_state else ""
        lines.append(f"  {code:8s} {str(stock or '—'):>5s} шт  порог {thr:g}  "
                     f"{last_state}→{state} {mark}")
        if state == last_state:
            continue
        changed.append((wid, code, store, state, stock, note or name, supplier, in_transit, row))

    print(f"снимок остатков: {snap_date}; подписок: {len(rows)}; смен состояния: {len(changed)}")
    for ln in lines:
        print(ln)

    if not changed:
        print("уведомлять не о чем")
        conn.close()
        return

    # Одно сообщение на все смены, а не письмо на каждый код: четыре подряд уведомления
    # про один и тот же набор картриджей читаются как спам и перестают замечаться.
    body = [f"📦 Остатки поставщика — изменения на {snap_date}", ""]
    for wid, code, store, state, stock, label, supplier, in_transit, _row in changed:
        body.append(f"{STATE_WORD[state]}  {code} — {label or ''}")
        body.append(f"    осталось {stock if stock is not None else '—'} шт"
                    + (f", в пути {in_transit:g}" if in_transit else "")
                    + (f" · {supplier}" if supplier else ""))
    body.append("")
    body.append("Отключить слежку: /unwatch <код> в этом боте")
    text = "\n".join(body)

    if dry:
        print("\n--- НЕ ОТПРАВЛЕНО (--dry) ---")
        print(text)
        conn.close()
        return

    print("telegram:", tg(text))
    for wid, code, store, state, stock, _label, _sup, _it, row in changed:
        cur.execute("""UPDATE stock_watch SET last_state=%s, last_stock=%s, notified_at=now()
                        WHERE id=%s""", (state, stock, wid))
        if state in ("zero", "low"):
            log_event(cur, row, state, stock, snap_date)
    conn.commit()
    conn.close()
    print("состояния обновлены, события записаны в дневник")


def seed():
    conn = db()
    cur = conn.cursor()
    for code, thr, note in SEED:
        cur.execute("""INSERT INTO stock_watch (ms_code, threshold, note)
                       VALUES (%s, %s, %s) ON CONFLICT (ms_code, store) DO NOTHING""",
                    (code, thr, note))
    conn.commit()
    cur.execute("SELECT count(*) FROM stock_watch WHERE active")
    print("активных подписок:", cur.fetchone()[0])
    conn.close()


def show():
    conn = db()
    cur = conn.cursor()
    cur.execute("""SELECT ms_code, store, threshold, last_state, last_stock, active, note
                     FROM stock_watch ORDER BY ms_code""")
    print(f"{'код':9s}{'склад':18s}{'порог':>6s}{'сост.':>9s}{'ост.':>7s}  примечание")
    for c, s, t, st, ls, a, n in cur.fetchall():
        flag = "" if a else "  (выключена)"
        print(f"{c:9s}{s:18s}{t:6g}{st:>9s}{(str(ls) if ls is not None else '—'):>7s}  {n or ''}{flag}")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="завести 4 кода из записки 21.08")
    ap.add_argument("--list", action="store_true", help="показать подписки")
    ap.add_argument("--dry", action="store_true", help="прогон без отправки и без записи")
    a = ap.parse_args()
    if a.seed:
        seed()
    elif a.list:
        show()
    else:
        run(dry=a.dry)
