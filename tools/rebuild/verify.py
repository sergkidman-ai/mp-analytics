# поток: gab
"""Шаг 1, шлюз: обратная проверка указателя.

Загрузчик обязан по своему же src_locator заново открыть исходник и прочитать
то же самое значение. Проверка идёт по ВСЕЙ загрузке, не по выборке.
Первое расхождение — исключение с файлом, строкой и обоими значениями.
Этот модуль намеренно не переиспользует парсеры: он читает файлы своим кодом.
"""
from __future__ import annotations

import csv
import json
import re
from pathlib import Path

import openpyxl
import xlrd

from common import INCOMING, canon
from parsers import SAKURA_FILE

XLSX_SOURCES = {
    'cactus': ('Вся расходка Китай РФ Cactus GG PR.xlsx', 'Лист1', 1),
    'izi': ('Изи.xlsx', 'Данные по картриджам', 1),
    'sakura_laser': (SAKURA_FILE, 'Лазерная Sakura', 1),
    'sakura_inkjet': (SAKURA_FILE, 'Струйная Sakura', 1),
}
LOC_XLSX = re.compile(r'^(?P<sheet>.+)!r(?P<row>\d+)$')
LOC_JSON = re.compile(r'^price\[(?P<idx>\d+)\]#ID_1C2=(?P<id>.*)$')


class GateError(RuntimeError):
    pass


def _xlsx_probe(source: str, wanted: dict[int, set[str]]) -> dict[int, dict[str, str]]:
    fname, sheet, hdr_row = XLSX_SOURCES[source]
    wb = openpyxl.load_workbook(INCOMING / fname, read_only=True, data_only=True)
    ws = wb[sheet]
    header, got = None, {}
    for i, raw in enumerate(ws.iter_rows(values_only=True), 1):
        if i == hdr_row:
            header = {canon(v).strip(): j for j, v in enumerate(raw) if canon(v)}
            continue
        if i not in wanted:
            continue
        vals = [canon(v) for v in raw]
        got[i] = {f: (vals[header[f]] if f in header and header[f] < len(vals) else None)
                  for f in wanted[i]}
    wb.close()
    return got


def _xls_probe(wanted: dict[int, set[str]]) -> dict[int, dict[str, str]]:
    sh = xlrd.open_workbook(INCOMING / 'Профилайн.xls').sheet_by_index(0)
    header = {canon(v).strip(): j for j, v in enumerate(sh.row_values(9)) if canon(v)}
    got = {}
    for rn in wanted:
        vals = [canon(v) for v in sh.row_values(rn - 1)]
        got[rn] = {f: (vals[header[f]] if f in header and header[f] < len(vals) else None)
                   for f in wanted[rn]}
    return got


def _json_probe(wanted: dict[int, set[str]]) -> dict[int, dict[str, str]]:
    doc = json.loads((INCOMING / 'rapid_2026-07-18.json').read_text(encoding='utf-8'))
    recs = doc['price']
    return {i: {f: canon(recs[i].get(f)) for f in fields} for i, fields in wanted.items()}


def _probes_from_csv(path: Path):
    """Читает записанный CSV обратно с диска и восстанавливает пары поле→сырьё."""
    with path.open(encoding='utf-8', newline='') as fh:
        for row in csv.DictReader(fh, delimiter=';'):
            probe = {}
            for fkey, rkey in (('iu_fields', 'iu_raw'), ('mk_fields', 'mk_raw'),
                               ('alt_fields', 'alt_raw')):
                if row[fkey]:
                    fs, rs = row[fkey].split('|'), row[rkey].split('|')
                    for f, r in zip(fs, rs):
                        if f:
                            probe[f] = r
            for fkey, rkey in (('weight_field', 'weight_raw'), ('volume_field', 'volume_raw'),
                               ('code_field', 'supplier_code'), ('oem_field', 'oem_code'),
                               ('barcode_field', 'barcode_raw')):
                if row[fkey]:
                    probe[row[fkey]] = row[rkey]
            yield row['src_locator'], probe


def run_gate(source: str, csv_path: Path) -> int:
    """Возвращает число проверенных строк. Расхождение — GateError."""
    wanted: dict[int, set[str]] = {}
    order: list[tuple[str, int, dict]] = []
    for loc, probe in _probes_from_csv(csv_path):
        if source == 'rapid':
            m = LOC_JSON.match(loc)
            if not m:
                raise GateError(f'{source}: нечитаемый указатель «{loc}»')
            key = int(m.group('idx'))
            probe = dict(probe, ID_1C2=m.group('id'))  # указатель обязан попасть в ту же запись
        else:
            m = LOC_XLSX.match(loc)
            if not m:
                raise GateError(f'{source}: нечитаемый указатель «{loc}»')
            key = int(m.group('row'))
        order.append((loc, key, probe))
        wanted.setdefault(key, set()).update(probe.keys())

    if source == 'rapid':
        got = _json_probe(wanted)
    elif source == 'profiline':
        got = _xls_probe(wanted)
    else:
        got = _xlsx_probe(source, wanted)

    checked = 0
    for loc, key, probe in order:
        if key not in got:
            raise GateError(f'{source}: указатель «{loc}» не открывается обратно')
        for fname, expected in probe.items():
            actual = got[key].get(fname)
            if actual is None:
                raise GateError(f'{source}: поле «{fname}» не найдено по указателю «{loc}»')
            if actual != expected:
                raise GateError(
                    f'ШЛЮЗ: расхождение. файл={source} указатель={loc} поле={fname} '
                    f'записано=«{expected}» в файле=«{actual}»')
            checked += 1
    return checked
