# поток: mkt
"""Заводка утверждённого списка кандидатов в рекламу ВБ — обход всех живых кампаний.

ЗАЧЕМ ОТДЕЛЬНО ОТ `wb_ad_enroll`. Тот модуль заводит в ОДНУ кампанию, заданную `--advert-id`,
и опирался на оракул `real_count()`. Оракул умер: 07.09.2026 ВБ убрал из текста отказа
«advert X would have N nomenclatures» и теперь отвечает сухим «number of added nomenclatures
in advert exceed limit». Значит узнать, где есть место, можно ТОЛЬКО попыткой добавления.

МЕХАНИКА (дёшево по запросам). У кампании нет геттера состава, зато отказ атомарен: при
«exceed limit» не заводится ничего. Поэтому на каждую кампанию сначала идёт проба ОДНОЙ
карточкой из утверждённого списка:
  · отказ «exceed limit» → мест нет вообще, кампания забита (1 запрос на кампанию);
  · HTTP 200 → карточка заведена (это и есть цель), дальше добираем партиями с делением
    пополам 49 → 24 → 12 → 6 → 3 → 1, пока ВБ не откажет на единице.
Проба не «холостая»: её материал — те самые карточки, которые велено завести.

ГРАНИЦЫ. Список кандидатов берётся ФАЙЛОМ (утверждён человеком), сам не пересобирается.
Наличие перепроверяется живой витриной покупателя — карточку без остатка не заводим.
Категория: ВБ не примет чужой предмет, такой отказ просто уводит к следующей кампании.
Каждое успешное добавление пишется в docs/reports/mkt_ad_enroll_journal.jsonl.

  ./venv/bin/python -m ops.wb_ad_enroll_sweep --account wb_acc2 --file <csv>          # dry-run
  ./venv/bin/python -m ops.wb_ad_enroll_sweep --account wb_acc2 --file <csv> --apply
"""
import os
import sys
import csv
import json
import time
import argparse
import datetime
import pathlib

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from ops.wb_ad_enroll import (WB_ADS_HOST, CAMPAIGN_CAP, JOURNAL, _token,  # noqa: E402
                              live_stock, eligible_nms, body_for, known_in_campaign)

PAUSE = 21          # пауза между записями в /adv/v0/auction/nms (лимит контура)


def active_campaigns(account):
    """Все кампании аккаунта со статусом 9 (идёт) — единственный рабочий геттер списка."""
    r = requests.get(WB_ADS_HOST + "/adv/v1/promotion/count",
                     headers={"Authorization": _token(account)}, timeout=30)
    r.raise_for_status()
    out = []
    for g in r.json().get("adverts") or []:
        if g.get("status") != 9:
            continue
        for a in g.get("advert_list") or []:
            out.append(int(a["advertId"]))
    return out


def add(account, advert_id, nms):
    """Одно добавление. Возвращает (код, текст)."""
    r = requests.patch(WB_ADS_HOST + "/adv/v0/auction/nms",
                       headers={"Authorization": _token(account),
                                "Content-Type": "application/json"},
                       json=body_for(advert_id, add=list(nms)), timeout=60)
    return r.status_code, r.text[:200].replace("\n", " ")


def journal(account, advert_id, nms, code, txt):
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                            "account": account, "advert_id": advert_id, "nms": list(nms),
                            "http": code, "resp": txt[:300]}, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="wb_acc2")
    ap.add_argument("--file", required=True, help="csv утверждённых кандидатов (колонка nm_id)")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    with open(a.file, encoding="utf-8") as f:
        want = [int(row["nm_id"]) for row in csv.DictReader(f, delimiter=";")]
    print(f"утверждённый список: {len(want)} карточек", flush=True)

    subj_of = eligible_nms(a.account)
    not_eligible = [n for n in want if n not in subj_of]
    want = [n for n in want if n in subj_of]
    print(f"  ВБ считает доступными для кампаний: {len(want)}"
          f" (недоступны {len(not_eligible)})", flush=True)

    stock = live_stock(want)
    no_stock = [n for n in want if not stock.get(n)]
    want = [n for n in want if stock.get(n)]
    print(f"  с живым остатком: {len(want)} (без остатка {len(no_stock)})", flush=True)
    if not want:
        return

    camps = active_campaigns(a.account)
    # кампании с нашей статистикой идут первыми: у них известен предмет и они реально крутятся
    with_stat = {c: known_in_campaign(a.account, c) for c in camps}
    camps.sort(key=lambda c: (not with_stat[c], c))
    subj_want = {}
    for n in want:
        subj_want.setdefault(subj_of[n], []).append(n)
    print(f"живых кампаний: {len(camps)} · предметы кандидатов: "
          f"{ {s: len(v) for s, v in subj_want.items()} }", flush=True)

    if not a.apply:
        print("\nDRY-RUN. В ВБ не отправлено ничего.")
        print("План: по каждой живой кампании проба 1 карточкой, при HTTP 200 добор партиями.")
        print(f"Первые кампании обхода: {camps[:10]}")
        print(f"Первые карточки к заводке: {want[:10]}")
        return

    left = list(want)
    placed, full, other = {}, 0, 0
    for c in camps:
        if not left:
            break
        code, txt = add(a.account, c, left[:1])
        time.sleep(PAUSE)
        if code == 429:
            time.sleep(PAUSE)
            code, txt = add(a.account, c, left[:1])
            time.sleep(PAUSE)
        if code != 200:
            if "exceed limit" in txt:
                full += 1
            else:
                other += 1
                print(f"  {c}: HTTP {code} | {txt[:110]}", flush=True)
            continue
        journal(a.account, c, left[:1], code, txt)
        placed.setdefault(c, []).extend(left[:1])
        left = left[1:]
        print(f"  {c}: место ЕСТЬ, заведена 1 → добираю", flush=True)
        n = min(len(left), CAMPAIGN_CAP - 1)
        while left and n >= 1:
            code, txt = add(a.account, c, left[:n])
            time.sleep(PAUSE)
            if code == 200:
                journal(a.account, c, left[:n], code, txt)
                placed[c].extend(left[:n])
                left = left[n:]
                print(f"  {c}: +{n} (осталось завести {len(left)})", flush=True)
                n = min(len(left), CAMPAIGN_CAP - 1)
            elif code == 429:
                time.sleep(PAUSE)
            elif "exceed limit" in txt:
                n //= 2
            else:
                print(f"  {c}: стоп на добора — HTTP {code} | {txt[:110]}", flush=True)
                break

    print(f"\nИТОГ: заведено {sum(len(v) for v in placed.values())} карточек "
          f"в {len(placed)} кампаний; забитых кампаний {full}, прочих отказов {other}; "
          f"осталось незаведённых {len(left)}", flush=True)
    for c, v in placed.items():
        print(f"  {c}: {len(v)}", flush=True)
    print("СОСТАВ ПРОВЕРИТЬ ОТДЕЛЬНО: ответ контура приходит из общего кэша ВБ.")


if __name__ == "__main__":
    main()
