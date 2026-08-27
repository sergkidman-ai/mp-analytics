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

Данные берём ЖИВЬЁМ из МойСклада (`report/stock/bystore/current`), а не из снимка
`supplier_stock`. Снимок собирается основным прогоном дважды в сутки — между ними до 17 часов,
и уведомление «товар кончился» опаздывало на смену. Второй капкан снимка: сборщик не пишет
строки с нулём, поэтому ноль в нём неотличим от «товара нет в выгрузке» и приходил как nodata,
то есть молчанием вместо тревоги. У живой ручки отсутствие строки И ЕСТЬ ноль.

    ./venv/bin/python -m ops.stock_watch --seed   # завести 4 кода из записки
    ./venv/bin/python -m ops.stock_watch --dry    # показать, что бы отправил
    ./venv/bin/python -m ops.stock_watch          # боевой прогон
    ./venv/bin/python -m ops.stock_watch --list   # текущие подписки
"""
import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, "/opt/mp-analytics")
load_dotenv("/opt/mp-analytics/.env")

from collectors.suppliers import _ms  # noqa: E402  — тот же клиент МС, что у сборщика остатков

# Свой бот потока prc — @ds_prc_bot, как и просили («туда же в бот PRC»).
# В TG_NOTIFY_ID лежат ДРУГИЕ люди, туда слать нельзя (память telegram-channels).
NOTIFY_IDS = [x.strip() for x in os.getenv("TG_PRC_NOTIFY_ID", "1031321444").split(",") if x.strip()]
TG_TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "").strip()

# Коды из записки Натальи 21.08.2026 — HP 913A PageWide, поставщик ООО «ОДИССЕЙ» WB.
# Порог 0, а не 5: в записке «сообщить, когда ЗАКОНЧИТСЯ остаток». Ранние «на исходе»
# при пороге 5 — другой вопрос и лишний шум (решение Сергея 25.08.2026).
SEED = [("3801at", 0, "HP 913A чёрный (AT-L0R95AE Bk)"),
        ("3802at", 0, "HP 913A голубой (AT-F6T77AE C)"),
        ("3803at", 0, "HP 913A пурпурный (AT-F6T78AE M)"),
        ("3804at", 0, "HP 913A жёлтый (AT-F6T79AE Y)")]

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


def ms_ids_for(code, supplier=None):
    """Карточки МС с этим «Кодом»: [(ms_id, name, supplier)].

    Одному коду соответствует НЕСКОЛЬКО карточек (у 5439sp их две — «Солюшнс принт МСК»
    и «Солюшнс принт»), остатки по ним складываются. Поставщика берём живьём (expand=supplier),
    а не из снимка: карточка с вечным нулём в снимок не попадает вовсе, и фильтр по поставщику
    по снимку молча терял бы именно тот случай, ради которого сторож и заведён.
    """
    rows = _ms("entity/product", expand="supplier", limit=100,
               filter=f"code={code}").get("rows", [])
    out = []
    for p in rows:
        sup = (p.get("supplier") or {}).get("name")
        if supplier and sup != supplier:
            continue
        out.append((p["id"], p.get("name"), sup))
    return out


def store_ids():
    return {st["name"]: st["id"] for st in _ms("entity/store", limit=100).get("rows", [])}


def live_stock(cur):
    """Остаток по каждой активной подписке — живым запросом в МойСклад.

    Отсутствие строки в ответе = ноль (ручка отдаёт только ненулевые остатки) — именно это
    и есть событие подписки. А вот сбой сети/МС нулём считать нельзя: он даёт stock=None
    (состояние nodata), и такие подписки прогон пропускает молча, не трогая last_state.

    supplier в подписке — фильтр «следить за товаром ИМЕННО этого поставщика» (миграция 509);
    NULL = любой поставщик под этим кодом.
    """
    cur.execute("""SELECT id, ms_code, store, threshold, last_state, chat_id, note, supplier
                     FROM stock_watch WHERE active ORDER BY ms_code""")
    watches = cur.fetchall()
    if not watches:
        return []

    failed, cards, per_store = set(), {}, {}
    try:
        stores = store_ids()
    except Exception as exc:
        print(f"МС не ответил (склады): {type(exc).__name__}: {exc}")
        stores, failed = {}, {w[0] for w in watches}

    for wid, code, store, _thr, _last, _chat, _note, sup in watches:
        if wid in failed:
            continue
        if store not in stores:
            print(f"склад «{store}» не найден в МС — подписка {code} пропущена")
            failed.add(wid)
            continue
        try:
            cards[wid] = ms_ids_for(code, sup)
        except Exception as exc:
            print(f"МС не ответил ({code}): {type(exc).__name__}: {exc}")
            failed.add(wid)
            continue
        per_store.setdefault(store, set()).update(c[0] for c in cards[wid])

    stock = {}
    for store, ids in per_store.items():
        ids = sorted(ids)
        try:
            for i in range(0, len(ids), 25):
                flt = f"storeId={stores[store]};" + ";".join(
                    f"assortmentId={x}" for x in ids[i:i + 25])
                for r in _ms("report/stock/bystore/current", filter=flt):
                    stock[(store, r["assortmentId"])] = r.get("stock") or 0
        except Exception as exc:
            print(f"МС не ответил (остатки «{store}»): {type(exc).__name__}: {exc}")
            failed.update(w[0] for w in watches if w[2] == store)

    # «В пути» живая ручка не отдаёт — берём из последнего снимка. Это справка в тексте
    # уведомления («осталось 0, в пути 20»), на срабатывание сторожа она не влияет.
    cur.execute("""SELECT ms_id, store, in_transit FROM supplier_stock
                    WHERE captured_at = (SELECT max(captured_at) FROM supplier_stock)""")
    transit = {(st, mid): it for mid, st, it in cur.fetchall()}

    out = []
    for wid, code, store, thr, last, chat, note, sup in watches:
        ids = cards.get(wid, [])
        name = ids[0][1] if ids else None
        sups = ", ".join(sorted({c[2] for c in ids if c[2]})) or sup
        if wid in failed:
            qty, in_transit = None, None
        else:
            qty = sum(stock.get((store, i[0]), 0) for i in ids)
            in_transit = sum(transit.get((store, i[0])) or 0 for i in ids) or None
        out.append((wid, code, store, thr, last, chat, note, name, qty, sups, in_transit))
    return out


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
    now = datetime.now(timezone.utc) + timedelta(hours=3)  # МСК — в тексте для человека
    snap_date = now.date()
    rows = live_stock(cur)
    if not rows:
        print("подписок нет — заведите: --seed или INSERT в stock_watch")
        return

    changed, lines = [], []
    for row in rows:
        wid, code, store, thr, last_state, _chat, note, name, stock, supplier, in_transit = row
        state = classify(stock, thr)
        mark = "→ СМЕНА" if state != last_state else ""
        lines.append(f"  {code:8s} {str(stock if stock is not None else '—'):>5s} шт  "
                     f"порог {thr:g}  {last_state}→{state} {mark}")
        # nodata здесь = МС не ответил (ноль приходит нулём). Молчим и не трогаем last_state:
        # иначе сбой сети выдавал бы «товар кончился», а потом «восстановился».
        if state == "nodata" or state == last_state:
            continue
        changed.append((wid, code, store, state, stock, note or name, supplier, in_transit, row))

    print(f"живые остатки МС {now:%d.%m %H:%M} МСК; подписок: {len(rows)}; "
          f"смен состояния: {len(changed)}")
    for ln in lines:
        print(ln)

    if not changed:
        print("уведомлять не о чем")
        conn.close()
        return

    # Одно сообщение на все смены, а не письмо на каждый код: четыре подряд уведомления
    # про один и тот же набор картриджей читаются как спам и перестают замечаться.
    body = [f"📦 Остатки поставщика — изменения на {now:%d.%m %H:%M} МСК", ""]
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
                       VALUES (%s, %s, %s)
                       ON CONFLICT (ms_code, store, coalesce(supplier, '')) DO NOTHING""",
                    (code, thr, note))
    conn.commit()
    cur.execute("SELECT count(*) FROM stock_watch WHERE active")
    print("активных подписок:", cur.fetchone()[0])
    conn.close()


def add_watch(code, store, threshold, supplier=None, note=None):
    """Завести подписку. Порог 0 = сообщить, когда ЗАКОНЧИТСЯ (классификация: 0 всегда zero).

    Стартовое состояние берём по факту сегодняшнего снимка, а не 'ok' по умолчанию: иначе
    подписка на уже закончившийся товар промолчала бы (состояние zero совпало бы с 'ok'→zero
    только на следующей смене, а её может не быть месяцами).
    """
    conn = db()
    cur = conn.cursor()
    cur.execute("""INSERT INTO stock_watch (ms_code, store, threshold, supplier, note)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (ms_code, store, coalesce(supplier, '')) DO UPDATE
                      SET threshold = EXCLUDED.threshold, note = EXCLUDED.note, active = true
                   RETURNING id""", (code, store, threshold, supplier, note))
    wid = cur.fetchone()[0]
    conn.commit()
    rows = [r for r in live_stock(cur) if r[0] == wid]
    stock = rows[0][8] if rows else None
    state = classify(stock, threshold)
    cur.execute("UPDATE stock_watch SET last_state=%s, last_stock=%s WHERE id=%s",
                (state, stock, wid))
    conn.commit()
    conn.close()
    print(f"подписка #{wid}: {code} · склад «{store}» · порог {threshold:g}"
          + (f" · {supplier}" if supplier else "")
          + f" — сейчас {stock if stock is not None else '—'} шт ({state})")


def show():
    conn = db()
    cur = conn.cursor()
    cur.execute("""SELECT ms_code, store, threshold, last_state, last_stock, active, note, supplier
                     FROM stock_watch ORDER BY ms_code, store""")
    print(f"{'код':9s}{'склад':13s}{'порог':>6s}{'сост.':>9s}{'ост.':>6s}  поставщик / примечание")
    for c, s, t, st, ls, a, n, sup in cur.fetchall():
        flag = "" if a else "  (выключена)"
        tail = " · ".join(x for x in (sup, n) if x)
        print(f"{c:9s}{s:13s}{t:6g}{st:>9s}{(str(ls) if ls is not None else '—'):>6s}  {tail}{flag}")
    conn.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true", help="завести 4 кода из записки 21.08")
    ap.add_argument("--list", action="store_true", help="показать подписки")
    ap.add_argument("--dry", action="store_true", help="прогон без отправки и без записи")
    ap.add_argument("--add", metavar="КОД", help="завести подписку на ms_product.code")
    ap.add_argument("--store", default="Удаленный склад", help="имя склада в МойСкладе")
    ap.add_argument("--threshold", type=float, default=0,
                    help="порог в штуках; 0 = сообщить, когда закончится")
    ap.add_argument("--supplier", help="следить за товаром именно этого поставщика")
    ap.add_argument("--note", help="примечание к подписке")
    a = ap.parse_args()
    if a.add:
        add_watch(a.add, a.store, a.threshold, a.supplier, a.note)
    elif a.seed:
        seed()
    elif a.list:
        show()
    else:
        run(dry=a.dry)
