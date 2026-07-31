# поток: gab
"""Шаг 1 пересборки габаритов: общие примитивы.

Правила, зашитые здесь (см. docs/BRIEF_GAB.md):
  * единица измерения берётся ТОЛЬКО из того, что объявлено в самом файле;
    не объявлена — значение остаётся сырым текстом и не пересчитывается;
  * у каждой величины есть указатель src_file / src_locator / src_field / src_value_raw;
  * ИУ и МК живут в разных полях, МК никогда не становится размером карточки.
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field, asdict
from pathlib import Path

INCOMING = Path('/opt/mp-analytics/incoming/gab')
OUTDIR = Path(__file__).resolve().parents[2] / 'docs' / 'rebuild' / 'data'

# --- статусы строки ---------------------------------------------------------
ST_LOADED = 'ЗАГРУЖЕНО'                      # есть ДхШхВ в объявленных единицах
ST_LOADED_NOUNIT = 'ЗАГРУЖЕНО_БЕЗ_ЕДИНИЦ'    # ДхШхВ есть, единица в файле не объявлена
ST_NOTFOUND = 'НЕ_НАЙДЕНО'
ST_SERVICE = 'СЛУЖЕБНАЯ'
ST_EMPTY = 'ПУСТАЯ_СТРОКА'

UNIT_UNKNOWN = 'ЕДИНИЦА_НЕ_ОПРЕДЕЛЕНА'
UNIT_TO_MM = {'мм': 1.0, 'см': 10.0, 'м': 1000.0}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def file_meta(path: Path) -> dict:
    st = path.stat()
    return {
        'file': str(path),
        'size': st.st_size,
        'mtime': __import__('datetime').datetime.fromtimestamp(st.st_mtime).isoformat(timespec='seconds'),
        'sha256': sha256(path),
    }


def canon(v) -> str:
    """Каноничное текстовое представление ячейки.

    Одна и та же функция применяется и при загрузке, и при обратной проверке
    указателя, поэтому сравнение сырых значений осмысленно.
    """
    if v is None:
        return ''
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        return str(int(v)) if v.is_integer() else repr(v)
    return str(v).strip()


def num(raw: str):
    """Число из сырого текста. Не угадывает единицы — только парсит запись."""
    if raw is None:
        return None
    s = str(raw).strip().replace(' ', '').replace(' ', '').replace(',', '.')
    if not s:
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    return val


def to_mm(raw: str, unit: str):
    """Пересчёт в мм ТОЛЬКО при объявленной в файле единице."""
    if unit not in UNIT_TO_MM:
        return None
    v = num(raw)
    return None if v is None else round(v * UNIT_TO_MM[unit], 3)


@dataclass
class Row:
    source: str = ''
    src_file: str = ''
    src_sheet: str = ''
    src_locator: str = ''
    src_sha256: str = ''
    status: str = ''
    reason: str = ''
    supplier_code: str = ''
    oem_code: str = ''
    name: str = ''
    brand: str = ''
    # индивидуальная упаковка (единственный источник размера карточки)
    iu_l_mm: str = ''
    iu_w_mm: str = ''
    iu_h_mm: str = ''
    iu_unit: str = ''
    iu_fields: str = ''
    iu_raw: str = ''
    # мастер-короб — отдельные поля, в размер карточки не попадают никогда
    mk_l_mm: str = ''
    mk_w_mm: str = ''
    mk_h_mm: str = ''
    mk_unit: str = ''
    mk_fields: str = ''
    mk_raw: str = ''
    # дублирующая запись того же короба (у сакуры струйной — в см)
    alt_l_mm: str = ''
    alt_w_mm: str = ''
    alt_h_mm: str = ''
    alt_unit: str = ''
    alt_fields: str = ''
    alt_raw: str = ''
    alt_agrees: str = ''
    weight_field: str = ''
    weight_raw: str = ''
    volume_field: str = ''
    volume_raw: str = ''
    qty_in_pack: str = ''
    vol_calc_m3: str = ''
    vol_declared_m3: str = ''
    vol_delta_pct: str = ''
    volume_mismatch: str = ''
    extra: str = ''

    def probe(self) -> dict:
        """Пары (поле источника → сырое значение) для обратной проверки указателя."""
        out = {}
        for fields, raws in ((self.iu_fields, self.iu_raw), (self.mk_fields, self.mk_raw),
                             (self.alt_fields, self.alt_raw)):
            if fields:
                fs, rs = fields.split('|'), raws.split('|')
                for f, r in zip(fs, rs):
                    if f:
                        out[f] = r
        if self.weight_field:
            out[self.weight_field] = self.weight_raw
        if self.volume_field:
            out[self.volume_field] = self.volume_raw
        return out


FIELDS = list(Row.__dataclass_fields__.keys())


def write_csv(rows, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, delimiter=';')
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))


def set_dims(row: Row, kind: str, triples, unit: str) -> bool:
    """triples: [(имя колонки, сырое значение), ...] в порядке Д, Ш, В."""
    fields = '|'.join(f for f, _ in triples)
    raws = '|'.join(r for _, r in triples)
    setattr(row, f'{kind}_fields', fields)
    setattr(row, f'{kind}_raw', raws)
    setattr(row, f'{kind}_unit', unit if unit in UNIT_TO_MM else UNIT_UNKNOWN)
    if unit not in UNIT_TO_MM:
        return False
    vals = [to_mm(r, unit) for _, r in triples]
    if any(v is None or v <= 0 for v in vals):
        return False
    for key, v in zip(('l', 'w', 'h'), vals):
        setattr(row, f'{kind}_{key}_mm', f'{v:g}')
    return True


def volume_check(row: Row, kind: str, declared_raw: str, tol: float = 0.05) -> None:
    """Сверка объёма как индикатор. Ничего не чинит и не пересчитывает размеры."""
    declared = num(declared_raw)
    dims = [num(getattr(row, f'{kind}_{k}_mm')) for k in ('l', 'w', 'h')]
    if declared is None or declared <= 0 or any(d is None for d in dims):
        return
    calc = dims[0] * dims[1] * dims[2] / 1e9  # мм³ → м³
    row.vol_calc_m3 = f'{calc:.6f}'
    row.vol_declared_m3 = f'{declared:.6f}'
    delta = (calc - declared) / declared * 100
    row.vol_delta_pct = f'{delta:.1f}'
    row.volume_mismatch = '1' if abs(delta) > tol * 100 else '0'
