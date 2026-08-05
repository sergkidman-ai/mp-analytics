# поток: prc
# -*- coding: utf-8 -*-
"""
Разбор прайса поставщика в плоский список строк.

Колонки ищутся по ЗАГОЛОВКУ из профиля, а не по номеру: поставщик может вставить колонку,
и загрузка не должна от этого поехать. Если хоть один заголовок не найден — стоп с внятной
ошибкой, а не тихий разбор половины файла.
"""
import io

import openpyxl

from .profiles import norm


class PriceFormatError(RuntimeError):
    pass


def _find_header(sheet, wanted, scan_rows):
    """Строка шапки и карта {логическое имя -> номер колонки}."""
    targets = {key: norm(title) for key, title in wanted.items()}
    for row in range(1, min(scan_rows, sheet.max_row) + 1):
        found = {}
        for col in range(1, sheet.max_column + 1):
            cell = norm(sheet.cell(row, col).value)
            for key, title in targets.items():
                if cell == title and key not in found:
                    found[key] = col
        if len(found) == len(targets):
            return row, found
    raise PriceFormatError(
        f"шапка не найдена в первых {scan_rows} строках; ищем колонки: "
        + ", ".join(sorted(wanted.values()))
    )


def parse(content, profile):
    """bytes прайса -> (список строк, номер строки шапки).

    Строка: dict(row, article, name, stock_raw, qty, price_raw). qty=None — формулировка
    остатка не разобрана, такую строку загрузчик не грузит и выносит в отчёт.
    """
    book = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=False)
    sheet = book[profile.sheet] if profile.sheet and profile.sheet in book.sheetnames else book[book.sheetnames[0]]
    header_row, cols = _find_header(sheet, profile.columns, profile.header_scan_rows)

    rows = []
    for idx in range(header_row + 1, sheet.max_row + 1):
        article = sheet.cell(idx, cols["article"]).value
        if article is None or str(article).strip() == "":
            continue
        if isinstance(article, float) and article.is_integer():
            article = int(article)
        stock_raw = sheet.cell(idx, cols["stock"]).value
        rows.append({
            "row": idx,
            "article": str(article).strip(),
            "name": str(sheet.cell(idx, cols["name"]).value or "").strip(),
            "stock_raw": None if stock_raw is None else str(stock_raw).strip(),
            "qty": profile.qty(stock_raw),
            "price_raw": sheet.cell(idx, cols["price"]).value,
        })
    book.close()
    if not rows:
        raise PriceFormatError("в прайсе нет ни одной строки с артикулом")
    return rows, header_row
