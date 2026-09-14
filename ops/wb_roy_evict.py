#!/usr/bin/env python3
# поток: mkt
"""ops/wb_roy_evict.py — вывод ⚫-позиций ИЗ кампаний ВБ (не ставкой, а составом).

ЗАЧЕМ. Рой красит ⚫ «вывод» позицию, которая тратит и не продаёт настолько, что ставка уже
не лечит: её надо убрать из кампании совсем. До 14.09.2026 это делалось руками в ЛК, потому
что метод снятия номенклатуры считался несуществующим. Он есть — тот же, что и заводка:

    PATCH /adv/v0/auction/nms   {"nms":[{"advert_id":N,"nms":{"delete":[nm,...]}}]}

Снятие необратимо в одну сторону: вернуть карточку можно только заводкой, а свободных мест
в кампаниях почти нет (лимит 50 у новых, старые заграндфазерены выше лимита и add не принимают).
Поэтому шаг ГЕЙТИТСЯ ЧЕЛОВЕКОМ: недельный прогон только КЛАДЁТ заявку в очередь и шлёт её в бот,
а снятие происходит после подтверждения (страница mkt-сервиса /wb-evict или --apply руками).

ПРЕДОХРАНИТЕЛЬ. Если из кампании выводится не меньше карточек, чем мы у неё вообще знаем
(состав считаем по wb_ad_nm — карточки, которые получали показы), кампания пропускается:
опустошать кампанию вслепую нельзя, состав у ВБ не читается (геттера нет).

Запуск:
  ./venv/bin/python -m ops.wb_roy_evict docs/reports/mkt_roy_profile_2026-09-13.csv            # план
  ./venv/bin/python -m ops.wb_roy_evict <csv> --account wb_acc2 --queue --notify               # в очередь
  ./venv/bin/python -m ops.wb_roy_evict --account wb_acc2 --apply                              # снять
"""
import os
import csv
import sys
import json
import time
import argparse
import pathlib
import datetime

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from ops.wb_ad_enroll import TOKEN_ENV, _token  # noqa: E402  (один контур токенов с заводкой)

REPORTS = BASE_DIR / "docs" / "reports"
QUEUE = REPORTS / "mkt_roy_evict_queue.json"
JOURNAL = REPORTS / "mkt_roy_evict_journal.jsonl"
BLACK = '⚫ вывод'
HOST = "https://advert-api.wildberries.ru"
PAUSE = 21          # пауза между записями в /adv/v0/auction/nms (лимит контура)


def known_composition(account, days=60):
    """advert_id -> сколько карточек кампании мы ВООБЩЕ видели (нижняя оценка состава)."""
    rows = db.query(
        """select advert_id, count(distinct nm_id) n from wb_ad_nm
             where account=%s and period > current_date - (%s || ' days')::interval
             group by advert_id""", (account, days))
    return {int(r["advert_id"]): int(r["n"]) for r in rows}


def plan(csv_path, account):
    """Список снятий из отчёта Роя + отсев предохранителем."""
    comp = known_composition(account)
    by_adv, skip = {}, {}
    with open(csv_path, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            if r["цвет"] != BLACK:
                continue
            adv = (r["advert_id"] or "").strip()
            if not adv:
                skip["нет advert_id"] = skip.get("нет advert_id", 0) + 1
                continue
            by_adv.setdefault(int(adv), []).append(
                {"nm_id": int(r["nm_id"]), "cpc": float(r["ставка_₽"] or 0),
                 "spend": float(r["расход_₽"] or 0), "why": (r.get("причина") or "")[:120]})
    out = {}
    for adv, items in by_adv.items():
        n = comp.get(adv, 0)
        if n and len(items) >= n:
            skip[f"кампания {adv}: снимаем {len(items)} из известных {n} — опустошение"] = len(items)
            continue
        out[adv] = items
    return out, skip


def evict(account, advert_id, nms):
    import requests
    body = {"nms": [{"advert_id": int(advert_id), "nms": {"delete": [int(n) for n in nms]}}]}
    r = requests.patch(HOST + "/adv/v0/auction/nms", json=body, timeout=60,
                       headers={"Authorization": _token(account),
                                "Content-Type": "application/json"})
    return r.status_code, r.text[:220].replace("\n", " ")


def journal(rec):
    with open(JOURNAL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def log_db(account, advert_id, nm, status, resp, note):
    db.insert("wb_bid_log", [{
        "ts": datetime.datetime.now(), "account": account, "nm_id": nm, "advert_id": advert_id,
        "action": "evict", "applied": status == 200, "author": "roy", "note": note,
        "req_json": json.dumps({"_endpoint": "PATCH /adv/v0/auction/nms",
                                "advert_id": advert_id, "delete": [nm]}, ensure_ascii=False),
        "resp_status": status, "resp_json": json.dumps({"text": resp}, ensure_ascii=False)}])


def load_queue():
    return json.loads(QUEUE.read_text(encoding="utf-8")) if QUEUE.exists() else {}


def save_queue(q):
    QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=1), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", nargs="?")
    ap.add_argument("--account", default="wb_acc1")
    ap.add_argument("--queue", action="store_true", help="положить заявку в очередь на подтверждение")
    ap.add_argument("--notify", action="store_true", help="сообщить о заявке в бот")
    ap.add_argument("--apply", action="store_true", help="СНЯТЬ подтверждённое из очереди (живая запись)")
    a = ap.parse_args()

    if a.apply:
        q = load_queue()
        item = q.get(a.account)
        if not item:
            print(f"{a.account}: очередь пуста")
            return
        if not item.get("confirmed"):
            print(f"{a.account}: заявка от {item['date']} НЕ подтверждена — снятие не делаю")
            return
        if not os.environ.get(TOKEN_ENV.get(a.account, "")):
            print(f"{a.account}: нет токена {TOKEN_ENV.get(a.account)} — снятие не делаю")
            return
        ok = bad = 0
        for adv, nms in item["by_advert"].items():
            status, resp = evict(a.account, adv, [x["nm_id"] for x in nms])
            journal({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                     "account": a.account, "advert_id": int(adv),
                     "nm_ids": [x["nm_id"] for x in nms], "status": status, "resp": resp})
            for x in nms:
                log_db(a.account, int(adv), x["nm_id"], status, resp, f"roy evict {item['date']}")
            if status == 200:
                ok += len(nms)
            else:
                bad += len(nms)
                print(f"  кампания {adv}: HTTP {status} | {resp[:110]}")
            time.sleep(PAUSE)
        print(f"СНЯТО С РЕКЛАМЫ: {ok} карточек, отказов {bad}")
        q.pop(a.account, None)
        save_queue(q)
        return

    if not a.csv_path:
        ap.error("нужен путь к отчёту Роя (или --apply)")
    by_adv, skip = plan(a.csv_path, a.account)
    total = sum(len(v) for v in by_adv.values())
    spend = sum(x["spend"] for v in by_adv.values() for x in v)
    print(f"ВЫВОД ⚫ ПО ОТЧЁТУ {pathlib.Path(a.csv_path).name} · {a.account}")
    print(f"  к снятию {total} карточек в {len(by_adv)} кампаниях; их расход за неделю {spend:,.0f} ₽"
          .replace(",", " "))
    for k, v in sorted(skip.items(), key=lambda x: -x[1]):
        print(f"  пропуск · {k}: {v}")
    if not total:
        return
    if not a.queue:
        print("\nПЛАН. В ВБ не отправлено ничего. Заявка на подтверждение: --queue")
        return
    q = load_queue()
    q[a.account] = {"date": datetime.date.today().isoformat(),
                    "csv": pathlib.Path(a.csv_path).name, "confirmed": False,
                    "by_advert": {str(k): v for k, v in by_adv.items()}}
    save_queue(q)
    print(f"заявка положена в {QUEUE.name}; снятие — только после подтверждения")
    if a.notify:
        from ops.wb_daily_report import send
        send(f"*Рой ВБ — вывод ⚫ · {a.account}*\n"
             f"К снятию с рекламы *{total}* карточек в {len(by_adv)} кампаниях.\n"
             f"Их расход за неделю {spend:,.0f} ₽ при заказах 0.\n".replace(",", " ") +
             "Подтвердить или отклонить: http://127.0.0.1:8092/wb-evict\n"
             "Без подтверждения не снимается ничего.")


if __name__ == "__main__":
    main()
