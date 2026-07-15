"""collectors/yandex_services.py — импорт «Отчёта о стоимости услуг маркетплейса» Яндекса.

Ручная выгрузка из ЛК (Финансы → Стоимость услуг), один лист на вид услуги.
Нужна, потому что API продвижения отдаёт лишь ~70 дней вглубь: реклама за янв–апр
(буст продаж/показов, Полки, товарные баннеры), а также баллы за отзыв и подписка
достаются только отсюда. Май–июнь по бусту сходятся с API до рубля (сверено).

Пишем построчно в raw_yandex_services (сырьё), свёртка по месяцам — в collect() витрины
(reports через services_monthly). Идемпотентно: полный снапшот, перезаливаем целиком.

Запуск:  ./venv/bin/python collectors/yandex_services.py [путь.xlsx]
По умолчанию — incoming/marketplace_services_financial_month.xlsx.
"""
import sys
import json
import hashlib
import pathlib

import openpyxl
from psycopg2.extras import Json

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

ACCOUNT = "ya_acc1"
DEFAULT_FILE = BASE_DIR / "incoming" / "marketplace_services_financial_month.xlsx"

# Правило разбора на каждый лист услуги:
#   category   — свёрнутая категория (ad|subscription|reviews);
#   cost_cols  — заголовки колонок, чью сумму берём как реальную оплату в ₽;
#   bonus_col  — колонка «оплата бонусами» (не наши деньги), если есть;
#   date_col   — колонка даты оказания услуги.
DATE = "Дата оказания услуги"
SHEETS = {
    "Буст продаж, оплата за показы":  ("ad",           ["Оплата, ₽"],                 "Оплата бонусами", DATE),
    "Буст продаж, оплата за продажи": ("ad",           ["Предоплата, ₽", "Постоплата, ₽"], "Оплата бонусами", DATE),
    "Полки":                          ("ad",           ["Оплата, ₽"],                 "Оплата бонусами", DATE),
    "Товарные баннеры":               ("ad",           ["Оплата, ₽"],                 "Оплата бонусами", DATE),
    "Программа лояльности и отзывы":   ("reviews",      ["Стоимость услуги, ₽"], "Оплата бонусами", DATE),
    "Подписки":                       ("subscription", ["Стоимость услуги, ₽"],       None,              DATE),
}
ORDER_HDRS = ("Номер заказа или отгрузки",)
SKU_HDRS = ("Ваш SKU",)


def _num(v):
    return float(v) if isinstance(v, (int, float)) else 0.0


def _ym(v):
    if hasattr(v, "year"):
        return f"{v.year}-{v.month:02d}"
    if isinstance(v, str) and len(v) >= 7 and v[4] == "-":
        return v[:7]
    return None


def _svc_date(v):
    if hasattr(v, "year"):
        return v.date() if hasattr(v, "date") else v
    if isinstance(v, str) and len(v) >= 10 and v[4] == "-":
        return v[:10]
    return None


def _header_row(ws):
    """Шапка не всегда во 2-й строке (у части листов сверху текст-примечание)."""
    for r in range(1, 8):
        names = [(str(ws.cell(r, c).value).strip() if ws.cell(r, c).value is not None else "")
                 for c in range(1, ws.max_column + 1)]
        if DATE in names or any(n.startswith("Оплата, ₽") for n in names) \
           or "Стоимость услуги, ₽" in names or "Предоплата, ₽" in names:
            return r, {n: i + 1 for i, n in enumerate(names) if n}
    return None, {}


def parse(path):
    """Возвращает список нормализованных строк услуг (dict)."""
    wb = openpyxl.load_workbook(path, data_only=True)
    rows = []
    for sheet, (category, cost_cols, bonus_col, date_col) in SHEETS.items():
        if sheet not in wb.sheetnames:
            print(f"  [services] лист «{sheet}» не найден — пропуск", flush=True)
            continue
        ws = wb[sheet]
        hr, h = _header_row(ws)
        if hr is None:
            print(f"  [services] «{sheet}»: шапка не найдена — пропуск", flush=True)
            continue
        cc = [h[x] for x in cost_cols if x in h]
        bc = h.get(bonus_col) if bonus_col else None
        # дата оказания услуги на части листов зовётся «Дата и время оказания услуги»
        dc = h.get(date_col) or next((h[k] for k in h if "оказания услуги" in k), None)
        oc = next((h[x] for x in ORDER_HDRS if x in h), None)
        sc = next((h[x] for x in SKU_HDRS if x in h), None)
        n = 0
        for r in range(hr + 1, ws.max_row + 1):
            cost = sum(_num(ws.cell(r, c).value) for c in cc)
            bonus = _num(ws.cell(r, bc).value) if bc else 0.0
            if cost == 0 and bonus == 0:
                continue
            dval = ws.cell(r, dc).value if dc else None
            ym = _ym(dval)
            if not ym:
                continue
            order_id = str(ws.cell(r, oc).value) if oc and ws.cell(r, oc).value is not None else None
            sku = str(ws.cell(r, sc).value) if sc and ws.cell(r, sc).value is not None else None
            key = f"{sheet}|{ym}|{order_id}|{sku}|{_svc_date(dval)}|{cost:.4f}|{bonus:.4f}|{n}"
            rows.append({
                "account": ACCOUNT, "service": sheet, "category": category,
                "ym": ym, "svc_date": _svc_date(dval),
                "order_id": order_id, "sku": sku,
                "cost": round(cost, 2), "bonus": round(bonus, 2),
                "row_hash": hashlib.md5(key.encode()).hexdigest(),
                "payload": Json({"service": sheet, "ym": ym, "order_id": order_id,
                                 "sku": sku, "cost": round(cost, 2), "bonus": round(bonus, 2)}),
            })
            n += 1
        print(f"  [services] «{sheet}»: {n} строк, Σ оплата "
              f"{sum(x['cost'] for x in rows if x['service'] == sheet):,.0f} ₽", flush=True)
    return rows


def import_file(path=DEFAULT_FILE, account=ACCOUNT):
    """Полный снапшот: чистим прежние строки аккаунта и заливаем заново (идемпотентно)."""
    path = pathlib.Path(path)
    if not path.exists():
        print(f"  [services] файл не найден: {path}", flush=True)
        return 0
    rows = parse(path)
    if not rows:
        return 0
    db.execute("DELETE FROM raw_yandex_services WHERE account=%s", (account,))
    n = db.upsert("raw_yandex_services", rows, conflict_cols=["account", "row_hash"])
    months = sorted({r["ym"] for r in rows})
    print(f"  [services] залито {n} строк, месяцы {months[0]}..{months[-1]}", flush=True)
    return n


def services_monthly(account=ACCOUNT):
    """Свёртка по месяцам: {ym: {'ad':..,'subscription':..,'reviews':..,'ad_bonus':..}}."""
    out = {}
    for r in db.query("""
            SELECT ym, category, sum(cost)::float cost, sum(bonus)::float bonus
            FROM raw_yandex_services WHERE account=%s GROUP BY ym, category""", (account,)):
        d = out.setdefault(r["ym"], {"ad": 0.0, "subscription": 0.0, "reviews": 0.0, "ad_bonus": 0.0})
        d[r["category"]] = r["cost"]
        if r["category"] == "ad":
            d["ad_bonus"] = r["bonus"]
    return out


if __name__ == "__main__":
    src = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_FILE
    import_file(src)
    print("\nСвёртка по месяцам (реклама | подписка | отзывы):")
    for ym, d in sorted(services_monthly().items()):
        print(f"  {ym}: реклама {d['ad']:>10,.0f} | подписка {d['subscription']:>7,.0f} "
              f"| отзывы {d['reviews']:>7,.0f}")
