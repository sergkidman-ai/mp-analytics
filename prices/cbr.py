# поток: prc
# -*- coding: utf-8 -*-
"""
Курс ЦБ РФ на дату + надбавка поставщика.

Правило (согласовано 05.08.2026): берём курс на ДАТУ ЗАГРУЗКИ, последний опубликованный.
В выходные и праздники ЦБ отдаёт курс последнего рабочего дня — это и есть «последний
опубликованный», отдельной логики не требуется, но фактическую дату курса мы фиксируем
в журнале загрузки.

Округления курса НЕТ. Проверено на ручной загрузке Колортека 05.08.2026: округление курса
до четырёх знаков (83.5630 вместо 81.1291 × 1.03 = 83.562973) даёт 51 расхождение на копейку
из 1108 позиций. Умножаем в полной точности, до копеек округляем только итоговую цену.
"""
import urllib.request
import xml.etree.ElementTree as ET
from decimal import Decimal, ROUND_HALF_UP

CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp?date_req={}"
_cache = {}


def cbr_rate(on_date, code="USD"):
    """(курс, дата_курса_по_ЦБ) для валюты на указанную дату."""
    key = (on_date.isoformat(), code)
    if key in _cache:
        return _cache[key]
    url = CBR_URL.format(on_date.strftime("%d/%m/%Y"))
    with urllib.request.urlopen(url, timeout=60) as resp:
        tree = ET.fromstring(resp.read())
    for valute in tree.findall("Valute"):
        if valute.find("CharCode").text != code:
            continue
        value = Decimal(valute.find("Value").text.replace(",", "."))
        nominal = Decimal(valute.find("Nominal").text.replace(",", "."))
        rate = value / nominal
        _cache[key] = (rate, tree.get("Date"))
        return _cache[key]
    raise RuntimeError(f"ЦБ не отдал курс {code} на {on_date}")


def effective_rate(on_date, currency, markup):
    """Курс пересчёта в рубли: 1.0 для рублёвого прайса, иначе курс ЦБ × надбавка."""
    if currency == "RUB":
        return Decimal(1), None
    rate, rate_date = cbr_rate(on_date, currency)
    return rate * Decimal(markup), rate_date


def to_kopecks(price, rate):
    """Цена прайса × курс -> копейки, округление половины вверх (как в ручной загрузке)."""
    return int((Decimal(str(price)) * rate * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
