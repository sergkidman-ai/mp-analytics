#!/usr/bin/env python3
# поток: mkt
"""ops/wb_ad_enroll.py — заводчик номенклатур: довести товар, который снова можно купить,
до рекламной кампании.

ЗАЧЕМ. У нас замкнуты три из четырёх звеньев цикла и разорвано последнее.
  1. Товар пропадает из наличия  → 25.08 `ops/wb_stock_guard.py` снимает ставку до пола,
     чтобы не платить за клики по карточке, которую нельзя купить.
  2. Товар возвращается         → тот же сторож возвращает ровно ту ставку, с которой опустил.
  3. Товар появляется ВПЕРВЫЕ или после долгого отсутствия → `ops/tc_price_alert.py` ловит момент,
     когда у TheCartridge появилась цена («вышла из сумрака», журнал `mkt_tc_resurfaced`),
     и шлёт телеграм с уже посчитанной маржой.
  4. …и на этом сигнал УМИРАЕТ. Карточки нет ни в одной кампании, ставки у неё нет, показов нет.
     Сторож её не поднимет — он умеет только вернуть свою же ставку, а заводить в рекламу не умеет.
Этот модуль закрывает четвёртое звено: берёт карточки с доказанным спросом и живым наличием,
которых сейчас НЕТ в рекламе, и заводит их в кампанию. Иначе «вышла из сумрака» остаётся
уведомлением, на которое никто не нажимает: замер 25.08 — 98 таких карточек на acc1.

ОТБОР (все условия одновременно):
  · карточку можно купить прямо сейчас — живой `totalQuantity` с card.wb.ru, не витрина `wb_stocks`
    (она FBO-only и врёт про ФБС-товар);
  · спрос доказан — продажи с 01.06 ИЛИ запись в `mkt_tc_resurfaced`;
  · маржа не ниже пола `WB_MARGIN_FLOOR` (15 %) — ниже реклама съедает остаток;
  · карточки нет ни в `wb_ad_nm` за текущий период, ни в `wb_bid_override`;
  · предмет карточки доступен для кампаний — проверяем у ВБ (`/adv/v2/supplier/nms`).

КОНТРАКТ ЗАПИСИ (разведка 25.08.2026, оба токена дают одинаковые коды):
  PATCH {WB_ADS_HOST}/adv/v0/auction/nms   — единственный живой метод правки состава кампании
                                             (Allow: PATCH; тело ещё не подтверждено, см. ниже)
  POST  /adv/v2/supplier/nms  тело [subjectId]  → 200, карточки, доступные для кампаний
  GET   /adv/v1/supplier/subjects               → 200, предметы с количеством
  POST  /adv/v2/seacat/save-ad                  → СОЗДАЁТ кампанию (name + nms), а НЕ правит
                                                  состав существующей. Здесь не используется.
  Все `/adv/v1|v2|v3/promotion/adverts` → 404: ГЕТТЕРА СОСТАВА КАМПАНИИ У ВБ НЕТ.

ЧЕГО МЫ НЕ ЗНАЕМ И ПОЧЕМУ ЭТО ВАЖНО:
  · Состав кампании прочитать нечем. Наша оценка (`wb_ad_nm` ∪ `wb_bid_override`) — это НИЖНЯЯ
    граница: карточка без показов в статистику не попадает. Поэтому лимит ВБ «не более 50
    номенклатур в аукционной кампании» мы проверить до отправки не можем и держим свой запас.
  · Ответы этого контура приходят с `origin: camp-api-public-cache`, и один раз пришёл ответ про
    ЧУЖУЮ кампанию 33614302, которой у нас нет. Значит текст ошибки не является доказательством
    того, что случилось с НАШЕЙ кампанией: после записи состав проверяем отдельно, по факту.
  · Точная форма тела PATCH не подтверждена: `{"advertId":…,"nms":[…]}` даёт
    «can not deserialize response body». Варианты перебираются флагом --probe, и он намеренно
    шлёт ЗАВЕДОМО СВЕРХЛИМИТНЫЙ список (>50), чтобы корректно разобранное тело всё равно было
    отклонено бизнес-правилом и ничего не изменило.

По умолчанию DRY-RUN: печатает отобранных и точное тело запроса, в ВБ не пишет.
Живая запись только с --apply и только под конкретный прогон (инвариант 6).

Запуск:
  ./venv/bin/python -m ops.wb_ad_enroll --account wb_acc2 --advert-id 35498721 --limit 5
  ./venv/bin/python -m ops.wb_ad_enroll --account wb_acc2 --advert-id 35498721 --limit 5 --apply
  ./venv/bin/python -m ops.wb_ad_enroll --account wb_acc2 --probe        # разведка формы тела
"""
import os
import sys
import json
import time
import argparse
import datetime
import pathlib

import requests
from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from reports.bid_policy import WB_MARGIN_FLOOR  # noqa: E402

load_dotenv(BASE_DIR / ".env")

WB_ADS_HOST = "https://advert-api.wildberries.ru"
TOKEN_ENV = {"wb_acc1": "WB_TOKEN_ACC1", "wb_acc2": "WB_TOKEN_ACC2"}
CAMPAIGN_CAP = 50          # лимит ВБ на номенклатуры в аукционной кампании
SAFETY_MARGIN = 5          # запас: состав кампании прочитать нечем, наша оценка — нижняя граница
V4 = "https://card.wb.ru/cards/v4/detail"
JOURNAL = BASE_DIR / "docs" / "reports" / "mkt_ad_enroll_journal.jsonl"


def _token(account):
    return os.environ[TOKEN_ENV[account]]


def live_stock(nms):
    """{nm_id: totalQuantity} — то, что видит покупатель. Молчание батча не читаем как «нет»."""
    out = {}
    for i in range(0, len(nms), 100):
        chunk = nms[i:i + 100]
        try:
            r = requests.get(V4, params={"appType": 1, "curr": "rub", "dest": -1257786, "spp": 30,
                                         "nm": ";".join(str(n) for n in chunk)}, timeout=45)
            for p in (r.json().get("products") or []):
                out[int(p["id"])] = p.get("totalQuantity", 0)
        except Exception as exc:
            print(f"  [!] батч {i // 100 + 1} не ответил: {str(exc)[:70]}", flush=True)
        time.sleep(0.25)
    return out


def eligible_nms(account):
    """Карточки, которые ВБ считает доступными для кампаний. Предметы берём у самого ВБ."""
    H = {"Authorization": _token(account)}
    subj = requests.get(WB_ADS_HOST + "/adv/v1/supplier/subjects", headers=H, timeout=30)
    subj.raise_for_status()
    ids = [s["id"] for s in subj.json() or []]
    r = requests.post(WB_ADS_HOST + "/adv/v2/supplier/nms", headers={**H, "Content-Type": "application/json"},
                      json=ids, timeout=60)
    r.raise_for_status()
    return {int(x["nm"]) for x in r.json() or []}


def candidates(account):
    """Спрос доказан, маржа выше пола, в рекламе не ведём. Наличие проверяется отдельно."""
    return db.query("""
      with adv as (select nm_id from wb_ad_nm
                    where account=%s and period=(select max(period) from wb_ad_nm where account=%s)
                   union select nm_id from wb_bid_override where account=%s),
           sold as (select article::bigint nm, sum(qty) o6, sum(revenue_buyer) s6,
                           sum(revenue_buyer) filter (where period_from>='2026-08-01') s8
                      from sales where platform='wb' and account=%s and period_from>='2026-06-01'
                       and article ~ '^[0-9]+$' and qty>0 group by 1),
           mc as (select distinct on (nm_id) nm_id, margin_own_live
                    from mkt_margin_control where account=%s order by nm_id, captured_date desc),
           rs as (select nm_id, first_price_on from mkt_tc_resurfaced where account=%s)
      select c.nm_id, min(c.vendor_code) vc, min(c.title) ttl,
             coalesce(max(s.o6),0) o6, coalesce(max(s.s6),0) s6, coalesce(max(s.s8),0) s8,
             max(mc.margin_own_live) mg, max(rs.first_price_on) resurf
        from wb_cards c
        left join sold s on s.nm=c.nm_id
        left join mc on mc.nm_id=c.nm_id
        left join rs on rs.nm_id=c.nm_id
       where c.account=%s and c.nm_id not in (select nm_id from adv)
         and (s.nm is not null or rs.nm_id is not null)
       group by c.nm_id""", (account, account, account, account, account, account, account))


def known_in_campaign(account, advert_id):
    """НИЖНЯЯ граница состава кампании: карточка без показов в статистику ВБ не попадает."""
    return {r["nm_id"] for r in db.query("""
        select nm_id from wb_ad_nm where account=%s and advert_id=%s
         union select nm_id from wb_bid_override where account=%s and advert_id=%s""",
        (account, advert_id, account, advert_id))}


def body_for(advert_id, nms):
    """Тело PATCH. Форма подтверждается флагом --probe; здесь — рабочая гипотеза."""
    return {"advertId": int(advert_id), "nms": [int(n) for n in nms]}


def probe_shapes(account, advert_id, pool):
    """Разведка формы тела. Во ВСЕХ пробах список ЗАВЕДОМО больше лимита кампании,
    поэтому корректно разобранное тело отклоняется бизнес-правилом и ничего не меняет."""
    over = [int(n) for n in list(pool)[:CAMPAIGN_CAP + 1]]
    assert len(over) > CAMPAIGN_CAP, "проба обязана быть сверхлимитной, иначе она может примениться"
    H = {"Authorization": _token(account), "Content-Type": "application/json"}
    shapes = [("advertId/nms", {"advertId": advert_id, "nms": over}),
              ("advert_id/nms", {"advert_id": advert_id, "nms": over}),
              ("nms объектами", {"advertId": advert_id, "nms": [{"nm": n} for n in over]}),
              ("nm_ids", {"advertId": advert_id, "nm_ids": over}),
              ("массив-обёртка", [{"advertId": advert_id, "nms": over}]),
              ("id/nms", {"id": advert_id, "nms": over})]
    for name, body in shapes:
        r = requests.patch(WB_ADS_HOST + "/adv/v0/auction/nms", headers=H, json=body, timeout=30)
        print(f"  {name:16} → HTTP {r.status_code} | {r.text[:120].replace(chr(10), ' ')}")
        time.sleep(13)      # у контура жёсткий лимит запросов


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', default='wb_acc2')
    ap.add_argument('--advert-id', type=int, help='кампания-приёмник')
    ap.add_argument('--limit', type=int, default=5, help='сколько карточек завести за прогон')
    ap.add_argument('--only-resurfaced', action='store_true', help='только «вышедшие из сумрака»')
    ap.add_argument('--apply', action='store_true', help='живая запись в ВБ')
    ap.add_argument('--probe', action='store_true', help='разведка формы тела, сверхлимитная')
    a = ap.parse_args()

    ok_nms = eligible_nms(a.account)
    print(f"{a.account}: ВБ считает доступными для кампаний {len(ok_nms)} карточек")

    if a.probe:
        if not a.advert_id:
            sys.exit("--probe требует --advert-id")
        print("РАЗВЕДКА ФОРМЫ ТЕЛА (все пробы сверхлимитные, применить ничего не могут):")
        probe_shapes(a.account, a.advert_id, ok_nms)
        return

    cand = [r for r in candidates(a.account) if r["nm_id"] in ok_nms]
    if a.only_resurfaced:
        cand = [r for r in cand if r["resurf"]]
    cand = [r for r in cand
            if r["mg"] is None or float(r["mg"]) >= WB_MARGIN_FLOOR]
    stock = live_stock([r["nm_id"] for r in cand])
    cand = [r for r in cand if stock.get(r["nm_id"])]
    cand.sort(key=lambda r: -float(r["s6"] or 0))
    print(f"  кандидатов после наличия/маржи/доступности: {len(cand)}")
    if not cand:
        print("  заводить некого")
        return

    if not a.advert_id:
        print("\nТОП кандидатов (кампания не задана — только список):")
        for r in cand[:a.limit]:
            print(f"  {r['nm_id']} {(r['vc'] or ''):<12} {(r['ttl'] or '')[:38]:<38} "
                  f"выручка {round(float(r['s6'] or 0)):>7} ₽ маржа "
                  f"{round(float(r['mg']),1) if r['mg'] is not None else '—'}"
                  f"{' · из сумрака' if r['resurf'] else ''}")
        return

    known = known_in_campaign(a.account, a.advert_id)
    room = CAMPAIGN_CAP - SAFETY_MARGIN - len(known)
    take = cand[:min(a.limit, max(room, 0))]
    print(f"\nКампания {a.advert_id}: знаем о {len(known)} номенклатурах (это нижняя граница), "
          f"запас до лимита {CAMPAIGN_CAP} с поправкой {SAFETY_MARGIN} → можно завести {max(room,0)}")
    if not take:
        print("  места нет — нужна другая кампания")
        return
    for r in take:
        print(f"  + {r['nm_id']} {(r['vc'] or ''):<12} {(r['ttl'] or '')[:38]:<38} "
              f"выручка {round(float(r['s6'] or 0)):>7} ₽"
              f"{' · из сумрака' if r['resurf'] else ''}")

    body = body_for(a.advert_id, [r["nm_id"] for r in take])
    print("\nТЕЛО ЗАПРОСА PATCH /adv/v0/auction/nms:")
    print(" ", json.dumps(body, ensure_ascii=False))
    if not a.apply:
        print("\nDRY-RUN. В ВБ не отправлено ничего. Живая запись: добавить --apply")
        return

    H = {"Authorization": _token(a.account), "Content-Type": "application/json"}
    r = requests.patch(WB_ADS_HOST + "/adv/v0/auction/nms", headers=H, json=body, timeout=60)
    print(f"\nОТВЕТ ВБ: HTTP {r.status_code} | {r.text[:200].replace(chr(10), ' ')}")
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                            "account": a.account, "advert_id": a.advert_id,
                            "nms": [r_["nm_id"] for r_ in take],
                            "http": r.status_code, "resp": r.text[:300]}, ensure_ascii=False) + "\n")
    print(f"записано в журнал {JOURNAL.name}")
    print("ПРОВЕРИТЬ СОСТАВ ОТДЕЛЬНО: ответ контура приходит из общего кэша ВБ и один раз "
          "приходил про чужую кампанию — доверять тексту ответа нельзя.")


if __name__ == '__main__':
    main()
