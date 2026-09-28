# поток: fin
"""Роутер раздела «Оборот для премии» — деньги покупателя по месяцам, площадкам и фирмам.

База премии = ровно те показатели, которые Сергей читает в кабинетах (решение 28.09.2026):
- ВБ     «Продажа» = Σ retail_amount по строкам «Продажа», возвраты — по строкам «Возврат».
         Месяц берём по дате продажи (rr_dt), НЕ по дате формирования отчёта: недельные пачки
         сдвигают границы и месяц выглядит то вдвое больше, то вдвое меньше.
         Сверено: отчёт 771274605 → 382 949 − 24 402 = 358 547 ₽, цифра из ЛК.
- Ozon   «Реализовано на сумму» = Σ rows[].delivery_commission.amount отчёта о реализации,
         возвраты = Σ rows[].return_commission.amount. Сверено: июль oz_acc1 = 5 998 736,68.
         ВАЖНО: это деньги ПОКУПАТЕЛЯ; баллы Ozon сюда не входят (их платит площадка,
         за июль это ещё 4,53 млн ₽ сверху). Отчёт выходит 8–10 числа → текущего месяца нет.
- Маркет «Получено от потребителей» и «Возвращено потребителям» из детализации закрывающих
         документов (raw_yandex_closure, collectors/yandex_closure.py). Июнь сверен с ЛК
         с точностью 1 388 ₽ (одна транзакция по заказу 58253045507).

Комиссия, логистика, реклама и штрафы базу НЕ уменьшают — это уже не выручка.
"""
import sys
import pathlib
import datetime as dt
from collections import defaultdict

from fastapi import APIRouter
from fastapi.responses import FileResponse

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core import db  # noqa: E402
from cf.mp_forecast import ACC_ORG  # noqa: E402

router = APIRouter(tags=["Оборот"])
STATIC = BASE_DIR / "web" / "static"
ORGS = {"7807355364": "Цифровой Квадрат", "7811803918": "Дисквэр"}
PLATFORMS = {"wb": "Wildberries", "ozon": "Ozon", "yandex": "Яндекс Маркет"}


def _wb(since):
    return db.query("""select account, to_char((payload->>'rr_dt')::date, 'YYYY-MM') ym,
                  coalesce(sum((payload->>'retail_amount')::numeric)
                           filter (where payload->>'doc_type_name' = 'Продажа'), 0)::float sales,
                  coalesce(sum((payload->>'retail_amount')::numeric)
                           filter (where payload->>'doc_type_name' = 'Возврат'), 0)::float rets
                from raw_wb_report
               where (payload->>'rr_dt')::date >= %s
               group by 1, 2""", (since,))


def _ozon(since):
    return db.query("""select account, to_char(make_date(year, month, 1), 'YYYY-MM') ym,
                  coalesce(sum((row->'delivery_commission'->>'amount')::numeric), 0)::float sales,
                  coalesce(sum((row->'return_commission'->>'amount')::numeric), 0)::float rets
                from raw_ozon_realization,
                     lateral jsonb_array_elements(payload->'rows') row
               where make_date(year, month, 1) >= %s
               group by 1, 2""", (since,))


def _yandex(since):
    return db.query("""select account, ym,
                  coalesce(sum(amount) filter (where category = 'revenue'), 0)::float sales,
                  coalesce(-sum(amount) filter (where category = 'returns'), 0)::float rets
                from raw_yandex_closure
               where ym >= to_char(%s::date, 'YYYY-MM')
               group by 1, 2""", (since,))


@router.get("/turnover")
def page():
    return FileResponse(STATIC / "turnover.html")


@router.get("/api/turnover")
def turnover(months: int = 6):
    """Деньги покупателя по месяцам: (площадка × фирма) и итоги. Возвраты — отдельной строкой."""
    first = (dt.date.today().replace(day=1) - dt.timedelta(days=31 * (months - 1))).replace(day=1)
    cells = defaultdict(lambda: {"sales": 0.0, "rets": 0.0})
    for platform, rows in (("wb", _wb(first)), ("ozon", _ozon(first)), ("yandex", _yandex(first))):
        for r in rows:
            org = ACC_ORG.get(r["account"])
            if not org:
                continue
            c = cells[(r["ym"], platform, org)]
            c["sales"] += r["sales"]
            c["rets"] += r["rets"]

    yms = sorted({k[0] for k in cells})
    out = []
    for ym in yms:
        row = {"ym": ym, "orgs": {}, "total": {"sales": 0.0, "rets": 0.0, "net": 0.0}}
        for org in ORGS:
            o = {"platforms": {}, "sales": 0.0, "rets": 0.0, "net": 0.0}
            for p in PLATFORMS:
                c = cells.get((ym, p, org))
                if not c:
                    continue
                net = round(c["sales"] - c["rets"], 2)
                o["platforms"][p] = {"sales": round(c["sales"], 2),
                                     "rets": round(c["rets"], 2), "net": net}
                o["sales"] += c["sales"]
                o["rets"] += c["rets"]
                o["net"] += net
            if o["platforms"]:
                for k in ("sales", "rets", "net"):
                    o[k] = round(o[k], 2)
                    row["total"][k] = round(row["total"][k] + o[k], 2)
                row["orgs"][org] = o
        out.append(row)

    # чего ещё нет: отчёт о реализации Ozon выходит 8–10 числа, ВБ отдаёт финотчёт понедельно
    have_oz = {k[0] for k in cells if k[1] == "ozon"}
    pending = [ym for ym in yms if ym not in have_oz]
    wb_last = db.query("""select max((payload->>'rr_dt')::date) d from raw_wb_report""")[0]["d"]
    return {"months": out, "orgs": ORGS, "platforms": PLATFORMS,
            "ozon_pending": pending,
            "wb_last_day": wb_last.isoformat() if wb_last else None}
