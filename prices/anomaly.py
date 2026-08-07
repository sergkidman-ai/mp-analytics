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

База сравнения — **закупочная цена, которую записали МЫ** (решение Сергея 05.08.2026):
цена этой же позиции в нашей прошлой БОЕВОЙ загрузке. Технически берём её из журнала
`prc_price_row`, а не читаем `buyPrice` из карточки, и это осознанно: в карточке может
лежать чужая цифра — та, что жила там до нас, или правка руками. «Было 50 ₽, стало 2 ₽»
имеет смысл только против своей же записи. Проверено на трёх поставщиках: цена прайса
относится к живущей в карточках закупочной как 0.77-0.91 по медиане, то есть чужая
закупочная — вообще не цена прайса, и сравнивать с ней бессмысленно.

Пока боевой загрузки поставщика не было, базы нет: строка проверяется только на коридор.
Со второго дня проверка включается сама.
"""
from decimal import Decimal

SRC_LOAD = "наша прошлая закупочная"

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


def baseline(profile, use_db=True):
    """{артикул -> (цена, откуда)} — закупочная, записанная нами в прошлую боевую загрузку."""
    if not use_db:
        return {}
    return {article: (price, SRC_LOAD)
            for article, price in previous_prices(profile.key).items()}


def screen(ready, profile, base):
    """(к загрузке, снятые с загрузки, флаги, сводка).

    Абсурдная цена снимает строку с загрузки (уходит в пропущенные с причиной
    `price_absurd`) — такую цену нельзя ни класть на склад, ни писать в закупочную
    карточки; прыжок/падение — отметка в отчёте и вклад в блокировку `--apply`.
    """
    low, high = Decimal(profile.price_min_rub), Decimal(profile.price_max_rub)
    jump = Decimal(profile.jump_pct) / 100
    drop = Decimal(profile.drop_pct) / 100

    kept, dropped, flags, ratios = [], [], [], []
    for item in ready:
        price = Decimal(item["price_kop"]) / 100
        prev, source = base.get(item["article"], (None, None))
        ratio = (price / prev) if prev and prev > 0 else None
        if ratio is not None:
            ratios.append(ratio)

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
    # массовый сдвиг судим только на широкой базе: на десятке позиций медиана ничего не значит
    mass_shift = bool(median is not None and len(ratios) >= 20 and abs(median - 1) > shift)
    share = (Decimal(len(flags)) / Decimal(len(ready))) if ready else Decimal(0)
    blocked = mass_shift or share > Decimal(profile.anomaly_share)

    summary = {
        "flags": len(flags),
        "by_flag": {f: sum(1 for x in flags if x["flag"] == f) for f in FLAGS},
        "dropped": len(dropped),
        "compared": len(ratios),
        "no_baseline": len(ready) - len(ratios),
        "median_ratio": median,
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
        out.append(f"аномальных позиций {summary['share'] * 100:.1f}% "
                   f"при пороге {Decimal(profile.anomaly_share) * 100:.1f}%")
    return out


def write_report(flags, path):
    """Отчёт по аномалиям. Пишем всегда — пустой файл тоже ответ."""
    import csv
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh, delimiter=";")
        writer.writerow(["строка", "артикул", "наименование МС", "цена прайса",
                         "цена ₽", "наша прошлая закупочная ₽", "база сравнения",
                         "во сколько раз", "флаг", "пояснение"])
        for row in flags:
            ratio = f"{row['ratio']:.3f}" if row["ratio"] is not None else ""
            writer.writerow([row["row"], row["article"], row.get("ms_name", ""),
                             row["price_raw"], f"{row['price_rub']:.2f}",
                             f"{row['prev_rub']:.2f}" if row["prev_rub"] else "",
                             row["prev_source"] or "", ratio,
                             row["flag"], FLAGS[row["flag"]]])
    return path
