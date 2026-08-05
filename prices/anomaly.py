# поток: prc
# -*- coding: utf-8 -*-
"""
Проверка аномалий цены перед записью в МойСклад.

Зачем: прайс — чужой файл, и он ломается тихо. Поставщик переставил колонки, прислал цены
в рублях вместо долларов, уронил разделитель разрядов — загрузчик всё это разберёт и
аккуратно положит мусор на склад, а закупочные цены карточек перезапишет. Проверка ловит
два разных класса поломки:

  * **точечный** — цена одной позиции вне разумного коридора или прыгнула к прошлой
    загрузке сильнее порога. Абсурдная цена строку НЕ грузит, прыжок — только флаг;
  * **массовый** — медиана всего прайса уехала (смена валюты/колонки). Это не «странная
    позиция», это сломанный файл, и он блокирует `--apply` целиком.

База сравнения — цена этой же позиции в ПРОШЛОЙ загрузке этого поставщика (журнал
`prc_price_row`). Закупочная цена карточки МС используется только как ЗАПАСНАЯ и только
справочно: проверено на Колортеке, Одиссее и Сакуре — `buyPrice` в карточках живёт своей
жизнью (медиана к цене прайса 0.77-0.91), это не цена прайса, и блокировать по ней
загрузку нельзя. По ней мы флаг ставим (в отчёт), но `--apply` не блокируем. Как только
у поставщика появится наша прошлая загрузка, база станет настоящей — и заблокирует.
Без базы сравнения строка проверяется только на коридор — это не повод для флага.
"""
from decimal import Decimal

from core import ms_api

SRC_LOAD = "прошлая загрузка"       # настоящая база: наша же цифра
SRC_CARD = "закупочная карточки"    # запасная: только справочно, загрузку не блокирует

FLAGS = {
    "price_absurd": "цена вне коридора — строка НЕ загружается",
    "price_jump": "цена выросла сильнее порога",
    "price_drop": "цена упала сильнее порога",
}

PREV_SQL = """
SELECT r.article, r.price_rub
  FROM prc_price_row r
  JOIN (SELECT id FROM prc_price_load
         WHERE supplier_key = %s AND status = 'ok'
           AND dry_run = false AND rows_loaded > 0
         ORDER BY moment DESC LIMIT 1) last ON last.id = r.load_id
 WHERE r.status = 'loaded' AND r.price_rub IS NOT NULL
"""


def _median(values):
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return None
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2


def previous_prices(supplier_key):
    """{артикул -> цена в рублях} из последней БОЕВОЙ загрузки. Нет журнала — пусто.

    Сухие прогоны в базу сравнения не берём: сверяемся с тем, что реально лежит на складе,
    иначе первый же прогон по битому файлу станет эталоном для следующего.
    """
    try:
        from core.db import query
        return {r["article"]: Decimal(r["price_rub"]) for r in query(PREV_SQL, (supplier_key,))}
    except Exception:
        return {}


def baseline(ready, profile, use_db=True):
    """{артикул -> (цена, откуда)} для сравнения. Журнал важнее карточки: это наша же цифра."""
    base = {}
    if use_db:
        for article, price in previous_prices(profile.key).items():
            base[article] = (price, SRC_LOAD)
    for item in ready:
        if item["article"] in base:
            continue
        card = item["card"]
        if ms_api.meta_id(card, "supplier") not in profile.supplier_ids:
            continue                      # чужая закупочная — не наша база сравнения
        value = (card.get("buyPrice") or {}).get("value")
        if value:
            base[item["article"]] = (Decimal(value) / 100, SRC_CARD)
    return base


def screen(ready, profile, base):
    """(к загрузке, снятые с загрузки, флаги, сводка).

    Абсурдная цена снимает строку с загрузки (уходит в пропущенные с причиной
    `price_absurd`); прыжок/падение — только отметка в отчёте. Блокируют загрузку
    лишь флаги, посчитанные от НАШЕЙ прошлой загрузки, плюс абсурдные цены.
    """
    low, high = Decimal(profile.price_min_rub), Decimal(profile.price_max_rub)
    jump = Decimal(profile.jump_pct) / 100
    drop = Decimal(profile.drop_pct) / 100

    kept, dropped, flags = [], [], []
    ratios, ratios_card = [], []
    for item in ready:
        price = Decimal(item["price_kop"]) / 100
        prev, source = base.get(item["article"], (None, None))
        ratio = (price / prev) if prev and prev > 0 else None
        if ratio is not None:
            (ratios if source == SRC_LOAD else ratios_card).append(ratio)

        record = {**item, "price_rub": price, "prev_rub": prev,
                  "prev_source": source, "ratio": ratio}
        if price < low or price > high:
            flags.append({**record, "flag": "price_absurd"})
            dropped.append({**item, "reason": "price_absurd"})
            continue
        if ratio is not None and ratio - 1 > jump:
            flags.append({**record, "flag": "price_jump"})
        elif ratio is not None and 1 - ratio > drop:
            flags.append({**record, "flag": "price_drop"})
        kept.append(item)

    median = _median(ratios)
    shift = Decimal(profile.shift_pct) / 100
    # массовый сдвиг судим только по нашей прошлой загрузке и только на широкой базе
    mass_shift = bool(median is not None and len(ratios) >= 20 and abs(median - 1) > shift)
    hard = [f for f in flags
            if f["flag"] == "price_absurd" or f["prev_source"] == SRC_LOAD]
    share = (Decimal(len(hard)) / Decimal(len(ready))) if ready else Decimal(0)
    blocked = mass_shift or share > Decimal(profile.anomaly_share)

    summary = {
        "flags": len(flags),
        "hard_flags": len(hard),
        "by_flag": {f: sum(1 for x in flags if x["flag"] == f) for f in FLAGS},
        "dropped": len(dropped),
        "compared": len(ratios),
        "compared_card": len(ratios_card),
        "no_baseline": len(ready) - len(ratios) - len(ratios_card),
        "median_ratio": median,
        "median_card": _median(ratios_card),
        "mass_shift": mass_shift,
        "share": share,
        "blocked": blocked,
    }
    return kept, dropped, flags, summary


def reasons(summary, profile):
    """Человеческие причины блокировки — для консоли и для журнала."""
    out = []
    if summary["mass_shift"]:
        out.append(f"медиана цен уехала в {summary['median_ratio']:.3f} раза "
                   f"при допуске ±{profile.shift_pct}% — похоже на смену колонки или валюты")
    if summary["share"] > Decimal(profile.anomaly_share):
        out.append(f"аномальных позиций к прошлой загрузке {summary['share'] * 100:.1f}% "
                   f"при пороге {Decimal(profile.anomaly_share) * 100:.1f}%")
    return out


def write_report(flags, path):
    """Отчёт по аномалиям. Пишем всегда — пустой файл тоже ответ."""
    import csv
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["строка", "артикул", "наименование МС", "цена прайса",
                         "цена ₽", "было ₽", "база сравнения", "во сколько раз",
                         "флаг", "пояснение"])
        for row in flags:
            ratio = f"{row['ratio']:.3f}" if row["ratio"] is not None else ""
            writer.writerow([row["row"], row["article"], row.get("ms_name", ""),
                             row["price_raw"], f"{row['price_rub']:.2f}",
                             f"{row['prev_rub']:.2f}" if row["prev_rub"] else "",
                             row["prev_source"] or "", ratio,
                             row["flag"], FLAGS[row["flag"]]])
    return path
