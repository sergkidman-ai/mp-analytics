# поток: gab
"""Шаг 2: единый реестр разобранных строк и конфликты между поставщиками.

  ./venv/bin/python tools/rebuild/step2_registry.py

Вход — только выгрузки шага 1 (docs/rebuild/data/<источник>.csv). Сети нет,
supplier_dims не открывается. Ничего не выбирается, не усредняется и не
округляется: реестр показывает все записи как есть, конфликты — как есть.

Пишет: registry.csv, keys.csv, conflicts.csv, mk.csv, step2_stats.json.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common import OUTDIR, ST_LOADED, ST_LOADED_NOUNIT, num

csv.field_size_limit(1 << 24)

# источники с доказанной в файле единицей → размеры приведены к мм
SRC_MM = ('cactus', 'izi', 'sakura_laser', 'sakura_inkjet')
# рапид: единица объёмом не подтверждена, принадлежность ИУ/МК не объявлена → карантин
SRC_QUARANTINE = ('rapid',)

# префиксы поставщика в его собственном артикуле; снимаются только эти, только целиком
CACTUS_PREFIXES = {'CS', 'CSP', 'CR', 'GG', 'PR', 'СS'}  # последний — кириллица, опечатка в файле
RAPID_PREFIXES = {'SF', 'SFR'}

CONFLICT_MM = 5.0            # расхождение любой стороны больше 5 мм = конфликт
BUCKETS = ((5, 10), (10, 30), (30, 100), (100, float('inf')))
TOKEN_SEP = re.compile(r'[/;,\s]+')


# --- нормализация ключей ----------------------------------------------------
def norm_code(s: str) -> str:
    return re.sub(r'[^0-9A-ZА-Я]', '', (s or '').upper())


def norm_barcode(s: str) -> str:
    d = re.sub(r'\D', '', s or '')
    return d if len(d) >= 8 else ''


def key_risk(k: str, ktype: str = 'oem') -> str:
    """Слабый ключ = короткий или чисто цифровой код: такой может склеить разные товары.

    К штрихкоду не применяется: там 8–14 цифр — это нормальная форма ключа.
    """
    if ktype == 'штрихкод':
        return 'обычный'
    return 'слабый' if (k.isdigit() or len(k) < 5) else 'обычный'


def strip_prefix(code: str, prefixes: set[str]):
    if '-' in code:
        head, rest = code.split('-', 1)
        if head.upper() in prefixes and rest.strip():
            return rest.strip(), f'{head}-'
    return code, ''


def oem_tokens(raw: str):
    """Ячейка аналога может нести несколько кодов через / ; , пробел."""
    out = []
    for t in TOKEN_SEP.split(raw or ''):
        k = norm_code(t)
        if len(k) >= 3 and any(c.isdigit() for c in k):
            out.append((k, t))
    return out


# --- чтение выгрузок шага 1 -------------------------------------------------
def load_rows(source: str):
    path = OUTDIR / f'{source}.csv'
    with path.open(encoding='utf-8', newline='') as fh:
        for row in csv.DictReader(fh, delimiter=';'):
            yield row


def registry_records():
    """Записи с прочитанным коробом ИУ. Указатель сохраняется целиком."""
    recs = []
    for source in SRC_MM + SRC_QUARANTINE:
        for row in load_rows(source):
            quarantine = row['quarantine'] == '1'
            if quarantine:
                if row['status'] != ST_LOADED_NOUNIT:
                    continue
            elif row['status'] != ST_LOADED or not row['iu_l_mm']:
                continue
            dims = [num(row[f'iu_{k}_mm']) for k in ('l', 'w', 'h')]
            recs.append({
                'source': source,
                'quarantine': '1' if quarantine else '',
                'supplier_code': row['supplier_code'],
                'oem_code': row['oem_code'],
                'barcode': row['barcode_raw'],
                'brand': row['brand'],
                'name': row['name'][:120],
                'iu_l_mm': row['iu_l_mm'], 'iu_w_mm': row['iu_w_mm'], 'iu_h_mm': row['iu_h_mm'],
                'iu_unit': row['iu_unit'],
                'объём_л': (f'{dims[0] * dims[1] * dims[2] / 1e6:.3f}'
                            if not quarantine and all(dims) else ''),
                'weight_raw': row['weight_raw'], 'volume_raw': row['volume_raw'],
                'volume_mismatch': row['volume_mismatch'],
                'src_file': row['src_file'], 'src_sheet': row['src_sheet'],
                'src_locator': row['src_locator'], 'src_fields': row['iu_fields'],
                'src_value_raw': row['iu_raw'], 'src_sha256': row['src_sha256'],
                '_dims': dims if not quarantine else None,
            })
    return recs


def master_cartons():
    out = []
    for source in SRC_MM:
        for row in load_rows(source):
            if not row['mk_l_mm']:
                continue
            d = [num(row[f'mk_{k}_mm']) for k in ('l', 'w', 'h')]
            out.append({
                'source': source, 'supplier_code': row['supplier_code'],
                'oem_code': row['oem_code'], 'name': row['name'][:120],
                'mk_l_mm': row['mk_l_mm'], 'mk_w_mm': row['mk_w_mm'], 'mk_h_mm': row['mk_h_mm'],
                'объём_л': f'{d[0] * d[1] * d[2] / 1e6:.2f}',
                'кратность': row['qty_in_pack'],
                'пометка': 'МАСТЕР-КОРОБ: размером карточки быть не может',
                'src_file': row['src_file'], 'src_locator': row['src_locator'],
                'src_fields': row['mk_fields'], 'src_value_raw': row['mk_raw'],
                'src_sha256': row['src_sha256'],
            })
    return out


# --- ключи ------------------------------------------------------------------
def record_keys(rec: dict):
    """[(тип ключа, нормализованный, сырой, откуда взят), ...] для одной записи."""
    out = []
    bc = norm_barcode(rec['barcode'])
    if bc:
        out.append(('штрихкод', bc, rec['barcode'], 'колонка файла'))
    code = norm_code(rec['supplier_code'])
    if code:
        out.append(('артикул_поставщика', code, rec['supplier_code'], 'колонка файла'))

    if rec['source'] == 'cactus':
        body, pref = strip_prefix(rec['supplier_code'], CACTUS_PREFIXES)
        if pref:
            for k, raw in oem_tokens(body):
                out.append(('oem', k, raw, f'PartNo без префикса «{pref}»'))
    elif rec['source'] == 'rapid':
        # CodeShort у рапида — внутренний числовой код (1201081), это НЕ OEM
        body, pref = strip_prefix(rec['supplier_code'], RAPID_PREFIXES)
        for k, raw in oem_tokens(body):
            out.append(('oem', k, raw, f'CodeID без префикса «{pref}»' if pref else 'CodeID'))
    else:
        for k, raw in oem_tokens(rec['oem_code']):
            out.append(('oem', k, raw, 'колонка аналога'))
    return out


# --- конфликты --------------------------------------------------------------
def find_conflicts(groups: dict, key_type: str):
    """Внутри одной группы ключа сравниваем отсортированные тройки сторон.

    Ориентация короба смыслом не является (правило 9 CLAUDE.md), поэтому
    320×90×100 и 320×100×90 — не конфликт; такие случаи считаются отдельно.
    """
    rows, orient_only = [], 0
    for key, members in groups.items():
        variants = {}
        for rec in members:
            if not rec['_dims']:
                continue
            trip = tuple(sorted(rec['_dims']))
            v = variants.setdefault((rec['source'], trip), {'rec': rec, 'n': 0, 'raw': set()})
            v['n'] += 1
            v['raw'].add(tuple(rec['_dims']))
        for v in variants.values():
            if len(v['raw']) > 1:
                orient_only += 1
        for (sa, ta), (sb, tb) in combinations(sorted(variants), 2):
            diffs = [abs(x - y) for x, y in zip(ta, tb)]
            worst = max(diffs)
            if worst <= CONFLICT_MM:
                continue
            a, b = variants[(sa, ta)], variants[(sb, tb)]
            va, vb = ta[0] * ta[1] * ta[2] / 1e6, tb[0] * tb[1] * tb[2] / 1e6
            rows.append({
                'ключ': key, 'тип_ключа': key_type, 'надёжность_ключа': key_risk(key, key_type),
                'тип_конфликта': 'между поставщиками' if sa != sb else 'внутри поставщика',
                'макс_разница_мм': f'{worst:.1f}',
                'разница_по_сторонам_мм': '|'.join(f'{d:.1f}' for d in diffs),
                'объём_A_л': f'{va:.3f}', 'объём_B_л': f'{vb:.3f}',
                'отношение_объёмов': f'{max(va, vb) / min(va, vb):.2f}' if min(va, vb) else '',
                'A_поставщик': sa, 'A_код': a['rec']['supplier_code'],
                'A_наименование': a['rec']['name'][:60],
                'A_ДхШхВ_мм': '×'.join(f'{x:g}' for x in a['rec']['_dims']),
                'A_строк_с_таким_коробом': a['n'],
                'A_файл': a['rec']['src_file'], 'A_указатель': a['rec']['src_locator'],
                'A_поля': a['rec']['src_fields'], 'A_сырьё': a['rec']['src_value_raw'],
                'A_sha256': a['rec']['src_sha256'],
                'B_поставщик': sb, 'B_код': b['rec']['supplier_code'],
                'B_наименование': b['rec']['name'][:60],
                'B_ДхШхВ_мм': '×'.join(f'{x:g}' for x in b['rec']['_dims']),
                'B_строк_с_таким_коробом': b['n'],
                'B_файл': b['rec']['src_file'], 'B_указатель': b['rec']['src_locator'],
                'B_поля': b['rec']['src_fields'], 'B_сырьё': b['rec']['src_value_raw'],
                'B_sha256': b['rec']['src_sha256'],
            })
    return rows, orient_only


def write_csv(rows, path: Path, fields=None) -> None:
    fields = fields or (list(rows[0].keys()) if rows else ['пусто'])
    with path.open('w', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=fields, delimiter=';', extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    recs = registry_records()
    reg_fields = [k for k in recs[0] if not k.startswith('_')]
    write_csv(recs, OUTDIR / 'registry.csv', reg_fields)

    mk = master_cartons()
    write_csv(mk, OUTDIR / 'mk.csv')

    links, groups = [], defaultdict(lambda: defaultdict(list))
    for rec in recs:
        for ktype, knorm, kraw, origin in record_keys(rec):
            links.append({'тип_ключа': ktype, 'ключ': knorm, 'сырьё_ключа': kraw,
                          'надёжность_ключа': key_risk(knorm, ktype), 'откуда': origin,
                          'поставщик': rec['source'], 'код_поставщика': rec['supplier_code'],
                          'файл': rec['src_file'], 'указатель': rec['src_locator'],
                          'карантин': rec['quarantine']})
            groups[ktype][knorm].append(rec)
    write_csv(links, OUTDIR / 'keys.csv')

    conflicts, orient = [], {}
    for ktype in ('oem', 'штрихкод', 'артикул_поставщика'):
        rows, o = find_conflicts(groups[ktype], ktype)
        conflicts += rows
        orient[ktype] = o
    conflicts.sort(key=lambda r: -float(r['макс_разница_мм']))
    write_csv(conflicts, OUTDIR / 'conflicts.csv')

    # --- статистика
    def group_stats(ktype):
        g = groups[ktype]
        multi = {k: v for k, v in g.items() if len({r['source'] for r in v}) >= 2}
        return {
            'групп_всего': len(g),
            'записей_в_группах': sum(len(v) for v in g.values()),
            'групп_в_2+_источниках': len(multi),
            'групп_слабый_ключ': sum(1 for k in g if key_risk(k, ktype) == 'слабый'),
            'ориентация_без_конфликта': orient.get(ktype, 0),
        }

    by_bucket = Counter()
    for c in conflicts:
        d = float(c['макс_разница_мм'])
        for lo, hi in BUCKETS:
            if lo < d <= hi:
                by_bucket[f'{lo}-{hi if hi != float("inf") else "∞"} мм'] += 1
    oem_multi = {k for k, v in groups['oem'].items() if len({r['source'] for r in v}) >= 2}
    stats = {
        'записей_в_реестре': len(recs),
        'по_источникам': dict(Counter(r['source'] for r in recs)),
        'в_карантине': sum(1 for r in recs if r['quarantine']),
        'мастер_коробов': len(mk),
        'связей_всего': len(links),
        'связей_по_типу': dict(Counter(l['тип_ключа'] for l in links)),
        'ключи': {k: group_stats(k) for k in ('oem', 'штрихкод', 'артикул_поставщика')},
        'изделий_по_oem': len(groups['oem']),
        'изделий_в_2+_поставщиках': len(oem_multi),
        'конфликтов_всего': len(conflicts),
        'конфликтов_по_типу': dict(Counter(c['тип_конфликта'] for c in conflicts)),
        'конфликтов_по_ключу': dict(Counter(c['тип_ключа'] for c in conflicts)),
        'конфликтов_слабый_ключ': sum(1 for c in conflicts if c['надёжность_ключа'] == 'слабый'),
        'распределение_разниц': dict(by_bucket),
    }
    (OUTDIR / 'step2_stats.json').write_text(
        json.dumps(stats, ensure_ascii=False, indent=2), encoding='utf-8')

    print(f'реестр={len(recs)} (карантин {stats["в_карантине"]}) связей={len(links)} '
          f'изделий_по_oem={stats["изделий_по_oem"]} в_2+={len(oem_multi)} '
          f'конфликтов={len(conflicts)}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
