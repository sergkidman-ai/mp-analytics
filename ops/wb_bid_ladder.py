#!/usr/bin/env python3
# поток: mkt
"""ops/wb_bid_ladder.py — лестница ставок WB acc1: +10 % через день по «кору» с гейтом по ДРР.

Программа согласована Сергеем 2026-08-07: ведём кор шагами +10 % ближайшие 2 недели, ДРР не выше 10 %.

Контракт записи (подтверждён 2026-08-04): PATCH {WB_ADS_HOST}/api/advert/v1/bids
  {"bids":[{"advert_id":ADV,"nm_bids":[{"nm_id":NM,"bid_kopecks":V,"placement":"search"}]}]}
Геттера текущей ставки у ВБ НЕТ → источник текущей = наш wb_bid_override (прошлый PATCH).

ГЕЙТЫ (проверяются ДО записи, любой сработавший снимает SKU с шага; аккаунтный — отменяет весь шаг):
  1. АККАУНТ: ДРР рекламы за окно > DRR_LIMIT → шаг НЕ выполняется совсем.
  2. SKU с выручкой: расход/выручка > DRR_LIMIT → заморозить (ставку не трогаем).
  3. SKU-костёр: расход ≥ BURN_RUB за окно при НУЛЕВОЙ выручке → заморозить.
  4. Потолок MAX_CPC и пол ВБ 7.3 ₽.
  5. МАРЖА (добавлен 07.08.2026): net_live < 0 → заморозить. Разгонять трафик на товар, который
     теряет деньги с каждой продажи, — усиление убытка, а не рост. ДРР этого не ловит: он меряет
     расход к ВЫРУЧКЕ и ничего не знает о себестоимости.
     Источник — mkt_margin_control за последний снимок (read-only).
     Заморозки по cogs_stale больше НЕТ: с 07.08.2026 витрина сама подменяет протухший FIFO живой
     закупкой, маржа по таким SKU честная (и ниже прежней) — морозить не за что, их судит гейт
     убытка наравне со всеми. Флаг остался пометкой о подмене и попадает в сводку.
  6. КАРАНТИН ПОСЛЕ ОТКАТА (добавлен 13.08.2026): SKU, которому `ops.wb_bid_lower` за последние
     WINDOW_DAYS дней снял ставку до пола, не поднимаем. Иначе лестница возвращает ставку через
     сутки и откат становится холостым ходом.

По умолчанию — DRY-RUN: считает и печатает, в ВБ НЕ пишет. Живая запись только с --apply
(правило: данные ≠ разрешение писать; --apply даётся под конкретный прогон).

Запуск:
  ./venv/bin/python -m ops.wb_bid_ladder --cohort a          # список на сегодня, dry-run
  ./venv/bin/python -m ops.wb_bid_ladder --cohort a --apply  # живая запись
  ./venv/bin/python -m ops.wb_bid_ladder --cohort all --apply
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
from reports.bid_policy import (WB_MARGIN_GATE, WB_MARGIN_FLOOR,  # noqa: E402
                                raise_allowed)

load_dotenv(BASE_DIR / ".env")

WB_ADS_HOST = "https://advert-api.wildberries.ru"
TOKEN_ENV = {"wb_acc1": "WB_TOKEN_ACC1", "wb_acc2": "WB_TOKEN_ACC2"}
FLOOR = 7.3          # пол ВБ для поиска
STEP = 1.10          # +10 % за шаг
MAX_CPC = 25.0       # потолок ставки, ₽
DRR_LIMIT = 0.10     # 10 % — граница Сергея
BURN_RUB = 100.0     # расход без единой выручки за окно → костёр
WINDOW_DAYS = 7      # окно оценки ДРР
# Пороги маржи — из общей политики reports/bid_policy.py (одна на страницу «Ставки» и на лестницу):
#   WB_MARGIN_GATE 25% — KPI: между ним и полом ставку поднимаем, но считаем и показываем в сводке;
#   WB_MARGIN_FLOOR 15% — жёсткий пол (Сергей, 07.08.2026): ниже НЕ поднимаем, реклама съест остаток.
OUT_DIR = BASE_DIR / "reports" / "data"


def _cohort_nms(account, cohort):
    """a — 57 SKU первого подъёма 04.08 (идут на шаг впереди); all — весь кор из оверрайдов."""
    if cohort == "a":
        return {r["nm_id"] for r in db.query(
            "SELECT DISTINCT nm_id FROM wb_bid_log WHERE applied AND ts::date = '2026-08-04'")}
    return {r["nm_id"] for r in db.query(
        "SELECT nm_id FROM wb_bid_override WHERE account=%s", (account,))}


def _drr(account, nms, days=WINDOW_DAYS):
    """ДРР за окно: по аккаунту (весь рекламный слой) и по каждому SKU кора."""
    acc = db.query("""
      SELECT round(sum(spend)) sp, round(sum(revenue)) rv
        FROM wb_ad_nm_daily WHERE account=%s AND dt > current_date - %s
    """, (account, days))[0]
    per = {r["nm_id"]: (float(r["sp"] or 0), float(r["rv"] or 0)) for r in db.query("""
      SELECT nm_id, sum(spend) sp, sum(revenue) rv FROM wb_ad_nm_daily
       WHERE account=%s AND dt > current_date - %s AND nm_id = ANY(%s::bigint[]) GROUP BY 1
    """, (account, days, list(nms)))}
    sp, rv = float(acc["sp"] or 0), float(acc["rv"] or 0)
    return (sp / rv if rv else None), sp, rv, per


def _margin(account, nms):
    """Маржа по последнему снимку контроля маржи: {nm: (net_live, margin_own_live, cogs_stale)}.
    Read-only. Снимок берём последний доступный — если контроль сегодня не отработал, гейт
    считается по вчерашнему, но НЕ пропадает: молча поднимать ставки без маржи нельзя."""
    d = db.query("SELECT max(captured_date) d FROM mkt_margin_control WHERE account=%s", (account,))
    day = d[0]["d"] if d else None
    if not day:
        return {}, None
    return {r["nm_id"]: (float(r["net_live"]) if r["net_live"] is not None else None,
                         float(r["margin_own_live"]) if r["margin_own_live"] is not None else None,
                         r["cogs_stale"])
            for r in db.query("""
              SELECT nm_id, net_live, margin_own_live, cogs_stale FROM mkt_margin_control
               WHERE account=%s AND captured_date=%s AND nm_id = ANY(%s::bigint[])
            """, (account, day, list(nms)))}, day


def plan(account="wb_acc1", cohort="a"):
    """Считает шаг. Возвращает (rows, blocked_reason|None, stats)."""
    nms = _cohort_nms(account, cohort)
    if not nms:
        return [], "когорта пуста", {}
    marg, marg_day = _margin(account, nms)
    cur = {r["nm_id"]: (float(r["cpc"]), r["advert_id"]) for r in db.query(
        "SELECT nm_id, cpc, advert_id FROM wb_bid_override WHERE account=%s AND nm_id = ANY(%s::bigint[])",
        (account, list(nms)))}
    acc_drr, acc_sp, acc_rv, per = _drr(account, nms)

    # Карантин после отката (добавлен 13.08.2026). `ops.wb_bid_lower` снимает ставку до пола у
    # SKU, мёртвых и в рекламе, и в органике. Без этой проверки лестница на следующем же шаге
    # поднимет их обратно — откат превратится в холостой ход, а деньги продолжат гореть.
    # Срок карантина = длина окна замера: раньше просто нечем оценить, изменилось ли что-то.
    cooled = {r["nm_id"] for r in db.query(
        "SELECT DISTINCT nm_id FROM wb_bid_log WHERE account=%s AND author='lower' AND applied "
        "AND ts >= now() - make_interval(days => %s)", (account, WINDOW_DAYS))}

    rows, watch, n_substituted = [], 0, 0
    frozen = {"drr": 0, "burn": 0, "cap": 0, "no_adv": 0, "loss": 0, "no_margin": 0,
              "low_margin": 0, "cooldown": 0}
    for nm, (cpc, adv) in sorted(cur.items()):
        if nm in cooled:             # только что откатили на пол — держим карантин
            frozen["cooldown"] += 1
            continue
        sp, rv = per.get(nm, (0.0, 0.0))
        net_l, marg_l, stale = marg.get(nm, (None, None, False))
        if net_l is None:            # маржи нет: карточка не в продаже либо нет живой закупки
            frozen["no_margin"] += 1
            continue
        if net_l < 0:                # убыточная штука — трафик только усилит убыток
            frozen["loss"] += 1
            continue
        if stale:                    # себест подменён живой закупкой — маржа честная, не морозим
            n_substituted += 1
        ok, why, below_kpi = raise_allowed(marg_l)
        if not ok:                   # ниже пола 15% — дороже реклама эту маржу просто съест
            frozen["low_margin"] += 1
            continue
        if below_kpi:
            watch += 1               # между полом и KPI: не блокируем, но считаем и показываем
        if rv > 0 and sp / rv > DRR_LIMIT:
            frozen["drr"] += 1
            continue
        if rv == 0 and sp >= BURN_RUB:
            frozen["burn"] += 1
            continue
        if not adv:
            frozen["no_adv"] += 1
            continue
        new = round(max(cpc, FLOOR) * STEP, 2)
        if new > MAX_CPC:
            frozen["cap"] += 1
            continue
        rows.append({"nm_id": nm, "advert_id": adv, "old_cpc": round(cpc, 2), "new_cpc": new,
                     "spend_7d": round(sp, 2), "revenue_7d": round(rv, 2),
                     "drr_7d": (round(100 * sp / rv, 1) if rv else None),
                     "net_live": net_l, "margin_live": marg_l})

    # Нет снимка маржи — шаг НЕ выполняется. Маржа решающий фактор; отсутствие данных о ней
    # это не «гейт не сработал», а «проверить нечем» → вслепую ставки не поднимаем.
    blocked = None if marg else ("контроль маржи пуст — гейт по марже проверить нечем, "
                                 "шаг отменён (вслепую ставки не поднимаем)")
    if acc_drr is not None and acc_drr > DRR_LIMIT:
        blocked = (f"ДРР аккаунта за {WINDOW_DAYS} дн = {100*acc_drr:.1f}% > {100*DRR_LIMIT:.0f}% "
                   f"(расход {acc_sp:.0f} ₽ / выручка {acc_rv:.0f} ₽) — шаг отменён")
    stats = {"cohort_size": len(nms), "to_raise": len(rows), "frozen": frozen,
             "margin_day": (marg_day.isoformat() if marg_day else None),
             "margin_known": len(marg), "watch_below_kpi": watch,
             "cogs_substituted": n_substituted,
             "acc_drr": (round(100 * acc_drr, 1) if acc_drr is not None else None),
             "acc_spend": acc_sp, "acc_revenue": acc_rv,
             "avg_old": (round(sum(r["old_cpc"] for r in rows) / len(rows), 2) if rows else None),
             "avg_new": (round(sum(r["new_cpc"] for r in rows) / len(rows), 2) if rows else None)}
    return rows, blocked, stats


def _patch_group(token, advert_id, nm_cpcs):
    body = {"bids": [{"advert_id": int(advert_id),
                      "nm_bids": [{"nm_id": int(nm), "bid_kopecks": round(float(c) * 100),
                                   "placement": "search"} for nm, c in nm_cpcs]}]}
    r = requests.patch(WB_ADS_HOST + "/api/advert/v1/bids", json=body, timeout=60,
                       headers={"Authorization": token, "Content-Type": "application/json"})
    try:
        return r.status_code, r.json(), body
    except Exception:
        return r.status_code, {"_text": r.text[:500]}, body


def _log(rows_):
    cols = list(rows_[0].keys())
    db.execute(f"INSERT INTO wb_bid_log ({', '.join(cols)}) VALUES " +
               ", ".join(["(" + ", ".join(["%s"] * len(cols)) + ")"] * len(rows_)),
               [v for r in rows_ for v in (r[c] for c in cols)])


def apply_step(account, rows, note):
    """Живая запись. Группируем по кампании: один PATCH на advert_id."""
    token = os.getenv(TOKEN_ENV[account], "")
    if not token:
        raise RuntimeError(f"{TOKEN_ENV[account]} не задан")
    by_adv = {}
    for r in rows:
        by_adv.setdefault(r["advert_id"], []).append((r["nm_id"], r["new_cpc"]))
    ok = bad = 0
    logs = []
    now = datetime.datetime.now()
    for adv, pairs in by_adv.items():
        status, resp, req = _patch_group(token, adv, pairs)
        # ВБ отклоняет ВЕСЬ батч, если хоть одна номенклатура выпала из кампании
        # («nomenclature N not found in advert»). Один протухший advert_id в снимке статистики
        # уносил с собой всю кампанию: 10.08 так потерялось 28 товаров, из них 24 живые.
        # Поэтому на 400 разбираем кампанию поштучно и сохраняем всё, что реально в ней есть.
        if status == 400 and len(pairs) > 1:
            print(f"  [!] кампания {adv}: батч отклонён, разбираю поштучно ({len(pairs)})", flush=True)
            alive = []
            for nm, cpc in pairs:
                st1, resp1, _ = _patch_group(token, adv, [(nm, cpc)])
                if st1 == 200:
                    alive.append((nm, cpc))
                else:
                    print(f"      [!] {nm}: HTTP {st1} {str(resp1)[:110]}", flush=True)
                time.sleep(0.35)
            for nm, cpc in pairs:
                old = next(r["old_cpc"] for r in rows if r["nm_id"] == nm)
                hit = (nm, cpc) in alive
                logs.append({"account": account, "nm_id": nm, "advert_id": adv, "action": "api_set",
                             "applied": hit, "old_cpc": old, "new_cpc": cpc,
                             "old_source": "api_set", "author": "ladder", "note": note})
                ok, bad = (ok + 1, bad) if hit else (ok, bad + 1)
            if alive:
                db.upsert("wb_bid_override",
                          [{"account": account, "nm_id": nm, "cpc": cpc, "source": "api_set",
                            "advert_id": adv, "note": note, "author": "ladder", "updated_at": now}
                           for nm, cpc in alive],
                          conflict_cols=["account", "nm_id"],
                          update_cols=["cpc", "source", "advert_id", "note", "author", "updated_at"])
            time.sleep(0.5)
            continue
        applied = status == 200
        for nm, cpc in pairs:
            old = next(r["old_cpc"] for r in rows if r["nm_id"] == nm)
            logs.append({"account": account, "nm_id": nm, "advert_id": adv, "action": "api_set",
                         "applied": applied, "old_cpc": old, "new_cpc": cpc,
                         "old_source": "api_set", "author": "ladder", "note": note})
            if applied:
                ok += 1
            else:
                bad += 1
        if applied:
            db.upsert("wb_bid_override",
                      [{"account": account, "nm_id": nm, "cpc": cpc, "source": "api_set",
                        "advert_id": adv, "note": note, "author": "ladder", "updated_at": now}
                       for nm, cpc in pairs],
                      conflict_cols=["account", "nm_id"],
                      update_cols=["cpc", "source", "advert_id", "note", "author", "updated_at"])
        else:
            print(f"  [!] кампания {adv}: HTTP {status} {str(resp)[:160]}", flush=True)
        time.sleep(0.5)
    if logs:
        _log(logs)
    return ok, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", default="wb_acc1")
    ap.add_argument("--cohort", default="a", choices=["a", "all"])
    ap.add_argument("--apply", action="store_true", help="живая запись в ВБ (иначе dry-run)")
    ap.add_argument("--notify", action="store_true", help="итог шага — в телеграм Сергею")
    ap.add_argument("--note", default=None)
    a = ap.parse_args()

    rows, blocked, st = plan(a.account, a.cohort)
    today = datetime.date.today().isoformat()
    note = a.note or f"ladder +10% {today} cohort={a.cohort}"
    print(f"[лестница {a.cohort}] когорта {st.get('cohort_size')} SKU | к подъёму {st.get('to_raise')} | "
          f"заморожено ДРР {st['frozen']['drr']} костёр {st['frozen']['burn']} потолок {st['frozen']['cap']} "
          f"без кампании {st['frozen']['no_adv']} УБЫТОК {st['frozen']['loss']} "
          f"без маржи {st['frozen']['no_margin']} "
          f"карантин после отката {st['frozen']['cooldown']}")
    print(f"  гейт маржи: снимок {st.get('margin_day')}, маржа известна по {st.get('margin_known')} SKU когорты; "
          f"ниже пола {WB_MARGIN_FLOOR:.0f}% заморожено {st['frozen']['low_margin']}; "
          f"из поднимаемых ниже KPI {WB_MARGIN_GATE:.0f}%: {st.get('watch_below_kpi')} (не блокируем); "
          f"себест подменён живой закупкой у {st.get('cogs_substituted')}")
    print(f"  ставка {st.get('avg_old')} → {st.get('avg_new')} ₽ | ДРР аккаунта {WINDOW_DAYS} дн: "
          f"{st.get('acc_drr')}% (расход {st.get('acc_spend'):.0f} ₽ / выручка {st.get('acc_revenue'):.0f} ₽)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"bid_ladder_{today}_{a.cohort}.json"
    out.write_text(json.dumps({"rows": rows, "stats": st, "blocked": blocked}, ensure_ascii=False))
    print(f"  список → {out}")

    def notify(text):
        if a.notify:
            from ops.wb_daily_report import send
            send(text)

    if blocked:
        print(f"  СТОП: {blocked}")
        notify(f"*Лестница ставок ВБ — шаг {today} ОТМЕНЁН*\n{blocked}\nСтавки не менялись.")
        return 1
    if not a.apply:
        print("  DRY-RUN: в ВБ ничего не отправлено (для записи нужен --apply)")
        return 0
    ok, bad = apply_step(a.account, rows, note)
    print(f"  ЗАПИСАНО в ВБ: успешно {ok}, отказано {bad}")
    fr = st["frozen"]
    notify(f"*Лестница ставок ВБ — шаг {today} (+10 %)*\n"
           f"Поднято *{ok}* SKU: {st['avg_old']} → *{st['avg_new']} ₽*"
           + (f", отказано {bad}" if bad else "") + "\n"
           f"Заморожено: ДРР {fr['drr']} · костры {fr['burn']} · потолок {fr['cap']} · "
           f"убыток {fr['loss']} · без маржи {fr['no_margin']} · "
           f"карантин после отката {fr['cooldown']}\n"
           f"Заморожено ниже пола {WB_MARGIN_FLOOR:.0f}% маржи: {fr['low_margin']}\n"
           f"Из поднятых ниже KPI {WB_MARGIN_GATE:.0f}% маржи: {st.get('watch_below_kpi')}\n"
           f"ДРР аккаунта за {WINDOW_DAYS} дн: {st['acc_drr']}% (граница {100*DRR_LIMIT:.0f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
