# поток: fin
"""Read-only import of accountant's Section I; never creates tax postings."""
from collections import defaultdict
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
import hashlib
from pathlib import Path
import re
import struct

import xlrd
from xlrd.compdoc import CompDoc

BASE_DIR = Path(__file__).resolve().parent.parent
SOURCES = {
    1: "20261014_115803_Natalie_Pro_natalya_КУДиР_1_квартал_2026_г_Раздел_I_Доходы_и_расходы_за_1_кв_.xls",
    2: "20261014_115808_Natalie_Pro_natalya_КУДиР_2_квартал_2026_г_Раздел_I_Доходы_и_расходы_за_2_кв_.xls",
}
CENT = Decimal("0.01")
ORGANISATIONS = {"cifrovoi": "Цифровой квадрат", "diskver": "Дисквер"}


def quarters(org="cifrovoi"):
    if org not in ORGANISATIONS:
        raise KeyError(org)
    return (1, 2, 3, 4) if org == "diskver" else (1, 2)


def money(value):
    return Decimal(str(value or 0)).quantize(CENT, rounding=ROUND_HALF_UP)


def source_path(quarter, org="cifrovoi"):
    # Fixed allowlist: no caller-supplied filename/path is accepted.
    if quarter not in quarters(org):
        raise KeyError(quarter)
    if org == "diskver":
        from reports import kudir_diskver
        return kudir_diskver.source_path()
    name = SOURCES[quarter]
    archived = BASE_DIR / "reports/data/kudir/originals" / name
    # Dropbox-bot's inbox is temporary; report downloads use preserved originals.
    return archived if archived.is_file() else BASE_DIR / "dropbox" / name


def formulas(path, book):
    """Recover BIFF formula ranges, not just the cached Excel totals."""
    compound = CompDoc(path.read_bytes())
    stream = compound.get_named_stream("Workbook") or compound.get_named_stream("Book")
    result = {}
    offset = 0
    while offset + 4 <= len(stream):
        code, size = struct.unpack_from("<HH", stream, offset)
        data = stream[offset + 4:offset + 4 + size]
        offset += 4 + size
        if code != 6 or len(data) < 22:
            continue
        row, col = struct.unpack_from("<HH", data)
        length = struct.unpack_from("<H", data, 20)[0]
        result[(row, col)] = xlrd.formula.decompile_formula(
            book, data[22:], length, xlrd.formula.FMLA_TYPE_CELL,
            browx=row, bcolx=col,
        )
    return result


def category(text):
    if "Себестоимость" in text:
        return "Себестоимость (месячные ручные строки)"
    if text.startswith("Отчет"):
        for needle, label in (("Интернет Решения", "Ozon"), ("РВБ", "WB"), ("Яндекс", "Маркет")):
            if needle in text:
                return "Отчёты " + label
        return "Отчёты прочие"
    if "Прочий приход денежных средств" in text:
        return "Прочие поступления"
    if "Поступила на банковский" in text:
        return "Оплата покупателей в банк"
    if "платежным картам" in text:
        return "Эквайринг"
    if "кредитными организациями" in text:
        return "Банковские услуги"
    if "заработная плата" in text:
        return "Зарплата"
    if "взнос" in text.lower():
        return "Взносы"
    return "Прочие операции — сверить первичку"


def parse_sheet(sheet, quarter, formula_map=None, datemode=0):
    formula_map = formula_map or {}
    # These are Section I extracts, not title pages or the full annual book.
    if sheet.ncols != 6 or not any(
        "Доходы, учитываемые" in str(sheet.cell_value(r, 4))
        for r in range(min(7, sheet.nrows))
    ):
        raise ValueError("Неизвестная структура раздела I КУДиР")
    operations = []
    totals = None
    current_document = ""
    document_date = None
    for row in range(sheet.nrows):
        label = str(sheet.cell_value(row, 1)).strip()
        if label.startswith("Итого за"):
            if totals is None:
                totals = {
                    "income": str(money(sheet.cell_value(row, 4))),
                    "expense": str(money(sheet.cell_value(row, 5))),
                    "row": row + 1,
                    "income_formula": formula_map.get((row, 4)),
                    "expense_formula": formula_map.get((row, 5)),
                }
            break  # Excludes unlabeled tax calculations and cumulative footer.
        description = str(sheet.cell_value(row, 3)).strip()
        if not description:
            continue
        income = money(sheet.cell_value(row, 4)) if sheet.cell_type(row, 4) == xlrd.XL_CELL_NUMBER else money(0)
        expense = money(sheet.cell_value(row, 5)) if sheet.cell_type(row, 5) == xlrd.XL_CELL_NUMBER else money(0)
        if not (income or expense):
            continue
        doc = sheet.cell_value(row, 2)
        inherited = not bool(doc)
        if doc:
            if sheet.cell_type(row, 2) == xlrd.XL_CELL_DATE:
                document_date = xlrd.xldate_as_datetime(doc, datemode).date().isoformat()
                current_document = datetime.fromisoformat(document_date).strftime("%d.%m.%Y")
            else:
                current_document = str(doc)
                match = re.search(r"\d{2}\.\d{2}\.\d{4}", current_document)
                document_date = datetime.strptime(match[0], "%d.%m.%Y").date().isoformat() if match else None
        outside = bool(document_date and (
            not document_date.startswith("2026-") or
            int(document_date[5:7]) not in range((quarter - 1) * 3 + 1, quarter * 3 + 1)
        ))
        operations.append({
            "row": row + 1, "number": label,
            "document": current_document, "document_inherited": inherited,
            "document_date": document_date, "date_outside_quarter": outside,
            "description": description, "income": str(income), "expense": str(expense),
            "category": category(description),
        })
    if totals is None:
        raise ValueError("Не найден квартальный итог КУДиР")
    actual = {key: str(sum((Decimal(o[key]) for o in operations), Decimal(0))) for key in ("income", "expense")}
    delta = {key: str(Decimal(actual[key]) - Decimal(totals[key])) for key in actual}
    excluded = []
    formula = totals["income_formula"] or ""
    match = re.fullmatch(r"SUM\(E(\d+):E(\d+)\)", formula)
    if match:
        first, last = map(int, match.groups())
        excluded = [o["row"] for o in operations if Decimal(o["income"]) and not first <= o["row"] <= last]
    groups = defaultdict(lambda: {"count": 0, "income": Decimal(0), "expense": Decimal(0)})
    for op in operations:
        g = groups[op["category"]]
        g["count"] += 1
        for key in ("income", "expense"):
            g[key] += Decimal(op[key])
    return {
        "quarter": quarter, "year": 2026, "org": "Цифровой квадрат",
        "org_inn": "7807355364", "org_source": "По поручению пользователя; титульного листа в файлах нет",
        "operations": operations, "accountant": totals, "calculated": actual,
        "delta": delta, "income_rows_outside_formula": excluded,
        "outside_document_dates": [o["row"] for o in operations if o["date_outside_quarter"]],
        "groups": [{"category": key, "count": g["count"], "income": str(g["income"]), "expense": str(g["expense"])} for key, g in groups.items()],
        "status": "accountant_import_with_discrepancy" if any(Decimal(v) for v in delta.values()) else "accountant_import_totals_match",
    }


@lru_cache(maxsize=8)
def _load(quarter, mtime_ns, size):
    path = source_path(quarter)
    book = xlrd.open_workbook(path)
    if book.nsheets != 1:
        raise ValueError("Ожидался один лист раздела I")
    result = parse_sheet(book.sheet_by_index(0), quarter, formulas(path, book), book.datemode)
    result.update(filename=path.name, sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    return result


def load(quarter, org="cifrovoi"):
    if quarter not in quarters(org):
        raise KeyError(quarter)
    if org == "diskver":
        from reports import kudir_diskver
        return kudir_diskver.load(quarter)
    path = source_path(quarter)
    stat = path.stat()
    return _load(quarter, stat.st_mtime_ns, stat.st_size)
