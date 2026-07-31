# поток: gab
"""Шаг 1 пересборки габаритов: прочитать источники и положить их содержимое
с честными указателями. Полностью офлайн.

  ./venv/bin/python tools/rebuild/run_step1.py

Пишет docs/rebuild/data/<источник>.csv, sources_meta.json, stats.json,
rapid_unit_samples.txt. Ничего никуда не отправляет.

Что НЕ делает по условию шага: наследование, наборы, веб, сопоставление с WB,
обращение к supplier_dims, любые сетевые вызовы.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import (OUTDIR, ST_EMPTY, ST_LOADED, ST_LOADED_NOUNIT, ST_NOTFOUND, ST_SERVICE,
                    UNIT_UNKNOWN, num, write_csv)
from parsers import INCOMING, PARSERS
from verify import run_gate

# контрольные числа, снятые с самих файлов (задание Сергея)
CONTROL = {'cactus': 4367, 'izi': 2500, 'sakura_laser': 1498, 'profiline': 11877, 'rapid': 8483}
# прежние числа недостоверного загрузчика — только для фиксации масштаба потерь
OLD_SUPPLIER_DIMS = {'cactus': 4246, 'izi': 2040, 'profiline': 10624, 'rapid': 2026}


def stats_for(name: str, rows, meta: dict, info: dict) -> dict:
    st = Counter(r.status for r in rows)
    reasons = Counter(r.reason for r in rows if r.status == ST_NOTFOUND)
    with_iu = sum(1 for r in rows if r.iu_l_mm)
    with_mk = sum(1 for r in rows if r.mk_l_mm)
    raw_nounit = sum(1 for r in rows if r.status == ST_LOADED_NOUNIT)
    mism = sum(1 for r in rows if r.volume_mismatch == '1')
    volchecked = sum(1 for r in rows if r.volume_mismatch in ('0', '1'))
    alt_ok = sum(1 for r in rows if r.alt_agrees == '1')
    alt_bad = sum(1 for r in rows if r.alt_agrees == '0')
    mk_diag = sum(1 for r in rows if 'сходится_с_объемом=1' in (r.extra or ''))
    body = len(rows)
    total = body + info['header_rows'] + info['tail']
    return {
        'источник': name,
        'файл': Path(meta['file']).name,
        'лист': info['sheet'],
        'sha256': meta['sha256'],
        'размер_байт': meta['size'],
        'mtime': meta['mtime'],
        'строк_в_файле': total,
        'контроль_из_задания': CONTROL.get(name),
        'шапка_служебные_сверху': info['header_rows'],
        'пустой_хвост_excel': info['tail'],
        'записано_строк': body,
        'статусы': dict(st),
        'не_найдено_причины': dict(reasons),
        'с_ИУ_в_мм': with_iu,
        'с_МК_в_мм': with_mk,
        'сырьё_без_единиц': raw_nounit,
        'сверка_объёма_строк': volchecked,
        'volume_mismatch': mism,
        'дубль_короба_сходится': alt_ok,
        'дубль_короба_расходится': alt_bad,
        'мк_на_шт_сходится_с_объёмом': mk_diag,
        'старое_число_supplier_dims': OLD_SUPPLIER_DIMS.get(name),
    }


def check_arithmetic(s: dict) -> list[str]:
    """строк в файле = записано + шапка/служебные сверху + пустой хвост."""
    problems = []
    lhs = s['строк_в_файле']
    rhs = s['записано_строк'] + s['шапка_служебные_сверху'] + s['пустой_хвост_excel']
    if lhs != rhs:
        problems.append(f'{s["источник"]}: {lhs} != {rhs} (записано+шапка+хвост)')
    # у профилайна контрольное число задано по НЕПУСТЫМ строкам, у остальных — по всем
    ctrl = s['контроль_из_задания']
    if ctrl is not None:
        got = s['непустых'] if s['источник'] == 'profiline' else lhs
        if got != ctrl:
            problems.append(f'{s["источник"]}: контроль {ctrl}, посчитано {got}')
    return problems


def rapid_unit_samples(rows, n: int = 10) -> str:
    out = ['# rapid: единицы Gabarity* в файле НЕ объявлены. Сырьё как есть, решение за Сергеем.',
           '# ID_1C2; CodeID; GabarityDlina; GabarityShirina; GabarityVisota; Volume; Weight; Name']
    picked = [r for r in rows if r.status == ST_LOADED_NOUNIT and r.volume_raw and r.weight_raw]
    picked += [r for r in rows if r.status == ST_LOADED_NOUNIT and r not in picked]
    for r in picked:
        d = r.iu_raw.split('|')
        loc = r.src_locator
        out.append(f'{loc}; {r.supplier_code}; {d[0]}; {d[1]}; {d[2]}; '
                   f'{r.volume_raw}; {r.weight_raw}; {r.name[:48]}')
        if len(out) >= n + 2:
            break
    return '\n'.join(out)


def main() -> int:
    OUTDIR.mkdir(parents=True, exist_ok=True)
    all_stats, metas, problems = [], {}, []

    for name, fn in PARSERS.items():
        print(f'== {name}', flush=True)
        rows, meta, info = fn()
        csv_path = OUTDIR / f'{name}.csv'
        write_csv(rows, csv_path)

        checked = run_gate(name, csv_path)  # падает при первом расхождении
        s = stats_for(name, rows, meta, info)
        s['шлюз_проверено_значений'] = checked
        if name == 'profiline':
            s['непустых'] = sum(1 for r in rows if r.status != ST_EMPTY)
        problems += check_arithmetic(s)
        all_stats.append(s)
        metas[name] = meta | info
        print(f'   строк={s["записано_строк"]} ИУ={s["с_ИУ_в_мм"]} МК={s["с_МК_в_мм"]} '
              f'шлюз={checked}', flush=True)

        if name == 'rapid':
            (OUTDIR / 'rapid_unit_samples.txt').write_text(rapid_unit_samples(rows), encoding='utf-8')

    (OUTDIR / 'sources_meta.json').write_text(
        json.dumps(metas, ensure_ascii=False, indent=2), encoding='utf-8')
    (OUTDIR / 'stats.json').write_text(
        json.dumps({'источники': all_stats, 'дефекты_арифметики': problems},
                   ensure_ascii=False, indent=2), encoding='utf-8')

    if problems:
        print('\nДЕФЕКТ АРИФМЕТИКИ (парсер, не файл):')
        for p in problems:
            print('  ' + p)
        return 2
    print('\nарифметика сходится по всем источникам')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
