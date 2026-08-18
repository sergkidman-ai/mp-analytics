# поток: prc
# -*- coding: utf-8 -*-
"""Одиссей: развести карточки по двум юрлицам — White Box и всё остальное.

White Box («белая», небрендированная коробка) по решению Сергея 18.08.2026 живёт на отдельном
контрагенте «ООО ОДИССЕЙ WB», остальной ассортимент Одиссея (Аквамарин, Hi-Black и пр.) —
на «ООО ОДИССЕЙ». Признак бренда — `ms_import.is_white_box`: метка в НАИМЕНОВАНИИ, префикс
артикула «WB …» как подстраховка. Тот же признак теперь ставит поставщика при заведении новых
карточек (`ms_import.supplier_of`), так что процесс и разбор старого не могут разъехаться.

Сверх правила скрипт показывает, где с ним спорит СУФФИКС НАШЕГО КОДА (`1234wb`): код
присваивал человек, и расхождение — повод посмотреть глазами, а не молча переписать.

    ./venv/bin/python -m tools.prc.odissey_wb_fix                 # сухой прогон + отчёт
    ./venv/bin/python -m tools.prc.odissey_wb_fix --apply         # запись в МС
    ./venv/bin/python -m tools.prc.odissey_wb_fix --with-archived # вместе с архивными
"""
import argparse
import csv
import re
import time
from pathlib import Path

from core import ms_api
from prices.ms_import import is_white_box

OD = ("7225fcf1-10f7-11ea-0a80-06380004eeda", 'ООО "ОДИССЕЙ"')
ODW = ("61d01266-2fe1-11f0-0a80-0f7f000bf967", 'ООО "ОДИССЕЙ" WB')
OUT = Path(__file__).resolve().parents[2] / "docs" / "reports"
PAUSE = 1.0          # МС днём занят рабочими процессами — идём мягко
BATCH = 100
CODE_TAIL = re.compile(r"^\d+([a-z]+)$", re.IGNORECASE)


def cards(supplier_id, archived):
    """Карточки одного юрлица постранично."""
    off = 0
    while True:
        page = ms_api.get("/entity/product", {"limit": 1000, "offset": off, "filter": [
            f"supplier={ms_api.BASE}/entity/counterparty/{supplier_id}",
            f"archived={'true' if archived else 'false'}"]})
        rows = page.get("rows", [])
        for p in rows:
            yield p
        off += len(rows)
        if len(rows) < 1000 or off >= page.get("meta", {}).get("size", 0):
            return
        time.sleep(PAUSE)


def plan(with_archived):
    """[(карточка, куда, почему)] — только те, у кого юрлицо не совпало с брендом."""
    moves, seen = [], 0
    for sid, title in (OD, ODW):
        for archived in ((False, True) if with_archived else (False,)):
            for p in cards(sid, archived):
                seen += 1
                wb = is_white_box(p.get("name"), p.get("article"))
                want = ODW if wb else OD
                if want[0] == sid:
                    continue
                tail = CODE_TAIL.match(p.get("code") or "")
                tail = (tail.group(1).lower() if tail else "")
                note = ""
                if tail == "wb" and not wb:
                    note = "код оканчивается на wb, а метки бренда в имени нет"
                elif tail and tail != "wb" and wb:
                    note = f"код оканчивается на {tail}, а в имени метка White Box"
                moves.append((p, want, note))
    return moves, seen


def apply(moves, log=print):
    """Переписываем только поле «Поставщик», остальное не трогаем."""
    for i in range(0, len(moves), BATCH):
        chunk = moves[i:i + BATCH]
        ms_api.post("/entity/product", [
            {"meta": p["meta"], "supplier": ms_api.ref("counterparty", want[0])}
            for p, want, _ in chunk])
        log(f"  записано {i + len(chunk)}/{len(moves)}")
        time.sleep(PAUSE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="писать в МС (без него сухой прогон)")
    ap.add_argument("--with-archived", action="store_true", help="включая архивные карточки")
    ap.add_argument("--out", default=str(OUT / "prc_odissey_wb_2026-08-18.csv"))
    args = ap.parse_args()

    moves, seen = plan(args.with_archived)
    to_wb = [m for m in moves if m[1] is ODW]
    to_od = [m for m in moves if m[1] is OD]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["куда", "код", "артикул", "внешний код", "наименование", "архив", "замечание"])
        for p, want, note in sorted(moves, key=lambda m: (m[1][1], m[0].get("code") or "")):
            w.writerow([want[1], p.get("code"), p.get("article"), p.get("externalCode"),
                        p.get("name"), "да" if p.get("archived") else "", note])
    print(f"карточек просмотрено: {seen}")
    print(f"  на «ОДИССЕЙ WB»: {len(to_wb)}   (архивных {sum(1 for m in to_wb if m[0].get('archived'))})")
    print(f"  на «ОДИССЕЙ»:    {len(to_od)}   (архивных {sum(1 for m in to_od if m[0].get('archived'))})")
    print(f"  спорят с суффиксом кода: {sum(1 for m in moves if m[2])}")
    print("отчёт ->", args.out)
    if args.apply:
        apply(moves)
        print("готово")
    else:
        print("сухой прогон: в МС ничего не записано (--apply)")


if __name__ == "__main__":
    main()
