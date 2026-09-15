# поток: prc
# -*- coding: utf-8 -*-
"""Закупочная цена карточки = цена позиции актуального оприходования на «Удаленном».

Почтовый загрузчик (`loader.card_updates`) пишет закупочную только по своим профилям.
Документы внешнего API-загрузчика (Булат, ВТТ, Рамис, Блоссом, S-Print, ProfiLine …) лежат
на том же складе, но закупочную в карточках не трогали. Этот модуль проходит ВСЕ актуальные
оприходования и переносит цену позиции в карточку этой же позиции.

Решения Сергея 14.09.2026:
- перезаписываем всегда, остатки на Звездном/Дисквере не проверяем: цена по УПД и цена
  прайса — одна и та же цена, расходятся на старых приёмках из-за роста цен (замер 14.09:
  приёмка ≤30 дней — медиана расхождения 1,6 %); себестоимость МС считает по документам,
  не по полю карточки;
- поставщика карточки со строкой прайса не сверяем — доверяем карточке из позиции;
- наборы поставщика — обычные карточки, пишем как товар; наборы «из цветов» живут на
  поставщике «Микс МСК» и в оприходования не входят — страхуемся явным пропуском;
- документ старше 4 дней (по дате в имени) не берём: у поставщика на паузе он залёживается;
  исключение — Blossom: внешний загрузчик не обновлял его с 14.07.2026, цены берём как есть;
- ночной прогон, запись только при изменении цены.

Темп — регламент массовых правок МС (skill `ms-mass-change`): окно 23:00–05:00 МСК,
пачка 50 через 60 с (3000 изменений/час), не больше 10 000 за ночь, канарейка 200 → 10 мин.

    ./venv/bin/python -m prices.enter_buyprice             # сухой прогон: план + CSV
    ./venv/bin/python -m prices.enter_buyprice --apply     # запись (только в окне)
"""
import argparse
import csv
import datetime as dt
import json
import sys
import time
from pathlib import Path

import psycopg2.extras

from core import ms_api
from core.db import get_conn
from prices import unlinked
from prices.loader import now_msk
from prices.profiles import STORE_REMOTE

ROOT = Path(__file__).resolve().parents[1]
REPORT_DIR = ROOT / "reports" / "data"
BACKUP_DIR = ROOT / "backups" / "prc_enter_buyprice"

MAX_AGE_DAYS = 4
NO_AGE_LIMIT = {"blossom"}                                      # решение Сергея 14.09.2026
SUPPLIER_MIX_MSK = "b4a20370-e872-11ef-0a80-0947000e2df8"      # «Микс МСК» — наборы из цветов
CURRENCY_RUB = "20d3bd8c-bde9-47cc-ba24-b299ba80b6d7"

WINDOW_START, WINDOW_END = dt.time(23, 0), dt.time(5, 0)       # МСК
BATCH, PAUSE = 50, 60
CANARY, CANARY_PAUSE = 200, 600
NIGHT_CAP = 10_000
SLOW_RESPONSE = 10.0                                            # с — МС тяжело, паузу удваиваем

JOURNAL_SQL = """
INSERT INTO prc_buyprice_write (run_at, card_id, supplier_key, doc_name, old_kop, new_kop)
VALUES %s
"""


def say(text):
    """Строка лога со временем МСК и сразу в журнал: systemd иначе копит вывод до конца прогона
    (15.09.2026 все строки легли одним временем 05:00 — не видно, когда МС тормозил)."""
    for line in str(text).split("\n"):
        print(f"{now_msk():%H:%M:%S} {line}", flush=True)


def in_window(moment):
    t = moment.time()
    return t >= WINDOW_START or t < WINDOW_END


def fresh_docs(today):
    """Актуальные документы групп, не старше MAX_AGE_DAYS по дате в имени."""
    out, stale = [], []
    for key, date, doc in unlinked.current(unlinked.enters(STORE_REMOTE)):
        age = (today - dt.date.fromisoformat(date)).days
        (out if age <= MAX_AGE_DAYS or key in NO_AGE_LIMIT else stale).append((key, date, doc))
    return out, stale


def plan(today):
    """Список правок + счётчики причин пропуска. В МС только читает."""
    docs, stale = fresh_docs(today)
    price, conflicts = {}, 0            # card_id -> (created, key, doc_name, price_kop)
    for key, _date, doc in docs:
        for pos in unlinked.positions(doc):
            card_id = ms_api.meta_id(pos, "assortment")
            cand = (doc.get("created", ""), key, doc["name"], int(pos.get("price") or 0))
            was = price.get(card_id)
            if was and was[3] != cand[3]:
                conflicts += 1
            if not was or cand[0] > was[0]:          # две позиции на карточку — последний документ
                price[card_id] = cand

    cards = unlinked.cards(price)
    edits, skip = [], {"zero": 0, "missing": 0, "type": 0, "archived": 0,
                       "mix_msk": 0, "currency": 0, "same": 0}
    for card_id, (_created, key, doc_name, new) in price.items():
        card = cards.get(card_id)
        buy = (card or {}).get("buyPrice") or {}
        old = int(buy.get("value") or 0)
        cur = ms_api.meta_id(buy, "currency")
        reason = ("missing" if not card else
                  "zero" if new <= 0 else
                  "type" if card["meta"]["type"] != "product" else
                  "archived" if card.get("archived") else
                  "mix_msk" if ms_api.meta_id(card, "supplier") == SUPPLIER_MIX_MSK else
                  "currency" if cur and cur != CURRENCY_RUB else
                  "same" if old == new else None)
        if reason:
            skip[reason] += 1
            continue
        edits.append({"card": card, "key": key, "doc": doc_name, "old": old, "new": new,
                      "buy": buy})
    return {"docs": docs, "stale": stale, "positions": len(price), "conflicts": conflicts,
            "edits": edits, "skip": skip}


def write_csv(edits, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["card_id", "code", "article", "name", "supplier_key", "doc", "old_rub",
                    "new_rub", "diff_pct"])
        for e in edits:
            c = e["card"]
            pct = round((e["new"] - e["old"]) / e["old"] * 100, 1) if e["old"] else ""
            w.writerow([c["id"], c.get("code", ""), c.get("article", ""), c.get("name", ""),
                        e["key"], e["doc"], e["old"] / 100, e["new"] / 100, pct])


def summary(p):
    edits = p["edits"]
    by_key = {}
    for e in edits:
        by_key[e["key"]] = by_key.get(e["key"], 0) + 1
    big = sum(1 for e in edits if e["old"] and abs(e["new"] - e["old"]) / e["old"] > 0.2)
    lines = [
        f"документов актуальных {len(p['docs'])}, отброшено старше {MAX_AGE_DAYS} дн: "
        + (", ".join(sorted({k for k, _, _ in p['stale']})) or "нет"),
        f"карточек в позициях {p['positions']}, из них в двух документах с разной ценой {p['conflicts']}",
        f"к записи {len(edits)} (пустая закупочная {sum(1 for e in edits if not e['old'])},"
        f" скачок >20 % {big})",
        "пропуск: " + ", ".join(f"{k} {v}" for k, v in p["skip"].items()),
        "по группам: " + ", ".join(f"{k} {v}" for k, v in sorted(by_key.items(), key=lambda x: -x[1])),
    ]
    return lines


def journal(rows, run_at):
    with get_conn() as conn, conn.cursor() as cur:
        psycopg2.extras.execute_values(cur, JOURNAL_SQL, [
            (run_at, e["card"]["id"], e["key"], e["doc"], e["old"], e["new"]) for e in rows])


def reread(edits):
    """Перечитка записанного: {card_id: фактическое значение} — только расхождения."""
    got = unlinked.cards([e["card"]["id"] for e in edits])
    bad = []
    for e in edits:
        val = int(((got.get(e["card"]["id"]) or {}).get("buyPrice") or {}).get("value") or 0)
        if val != e["new"]:
            bad.append((e["card"]["id"], e["new"], val))
    return bad


def apply(edits, run_at, log=say, limit=None, clock=now_msk):
    """Запись пачками в окне. Возвращает (записано, остановлено_по_причине)."""
    todo = edits[:min(limit or NIGHT_CAP, NIGHT_CAP)]
    backup = BACKUP_DIR / f"{run_at:%Y-%m-%d_%H%M%S}"
    backup.mkdir(parents=True, exist_ok=True)
    done, pause, stop = [], PAUSE, None
    with (backup / "before.jsonl").open("a", encoding="utf-8") as snap:
        for i in range(0, len(todo), BATCH):
            if not in_window(clock()):
                stop = "окно 23:00–05:00 закончилось"
                break
            chunk = todo[i:i + BATCH]
            for e in chunk:                          # слепок ДО отправки пачки
                snap.write(json.dumps({"id": e["card"]["id"], "buyPrice": e["buy"]},
                                      ensure_ascii=False) + "\n")
            snap.flush()
            body = [{"meta": e["card"]["meta"], "buyPrice": {
                "value": e["new"], "currency": e["buy"].get("currency")
                or ms_api.ref("currency", CURRENCY_RUB)}} for e in chunk]
            t0 = time.monotonic()
            ms_api.post("/entity/product", body)
            spent = time.monotonic() - t0
            journal(chunk, run_at)
            done += chunk
            if spent > SLOW_RESPONSE:
                pause = min(pause * 2, 600)
                log(f"    МС отвечает {spent:.0f} с — пауза {pause} с")
            elif pause > PAUSE:
                # МС отпустило — возвращаемся к штатному темпу. Ночь 14→15.09 пауза залипла
                # на 600 с, и 2 890 карточек не успели до 05:00.
                pause = PAUSE
                log(f"    МС ответил за {spent:.0f} с — пауза снова {pause} с")
            if len(done) % 1000 == 0:
                log(f"    записано {len(done)} из {len(todo)}")
            left = len(todo) - len(done)
            if not left:
                break
            if len(done) == CANARY:
                log(f"    канарейка {CANARY} записана, пауза {CANARY_PAUSE} с")
                time.sleep(CANARY_PAUSE)
            else:
                time.sleep(pause)
    if not stop and len(todo) < len(edits):
        stop = f"потолок {len(todo)} за прогон, осталось {len(edits) - len(todo)}"
    return done, stop


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="записать в МС (только в окне)")
    ap.add_argument("--limit", type=int, help="записать не больше N карточек")
    ap.add_argument("--notify", action="store_true", help="сводку и сбои — в @ds_prc_bot")
    args = ap.parse_args(argv)

    run_at = now_msk()
    if args.apply and not in_window(run_at):
        say(f"{run_at:%H:%M} МСК — вне окна 23:00–05:00, запись запрещена")
        return 2
    p = plan(run_at.date())
    csv_path = REPORT_DIR / f"prc_enter_buyprice_{run_at:%Y-%m-%d_%H%M}.csv"
    write_csv(p["edits"], csv_path)
    lines = summary(p)
    say("\n".join(lines))
    say(f"план: {csv_path}")
    if not args.apply:
        return 0

    done, stop = apply(p["edits"], run_at, limit=args.limit)
    bad = reread(done) if done else []
    tail = [f"записано {len(done)} из {len(p['edits'])}" + (f"; стоп: {stop}" if stop else ""),
            f"перечитка: расхождений {len(bad)}"]
    say("\n".join(tail))
    if bad:
        (REPORT_DIR / f"prc_enter_buyprice_reread_{run_at:%Y-%m-%d_%H%M}.json").write_text(
            json.dumps(bad), encoding="utf-8")
    if args.notify and (bad or stop):
        from ops.prc_price_watch import tg
        tg("Закупочная из оприходований\n" + "\n".join(lines + tail))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
