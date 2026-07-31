# поток: gab
"""Шаг 5 — правильные ключи (article / «Код поставщика» / штрихкод) и главное число:
сколько МОДЕЛЕЙ (уникальных ms_product.external_code) закрывают прайсы.

Только чтение: БД и собранные прайсы. Сети нет, в МС/WB/Ozon/Яндекс ничего не пишется.
Размеры из доп. полей МС не используются. Перенос размеров между товарами НЕ делается —
считаются только числа. Дочерние карточки принтеров и наборы в шаг не входят (не выделяются).
"""
from __future__ import annotations

import csv
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_bridge import DATA, PRICES, PRICES_WITH_MM, ATTR, norm, load_prices

BUCKETS = ((0, 2), (2, 5), (5, 10), (10, 30), (30, 100), (100, 10 ** 9))
DIGITS = re.compile(r'\D')


def norm_bc(s: str) -> str:
    """штрихкод: только цифры, ведущие нули срезаются (UPC-12 ↔ EAN-13)."""
    return DIGITS.sub('', s or '').lstrip('0')


def db():
    load_dotenv('/opt/mp-analytics/.env')
    cn = psycopg2.connect(os.environ['DATABASE_URL'])
    cur = cn.cursor()
    q = lambda s: (cur.execute(s), cur.fetchall())[1]
    prods = q("""select ms_id, coalesce(code,''), coalesce(external_code,''),
                        coalesce(article,''), coalesce(name,'') from ms_product""")
    attrs = q(f"""select r.ms_id, trim(a->>'value')
                  from raw_moysklad_product r,
                       jsonb_array_elements(r.payload->'attributes') a
                  where a->>'name' = '{ATTR}'
                    and coalesce(trim(a->>'value'), '') <> ''""")
    bcs = q('select ms_id, barcode from ms_barcode')
    cur.close(); cn.close()
    return prods, attrs, bcs


def dims_of(h):
    try:
        d = (float(h['Д_мм']), float(h['Ш_мм']), float(h['В_мм']))
    except (TypeError, ValueError):
        return None
    return tuple(sorted(d, reverse=True)) if all(x > 0 for x in d) else None


def main() -> int:
    prods, attrs, bcs = db()
    price_idx, per_price_rows = load_prices()
    # индекс прайсов по штрихкоду
    bcp = defaultdict(list)
    for src in PRICES:
        with (DATA / f'{src}.csv').open(encoding='utf-8') as fh:
            for row in csv.DictReader(fh, delimiter=';'):
                b = norm_bc(row.get('barcode_raw'))
                if b:
                    bcp[b].append({
                        'прайс': src,
                        'Д_мм': row.get('iu_l_mm', '') if src in PRICES_WITH_MM else '',
                        'Ш_мм': row.get('iu_w_mm', '') if src in PRICES_WITH_MM else '',
                        'В_мм': row.get('iu_h_mm', '') if src in PRICES_WITH_MM else '',
                        'src_locator': row.get('src_locator', ''),
                        'src_file': row.get('src_file', ''),
                        'src_field': row.get('barcode_field', ''),
                        'src_value_raw': row.get('barcode_raw', ''),
                        'src_sha256': row.get('src_sha256', ''),
                    })

    st = {}
    # ---- ЧАСТЬ A -----------------------------------------------------------
    ext_of, code_of, art_of, name_of = {}, {}, {}, {}
    a_ok = a_bad = 0
    bad_ex = []
    for ms_id, code, ext, art, name in prods:
        ext_of[ms_id], code_of[ms_id], art_of[ms_id], name_of[ms_id] = ext, code, art, name
        if not code:
            continue
        if ext and code.startswith(ext):
            a_ok += 1
        else:
            a_bad += 1
            if len(bad_ex) < 20:
                bad_ex.append({'ms_id': ms_id, 'code': code, 'external_code': ext,
                               'article': art, 'name': name[:70]})
    ext4 = sum(1 for _, _, e, _, _ in prods if re.fullmatch(r'\d{4}', e))
    not4 = [(e, n[:60]) for _, _, e, _, n in prods if not re.fullmatch(r'\d{4}', e)]
    st['A'] = {
        'товаров всего': len(prods),
        'с непустым code': sum(1 for _, c, *_ in prods if c),
        'code = external_code + окончание': a_ok,
        'code не начинается с external_code': a_bad,
        'примеры20': bad_ex,
        'external_code ровно 4 цифры': ext4,
        'external_code не 4 цифры': len(not4),
        'исключения примеры': [x[0] for x in not4[:20]],
        'уникальных external_code': len({e for _, _, e, _, _ in prods if e}),
        'article непустой': sum(1 for _, _, _, a, _ in prods if a.strip()),
    }
    st['A']['длины external_code'] = dict(Counter(len(e) for _, _, e, _, _ in prods).most_common())

    # ---- ЧАСТЬ B: сопоставление по трём ключам ------------------------------
    attr_of = defaultdict(list)
    for ms_id, v in attrs:
        attr_of[ms_id].append(v)
    bc_of = defaultdict(list)
    for ms_id, b in bcs:
        bc_of[ms_id].append(b)

    norm_examples = []
    links = []            # (ms_id, ключ, прайс, строка прайса) — приоритет k1 > k2 > k3
    per_price = {s: {'k1': 0, 'k2': 0, 'k3': 0, 'товаров': set(),
                     'т_k1': set(), 'т_k2': set(), 'т_k3': set()} for s in PRICES}
    key_products = {'k1': set(), 'k2': set(), 'k3': set()}
    for ms_id, code, ext, art, name in prods:
        seen_rows = set()
        for key, raws in (('k1', [art] if art.strip() else []),
                          ('k2', attr_of.get(ms_id, [])),
                          ('k3', bc_of.get(ms_id, []))):
            seen_key = set()
            for raw in raws:
                n = norm_bc(raw) if key == 'k3' else norm(raw)
                if not n:
                    continue
                if len(norm_examples) < 10 and key != 'k3' and raw != n:
                    norm_examples.append({'ключ': key, 'было': raw, 'стало': n})
                for h in (bcp.get(n, []) if key == 'k3' else price_idx.get(n, [])):
                    ident = (h['прайс'], h['src_locator'], h['src_sha256'])
                    # независимый счёт по ключу (для сравнения ключей между собой)
                    if ident not in seen_key:
                        seen_key.add(ident)
                        per_price[h['прайс']][key] += 1
                        per_price[h['прайс']][f'т_{key}'].add(ms_id)
                        key_products[key].add(ms_id)
                    # связь по приоритету (для размеров): строка берётся один раз
                    if ident in seen_rows:
                        continue
                    seen_rows.add(ident)
                    links.append((ms_id, key, h['прайс'], h))
                    per_price[h['прайс']]['товаров'].add(ms_id)

    st['B'] = {
        'нормализация': 'верхний регистр; выбрасываются все символы кроме 0-9 A-Z А-Я '
                        '(пробелы, дефисы, точки, слэши, скобки); ведущие нули НЕ срезаются; '
                        'штрихкод — только цифры, ведущие нули срезаются',
        'примеры нормализации': norm_examples,
        'товаров по ключу article': len(key_products['k1']),
        'товаров по ключу Код поставщика': len(key_products['k2']),
        'товаров по ключу штрихкод': len(key_products['k3']),
        'товаров всего сопоставлено': len(set().union(*key_products.values())),
        'только article (нет k2/k3)': len(key_products['k1'] - key_products['k2'] - key_products['k3']),
        'только Код поставщика': len(key_products['k2'] - key_products['k1'] - key_products['k3']),
        'только штрихкод': len(key_products['k3'] - key_products['k1'] - key_products['k2']),
        'по прайсам': {s: {'строк по article': v['k1'], 'строк по Код поставщика': v['k2'],
                           'строк по штрихкоду': v['k3'], 'наших товаров': len(v['товаров']),
                           'товаров k1': len(v['т_k1']), 'товаров k2 (=шаг 3)': len(v['т_k2'])}
                       for s, v in per_price.items()},
        'строк в прайсах всего': per_price_rows,
    }

    # ---- ЧАСТЬ C: модель = external_code -----------------------------------
    models = {e for _, _, e, _, _ in prods if e}
    model_dims = defaultdict(lambda: defaultdict(set))   # ext -> прайс -> {(d,w,h)}
    model_ptr = {}                                       # (ext, прайс, dims) -> указатель
    model_any_link = defaultdict(set)                    # ext -> прайсы (любая связь)
    for ms_id, key, src, h in links:
        e = ext_of.get(ms_id) or ''
        if not e:
            continue
        model_any_link[e].add(src)
        d = dims_of(h) if src in PRICES_WITH_MM else None
        if d and str(h.get('карантин', '')).strip().lower() not in ('1', 'true', 'да'):
            model_dims[e][src].add(d)
            model_ptr.setdefault((e, src, d), (ms_id, key, h))

    covered = set(model_dims)
    only = {s: 0 for s in PRICES_WITH_MM}
    by_price = {s: 0 for s in PRICES_WITH_MM}
    inter = Counter()
    for e, per in model_dims.items():
        ss = sorted(per)
        for s in ss:
            by_price[s] += 1
        if len(ss) == 1:
            only[ss[0]] += 1
        for a, b in combinations(ss, 2):
            inter[(a, b)] += 1

    diffs, worst = [], []
    for e, per in model_dims.items():
        flat = [(s, d) for s, ds in per.items() for d in ds]
        best = 0.0
        best_pair = None
        for (s1, d1), (s2, d2) in combinations(flat, 2):
            if s1 == s2:
                continue
            mx = max(abs(x - y) for x, y in zip(d1, d2))
            if best_pair is None or mx > best:
                best, best_pair = mx, ((s1, d1), (s2, d2))
        if best_pair is not None:
            diffs.append(best)
            worst.append((best, e, best_pair))
    buckets = Counter()
    for v in diffs:
        for lo, hi in BUCKETS:
            if lo <= v < hi or (hi == BUCKETS[-1][1] and v >= lo):
                buckets[f'{lo}-{hi if hi < 10 ** 9 else "∞"}'] += 1
                break
    worst.sort(key=lambda x: -x[0])

    m4 = {e for e in models if re.fullmatch(r'\d{4}', e)}
    st['C'] = {
        'уникальных моделей': len(models),
        'из них внешний код 4 цифры': len(m4),
        'из них служебные 22 символа': len({e for e in models if len(e) == 22}),
        'моделей с размером из прайса': len(covered),
        'моделей без размера': len(models) - len(covered),
        '4-значных моделей с размером': len(covered & m4),
        '4-значных моделей без размера': len(m4 - covered),
        'моделей со связью в прайсе (любой, включая rapid/profiline)': len(model_any_link),
        'вклад прайса (моделей)': by_price,
        'закрывает только этот прайс': only,
        'пересечения пар': {f'{a}∩{b}': n for (a, b), n in sorted(inter.items(), key=lambda x: -x[1])},
        'моделей с размерами из разных прайсов': len(diffs),
        'распределение расхождения': dict(buckets),
        'медиана мм': round(statistics.median(diffs), 1) if diffs else None,
        'p90 мм': round(sorted(diffs)[int(len(diffs) * 0.9)], 1) if diffs else None,
    }

    with (DATA / 'step5_model_conflicts.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['модель', 'Δмм', 'прайс_1', 'Д1', 'Ш1', 'В1', 'ключ_1', 'наш_код_1', 'локатор_1',
                    'файл_1', 'поле_1', 'сырое_1', 'sha256_1',
                    'прайс_2', 'Д2', 'Ш2', 'В2', 'ключ_2', 'наш_код_2', 'локатор_2', 'файл_2',
                    'поле_2', 'сырое_2', 'sha256_2'])
        for v, e, ((s1, d1), (s2, d2)) in worst:
            r = []
            for s, d in ((s1, d1), (s2, d2)):
                ms_id, key, h = model_ptr[(e, s, d)]
                r += [s, int(d[0]), int(d[1]), int(d[2]), key, code_of.get(ms_id, ''),
                      h['src_locator'], h['src_file'], h['src_field'], h['src_value_raw'],
                      h['src_sha256']]
            w.writerow([e, round(v, 1)] + r)

    lines = ['модель Δмм  прайс_1        размер_1        локатор_1             '
             'прайс_2        размер_2        локатор_2']
    for v, e, ((s1, d1), (s2, d2)) in worst[:20]:
        f = lambda d: '×'.join(str(int(x)) for x in d)
        lines.append(f'{e:<6}{int(v):<5}{s1:<15}{f(d1):<16}{model_ptr[(e,s1,d1)][2]["src_locator"][:21]:<22}'
                     f'{s2:<15}{f(d2):<16}{model_ptr[(e,s2,d2)][2]["src_locator"][:21]}')
    (DATA / 'step5_worst20_preview.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    # ---- ЧАСТЬ D -----------------------------------------------------------
    by_model = defaultdict(list)
    for ms_id, code, ext, art, name in prods:
        if ext:
            by_model[ext].append((code, art, name))
    suf_cnt = Counter()
    with (DATA / 'models_no_dims.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['внешний_код', 'наши_артикулы', 'окончания', 'наименования'])
        for e in sorted(models - covered):
            rows = by_model[e]
            sufs = []
            for code, art, name in rows:
                s = code[len(e):] if code.startswith(e) else ''
                if s:
                    sufs.append(s)
                    suf_cnt[s] += 1
            w.writerow([e, ' | '.join(c for c, _, _ in rows if c),
                        ' | '.join(sorted(set(sufs))),
                        ' | '.join(sorted({n for _, _, n in rows if n})[:3])])
    st['D'] = {
        'моделей без размера': len(models - covered),
        'топ20 окончаний': dict(suf_cnt.most_common(20)),
        'файл': 'docs/rebuild/data/models_no_dims.csv',
    }

    (DATA / 'step5_stats.json').write_text(
        json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
    print('модели:', st['C']['уникальных моделей'], '| с размером:', st['C']['моделей с размером из прайса'],
          '| конфликтных:', st['C']['моделей с размерами из разных прайсов'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
