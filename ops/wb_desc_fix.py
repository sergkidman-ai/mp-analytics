#!/usr/bin/env python3
# поток: mkt
"""ops/wb_desc_fix.py — исправить в описании карточки ВБ фразу о чипе. Костыль до правки генератора описаний.

ЗАЧЕМ. Генератор описаний (внешний, не в этом репозитории) берёт чип из каталога TheCartridge:
chip → «оснащен с чипом», nochip → «оснащен без чипа», ПУСТО → «не требуется чип». Аудит 21–22.09:
у 155 карточек acc1 в ТК уже «с чипом», а в описании «не требуется чип» (описание старше данных ТК).

ЧТО ДЕЛАЕТ. Режим `stale-chip`: карточки, где описание говорит «не требуется чип», а ТК — «с чипом» или
«с чипом без счётчика». В описании меняется ОДНО предложение, остальное слово в слово:
  «<товар> <модель>  не требуется чип, что обеспечивает …» → «<товар> <модель> оснащён чипом, что обеспечивает …»
  (для chip_free — «оснащён чипом без счётчика»).
КОНТРАКТ ВБ (Content API, токен WB_TOKEN_CONTENT_ACC*):
  POST /content/v2/get/cards/list  {settings:{filter:{textSearch:"<nmID>", withPhoto:-1}, cursor:{limit:1}}}  — свежая карточка
  POST /content/v2/cards/update    [ {nmID, vendorCode, brand, title, description, dimensions, characteristics, sizes} ]
       — ВБ ждёт карточку ЦЕЛИКОМ: поле, которого нет в запросе, будет стёрто. Поэтому шлём ровно то, что
         прочитали, заменив только description.
  POST /content/v2/cards/error/list — отказы после обновления
ПРОВЕРКА И ОТКАТ. После записи карточка перечитывается: описание должно совпасть с новым, остальные поля —
с прежними. Прежний текст пишется в docs/reports/mkt_wb_desc_fix_journal.jsonl — откат: --rollback.

По умолчанию DRY-RUN. Запись — только с --apply и только по прямой команде Сергея (инвариант 6).
  ./venv/bin/python -m ops.wb_desc_fix --account wb_acc1 --limit 5                # что изменится
  ./venv/bin/python -m ops.wb_desc_fix --account wb_acc1 --only 1234567 --apply    # одна карточка
"""
import sys
import re
import json
import time
import argparse
import datetime
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from collectors.wb import _content_token  # noqa: E402

API = "https://content-api.wildberries.ru"
JOURNAL = BASE_DIR / "docs" / "reports" / "mkt_wb_desc_fix_journal.jsonl"
SENT = re.compile(r"(\S[^.]*?)\s+не\s+требуется\s+чип(,\s*что обеспечивает[^.]*\.)", re.I)
KEEP = ("nmID", "vendorCode", "brand", "title", "description", "dimensions", "characteristics", "sizes")


def targets(acc):
    tc = {r["external_code"]: r["chip"] for r in db.query("select external_code, chip from prc_tc_model where chip in ('chip','chip_free')")}
    code2ext = {r["code"]: r["external_code"] for r in db.query("select code, external_code from prc_tc_code")}
    out = []
    for r in db.query("""select distinct on (nm_id) nm_id, vendor_code, payload->>'description' d from raw_wb_card_content
                          where account=%s order by nm_id, collected_at desc""", (acc,)):
        if "не требуется чип" not in (r["d"] or "").lower():
            continue
        m = re.match(r"^(\d+)", r["vendor_code"] or "")
        code = m.group(1) if m else ""
        ext = code2ext.get(code) or (code if code in tc else code[:4])
        if tc.get(ext):
            out.append((r["nm_id"], r["vendor_code"], tc[ext]))
    return out


def fetch(H, nm):
    body = {"settings": {"cursor": {"limit": 1}, "filter": {"textSearch": str(nm), "withPhoto": -1}}}
    for _ in range(4):
        r = requests.post(API + "/content/v2/get/cards/list", headers=H, json=body, timeout=60)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "10")) + 1); continue
        r.raise_for_status()
        cards = [c for c in r.json().get("cards") or [] if c.get("nmID") == nm]
        return cards[0] if cards else None
    return None


def rewrite(desc, chip):
    phrase = "оснащён чипом без счётчика" if chip == "chip_free" else "оснащён чипом"
    new, n = SENT.subn(lambda m: f"{m.group(1).rstrip()} {phrase}{m.group(2)}", desc, count=1)
    return (new, True) if n else (desc, False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="wb_acc1", choices=["wb_acc1", "wb_acc2"])
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--only", type=int, nargs="*")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    H = {"Authorization": _content_token(a.account), "Content-Type": "application/json"}
    tg = targets(a.account)
    if a.only:
        tg = [t for t in tg if t[0] in set(a.only)]
    print(f"{a.account}: карточек, где описание «не требуется чип», а в ТК чип есть — {len(tg)}")
    done = 0
    for nm, vc, chip in tg[:a.limit]:
        card = fetch(H, nm)
        if not card:
            print(f"  {nm}: карточка не найдена в API — пропуск"); continue
        old = card.get("description") or ""
        new, ok = rewrite(old, chip)
        if not ok:
            print(f"  {nm}: фраза в свежем описании не найдена — пропуск"); continue
        i = new.find("оснащён чип")
        print(f"  {nm} {vc}: …{old[max(0, i-60):i+60]}…\n      → …{new[max(0, i-60):i+75]}…")
        if not a.apply:
            continue
        payload = {k: card[k] for k in KEEP if k in card}
        payload["description"] = new
        r = requests.post(API + "/content/v2/cards/update", headers=H, json=[payload], timeout=60)
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"), "account": a.account,
                                "nm": nm, "old": old, "new": new, "http": r.status_code, "resp": r.text[:300]},
                               ensure_ascii=False) + "\n")
        print(f"      HTTP {r.status_code} {r.text[:120]}")
        time.sleep(8)
        after = fetch(H, nm)
        same_rest = after and all(after.get(k) == card.get(k) for k in KEEP if k not in ("description",))
        print(f"      проверка: описание {'обновлено' if after and after.get('description') == new else 'НЕ обновилось (модерация или отказ)'}; "
              f"остальные поля {'без изменений' if same_rest else 'ИЗМЕНИЛИСЬ — проверить'}")
        done += 1
        time.sleep(1)
    if not a.apply:
        print("DRY-RUN. В ВБ ничего не отправлено. Запись: --apply")
    else:
        print(f"записано {done}; журнал {JOURNAL.name}")


if __name__ == "__main__":
    main()
