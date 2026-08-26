"""ops/ozon_promo_bonus_watch.py — поток: ev

Сторож акции Ozon «Бонусы на продвижение FBS-новинок» (уведомление 26.08.2026, пункт №3).
Ozon с 31.08 ЕЖЕНЕДЕЛЬНО начисляет бонусы на продвижение новых карточек и сам заводит под них
кампании в «Оплате за клик». Критерии из письма:
  1) карточка стала доступна покупателям не более 7 дней назад;
  2) подробное описание, качественные фото, высокий контент-рейтинг;
  3) позиция НЕ продвигается в «Оплате за клик» и «Оплате за заказ»;
  4) на нашем складе не менее 5 единиц товара.

Сторож делает две вещи и обе кладёт в одно сообщение в бот PRC:
  * СКОЛЬКО У НАС ПРОХОДИТ — воронка по каталогу на дату ближайшего начисления (не на сегодня:
    окно «≤7 дней» едет, и карточка, годная сегодня, к понедельнику из него выпадает);
  * ФАКТ НАЧИСЛЕНИЯ — появились ли кампании, которых мы не заводили, и бонусные строки
    в отчётах МП. Первый обнаруженный факт заводится событием в дневник.

Запуск:
    ./venv/bin/python -m ops.ozon_promo_bonus_watch            # сводка в бот
    ./venv/bin/python -m ops.ozon_promo_bonus_watch --dry      # только на экран
    ./venv/bin/python -m ops.ozon_promo_bonus_watch --on 2026-08-31   # окно на другую дату
"""
import argparse
import os
import pathlib
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                                       # noqa: E402
from collectors.ozon import _headers, PRODUCT_LIST_URL, PRODUCT_INFO_URL  # noqa: E402
from ops import biz_diary                                                 # noqa: E402

ACCOUNTS = ("oz_acc1", "oz_acc2")
RATING_URL = "https://api-seller.ozon.ru/v1/product/rating-by-sku"
FIRST_ACCRUAL = date(2026, 8, 31)   # первое начисление; дальше — каждую неделю
FRESH_DAYS = 7                      # «доступна покупателям не более 7 дней назад»
MIN_STOCK = 5                       # «не менее 5 единиц на вашем складе»
# Порога контент-рейтинга в письме нет (он в PDF условий), поэтому берём консервативно:
# ниже 70 считаем риском и показываем отдельно, а не выкидываем из списка молча.
RATING_RISK = 70
# Бонус приходит на рекламный счёт, а не в финансовые операции продавца, поэтому ищем ДВА следа.
BONUS_WORDS = ("бонус", "балл", "продвижен", "бустинг")

NOTIFY_IDS = [x.strip() for x in os.getenv("TG_PRC_NOTIFY_ID", "1031321444").split(",") if x.strip()]
TG_TOKEN = os.getenv("TG_PRC_BOT_TOKEN", "").strip()


def tg(text):
    if not TG_TOKEN:
        return "нет TG_PRC_BOT_TOKEN"
    for chat in NOTIFY_IDS:
        try:
            r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                              json={"chat_id": chat, "text": text,
                                    "disable_web_page_preview": True}, timeout=30)
            if r.status_code != 200:
                return f"telegram {r.status_code}"
        except requests.RequestException as e:
            return f"telegram: {e}"
    return "ok"


def next_accrual(today=None):
    """Ближайшая дата начисления: 31.08, дальше каждый понедельник."""
    today = today or date.today()
    if today <= FIRST_ACCRUAL:
        return FIRST_ACCRUAL
    ahead = (FIRST_ACCRUAL.weekday() - today.weekday()) % 7
    return today + timedelta(days=ahead or 7)


# --- каталог -----------------------------------------------------------------------------
def _visible_cards(account):
    """Видимые карточки аккаунта с датой создания и остатком FBS."""
    H, pids, last = _headers(account), [], ""
    while True:
        r = requests.post(PRODUCT_LIST_URL, headers=H, timeout=120,
                          json={"filter": {"visibility": "VISIBLE"}, "last_id": last, "limit": 1000})
        if r.status_code == 429:
            time.sleep(int(r.headers.get("Retry-After", "5")) + 1)
            continue
        r.raise_for_status()
        res = r.json()["result"]
        items = res.get("items") or []
        pids += [i["product_id"] for i in items]
        last = res.get("last_id") or ""
        if len(items) < 1000:
            break
    out = []
    for i in range(0, len(pids), 1000):
        for _ in range(3):
            r = requests.post(PRODUCT_INFO_URL, headers=H, timeout=180,
                              json={"product_id": pids[i:i + 1000]})
            if r.status_code == 429:
                time.sleep(6)
                continue
            break
        r.raise_for_status()
        j = r.json()
        for it in (j.get("items") or j.get("result", {}).get("items") or []):
            stocks = (it.get("stocks") or {}).get("stocks") or []
            out.append({
                "account": account,
                "offer_id": it.get("offer_id"),
                "sku": str(it.get("sku")),
                "name": (it.get("name") or "")[:70],
                "created": _dt(it.get("created_at")),
                "status": (it.get("statuses") or {}).get("status_name"),
                "fbs": sum(s.get("present", 0) for s in stocks if s.get("source") == "fbs"),
            })
    return out


def _dt(s):
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _ratings(account, skus):
    """Контент-рейтинг по sku. Пустой ответ — не повод выкидывать карточку, вернём None."""
    out = {}
    for i in range(0, len(skus), 100):
        try:
            r = requests.post(RATING_URL, headers=_headers(account), timeout=60,
                              json={"skus": skus[i:i + 100]})
            if r.status_code != 200:
                continue
            for p in r.json().get("products", []):
                out[str(p.get("sku"))] = p.get("rating")
        except requests.RequestException:
            continue
    return out


def _advertised(days=7):
    """sku, которые реально крутятся в «Оплате за клик»/«за заказ» — их бонусом не награждают."""
    rows = db.query("""SELECT DISTINCT sku FROM mkt_ozon_ads_sku_daily
                       WHERE stat_date >= current_date - %s
                         AND (coalesce(money_spent,0) > 0 OR coalesce(views,0) > 0)""", (days,))
    return {str(r["sku"]) for r in rows}


def candidates(on_date):
    """Воронка по критериям на дату начисления. -> (проходят, рядом, всего видимых)."""
    edge = datetime.combine(on_date - timedelta(days=FRESH_DAYS), datetime.min.time(),
                            tzinfo=timezone.utc)
    ads, fit, near, total = _advertised(), [], [], 0
    for acc in ACCOUNTS:
        cards = _visible_cards(acc)
        total += len(cards)
        fresh = [c for c in cards if c["created"] and c["created"] >= edge]
        rat = _ratings(acc, [c["sku"] for c in fresh])
        for c in fresh:
            c["rating"] = rat.get(c["sku"])
            c["in_ads"] = c["sku"] in ads
            why = []
            if c["status"] != "Продается":
                why.append(f"статус «{c['status']}»")
            if c["fbs"] < MIN_STOCK:
                why.append(f"остаток {c['fbs']} < {MIN_STOCK}")
            if c["in_ads"]:
                why.append("уже в платном продвижении")
            c["why"] = "; ".join(why)
            (fit if not why else near).append(c)
    return fit, near, total


# --- факт начисления ---------------------------------------------------------------------
def new_campaigns(since=FIRST_ACCRUAL):
    """Кампании, впервые появившиеся в статистике с даты начала акции — кандидаты в «завёл Ozon»."""
    return db.query("""SELECT campaign_id, account, min(stat_date) AS since,
                              sum(coalesce(money_spent,0)) AS spent, count(DISTINCT sku) AS skus
                       FROM mkt_ozon_ads_sku_daily
                       GROUP BY campaign_id, account
                       HAVING min(stat_date) >= %s
                       ORDER BY 3, 1""", (since,))


def bonus_operations(since=FIRST_ACCRUAL):
    """Бонусные строки в отчётах МП. Ozon кладёт бонус на рекламный счёт, поэтому здесь
    вполне может быть пусто — это не ошибка, а ответ «в финансовых операциях следа нет»."""
    like = " OR ".join(["payload->>'operation_type_name' ILIKE %s"] * len(BONUS_WORDS))
    return db.query(f"""SELECT payload->>'operation_type' AS op,
                               payload->>'operation_type_name' AS name,
                               account, count(*) AS n,
                               round(sum((payload->>'amount')::numeric), 2) AS amount
                        FROM raw_ozon_transaction
                        WHERE period_from >= %s AND ({like})
                        GROUP BY 1,2,3 ORDER BY 4 DESC""",
                    tuple([since] + [f"%{w}%" for w in BONUS_WORDS]))


def report(on_date, dry=False):
    fit, near, total = candidates(on_date)
    camps, ops = new_campaigns(), bonus_operations()
    lines = [f"🎁 Ozon, бонусы на продвижение FBS-новинок — начисление {on_date.strftime('%d.%m')}",
             f"Каталог: {total} видимых карточек, в окно «новинка ≤{FRESH_DAYS} дн» попадает "
             f"{len(fit) + len(near)}."]
    if fit:
        lines.append(f"✅ Проходят под бонус: {len(fit)}")
        for c in fit[:15]:
            risk = " ⚠️ низкий контент-рейтинг" if (c["rating"] or 100) < RATING_RISK else ""
            lines.append(f"  {c['account']} {c['offer_id']} — {c['fbs']} шт, "
                         f"рейтинг {c['rating']}{risk} | {c['name'][:38]}")
    else:
        lines.append("❌ Под бонус не проходит ни одна карточка — начисления не будет.")
    if near:
        lines.append(f"Рядом (новинки, но критерий не выполнен): {len(near)}")
        for c in near[:10]:
            lines.append(f"  {c['account']} {c['offer_id']} — {c['why']}")
    lines.append("")
    if camps:
        lines.append(f"Новые кампании с {FIRST_ACCRUAL.strftime('%d.%m')}: {len(camps)}")
        for r in camps[:10]:
            lines.append(f"  {r['account']} #{r['campaign_id']} с {r['since']}, "
                         f"sku {r['skus']}, расход {r['spent']} ₽")
    else:
        lines.append(f"Кампаний, заведённых не нами, с {FIRST_ACCRUAL.strftime('%d.%m')} нет.")
    if ops:
        lines.append("Бонусные строки в отчётах МП:")
        for r in ops[:10]:
            lines.append(f"  {r['account']} {r['name']}: {r['n']} шт на {r['amount']} ₽")
    else:
        lines.append("В финансовых операциях бонусных строк нет "
                     "(бонус идёт на рекламный счёт — проверяйте кампании).")
    text = "\n".join(lines)
    # Первый живой след начисления — событие в дневник, чтобы факт лёг рядом с цифрами.
    if (camps or ops) and not dry:
        ev = biz_diary.add(
            event_date=date.today(), kind="mp", platform="ozon",
            title="Ozon начислил бонусы на продвижение FBS-новинок — появились кампании/строки, "
                  "которых мы не заводили",
            details=text, expect="Проверить, за какие карточки дали бонус и что кампании Ozon "
                                 "не конкурируют с нашими ставками.",
            author="Ozon", source="promo_bonus_watch",
            dedup_key=f"ozon:promo-bonus:{on_date.isoformat()}")
        if ev:
            text += f"\nЗаведено событие дневника #{ev}."
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description="сторож бонусов Ozon на продвижение FBS-новинок")
    ap.add_argument("--dry", action="store_true", help="не слать в бот и не писать событие")
    ap.add_argument("--on", metavar="ГГГГ-ММ-ДД", help="считать окно на эту дату начисления")
    a = ap.parse_args(argv)
    on = date.fromisoformat(a.on) if a.on else next_accrual()
    text = report(on, dry=a.dry)
    print(text)
    if not a.dry:
        print("телеграм:", tg(text))


if __name__ == "__main__":
    main()
