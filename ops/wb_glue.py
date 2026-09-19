#!/usr/bin/env python3
# поток: mkt
"""ops/wb_glue.py — склеить позиции ВБ в одну карточку (общий imtID) и проверить по витрине.

ЗАЧЕМ. Разбор 19.09.2026: 10 197 бандлов не склеены со своим одиночным картриджем, 258 групп
«обычный ↔ XL» и 339 пар «фотобарабан ↔ тонер-картридж» на один принтер стоят раздельно.
Сначала пилот на парах, которые Сергей выбрал руками; массово — только после пилота.

КОНТРАКТ ВБ (Content API, категория «Контент», токен WB_TOKEN_CONTENT_ACC*):
  POST https://content-api.wildberries.ru/content/v2/cards/moveNm
       {"targetIMT": <imtID группы-приёмника>, "nmIDs": [nm, ...]}  — перенести позиции в группу (≤30)
       {"nmIDs": [nm]}                                              — отклеить в новую отдельную группу
  Склеивать можно только позиции одного предмета (у нас все в 1820 «Картриджи для принтеров»).
ПРОВЕРКА. Ответу контура не верим: после записи читаем публичный card.wb.ru (поле root = imtID)
и печатаем, у кого группа совпала. Каждое действие пишется в журнал с прежним imtID каждой позиции —
откат = moveNm обратно.

По умолчанию DRY-RUN. Запись в ВБ — только с --apply и только по прямой команде Сергея (инвариант 6).
  ./venv/bin/python -m ops.wb_glue --account wb_acc1 --target 397270419 --nm 349953418
  ./venv/bin/python -m ops.wb_glue --account wb_acc1 --target 397270419 --nm 349953418 --apply
  ./venv/bin/python -m ops.wb_glue --account wb_acc1 --undo 349953418 --apply     # отклеить
"""
import os
import sys
import json
import time
import argparse
import datetime
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from collectors.wb import _content_token  # noqa: E402

MOVE = "https://content-api.wildberries.ru/content/v2/cards/moveNm"
V4 = "https://card.wb.ru/cards/v4/detail"
JOURNAL = BASE_DIR / "docs" / "reports" / "mkt_glue_journal.jsonl"


def live(nms):
    r = requests.get(V4, params={"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30,
                                 "nm": ";".join(map(str, nms))}, timeout=40)
    return {int(p["id"]): {"root": p.get("root"), "name": p.get("name"), "subj": p.get("subjectId")}
            for p in r.json().get("products") or []}


def note(entry):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"), **entry},
                           ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=True, choices=["wb_acc1", "wb_acc2"])
    ap.add_argument("--target", type=int, help="nm_id позиции, в чью группу клеим")
    ap.add_argument("--nm", type=int, nargs="*", default=[], help="nm_id, которые переносим в группу target")
    ap.add_argument("--undo", type=int, nargs="*", help="отклеить эти nm_id в отдельные группы")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    H = {"Authorization": _content_token(a.account), "Content-Type": "application/json"}

    if a.undo:
        before = live(a.undo)
        body = {"nmIDs": a.undo}
        print("ОТКЛЕИТЬ:", {n: (before.get(n) or {}).get("root") for n in a.undo}, "тело:", body)
        if not a.apply:
            print("DRY-RUN"); return
        r = requests.post(MOVE, headers=H, json=body, timeout=60)
        note({"account": a.account, "action": "undo", "before": {str(k): v for k, v in before.items()},
              "body": body, "http": r.status_code, "resp": r.text[:300]})
        print("HTTP", r.status_code, r.text[:200]); return

    ids = [a.target] + a.nm
    before = live(ids)
    for n in ids:
        b = before.get(n) or {}
        print(f"  {n}: группа {b.get('root')} · предмет {b.get('subj')} · {(b.get('name') or '')[:70]}")
    if len(before) != len(ids):
        sys.exit("витрина ответила не по всем позициям — не клеим")
    if len({b["subj"] for b in before.values()}) != 1:
        sys.exit("разные предметы — ВБ не склеит")
    target_imt = before[a.target]["root"]
    body = {"targetIMT": target_imt, "nmIDs": a.nm}
    print("ТЕЛО:", json.dumps(body))
    if not a.apply:
        print("DRY-RUN. В ВБ ничего не отправлено. Запись: --apply"); return
    r = requests.post(MOVE, headers=H, json=body, timeout=60)
    note({"account": a.account, "action": "glue", "target": a.target, "before": {str(k): v for k, v in before.items()},
          "body": body, "http": r.status_code, "resp": r.text[:300]})
    print("HTTP", r.status_code, r.text[:200])
    for _ in range(6):
        time.sleep(20)
        after = live(ids)
        ok = [n for n in a.nm if (after.get(n) or {}).get("root") == target_imt]
        print(f"  витрина: в группе {target_imt} {len(ok)} из {len(a.nm)}")
        if len(ok) == len(a.nm):
            break
    print(f"https://www.wildberries.ru/catalog/{a.target}/detail.aspx")


if __name__ == "__main__":
    main()
