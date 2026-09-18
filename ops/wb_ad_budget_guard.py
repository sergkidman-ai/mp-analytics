#!/usr/bin/env python3
# поток: mkt
"""ops/wb_ad_budget_guard.py — сторож бюджетов рекламных кампаний ВБ.

ЗАЧЕМ. 18.09.2026 нашли 14 рабочих cpc-кампаний (заказ по 34–166 ₽, в сумме 90 заказов на 8 269 ₽
за 28 дней), которые стояли на нуле: автопополнения у ВБ нет, и никто этого не видел. Сторож каждый
день читает баланс всех cpc-кампаний и пишет в PRC-бот, какие ВЫГОДНЫЕ кампании кончатся или уже
кончились, и на сколько их пополнить.

ТОЛЬКО ЧТЕНИЕ. В ВБ сторож ничего не пишет: пополнение — деньги, решение за Сергеем (инвариант 8).
  GET /adv/v1/promotion/count, GET /api/advert/v2/adverts — состав и тип оплаты
  GET /adv/v1/budget?id=                                   — баланс кампании

ПРАВИЛО.
  выгодная   = за 28 дней ≥ 2 заказов и цена заказа ≤ CPO_OK (200 ₽);
  тревога    = баланс < 50 ₽ ИЛИ денег меньше, чем на RUNWAY_DAYS (3) дня при расходе последних 7 дней;
  сумма      = расход за 28 дней, округлённый вверх до 100, но не меньше 1000 ₽ (минимум ВБ);
  прочие пустые кампании (дорогой заказ или нет заказов) перечисляются одной строкой без суммы.

  ./venv/bin/python -m ops.wb_ad_budget_guard --dry     # печать и файл, без телеграма
  ./venv/bin/python -m ops.wb_ad_budget_guard           # + сообщение в PRC-бот
"""
import sys
import json
import time
import math
import argparse
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from ops.wb_ad_enroll import composition  # noqa: E402
from ops.wb_ad_campaign_new import campaign_budget  # noqa: E402
from ops.mkt_margin_watch import tg  # noqa: E402

CPO_OK = 200
RUNWAY_DAYS = 3
MIN_DEPOSIT = 1000
REPORT = BASE_DIR / "docs" / "reports" / "mkt_ad_budget_guard_latest.txt"


def budget(account, advert_id):
    for _ in range(5):
        code, txt = campaign_budget(account, advert_id)
        try:
            return json.loads(txt).get("total")
        except Exception:
            time.sleep(4)
    return None


def scan(account):
    comp = composition(account)
    ids = [a for a, c in comp.items() if c["payment"] == "cpc"]
    stats = {r["advert_id"]: r for r in db.query("""
        select advert_id,
               coalesce(sum(spend) filter (where dt>current_date-8), 0) sp7,
               coalesce(sum(spend), 0) sp28, coalesce(sum(orders), 0) o28
          from wb_ad_nm_daily where account=%s and dt>current_date-29 and advert_id=any(%s)
         group by 1""", (account, ids))}
    rows = []
    for aid in ids:
        st = stats.get(aid) or {"sp7": 0, "sp28": 0, "o28": 0}
        sp7, sp28, o28 = float(st["sp7"]), float(st["sp28"]), int(st["o28"])
        bal = budget(account, aid)
        time.sleep(0.35)
        cpo = sp28 / o28 if o28 else None
        per_day = sp7 / 7
        runway = (bal / per_day) if (bal is not None and per_day > 0) else None
        good = o28 >= 2 and cpo is not None and cpo <= CPO_OK
        low = bal is not None and (bal < 50 or (runway is not None and runway < RUNWAY_DAYS))
        rows.append({"id": aid, "status": comp[aid]["status"], "bal": bal, "sp7": sp7, "sp28": sp28,
                     "o28": o28, "cpo": cpo, "runway": runway, "good": good, "low": low,
                     "topup": max(MIN_DEPOSIT, int(math.ceil(sp28 / 100.0)) * 100)})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="без телеграма")
    a = ap.parse_args()
    lines, total = [], 0
    for acc in ("wb_acc1", "wb_acc2"):
        rows = scan(acc)
        act = sorted((r for r in rows if r["good"] and r["low"]), key=lambda r: r["cpo"])
        weak = [r for r in rows if not r["good"] and r["bal"] is not None and r["bal"] < 50]
        unread = [r["id"] for r in rows if r["bal"] is None]
        lines.append(f"{acc}: cpc-кампаний {len(rows)}; выгодных на исходе {len(act)}")
        for r in act:
            left = "0 ₽" if r["bal"] < 50 else f"{round(r['bal'])} ₽ ≈ {r['runway']:.1f} дн"
            lines.append(f"  {r['id']}: осталось {left}; заказ {round(r['cpo'])} ₽ ({r['o28']} за 28 дн) "
                         f"→ пополнить {r['topup']} ₽")
            total += r["topup"]
        if weak:
            lines.append(f"  пустые, но невыгодные (не пополнять без решения): "
                         + ", ".join(str(r["id"]) for r in weak))
        if unread:
            lines.append(f"  баланс не прочитан: {unread}")
    head = (f"ВБ реклама: бюджеты. К пополнению выгодных — {total} ₽" if total
            else "ВБ реклама: бюджеты. Выгодные кампании при деньгах")
    text = head + "\n" + "\n".join(lines)
    REPORT.write_text(text + "\n", encoding="utf-8")
    print(text)
    if not a.dry and total:
        print("телеграм:", tg(text + "\n\nПополнение — только по вашему «да» с суммой."))


if __name__ == "__main__":
    main()
