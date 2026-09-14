"""Расчёт статформ ФТС за отчётный месяц.

Что считаем: отправления, доставленные в страны ЕАЭС в отчётном месяце, по которым
маркетплейс НЕ выкупил товар (значит отчитываемся мы). По каждому собираем реквизиты
товарного раздела статформы: ТН ВЭД, номер ГТД, страна происхождения, вес нетто,
стоимость в рублях и долларах по курсу ЦБ на дату доставки.

Источники и почему именно они:
  * признак выкупа и дата доставки — raw_ozon_orders_report (в postings-методах их нет);
  * страна — raw_ozon_posting.payload->financial_data->cluster_to: ни один метод Ozon
    не отдаёт страну получателя, кластер доставки — единственный признак;
  * вес нетто — ozon_dims (карточка Ozon). Каталожный prc_tc_model.weight_g запасной:
    сверка со сданной июльской формой показала, что в ФТС ушёл вес карточки;
  * ТН ВЭД и ГТД — цепочка МойСклада: отгрузка по номеру отправления → позиции →
    номенклатура → prc_tc_model.ved_code; ГТД по FIFO из последней приёмки позиции
    до даты отгрузки.

ВБ и Яндекс сюда не заведены осознанно: ВБ закрывает ЕАЭС сам (проверено — своих
уведомлений о выкупе он в API не отдаёт, и статформы по нему мы не сдавали), у Яндекса
зарубежных заказов нет. Структура данных площадко-независимая, добавить можно без правок
схемы.

Запуск сводкой:  venv/bin/python -m reports.fts_report 2026-08
"""
import sys
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
from core import db, cbr  # noqa: E402

# Кластер доставки Ozon → страна. Узбекистан в ЕАЭС НЕ входит, отчёт по нему не сдаём,
# поэтому он тут есть, но в EAES его нет — чтобы видеть его в сводке и не путать с ЕАЭС.
CLUSTER_COUNTRY = {
    "Астана": "KZ", "Алматы": "KZ", "Шымкент": "KZ", "Казахстан": "KZ",
    "Беларусь": "BY", "Белоруссия": "BY",
    "Армения": "AM",
    "Кыргызстан": "KG", "Киргизия": "KG",
    "Узбекистан": "UZ",
}
EAES = ("KZ", "BY", "AM", "KG")

COUNTRY_RU = {"KZ": "КАЗАХСТАН", "BY": "БЕЛАРУСЬ", "AM": "АРМЕНИЯ", "KG": "КИРГИЗИЯ"}
COUNTRY_TITLE = {"KZ": "Казахстан", "BY": "Беларусь", "AM": "Армения", "KG": "Киргизия"}
# Столица как город получателя там, где кластер Ozon — это вся страна и города в нём нет.
COUNTRY_CITY = {"BY": "Минск", "AM": "Ереван", "KG": "Бишкек"}

# Страна происхождения из МойСклада (текстом) → буквенный код для формы.
ORIGIN_CODE = {
    "Китай": "CN", "КИТАЙ": "CN", "Россия": "RU", "РОССИЯ": "RU",
    "Южная Корея": "KR", "Корея, Республика": "KR", "Япония": "JP",
    "Вьетнам": "VN", "Индия": "IN", "Тайвань (Китай)": "TW", "Тайвань": "TW",
}

DELIVERED = "Доставлен"


def month_bounds(period):
    nxt = (period.replace(day=28) + timedelta(days=4)).replace(day=1)
    return period, nxt - timedelta(days=1)


def _ved(raw):
    """prc_tc_model.ved_code хранит «8443999000 - Части и принадлежности» — нужен код."""
    code = "".join(ch for ch in (raw or "").split("-")[0] if ch.isdigit())
    return code or None


def _posting_goods(posting_number, items, weight_g, amount_rub):
    """Товарные разделы отправления: группы (ТН ВЭД, ГТД, страна происхождения).

    Вес и стоимость всего отправления делим между группами пропорционально каталожному
    весу позиций. Почему так: вес нетто мы берём с карточки Ozon (он прошёл проверку в
    ЛК ФТС), а ТН ВЭД и ГТД живут на позициях МойСклада — единицы учёта разные, и
    единственный честный способ свести их — доля позиции в каталожном весе.
    """
    d = db.query("SELECT demand_id, moment FROM ms_demand_cogs WHERE demand_name=%s",
                 (posting_number,))
    if not d:
        return [], "нет отгрузки в МойСкладе"
    demand_id, moment = d[0]["demand_id"], d[0]["moment"]

    pos = db.query("""
        SELECT p.ms_id, p.qty, pr.name, pr.external_code, t.ved_code, t.weight_g
          FROM ms_demand_pos p
          JOIN ms_product pr USING (ms_id)
          LEFT JOIN prc_tc_model t ON t.external_code = pr.external_code
         WHERE p.demand_id = %s""", (demand_id,))
    if not pos:
        return [], "в отгрузке нет позиций"

    groups, problems = defaultdict(lambda: {"w": Decimal(0), "names": [], "qty": Decimal(0)}), []
    for p in pos:
        ved = _ved(p["ved_code"])
        if not ved:
            problems.append(f"нет ТН ВЭД у {p['external_code']}")
        gtd = db.query("""SELECT gtd, country FROM ms_supply_pos
                           WHERE ms_id=%s AND gtd IS NOT NULL AND moment<=%s
                        ORDER BY moment DESC LIMIT 1""", (p["ms_id"], moment))
        gtd_no = gtd[0]["gtd"] if gtd else None
        origin = (gtd[0]["country"] if gtd else None) or "Китай"
        if not gtd_no:
            problems.append(f"нет ГТД у {p['external_code']}")
        key = (ved, gtd_no, origin)
        g = groups[key]
        qty = Decimal(str(p["qty"] or 0))
        g["w"] += Decimal(str(p["weight_g"] or 0)) * qty
        g["qty"] += qty
        if p["name"]:
            g["names"].append(p["name"])

    total_w = sum(g["w"] for g in groups.values())
    total_q = sum(g["qty"] for g in groups.values()) or Decimal(1)
    # Название товара берём с карточки Ozon: именно оно ушло в уже сданные формы.
    descr = "; ".join(dict.fromkeys(i.get("product_name") or "" for i in items)).strip("; ")

    out, n = [], 0
    for (ved, gtd_no, origin), g in sorted(groups.items(), key=lambda kv: str(kv[0])):
        share = (g["w"] / total_w) if total_w else (g["qty"] / total_q)
        n += 1
        out.append({
            "num": n,
            "ved": ved,
            "gtd": gtd_no,
            "origin": origin,
            "origin_code": ORIGIN_CODE.get(origin, "CN"),
            "description": descr or "; ".join(dict.fromkeys(g["names"]))[:1000],
            "weight_kg": (Decimal(weight_g) * share / 1000).quantize(Decimal("0.001")),
            "amount_rub": (amount_rub * share).quantize(Decimal("0.01")),
        })
    # Копейки и граммы округляем по группам — расхождение с суммой отправления гасим
    # в первой группе, иначе итог формы не сойдётся с CustCostTotalAmount.
    if out:
        out[0]["amount_rub"] += amount_rub - sum(g["amount_rub"] for g in out)
        out[0]["weight_kg"] += ((Decimal(weight_g) / 1000).quantize(Decimal("0.001"))
                                - sum(g["weight_kg"] for g in out))
    return out, "; ".join(dict.fromkeys(problems)) or None


def build(period):
    """Данные подраздела за отчётный месяц period (date первого числа)."""
    d_from, d_to = month_bounds(period)
    raw = db.query("""
        SELECT r.account, r.posting_number, r.delivered_at, r.is_buyout, r.delivery_schema,
               r.payload,
               p.payload->'financial_data'->>'cluster_to' AS cluster
          FROM raw_ozon_orders_report r
          LEFT JOIN raw_ozon_posting p ON p.posting_number = r.posting_number
         WHERE r.status = %s AND r.delivered_at BETWEEN %s AND %s
      ORDER BY r.delivered_at, r.posting_number""", (DELIVERED, d_from, d_to))

    marks = {(s["platform"], s["account"], s["posting_number"]): s
             for s in db.query("SELECT * FROM fts_posting_status WHERE period=%s", (period,))}

    rows, skipped_uz = [], 0
    for r in raw:
        country = CLUSTER_COUNTRY.get((r["cluster"] or "").strip())
        if not country:
            continue
        if country not in EAES:
            skipped_uz += 1
            continue
        items = (r["payload"] or {}).get("items") or []
        cur = next((i.get("customer_currency") for i in items if i.get("customer_currency")), "RUB")
        amount = sum(Decimal(str(i.get("paid_by_customer") or 0).replace(",", ".") or 0)
                     for i in items)
        weight_g = 0
        for i in items:
            w = db.query("SELECT weight_g FROM ozon_dims WHERE account=%s AND sku=%s",
                         (r["account"], i.get("sku")))
            if w:
                weight_g += float(w[0]["weight_g"]) * float(i.get("qty") or 1)

        row = {
            "platform": "ozon", "account": r["account"],
            "posting_number": r["posting_number"],
            "delivered_at": r["delivered_at"],
            "schema": r["delivery_schema"],
            "country": country, "cluster": r["cluster"],
            "currency": cur, "amount": amount,
            "is_buyout": bool(r["is_buyout"]),
            "weight_g": weight_g,
            "problems": None, "goods": [],
        }
        if not r["is_buyout"]:
            row["amount_rub"] = cbr.to_rub(amount, cur, r["delivered_at"])
            row["amount_usd"] = cbr.rub_to_usd(row["amount_rub"], r["delivered_at"])
            row["goods"], row["problems"] = _posting_goods(
                r["posting_number"], items, weight_g, row["amount_rub"])
            if not weight_g:
                row["problems"] = "; ".join(filter(None, [row["problems"], "нет веса карточки"]))
        st = marks.get(("ozon", r["account"], r["posting_number"]))
        row["status"] = (st or {}).get("status", "due")
        rows.append(row)

    due = [r for r in rows if not r["is_buyout"]]
    by_country = {}
    for r in due:
        c = by_country.setdefault(r["country"], {
            "code": r["country"], "title": COUNTRY_TITLE[r["country"]],
            "rows": [], "amount_rub": Decimal(0), "cities": defaultdict(Decimal)})
        c["rows"].append(r)
        c["amount_rub"] += r.get("amount_rub") or Decimal(0)
        c["cities"][r["cluster"]] += r.get("amount_rub") or Decimal(0)
    for c in by_country.values():
        # Город получателя: где кластер Ozon — это страна целиком, ставим столицу.
        top = max(c["cities"].items(), key=lambda kv: kv[1])[0] if c["cities"] else ""
        c["city"] = COUNTRY_CITY.get(c["code"], top)
        c["filed"] = sum(1 for r in c["rows"] if r["status"] == "filed")

    return {
        "period": period,
        "rows": rows,
        "by_country": dict(sorted(by_country.items())),
        "totals": {
            "eaes": len(rows),
            "buyout": sum(1 for r in rows if r["is_buyout"]),
            "due": sum(1 for r in due if r["status"] != "filed"),
            "filed": sum(1 for r in due if r["status"] == "filed"),
            "problems": sum(1 for r in due if r["problems"]),
            "uz": skipped_uz,
        },
    }


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else None
    period = (date(*map(int, p.split("-")), 1) if p
              else (date.today().replace(day=1) - timedelta(days=1)).replace(day=1))
    data = build(period)
    t = data["totals"]
    print(f"{period:%Y-%m}: ЕАЭС {t['eaes']}, выкуплено МП {t['buyout']}, "
          f"к сдаче {t['due']}, сдано {t['filed']}, с проблемами {t['problems']}, "
          f"Узбекистан (не ЕАЭС) {t['uz']}")
    for code, c in data["by_country"].items():
        print(f"  {c['title']} ({code}): отправлений {len(c['rows'])}, "
              f"{c['amount_rub']} ₽, город {c['city']}")
    for r in data["rows"]:
        if r["is_buyout"]:
            continue
        g = ", ".join(f"{x['ved']}/{x['gtd']}" for x in r["goods"]) or "—"
        print(f"    {r['posting_number']} {r['delivered_at']} {r['country']} "
              f"{r['amount']} {r['currency']} = {r.get('amount_rub')} ₽ / "
              f"{r.get('amount_usd')} $ | {r['weight_g']} г | {g}"
              + (f" | ПРОБЛЕМА: {r['problems']}" if r["problems"] else ""))
