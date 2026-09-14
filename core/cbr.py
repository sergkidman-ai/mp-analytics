"""Курсы ЦБ РФ на дату — для пересчёта валютной выручки в рубли и доллары.

Зачем отдельный модуль: в статформу ФТС стоимость идёт в рублях и в долларах по курсу
ЦБ на ДАТУ ДОСТАВКИ заказа. Курс на дату — вещь неизменяемая, поэтому кэшируем в
cbr_rate и на cbr.ru ходим только при промахе.

Источник открытый, без ключей и без оплаты: https://www.cbr.ru/scripts/XML_daily.asp
"""
import sys
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

URL = "https://www.cbr.ru/scripts/XML_daily.asp"
# В выходные ЦБ курс не публикует — отступаем назад максимум на столько дней.
MAX_BACKOFF = 7


def _fetch_day(d):
    """Забрать все курсы за день. Пустой список — значит на эту дату публикации не было."""
    r = requests.get(URL, params={"date_req": d.strftime("%d/%m/%Y")}, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    # Атрибут Date у ответа — та дата, за которую курс реально действует. ЦБ на запрос
    # выходного дня отдаёт курс предыдущего рабочего, и молча подменять дату нельзя:
    # иначе кэш запишется не на ту дату.
    actual = root.get("Date")
    if actual:
        dd, mm, yy = actual.split(".")
        actual = date(int(yy), int(mm), int(dd))
    rows = []
    for v in root.findall("Valute"):
        rows.append({
            "rate_date": actual or d,
            "code": v.findtext("CharCode"),
            "nominal": int(v.findtext("Nominal")),
            "rate": Decimal(v.findtext("Value").replace(",", ".")),
        })
    return rows


def rate(d, code):
    """Курс валюты на дату: (rate, nominal). RUB → (1, 1).

    Если на дату курса нет (выходной, праздник) — отступаем назад до рабочего дня,
    ровно как это делает сам ЦБ.
    """
    code = (code or "").upper()
    if code in ("RUB", "RUR", ""):
        return Decimal(1), 1
    if isinstance(d, str):
        d = date.fromisoformat(d[:10])

    for back in range(MAX_BACKOFF + 1):
        day = d - timedelta(days=back)
        got = db.query("SELECT rate, nominal FROM cbr_rate WHERE rate_date=%s AND code=%s",
                       (day, code))
        if got:
            return Decimal(str(got[0]["rate"])), got[0]["nominal"]
        rows = _fetch_day(day)
        if rows:
            db.upsert("cbr_rate", rows, ["rate_date", "code"])
            hit = [r for r in rows if r["code"] == code]
            if hit:
                return hit[0]["rate"], hit[0]["nominal"]
    raise RuntimeError(f"курс {code} на {d} не найден за {MAX_BACKOFF} дней назад")


def to_rub(amount, code, d):
    """Сумма в валюте → рубли по курсу на дату d."""
    r, nom = rate(d, code)
    return (Decimal(str(amount)) * r / nom).quantize(Decimal("0.01"))


def rub_to_usd(amount_rub, d):
    """Рубли → доллары по курсу на дату d (в статформе нужна и долларовая стоимость)."""
    r, nom = rate(d, "USD")
    return (Decimal(str(amount_rub)) * nom / r).quantize(Decimal("0.01"))


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else date.today().isoformat()
    for c in ("USD", "KZT", "BYN", "AMD", "KGS"):
        r, n = rate(d, c)
        print(f"{c}: {r} за {n}")
