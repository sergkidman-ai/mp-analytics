"""поток: card — сверка wb_cards с кабинетом ВБ: какие карточки в ЛК уже нет.

Зачем: `wb_cards` только копится. Карточку, удалённую в ЛК ВБ, коллектор перестаёт видеть,
но строка остаётся и дальше участвует во всех выборках как настоящая. 25.08.2026 это дало
7 ложных строк в списке «Карточки без наличия»: витрина card.wb.ru карточку не отдаёт вовсе,
а рядом тем же артикулом торгует живая карточка-близнец с остатком.

Что делает: тянет из content-api ПОЛНЫЙ список карточек аккаунта (активные + корзина) и
сверяет с nm_id в `wb_cards`. Результат — в свою таблицу `wb_card_presence`; чужую `wb_cards`
не трогаем (территория mkt/fin). Потребителям — вьюха `wb_cards_live`.

К площадке — ТОЛЬКО ЧТЕНИЕ: `/content/v2/get/cards/list` и `/content/v2/get/cards/trash`
(POST в них лишь потому, что фильтр едет телом). Ничего не создаём, не правим, не удаляем.

Запуск:  ./venv/bin/python tools/card_wb_presence.py             # оба аккаунта
         ./venv/bin/python tools/card_wb_presence.py wb_acc2     # один
         ./venv/bin/python tools/card_wb_presence.py --dry-run   # без записи в БД
"""
import pathlib
import sys
import time

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                     # noqa: E402
from collectors.wb import CARDS_URL                     # noqa: E402
from collectors.wb_card_content import _content_token   # noqa: E402

ACCOUNTS = ("wb_acc1", "wb_acc2")
TRASH_URL = CARDS_URL.replace("cards/list", "cards/trash")
PAGE = 100


def _fetch(account, url, label):
    """Полный список карточек постранично (курсор updatedAt+nmID) → {nm_id: vendorCode}.

    Тихо: печатаем каждые 20 страниц, а не каждую — иначе 146 строк в лог на аккаунт.
    """
    headers = {"Authorization": _content_token(account), "Content-Type": "application/json"}
    cursor, out, page = {"limit": PAGE}, {}, 0
    while True:
        body = {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}
        r = requests.post(url, headers=headers, json=body, timeout=120)
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "20")) + 1)
            continue
        r.raise_for_status()
        data = r.json()
        batch = data.get("cards", []) or []
        for c in batch:
            if c.get("nmID") is not None:
                out[c["nmID"]] = c.get("vendorCode")
        page += 1
        if page % 20 == 0:
            print(f"  [{account} {label}] страниц {page}, карточек {len(out)}", flush=True)
        if len(batch) < PAGE:
            break
        cur = data.get("cursor", {}) or {}
        cursor = {"limit": PAGE, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
        time.sleep(0.3)
    return out


def check(account, dry_run=False):
    active = _fetch(account, CARDS_URL, "активные")
    trash = _fetch(account, TRASH_URL, "корзина")
    known = {r["nm_id"]: r["vendor_code"]
             for r in db.query("select nm_id, vendor_code from wb_cards where account = %s",
                               (account,))}
    # Пустой ответ площадки — это сбой доступа, а не «удалили весь каталог».
    # Без этой отбивки один 200-с-пустым-телом пометил бы мёртвыми все 14 тыс. карточек.
    if not active:
        print(f"  [{account}] кабинет вернул 0 активных карточек — считаю это сбоем, не пишу")
        return None

    seen = {r["nm_id"]: r for r in db.query(
        "select nm_id, first_missing, last_present from wb_card_presence where account = %s",
        (account,))}
    # Время берём у БД один раз: в строках оно должно быть тем же, что и checked_at.
    now = db.query("select now() t")[0]["t"]
    rows, gone = [], []
    for nm in set(known) | set(active) | set(trash):
        present = nm in active
        prev = seen.get(nm) or {}
        rows.append({
            "account": account,
            "nm_id": nm,
            "vendor_code": active.get(nm) or trash.get(nm) or known.get(nm),
            "in_cabinet": present,
            "in_trash": nm in trash,
            "first_missing": None if present else (prev.get("first_missing") or now),
            "last_present": now if present else prev.get("last_present"),
            "checked_at": now,
        })
        if not present and nm in known:
            gone.append(nm)

    if not dry_run:
        db.upsert("wb_card_presence", rows, conflict_cols=["account", "nm_id"],
                  update_cols=["vendor_code", "in_cabinet", "in_trash",
                               "first_missing", "last_present", "checked_at"])
    print(f"  [{account}] в кабинете {len(active)}, в корзине {len(trash)}, "
          f"в wb_cards {len(known)} → мёртвых строк {len(gone)}"
          + (" (dry-run, не записано)" if dry_run else ""))
    return gone


def main(argv):
    dry_run = "--dry-run" in argv
    accounts = [a for a in argv if a in ACCOUNTS] or list(ACCOUNTS)
    total = 0
    for account in accounts:
        gone = check(account, dry_run=dry_run)
        if gone is None:
            continue
        total += len(gone)
        for nm in gone[:10]:
            row = db.query("select vendor_code, left(title, 45) t from wb_cards "
                           "where account = %s and nm_id = %s", (account, nm))
            if row:
                print(f"     {nm} {row[0]['vendor_code']}  {row[0]['t']}")
        if len(gone) > 10:
            print(f"     … и ещё {len(gone) - 10}, полный список — в wb_card_presence")
    print(f"итого карточек, которых больше нет в кабинете: {total}")


if __name__ == "__main__":
    main(sys.argv[1:])
