"""Генератор статистической формы ФТС (XML для загрузки в ЛК ФТС).

Собирается подстановкой в эталон reports/fts_sample_kz_08_26.xml — это форма за август
по Казахстану, которая уже загружена в ЛК ФТС и прошла проверку. Эталон нужен не из лени:
в документе три вложенных пространства имён по умолчанию (StaticForm:5.27.0,
CommonAggregateTypes:5.24.0, RUSCommonAggregateTypes:5.24.0), и любая сборка через
ElementTree переписывает их в ns0:/ns1: — ЛК такой файл не принимает. Поэтому работаем
со строками, а ElementTree используем только чтобы убедиться, что результат разбирается.

Одна форма — одна страна за отчётный месяц. Товарные разделы схлопываются по
(ТН ВЭД, номер ГТД, страна происхождения): по одному ТН ВЭД схлопывать нельзя, номер ГТД
относится к товару и при слиянии разных партий его пришлось бы выбирать произвольно.
"""
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import date
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from core import cbr  # noqa: E402
from reports import fts_report  # noqa: E402

SAMPLE = Path(__file__).resolve().parent / "fts_sample_kz_08_26.xml"


def _cut(lines, open_tag, close_tag):
    """Вернуть (индекс начала, индекс после конца) блока по его тегам."""
    i = next(n for n, s in enumerate(lines) if s.strip() == open_tag)
    j = next(n for n, s in enumerate(lines) if s.strip() == close_tag and n >= i)
    return i, j + 1


def _parts():
    """Разобрать эталон на константные куски и шаблоны повторяемых блоков."""
    lines = SAMPLE.read_text(encoding="utf-8").split("\n")
    c_i, c_j = _cut(lines, "<Consignee>", "</Consignee>")
    t_i, _ = _cut(lines, "<TradeCountry>", "</TradeCountry>")
    _, d_j = _cut(lines, "<DestinationCountry>", "</DestinationCountry>")
    doc_i, doc_j = _cut(lines, "<Documents>", "</Documents>")          # договор
    doc2_i, doc2_j = _cut(lines[doc_j:], "<Documents>", "</Documents>")  # проформа
    doc2_i, doc2_j = doc2_i + doc_j, doc2_j + doc_j
    g_i, g_j = _cut(lines, "<GoodsInfo>", "</GoodsInfo>")
    return {
        "head": lines[:c_i],                 # корень, реквизиты, отправитель
        "consignee": lines[c_i:c_j],
        "mid": lines[c_j:t_i],               # ответственный за финансовое урегулирование
        "countries": lines[t_i:d_j],
        "cost": lines[d_j:doc_i],            # валюта и итоговая стоимость
        "contract": lines[doc_i:doc_j],
        "proforma": lines[doc2_i:doc2_j],
        "goods": lines[g_i:g_j],
        "tail": lines[doc2_j if g_i < doc2_j else g_j:],
    }


def _blocks(lines):
    """Разрезать последовательность одноуровневых блоков (<X>…</X>) на список блоков."""
    out, cur = [], []
    for s in lines:
        cur.append(s)
        if s.strip().startswith("</") and len(cur) > 1:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _set(block, tag, value):
    """Заменить содержимое тега в блоке строк (тег может нести свой xmlns)."""
    out = []
    for s in block:
        if f"<{tag}>" in s or f"<{tag} " in s:
            head, rest = s.split(f"<{tag}", 1)
            attrs, rest = rest.split(">", 1)
            body, close = rest.rsplit(f"</{tag}>", 1)
            s = f"{head}<{tag}{attrs}>{value}</{tag}>{close}"
        out.append(s)
    return out


def _gtd_parts(gtd):
    """«10228020/030226/5014219» → (код таможни, дата регистрации, номер)."""
    if not gtd or gtd.count("/") != 2:
        return "", "", ""
    code, ddmmyy, num = gtd.split("/")
    d = f"20{ddmmyy[4:6]}-{ddmmyy[2:4]}-{ddmmyy[0:2]}"
    return code, d, num


def _num(x):
    """Decimal без хвостовых нулей там, где в эталоне их нет (13138.11, 1.755)."""
    s = f"{Decimal(str(x)):f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def make_xml(period, country, data=None):
    """XML статформы за period по стране country (двухбуквенный код). → bytes."""
    data = data or fts_report.build(period)
    c = data["by_country"].get(country)
    if not c:
        raise ValueError(f"за {period:%Y-%m} нет отправлений в {country}")
    rows = [r for r in c["rows"] if r["status"] != "filed"] or c["rows"]

    p = _parts()
    name_ru = fts_report.COUNTRY_RU[country]

    head = _set(p["head"], "DocumentID", str(uuid.uuid4()))
    head = _set(head, "ReportingDate", f"{period:%Y-%m}")

    consignee = _set(p["consignee"], "CountryCode", country)
    consignee = _set(consignee, "CounryName", name_ru)
    consignee = _set(consignee, "City", c["city"])

    # Торгующая страна и страна назначения — наш получатель, страна отправления всегда РФ.
    countries = []
    for blk in _blocks(p["countries"]):
        if "<DispatchCountry>" not in blk[0]:
            blk = _set(_set(blk, "CountryName", name_ru), "CountryCode", country)
        countries += blk

    total_rub = sum(r["amount_rub"] for r in rows)
    cost = _set(p["cost"], "CustCostTotalAmount", _num(total_rub))

    docs = list(_set(p["contract"], "PrDocumentDate", f"{period:%Y-%m-%d}"))
    for r in rows:
        blk = _set(p["proforma"], "PrDocumentNumber", r["posting_number"])
        docs += _set(blk, "PrDocumentDate", r["delivered_at"].isoformat())

    # Товарные разделы всех отправлений страны, схлопнутые по (ТН ВЭД, ГТД, происхождение).
    merged = {}
    for r in rows:
        for g in r["goods"]:
            key = (g["ved"], g["gtd"], g["origin_code"])
            m = merged.setdefault(key, {**g, "weight_kg": Decimal(0),
                                        "amount_rub": Decimal(0), "descr": []})
            m["weight_kg"] += Decimal(str(g["weight_kg"]))
            m["amount_rub"] += Decimal(str(g["amount_rub"]))
            if g["description"] and g["description"] not in m["descr"]:
                m["descr"].append(g["description"])

    goods = []
    for n, ((ved, gtd, origin_code), g) in enumerate(sorted(merged.items(),
                                                            key=lambda kv: str(kv[0])), 1):
        code, reg_date, gtd_num = _gtd_parts(gtd)
        usd = cbr.rub_to_usd(g["amount_rub"], max(r["delivered_at"] for r in rows))
        blk = _set(p["goods"], "GoodsNumeric", n)
        blk = _set(blk, "GoodsTNVEDCode", ved or "")
        blk = _set(blk, "GoodsDescription", _esc("; ".join(g["descr"])))
        blk = _set(blk, "NetWeightQuantity", _num(g["weight_kg"]))
        blk = _set(blk, "InvoicedCost", _num(g["amount_rub"]))
        blk = _set(blk, "StatisticalCostRUB", _num(g["amount_rub"]))
        blk = _set(blk, "StatisticalCostUSD", _num(usd))
        blk = _set(blk, "CountryName", g["origin"].upper())
        blk = _set(blk, "CountryCode", origin_code)
        blk = _set(blk, "CustomsCode", code)
        blk = _set(blk, "RegistrationDate", reg_date)
        blk = _set(blk, "GTDNumber", gtd_num)
        goods += blk

    tail = _set(p["tail"], "SigningDate", f"{date.today().isoformat()}T00:00:00")

    out = "\n".join(head + consignee + p["mid"] + countries + cost + docs + goods + tail)
    ET.fromstring(out.encode("utf-8"))   # только проверка разбора, не пересборка
    return out.encode("utf-8")


def _esc(s):
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def filename(period, country):
    return f"{fts_report.COUNTRY_TITLE[country]}_{period:%m-%y}.xml"


if __name__ == "__main__":
    per = sys.argv[1]
    cnt = sys.argv[2] if len(sys.argv) > 2 else "KZ"
    period = date(int(per[:4]), int(per[5:7]), 1)
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(filename(period, cnt))
    out.write_bytes(make_xml(period, cnt))
    print(f"{out} — {out.stat().st_size} байт")
