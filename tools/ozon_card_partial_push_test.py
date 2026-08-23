"""tools/ozon_card_partial_push_test.py — поток: card. ОПЫТ, не автомат.

Вопрос, который проверяем: лечится ли «На доработку» (PARTIAL_APPROVED) той же повторной
отправкой, что вылечила класс A? Гипотеза Сергея 23.08.2026 — да, большинство замечаний
снимается повторной отправкой карточки на модерацию.

Механика ровно та же, что у дожимателя: один безопасный атрибут отправляется обратно
с ЕГО ЖЕ текущим значением. Контент не меняется ни на байт.

Замер честный: снимок кодов замечаний ДО отправки → отправка → выдержка → перечитка →
сравнение множеств кодов. Успех = конкретное замечание исчезло, карточка осталась в продаже
и не ушла на повторную модерацию.

Три группы (по 5 карточек, берём карточки ровно одной группы — чтобы замер был чистым):
  models  — «Совместимые модели принтеров» (attribute_hierarchy_fail / out_of_range)
  marking — «Нужен код маркировки» (warning_attribute_values_empty)
  pics    — картинки (pics_http_error / some_image_failed / cover_unprocessed)

  ./venv/bin/python tools/ozon_card_partial_push_test.py            # сухой: только выборка
  ./venv/bin/python tools/ozon_card_partial_push_test.py --apply    # отправка и замер
"""
import sys
import csv
import time
import argparse

import requests

sys.path.insert(0, "/opt/mp-analytics")
from collectors.ozon import _headers, PRODUCT_LIST_URL, PRODUCT_INFO_URL  # noqa: E402
from tools.ozon_card_push import _send, _task_status, _log, SETTLE        # noqa: E402

GROUPS = {
    "models": {"attribute_hierarchy_fail", "warning_attribute_values_out_of_range"},
    "marking": {"warning_attribute_values_empty"},
    "pics": {"pics_http_error", "some_image_failed", "cover_unprocessed"},
    "dupes": {"double_without_merger_offer"},
    "erased": {"erased_attribute_value"},
}
SELLING = ("Продается", "Готов к продаже")
PAUSE = 1.5


def _pids(H):
    out, last = [], ""
    while True:
        r = requests.post(PRODUCT_LIST_URL, headers=H, timeout=120,
                          json={"filter": {"visibility": "PARTIAL_APPROVED"},
                                "last_id": last, "limit": 1000})
        r.raise_for_status()
        res = r.json()["result"]
        items = res.get("items") or []
        out += [i["product_id"] for i in items]
        last = res.get("last_id") or ""
        if len(items) < 1000:
            return out


def _snapshot(H, pids):
    """product_id -> {codes, status_name, moderate, offer_id}."""
    snap = {}
    for i in range(0, len(pids), 500):
        r = requests.post(PRODUCT_INFO_URL, headers=H, timeout=180,
                          json={"product_id": pids[i:i + 500]})
        r.raise_for_status()
        for it in r.json().get("items", []):
            st = it.get("statuses") or {}
            snap[it.get("id")] = {
                "offer_id": it.get("offer_id"),
                "codes": {e.get("code", "") for e in (it.get("errors") or [])},
                "status_name": st.get("status_name"),
                "moderate": st.get("moderate_status"),
            }
    return snap


def pick(snap, per_group):
    """Карточки, попадающие РОВНО в одну группу — иначе не понять, что именно снялось."""
    chosen = {}
    for name, codes in GROUPS.items():
        pure = [p for p, d in snap.items()
                if d["codes"] & codes
                and not any(d["codes"] & c for g, c in GROUPS.items() if g != name)]
        mixed = [p for p, d in snap.items() if d["codes"] & codes and p not in pure]
        take = pure[:per_group]
        if len(take) < per_group:                    # чистых не хватило — добираем смешанными
            take += mixed[:per_group - len(take)]
        chosen[name] = take
    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="oz_acc1")
    ap.add_argument("--per-group", type=int, default=5)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="", help="через запятую: какие группы гонять")
    ap.add_argument("--out", default="docs/reports/ozon_partial_test2_2026-08-23.csv")
    a = ap.parse_args()

    H = _headers(a.account)
    before = _snapshot(H, _pids(H))
    chosen = pick(before, a.per_group)
    if a.only:
        keep = {x.strip() for x in a.only.split(',')}
        chosen = {g: v for g, v in chosen.items() if g in keep}
    for g, pids in chosen.items():
        print(f"{g}: выбрано {len(pids)}")
    if not a.apply:
        print("сухой прогон: ничего не отправлено (нужен --apply)")
        return

    sent = []
    for g, pids in chosen.items():
        for p in pids:
            oid = before[p]["offer_id"]
            attr, code, tid, err = _send(H, oid)
            status = _task_status(H, tid) if tid else "нет задачи"
            try:
                _log({"platform": "ozon", "account": a.account, "offer_id": oid,
                      "product_id": p, "rung": 1, "attr_id": attr, "http_code": code,
                      "task_id": tid, "task_status": status, "healed": None,
                      "note": f"partial-test:{g}"})
            except Exception as e:                                   # noqa: BLE001
                print("журнал не записался:", type(e).__name__, e)
            sent.append((g, p, oid, status, err or ""))
            time.sleep(PAUSE)
    print(f"отправлено {len(sent)}, задач imported: "
          f"{sum(1 for s in sent if s[3] == 'imported')}")

    print(f"выдержка {SETTLE} с перед перечиткой…")
    time.sleep(SETTLE)
    after = _snapshot(H, [s[1] for s in sent])

    rows, tally = [], {}
    for g, p, oid, status, err in sent:
        b, af = before[p], after.get(p)
        gone = sorted(b["codes"] - af["codes"]) if af else []
        new = sorted(af["codes"] - b["codes"]) if af else []
        target_gone = bool(af) and not (af["codes"] & GROUPS[g])
        t = tally.setdefault(g, {"n": 0, "ok": 0, "left_partial": 0, "lost_sale": 0})
        t["n"] += 1
        t["ok"] += target_gone
        t["left_partial"] += (af is None)          # карточки нет в PARTIAL — вышла из списка
        t["lost_sale"] += bool(af) and af["status_name"] not in SELLING
        rows.append({"группа": g, "offer_id": oid, "task": status,
                     "было": " ".join(sorted(b["codes"])),
                     "стало": " ".join(sorted(af["codes"])) if af else "нет в PARTIAL",
                     "снялось": " ".join(gone), "добавилось": " ".join(new),
                     "статус_после": af["status_name"] if af else "",
                     "модерация_после": af["moderate"] if af else ""})

    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("группа | отправлено | замечание снялось | вышла из PARTIAL | ушла из продажи")
    for g, t in tally.items():
        print(f"  {g:8} {t['n']:6} {t['ok']:14} {t['left_partial']:12} {t['lost_sale']:12}")
    print("файл:", a.out)


if __name__ == "__main__":
    main()
