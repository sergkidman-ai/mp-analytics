# поток: gab
"""Шаг 7 — чистка и независимая перепроверка выбранных размеров.

1) own_manual (доп. поля МС «Длина/Ширина/Высота, см.») удалён как источник; покрытие
   пересчитано только по прайсам.
2) Каждый выбранный размер перечитывается ИЗ ИСХОДНОГО ФАЙЛА по его src_locator
   (лист!rN) и тем же колонкам, что указаны в парсере, и сверяется со сборкой.
3) Остаток без размера разбит по типу позиции (наборы / принтерные / прочее).
Ничего не пишется в реестр и на площадки.
"""
import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import openpyxl
import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_bridge import DATA                                     # noqa: E402

csv.field_size_limit(10 ** 7)
INCOMING = Path('/opt/mp-analytics/incoming/gab')
IS4 = re.compile(r'^\d{4}$')

# прайс -> (файл, лист, колонка кода, колонки габаритов, множитель к мм, разделитель в одной ячейке)
SRC = {
    'cactus': (INCOMING / 'Вся расходка Китай РФ Cactus GG PR.xlsx', 'Лист1', 'PartNo',
               ('Длина(ИУ) мм', 'Ширина(ИУ) мм', 'Высота(ИУ) мм'), 1.0, None),
    'izi': (INCOMING / 'Изи.xlsx', 'Данные по картриджам', 'артикул',
            ('Размеры мм',), 1.0, r'[*xх×]'),
    'sakura_laser': (INCOMING / 'САКУРА АБДУЛ 05.06.xlsx', 'Лазерная Sakura', 'Артикул',
                     ('Длина м', 'Ширина м', 'Высота м'), 1000.0, None),
    'sakura_inkjet': (INCOMING / 'САКУРА АБДУЛ 05.06.xlsx', 'Струйная Sakura',
                      'Код производителя Sakura',
                      ('Длина коробки мм', 'Ширина коробки мм', 'Высота коробки мм'), 1.0, None),
}
NABOR = re.compile(r'набор|комплект|\bкартриджи\b|\d\s*шт|\bx\s?[2-9]\b', re.I)
PRINTER = re.compile(r'принтер|мфу|копир|плоттер|сканер', re.I)


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open('rb') as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def cell_str(v) -> str:
    if v is None:
        return ''
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v).strip()


def to_mm(vals, mult, splitter):
    """значения ячеек -> кортеж мм по убыванию; None, если не читается."""
    if splitter:
        parts = [p for p in re.split(splitter, vals[0]) if p.strip()]
    else:
        parts = list(vals)
    try:
        d = [float(str(p).replace(',', '.').strip()) * mult for p in parts]
    except (ValueError, TypeError):
        return None
    if len(d) != 3 or min(d) <= 0:
        return None
    return tuple(sorted(d, reverse=True))


def main() -> int:
    chosen = list(csv.DictReader((DATA / 'step6_chosen_box.csv').open(encoding='utf-8'), delimiter=';'))
    st = {}

    # ---- 2. независимая перечитка каждой выбранной строки --------------------
    need = defaultdict(set)          # (файл, лист) -> номера строк
    for r in chosen:
        f, sh, *_ = SRC[r['прайс']]
        need[(f, sh)].add(int(r['src_locator'].split('!r')[1]))
    cache = {}
    heads = {}
    for (f, sh), rns in need.items():
        wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
        ws = wb[sh]
        hdr = None
        for i, raw in enumerate(ws.iter_rows(values_only=True), 1):
            if i == 1:
                hdr = [cell_str(v) for v in raw]
                heads[(f, sh)] = hdr
                continue
            if i in rns:
                cache[(f, sh, i)] = [cell_str(v) for v in raw]
            if i > max(rns):
                break
        wb.close()
    file_sha = {f: sha256(f) for f, _ in need}

    bad_sha = bad_code = bad_dims = 0
    diffs = []
    for r in chosen:
        f, sh, code_col, dim_cols, mult, splitter = SRC[r['прайс']]
        rn = int(r['src_locator'].split('!r')[1])
        hdr = heads[(f, sh)]
        vals = cache.get((f, sh, rn), [])
        col = lambda name: (vals[hdr.index(name)] if name in hdr and hdr.index(name) < len(vals) else '')
        note = []
        if file_sha[f] != r['src_sha256']:
            bad_sha += 1; note.append('sha256 файла не совпал')
        if col(code_col) != r['src_value_raw']:
            bad_code += 1; note.append(f"код в файле «{col(code_col)}» ≠ «{r['src_value_raw']}»")
        got = to_mm([col(c) for c in dim_cols], mult, splitter)
        want = tuple(sorted((float(r['Д_мм']), float(r['Ш_мм']), float(r['В_мм'])), reverse=True))
        if got is None or max(abs(a - b) for a, b in zip(got, want)) > 0.5:
            bad_dims += 1
            note.append(f"габариты в файле {got} ≠ в сборке {want}")
        if note:
            diffs.append({'external_code': r['external_code'], 'прайс': r['прайс'],
                          'локатор': r['src_locator'], 'расхождение': '; '.join(note)})
    st['2_перечитка'] = {'проверено выбранных размеров': len(chosen),
                         'файлов перечитано': len(need), 'sha256 файлов совпал': len(file_sha) - bad_sha,
                         'расхождений по коду строки': bad_code,
                         'расхождений по габаритам': bad_dims,
                         'моделей с любым расхождением': len(diffs)}
    with (DATA / 'step7_reread_diffs.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=['external_code', 'прайс', 'локатор', 'расхождение'],
                           delimiter=';')
        w.writeheader()
        w.writerows(diffs)

    # ---- 2б. выброс подозрительных ------------------------------------------
    big = {r['external_code'] for r in
           csv.DictReader((DATA / 'step6_review_big.csv').open(encoding='utf-8'), delimiter=';')}
    same = {r['external_code'] for r in
            csv.DictReader((DATA / 'step6_gap_over50.csv').open(encoding='utf-8'), delimiter=';')
            if r['код_1'] == r['код_2']}
    dropped = big | same | {d['external_code'] for d in diffs}
    kept = [r for r in chosen if r['external_code'] not in dropped]
    kept4 = [r for r in kept if IS4.match(r['external_code'])]
    st['2_чистка'] = {'выброшено: коробки-переростки (step6_review_big)': len(big),
                      'выброшено: один code — две строки одного прайса >50 мм': len(same),
                      'выброшено: не прошли перечитку': len({d['external_code'] for d in diffs}),
                      'выброшено всего (уникальных моделей)': len(dropped & {r['external_code'] for r in chosen}),
                      'ПОДТВЕРЖДЕНО строкой прайса': len(kept),
                      'из них четырёхзначных': len(kept4)}
    with (DATA / 'step7_confirmed.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=chosen[0].keys(), delimiter=';')
        w.writeheader(); w.writerows(kept)
    with (DATA / 'step7_dropped.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['external_code', 'причина'])
        for e in sorted(dropped):
            why = []
            if e in big: why.append('объём >30 л или ось >600 мм')
            if e in same: why.append('один code — две строки одного прайса, >50 мм')
            if e in {d['external_code'] for d in diffs}: why.append('не прошёл перечитку')
            w.writerow([e, '; '.join(why)])

    # ---- 1 + 3. покрытие без own_manual и разбор остатка ---------------------
    load_dotenv('/opt/mp-analytics/.env')
    cn = psycopg2.connect(os.environ['DATABASE_URL']); cur = cn.cursor()
    cur.execute("""select coalesce(external_code,''), coalesce(name,''), coalesce(article,'')
                     from ms_product where coalesce(external_code,'') <> ''""")
    rows = cur.fetchall()
    cur.close(); cn.close()
    names = defaultdict(list)
    arts = defaultdict(set)
    for e, n, a in rows:
        names[e].append(n)
        if a.strip():
            arts[e].add(a.strip())
    all4 = {e for e in names if IS4.match(e)}
    confirmed4 = {r['external_code'] for r in kept4}
    rest = sorted(all4 - confirmed4)
    kind = Counter()
    web = []
    for e in rest:
        nm = ' '.join(names[e])
        if NABOR.search(nm):
            kind['наборы (сумма компонентов)'] += 1
        elif PRINTER.search(nm):
            kind['принтерные/дочерние (наследование от матери)'] += 1
        else:
            kind['пачка для веба'] += 1
            web.append(e)
    st['1_покрытие_без_own_manual'] = {
        'четырёхзначных моделей всего': len(all4),
        'закрыто подтверждённым прайсом': len(confirmed4),
        '% от 4-значных': round(100 * len(confirmed4) / len(all4), 1),
        'без размера': len(rest), 'источник own_manual': 'удалён, в расчёте не участвует'}
    st['3_остаток'] = dict(kind)
    with (DATA / 'step7_web_batch.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['external_code', 'article', 'название'])
        for e in web:
            w.writerow([e, ', '.join(sorted(arts[e])[:4]), names[e][0][:90]])

    (DATA / 'step7_stats.json').write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(st, ensure_ascii=False, indent=1)[:1400])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
