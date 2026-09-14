#!/usr/bin/env python3
# поток: mkt
"""ops/wb_ad_campaign_new.py — создать новые аукционные кампании ВБ под утверждённый список.

ЗАЧЕМ. 11.09.2026 обход всех 103 живых кампаний acc2 показал: свободных мест НОЛЬ (47 забиты
под лимит 50, 40 — старые гиганты на 167–198 карточек, 16 не принимает эндпоинт). Завести
одобренные карточки в существующее нельзя физически — остаётся создать новые кампании.

КОНТРАКТ ВБ (сверен по OpenAPI 14.09.2026, не по памяти):
  POST /adv/v2/seacat/save-ad  {name, nms[<=50], bidType, paymentType, placementTypes}
    · bidType        manual — ставка задаётся по каждой карточке (так работает Рой), unified — одна
    · paymentType    cpc — за клик, при создании ВБ САМ ставит минимальную ставку; cpm — за показы
    · placementTypes только для manual; Рой пишет ставку с placement="search", поэтому и здесь search
  GET /adv/v0/start?id=N       запуск   · GET /adv/v1/budget?id=N   бюджет кампании
  POST /adv/v1/budget/deposit  пополнение

ПОЧЕМУ СОЗДАНИЕ ОТДЕЛЕНО ОТ ЗАПУСКА. Поля бюджета в теле создания НЕТ: создать кампанию —
бесплатно и обратимо. Деньги начинаются на запуске. Поэтому `--apply` только создаёт и
наполняет, а `--start` — отдельный флаг: сумму видно ДО того, как она потрачена (инвариант 8).

  ./venv/bin/python -m ops.wb_ad_campaign_new --account wb_acc2 --file <csv>            # dry-run
  ./venv/bin/python -m ops.wb_ad_campaign_new --account wb_acc2 --file <csv> --apply
  ./venv/bin/python -m ops.wb_ad_campaign_new --account wb_acc2 --budget                # что просит ВБ
"""
import os
import sys
import csv
import json
import time
import argparse
import datetime
import pathlib
import collections

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from ops.wb_ad_enroll import WB_ADS_HOST, CAMPAIGN_CAP, _token, live_stock, eligible_nms  # noqa: E402

JOURNAL = BASE_DIR / "docs" / "reports" / "mkt_ad_campaign_journal.jsonl"
PAUSE = 13


def note(account, action, payload, code, txt):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                            "account": account, "action": action, "payload": payload,
                            "http": code, "resp": str(txt)[:300]}, ensure_ascii=False) + "\n")


def create(account, name, nms):
    body = {"name": name, "nms": [int(n) for n in nms],
            "bidType": "manual", "paymentType": "cpc", "placementTypes": ["search"]}
    r = requests.post(WB_ADS_HOST + "/adv/v2/seacat/save-ad", json=body, timeout=60,
                      headers={"Authorization": _token(account), "Content-Type": "application/json"})
    note(account, "create", body, r.status_code, r.text)
    return r.status_code, r.text[:300].replace("\n", " ")


def campaign_budget(account, advert_id):
    r = requests.get(WB_ADS_HOST + "/adv/v1/budget", params={"id": int(advert_id)},
                     headers={"Authorization": _token(account)}, timeout=30)
    return r.status_code, r.text[:200].replace("\n", " ")


def deposit(account, advert_id, rub, src=0):
    """Пополнить бюджет кампании. ЭТО ДЕНЬГИ.

    src — откуда берём. ПРОВЕРЕНО 14.09.2026 живым пополнением: 1 = деньги со «Счёта»
    (`/adv/v1/balance` → net; после трёх пополнений net 53 654 → 50 654). Документация ВБ
    называет 0 «счётом», но с 0 приходит «insufficient funds» при полном счёте.
    """
    body = {"sum": int(rub), "type": int(src), "return": True}
    r = requests.post(WB_ADS_HOST + "/adv/v1/budget/deposit", params={"id": int(advert_id)},
                      json=body, timeout=60,
                      headers={"Authorization": _token(account), "Content-Type": "application/json"})
    note(account, "deposit", {"id": int(advert_id), **body}, r.status_code, r.text)
    return r.status_code, r.text[:200].replace("\n", " ")


def start(account, advert_id):
    r = requests.get(WB_ADS_HOST + "/adv/v0/start", params={"id": int(advert_id)},
                     headers={"Authorization": _token(account)}, timeout=30)
    note(account, "start", {"id": int(advert_id)}, r.status_code, r.text)
    return r.status_code, r.text[:200].replace("\n", " ")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="wb_acc2")
    ap.add_argument("--file", help="csv утверждённых кандидатов (колонка nm_id)")
    ap.add_argument("--prefix", default="Рой", help="префикс имени кампании")
    ap.add_argument("--apply", action="store_true", help="создать кампании (денег не стоит)")
    ap.add_argument("--start", type=int, nargs="*", help="запустить кампании по id — ЭТО ДЕНЬГИ")
    ap.add_argument("--budget", type=int, nargs="*", help="показать бюджет кампаний по id")
    ap.add_argument("--deposit", type=int, nargs="*", help="пополнить кампании по id — ЭТО ДЕНЬГИ")
    ap.add_argument("--sum", type=int, default=1000, help="сколько ₽ класть на каждую (--deposit)")
    ap.add_argument("--src", type=int, default=1, help="источник: 1 = Счёт (проверено), 3 бонусы")
    a = ap.parse_args()

    if a.budget is not None:
        bal = requests.get(WB_ADS_HOST + "/adv/v1/balance",
                           headers={"Authorization": _token(a.account)}, timeout=30)
        print(f"баланс кабинета {a.account}: {bal.text[:120]}")
        for i in a.budget:
            print(f"  кампания {i}: {campaign_budget(a.account, i)}")
        return

    if a.deposit:
        for i in a.deposit:
            print(f"  пополнение {i} на {a.sum} ₽: {deposit(a.account, i, a.sum, a.src)}", flush=True)
            time.sleep(PAUSE)
        return

    if a.start:
        for i in a.start:
            print(f"  запуск {i}: {start(a.account, i)}", flush=True)
            time.sleep(PAUSE)
        return

    if not a.file:
        sys.exit("нужен --file со списком, --budget или --start")
    with open(a.file, encoding="utf-8") as f:
        want = [int(r["nm_id"]) for r in csv.DictReader(f, delimiter=";")]
    subj = eligible_nms(a.account)
    want = [n for n in want if n in subj]
    stock = live_stock(want)
    want = [n for n in want if stock.get(n)]
    by_subj = collections.defaultdict(list)
    for n in want:
        by_subj[subj[n]].append(n)
    print(f"{a.account}: к заводке {len(want)} карточек, предметы "
          f"{ {s: len(v) for s, v in by_subj.items()} }")

    # ВБ требует единую категорию внутри кампании; предмет-одиночка отдельной кампании не стоит
    plan = []
    today = datetime.date.today().strftime("%d.%m")
    for s, nms in sorted(by_subj.items(), key=lambda kv: -len(kv[1])):
        if len(nms) < 5:
            print(f"  предмет {s}: всего {len(nms)} карточек — отдельную кампанию не завожу")
            continue
        for i in range(0, len(nms), CAMPAIGN_CAP):
            chunk = nms[i:i + CAMPAIGN_CAP]
            plan.append((f"{a.prefix} {today} предмет {s} №{len(plan) + 1}", chunk))
    print(f"план: {len(plan)} кампаний по ≤{CAMPAIGN_CAP} карточек")
    for name, chunk in plan:
        print(f"  «{name}» — {len(chunk)} карточек")
    if not a.apply:
        print("\nDRY-RUN. В ВБ не отправлено ничего. Создание: --apply (денег не стоит, "
              "кампания родится остановленной; запуск — отдельным --start)")
        return

    for name, chunk in plan:
        code, txt = create(a.account, name, chunk)
        print(f"  «{name}»: HTTP {code} | {txt}", flush=True)
        time.sleep(PAUSE)
    print(f"\nсоздано по журналу {JOURNAL.name}; кампании СТОЯТ — запуск отдельным --start")


if __name__ == "__main__":
    main()
