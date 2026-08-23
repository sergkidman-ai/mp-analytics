"""tools/ozon_card_c_report.py — поток: card. Разбор класса C (контентные ошибки) для ТК.

Класс C — то, что наш дожиматель не лечит по устройству: площадка отбила КОНТЕНТ, и пока
контент не изменится, повтор отправки бесполезен. Контент карточек пишет ТК, поэтому наша
работа тут — не править, а выдать задание: какой вид ошибки, сколько карточек, ЧТО именно
менять и в каком поле.

Почему нужен отдельный проход по API, а не выгрузка из card_status: детектор кладёт в err_texts
только описание ошибки, а у обязательных незаполненных полей всё описание — «это обязательное
поле», без имени поля. Адрес правки (attribute_id + attribute_name) лежит в errors[].texts
и добирается только перечиткой карточки. Запрос читающий, ничего не отправляет.

  ./venv/bin/python tools/ozon_card_c_report.py           # CSV + markdown-отчёт
"""
import sys
import re
import csv
import time
from collections import Counter, defaultdict

import requests

sys.path.insert(0, "/opt/mp-analytics")
from core.db import query                                # noqa: E402
from collectors.ozon import _headers, PRODUCT_INFO_URL   # noqa: E402

PLATFORM = "ozon"
PAGE = 100
CSV_PATH = "docs/reports/ozon_card_content_errors.csv"
MD_PATH = "docs/reports/21_ozon_card_class_c_2026-08-23.md"

# Что делать — формулировка задания для ТК. Сначала по ТЕКСТУ отбивки: под одним кодом
# DESCRIPTION_DECLINE площадка держит четыре разные претензии, и правки у них разные.
FIX_BY_TEXT = [
    ("упоминается другой бренд",
     "убрать упоминание чужого бренда из названия/описания/фото либо привести поле «Бренд» "
     "в соответствие. Для совместимого расходника бренд принтера — только в формулировке "
     "совместимости («для <модель>»), не как бренд товара"),
    ("много спецсимволов",
     "почистить название от спецсимволов (скобки, слэши, запятые подряд, «(10 шт.)» и т. п.), "
     "привести к шаблону Ozon"),
    ("получился другой товар",
     "правка ТК превратила карточку в другой товар — Ozon требует НОВУЮ карточку; "
     "решение по каждой отдельно, автоматом не чинится"),
    ("рекламную информацию",
     "убрать из текста рекламу: акции, цены, кэшбек, сравнение с другим брендом"),
]

FIX = {
    "error_attribute_values_empty":
        "заполнить обязательный атрибут (имя поля — в колонке attribute_name)",
    "error_attribute_values_out_of_range":
        "значение вне справочника: выбрать вариант из списка Ozon для этого атрибута",
    "DESCRIPTION_DECLINE":
        "убрать упоминание чужого бренда из названия/описания либо привести поле «Бренд» "
        "в соответствие (для совместимых расходников — формулировка «для <модель>», "
        "бренд принтера не выносить как бренд товара)",
    "BR_hashtag_brand":
        "вычистить названия чужих брендов из хештегов",
    "BR_hashtags_symbols_validation":
        "в хештегах оставить только буквы, цифры, # и _, разделитель — пробел",
    "ML_INCORRECT_VOLUME_WEIGHT":
        "проверить ОВХ (габариты/вес) карточки; если значения верны — обращение в поддержку Ozon",
}


def fix_for(code, descr):
    """Задание для ТК: сначала по тексту отбивки, потом по коду."""
    for marker, text in FIX_BY_TEXT:
        if marker in (descr or ""):
            return text
    return FIX.get(code, "разобрать вручную")


# Бренды принтеров — для подсказки, ЧЕЙ бренд площадка увидела в названии.
# Это вспомогательная выборка из текста названия, а не вердикт площадки.
BRANDS = ["HP", "Canon", "Samsung", "Xerox", "Brother", "Epson", "Kyocera", "Ricoh", "Sharp",
          "Panasonic", "OKI", "Lexmark", "Pantum", "Konica", "Minolta", "Toshiba", "Dell",
          "Philips", "Develop", "Katun", "Static Control", "Sindoh", "Avision"]


def _cards():
    # status_descr — это и есть «Не создан» / «Не обновлен»: карточка со «Не создан» в продажу
    # не попадала вовсе, и такие идут в задании первыми.
    return query(
        "SELECT account, offer_id, product_id, name, status_name, status_descr, is_selling, "
        "       err_codes, date_trunc('minute', first_seen) AS first_seen "
        "FROM card_status WHERE platform = %s AND is_open AND err_class = 'C' "
        "ORDER BY account, err_codes, offer_id", (PLATFORM,))


def _errors_live(account, product_ids):
    """errors[] прямо с площадки: только там лежит имя атрибута."""
    H = _headers(account)
    out = {}
    for i in range(0, len(product_ids), PAGE):
        r = requests.post(PRODUCT_INFO_URL, headers=H,
                          json={"product_id": product_ids[i:i + PAGE]}, timeout=180)
        r.raise_for_status()
        for it in r.json().get("items", []):
            rows = []
            for e in it.get("errors") or []:
                if e.get("level") != "ERROR_LEVEL_ERROR":
                    continue
                t = e.get("texts") or {}
                rows.append({
                    "code": e.get("code") or "",
                    "field": e.get("field") or t.get("field_name") or "",
                    "attribute_id": e.get("attribute_id") or t.get("attribute_id") or "",
                    "attribute_name": t.get("attribute_name") or "",
                    "description": (t.get("description") or t.get("message") or "").strip(),
                })
            out[it.get("offer_id")] = rows
        time.sleep(0.3)
    return out


def _foreign_brands(name):
    found = [b for b in BRANDS if re.search(r"\b" + re.escape(b) + r"\b", name or "", re.I)]
    return " ".join(found)


def main():
    cards = _cards()
    if not cards:
        print("класс C пуст")
        return
    live = {}
    for acc in sorted({c["account"] for c in cards}):
        ids = [int(c["product_id"]) for c in cards if c["account"] == acc and c["product_id"]]
        live[acc] = _errors_live(acc, ids)

    rows = []
    for c in cards:
        errs = live.get(c["account"], {}).get(c["offer_id"]) or [{
            "code": c["err_codes"], "field": "", "attribute_id": "", "attribute_name": "",
            "description": "(карточка не отдалась на перечитку)"}]
        for e in errs:
            rows.append({
                "account": c["account"], "offer_id": c["offer_id"],
                "product_id": c["product_id"], "status_name": c["status_name"],
                "status_descr": c["status_descr"],
                "is_selling": c["is_selling"], "first_seen": c["first_seen"],
                "code": e["code"], "attribute_id": e["attribute_id"],
                "attribute_name": e["attribute_name"], "field": e["field"],
                "description": e["description"],
                "brands_in_name": _foreign_brands(c["name"]),
                "name": c["name"],
                "what_to_fix": fix_for(e["code"], e["description"]),
            })

    with open(CSV_PATH, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    # ── сводка ──
    # Вид работы для ТК задаёт НЕ код, а связка «код + поле + текст отбивки»: под одним
    # DESCRIPTION_DECLINE у площадки лежат и чужой бренд, и спецсимволы в названии — это
    # разные правки в разных полях, и в одно задание их сводить нельзя.
    def _key(r):
        return (r["code"], r["attribute_name"], r["description"])

    by = defaultdict(lambda: {"cards": set(), "acc": Counter(), "selling": set(),
                              "not_created": set(), "brands": Counter(), "examples": []})
    for r in rows:
        b = by[_key(r)]
        key = (r["account"], r["offer_id"])
        b["cards"].add(key)
        b["acc"][r["account"]] += 1
        if r["is_selling"]:
            b["selling"].add(key)
        if (r["status_descr"] or "") == "Не создан":
            b["not_created"].add(key)
        for x in (r["brands_in_name"] or "").split():
            b["brands"][x] += 1
        if len(b["examples"]) < 3:
            b["examples"].append(r)

    groups = sorted(by.items(), key=lambda x: -len(x[1]["cards"]))
    total_cards = len({(r["account"], r["offer_id"]) for r in rows})
    not_created = len({(r["account"], r["offer_id"]) for r in rows
                       if (r["status_descr"] or "") == "Не создан"})
    selling = len({(r["account"], r["offer_id"]) for r in rows if r["is_selling"]})

    out = ["# Класс C — контентные ошибки карточек Ozon: задание для ТК",
           "",
           f"Срез 2026-08-23. Карточек в классе C: **{total_cards}** (строк ошибок {len(rows)}; "
           "на карточке бывает несколько отбивок). Полная таблица — "
           f"`{CSV_PATH}`, там же поле, текст площадки и что менять по каждой карточке.",
           "",
           "**Почему это задание для ТК, а не для нас.** Класс C — отбивки модерации по "
           "СОДЕРЖИМОМУ карточки. Приём, которым лечатся классы A и W (повторная отправка того "
           "же значения), тут бесполезен по устройству: пока текст/атрибут не изменится, "
           "площадка вернёт тот же вердикт. Контент карточек пишет ТК — правка её.",
           "",
           f"**Что горит.** {not_created} карточек в состоянии «Не создан» — товар не попал "
           f"в продажу вообще, это прямые потерянные продажи. Ещё {selling} карточек торгуют "
           "с висящей отбивкой: продажи идут, но карточка на карандаше у модерации.",
           "",
           "## Виды работ (код + поле + текст отбивки)", "",
           "| # | карточек | «Не создан» | торгуют | поле | код | что менять |",
           "|---:|---:|---:|---:|---|---|---|"]
    for i, ((code, attr, _descr), b) in enumerate(groups, 1):
        out.append(f'| {i} | {len(b["cards"])} | {len(b["not_created"])} | '
                   f'{len(b["selling"])} | {attr or "—"} | `{code}` | '
                   f'{fix_for(code, _descr)} |')
    out += ["", "## Подробно", ""]
    for i, ((code, attr, descr), b) in enumerate(groups, 1):
        out += [f"### {i}. {attr or code} — {len(b['cards'])} карточек", "",
                f"- **Код площадки:** `{code}`",
                f"- **Поле:** {attr or '—'}",
                f"- **Текст отбивки:** {descr or '—'}",
                f"- **Аккаунты:** acc1 {b['acc'].get('oz_acc1', 0)}, "
                f"acc2 {b['acc'].get('oz_acc2', 0)}; "
                f"«Не создан» {len(b['not_created'])}, торгуют {len(b['selling'])}",
                f"- **Что менять:** {fix_for(code, descr)}"]
        if b["brands"]:
            out.append("- **Чужие бренды в названиях этих карточек** (выборка из текста "
                       "названия, вспомогательно — не вердикт площадки): "
                       + ", ".join(f"{k} — {n}" for k, n in b["brands"].most_common(8)))
        out += ["", "Примеры:", ""]
        out += [f"- `{e['offer_id']}` ({e['account']}, {e['status_descr']}) — {e['name']}"
                for e in b["examples"]]
        out.append("")
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")

    print(f"класс C: {total_cards} карточек ({not_created} «Не создан», {selling} торгуют), "
          f"{len(rows)} строк ошибок, видов работ {len(groups)}")
    print(f"  CSV: {CSV_PATH}")
    print(f"  отчёт: {MD_PATH}")
    for (code, attr, _d), b in groups:
        print(f"  {len(b['cards']):>4}  {attr or '—':<24} {code}")


if __name__ == "__main__":
    main()
