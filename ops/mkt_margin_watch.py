#!/usr/bin/env python3
# поток: mkt
"""Сторож обвала маржи: заметить, что за ночь экономика каталога переехала, и сказать вслух.

Зачем отдельный сторож, когда уже есть `ops/wb_promo_watch` (поток ev). Тот предупреждает
о СОБЫТИИ — «завтра стартует автоакция». Этот меряет ПОСЛЕДСТВИЕ — что акция (или наша
правка цен, или пересчёт себеста) сделала с маржой по факту. Между двумя вещами нет
однозначной связи: акция может пройти мимо нашего ассортимента, а обвал маржи может
случиться вообще без акции.

Что случилось 07.09.2026 и почему сторож написан. Ночью стартовала автоакция WB
«Бархатные скидки: выгодные предложения»: на acc2 цена срезана у 5 516 карточек из 9 816,
медиана нашей цены 4 580 → 2 924 ₽, и 1 199 карточек ушли в минус — то есть каждая восьмая
продажа стала убыточной. Узнали мы об этом через сутки и случайно, разбирая рост заказов.
Автоакция включает товар САМА, наш рычаг — только выход, и цена этого рычага растёт
с каждым днём молчания.

Как меряет. Берёт два последних `captured_date` в `mkt_margin_control`, сопоставляет
по nm_id (только карточки, присутствующие в ОБА дня — иначе смена состава витрины
читается как обвал) и считает четыре числа: сколько карточек перевернулось в минус,
сколько вернулось в плюс, у скольких наша цена срезана глубже порога, и куда уехала
медиана цены. Сообщение уходит, если сработал хоть один порог.

Пороги грубые нарочно. Сторож обязан молчать в обычные дни и кричать в ненормальные;
точная граница «сколько это в рублях» — работа человека, у сторожа её данных нет
(выручки в витрине нет, только цена и маржа на единицу).

Дедуп — файл `logs/mkt_margin_watch.json`: на один `captured_date` одно сообщение,
даже если крон дёрнет сторожа дважды (витрина пересобирается утром и днём).

    ./venv/bin/python -m ops.mkt_margin_watch --dry     # что бы отправил, без телеграма
    ./venv/bin/python -m ops.mkt_margin_watch           # боевой прогон (крон)
    ./venv/bin/python -m ops.mkt_margin_watch --force   # повторить, игнорируя дедуп
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv

load_dotenv("/opt/mp-analytics/.env")

import requests

from core.db import query

ACCOUNTS = ("wb_acc1", "wb_acc2")
MSK = timezone(timedelta(hours=3))                  # часы сервера UTC, человек живёт в МСК
STATE = Path("/opt/mp-analytics/logs/mkt_margin_watch.json")
REPORTS = Path("/opt/mp-analytics/docs/reports")

# Бот потока prc — @ds_prc_bot, тот же транспорт, что у сторожа остатков и сторожа акций.
TG_TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "").strip()
NOTIFY_IDS = [x.strip() for x in os.getenv("TG_PRC_NOTIFY_ID", "1031321444").split(",") if x.strip()]
TG_LIMIT = 3900

CUT_PCT = 10.0          # «наша цена срезана» — глубже этого, мельче считаем шумом округления
FLIP_MIN = 100          # карточек, перевернувшихся в минус за сутки — само по себе повод
CUT_SHARE_MIN = 20.0    # % когорты, у которых цена срезана глубже CUT_PCT
MEDIAN_MOVE_MIN = 15.0  # % сдвига медианы нашей цены в любую сторону
# Широкая переоценка без ущерба марже — обычный рабочий день (акция прошла мимо ассортимента,
# ВБ подвинул СПП). Кричать о ней незачем: пороги по цене срабатывают, только если
# экономика реально пострадала хотя бы у горстки карточек.
FLIP_FLOOR = 10

SQL = """
with a as (select nm_id, our_price op, margin_own_live m, vendor_code vc
             from mkt_margin_control where account=%(acc)s and captured_date=%(d0)s),
     b as (select nm_id, our_price op, margin_own_live m, vendor_code vc
             from mkt_margin_control where account=%(acc)s and captured_date=%(d1)s)
select count(*) n,
       count(*) filter (where b.m < 0 and a.m >= 0)                       flip_down,
       count(*) filter (where b.m >= 0 and a.m < 0)                       flip_up,
       count(*) filter (where b.m < 0)                                    neg_now,
       count(*) filter (where a.m < 0)                                    neg_was,
       count(*) filter (where a.op > 0 and b.op < a.op*(1-%(cut)s/100.0)) cut_n,
       percentile_cont(0.5) within group (order by a.op)                  op0,
       percentile_cont(0.5) within group (order by b.op)                  op1,
       percentile_cont(0.5) within group (order by
           case when b.m < 0 and a.m >= 0 and a.op > 0
                then 100.0*(1 - b.op/a.op) end)                           flip_cut
  from a join b using (nm_id)
"""

WORST = """
with a as (select nm_id, our_price op, margin_own_live m
             from mkt_margin_control where account=%(acc)s and captured_date=%(d0)s),
     b as (select nm_id, vendor_code vc, our_price op, margin_own_live m
             from mkt_margin_control where account=%(acc)s and captured_date=%(d1)s)
select b.nm_id, b.vc, a.op op0, b.op op1, a.m m0, b.m m1, o.cpc
  from a join b using (nm_id)
  left join wb_bid_override o on o.account=%(acc)s and o.nm_id=b.nm_id
 where b.m < 0 and a.m >= 0
 order by b.m asc
"""


def tg(text):
    """Сообщение Сергею. Сбой телеграма не должен ронять прогон, но и молчать о нём нельзя."""
    if not TG_TOKEN:
        return "нет TG_PRC_BOT_TOKEN"
    if not NOTIFY_IDS:
        return "адресатов нет"
    out = []
    for chat in NOTIFY_IDS:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              data={"chat_id": chat, "text": text[:TG_LIMIT],
                                    "disable_web_page_preview": "true"}, timeout=60)
            out.append(f"{chat}: " + ("ok" if r.ok else f"{r.status_code} {r.text[:120]}"))
        except Exception as exc:                       # сеть моргает чаще, чем ломается витрина
            out.append(f"{chat}: {type(exc).__name__}: {exc}")
    return "; ".join(out)


def rub(x):
    return f"{round(x or 0):,}".replace(",", " ")


def promos_today(acc, day):
    """Какие автоакции WB идут в этот день — чтобы сторож называл вероятную причину.

    Источник — кэш календаря из `ops/wb_promo_watch` (поток ev). Своего запроса в API здесь
    нет нарочно: нужен токен со скоупом «Цены и скидки», а сторож должен работать и без него.
    """
    try:
        rows = query("""select name, starts_at from wb_promo_notice
                         where account=%s and starts_at::date <= %s and ends_at::date >= %s
                         order by starts_at desc""", (acc, day, day))
    except Exception:
        return []
    seen, out = set(), []                 # acc1/acc2 держат один календарь под разными id
    for r in rows:
        if r["name"] in seen:
            continue
        seen.add(r["name"])
        out.append((r["name"], r["starts_at"].astimezone(MSK).date()))
    return out


def check(acc):
    days = [r["captured_date"] for r in query(
        """select distinct captured_date from mkt_margin_control
            where account=%s order by captured_date desc limit 2""", (acc,))]
    if len(days) < 2:
        return None, f"{acc}: в витрине меньше двух дней — сравнивать не с чем"
    d1, d0 = days[0], days[1]
    p = {"acc": acc, "d0": d0, "d1": d1, "cut": CUT_PCT}
    r = query(SQL, p)[0]
    if not r["n"]:
        return None, f"{acc}: когорта пустая ({d0} → {d1})"

    cut_share = 100.0 * r["cut_n"] / r["n"]
    move = 100.0 * (float(r["op1"]) / float(r["op0"]) - 1) if r["op0"] else 0.0
    triggers = []
    hurt = r["flip_down"] >= FLIP_FLOOR
    if r["flip_down"] >= FLIP_MIN:
        triggers.append(f"в минус ушли {r['flip_down']} карточек")
    if cut_share >= CUT_SHARE_MIN and hurt:
        triggers.append(f"цена срезана у {cut_share:.0f} % каталога")
    if abs(move) >= MEDIAN_MOVE_MIN and hurt:
        triggers.append(f"медиана цены {move:+.0f} %")
    if not triggers:
        return None, (f"{acc}: спокойно ({d0} → {d1}): в минус {r['flip_down']}, "
                      f"срезано {cut_share:.0f} %, медиана {move:+.0f} %")

    worst = query(WORST, p)
    paid = sum(1 for w in worst if w["cpc"])
    lines = [f"⚠️ {acc}: маржа обвалилась за сутки {d0} → {d1}",
             "",
             f"Перевернулись в минус: {r['flip_down']} (обратно в плюс {r['flip_up']})",
             f"Всего в минусе: {r['neg_was']} → {r['neg_now']} из {r['n']} сопоставленных",
             f"Наша цена срезана глубже {CUT_PCT:.0f} %: {r['cut_n']} ({cut_share:.0f} % каталога)",
             f"Медиана нашей цены: {rub(r['op0'])} → {rub(r['op1'])} ₽ ({move:+.0f} %)"]
    if r["flip_cut"] is not None:
        lines.append(f"Медианный срез у перевернувшихся: {float(r['flip_cut']):.0f} %")
    if paid:
        lines.append(f"Из них со ставкой в рекламе: {paid} — платим за клики по убыточным")

    pr = promos_today(acc, d1)
    if pr:
        lines += ["", "Идут автоакции WB (вероятная причина):"]
        lines += [f"  • {n} — с {s.strftime('%d.%m')}" for n, s in pr[:4]]
        lines.append("Автоакция режет цену сама; наш рычаг — только выход из неё.")
    else:
        lines += ["", "Активных автоакций в кэше нет — причина в наших ценах или в себесте."]

    if worst:
        lines += ["", "Худшие:"]
        for w in worst[:5]:
            lines.append(f"  {w['nm_id']} {w['vc'] or ''} {rub(w['op0'])}→{rub(w['op1'])} ₽, "
                         f"маржа {float(w['m1']):.0f} %")
        # ИМЯ С ВРЕМЕНЕМ (правка 11.09.2026). Витрина mkt_margin_control держит ОДНУ строку
        # на captured_date, и дневной прогон затирает ночную цену. 10.09 сторож в 10:07 видел
        # 938 переворотов, в 17:07 на той же паре дат — 22, и файл улик был переписан этими 22.
        # Теперь каждый прогон пишет свой файл; ночной срез больше не пропадает.
        path = REPORTS / f"mkt_margin_flip_{d1:%Y-%m-%d}_{acc}_{datetime.now(MSK):%H%M}.csv"
        with path.open("w", encoding="utf-8") as fh:
            fh.write("nm_id;vendor_code;price_was;price_now;margin_was;margin_now;cpc\n")
            for w in worst:
                fh.write(f"{w['nm_id']};{w['vc'] or ''};{w['op0']};{w['op1']};"
                         f"{w['m0']};{w['m1']};{w['cpc'] or ''}\n")
        lines += ["", f"Полный список: {path.relative_to('/opt/mp-analytics')}"]

    # ключ дедупа — ПАРА дат, а не только вчера: сменилась база сравнения — это новое событие
    return {"date": str(d1), "key": f"{d0}->{d1}",
            "text": "\n".join(lines), "why": "; ".join(triggers)}, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="показать сообщение, не отправлять")
    ap.add_argument("--force", action="store_true", help="игнорировать дедуп по дате")
    a = ap.parse_args()

    state = {}
    if STATE.exists():
        try:
            state = json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:                          # битый файл не повод молчать про обвал
            state = {}

    now = datetime.now(MSK).strftime("%Y-%m-%d %H:%M")
    for acc in ACCOUNTS:
        hit, quiet = check(acc)
        if quiet:
            print(f"[{now}] {quiet}")
            continue
        if not a.force and state.get(acc) == hit["key"]:
            print(f"[{now}] {acc}: уже сообщали по паре {hit['key']} — молчу")
            continue
        print(f"[{now}] {acc}: СРАБОТАЛО — {hit['why']}")
        if a.dry:
            print(hit["text"])
            continue
        print(f"[{now}] {acc}: телеграм — {tg(hit['text'])}")
        state[acc] = hit["key"]

    if not a.dry:
        STATE.parent.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
