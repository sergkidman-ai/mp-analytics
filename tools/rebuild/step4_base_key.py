# поток: gab
"""Шаг 4 — измерить основу как ключ: потенциал переноса и расхождение между «братьями».

ИЗМЕРЕНИЕ, НЕ ПРИМЕНЕНИЕ: ни одной записи в реестр, никакого переноса размеров.
Только чтение БД + уже собранные прайсы. Размеры из доп. полей МС не используются.
"""
from __future__ import annotations

import csv
import json
import random
import re
import sys
from collections import Counter, defaultdict
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_bridge import (DATA, PRICES, PRICES_WITH_MM, BASE_RE, norm,  # noqa: E402
                          db_rows, load_prices, split_article)

BUCKETS = ((0, 2), (2, 5), (5, 10), (10, 30), (30, 100), (100, 10 ** 9))


def bucket(v: float) -> str:
    for lo, hi in BUCKETS:
        if lo <= v < hi:
            return f'{lo}–{hi} мм' if hi < 10 ** 9 else '>100 мм'
    return '>100 мм'


def pct(part: int, whole: int) -> float:
    return round(100 * part / whole, 1) if whole else 0.0


def main() -> int:
    total_products, total_code, pairs, arts, sup_of = db_rows()
    price_idx, per_price_rows = load_prices()

    # --- мост (та же логика, что в шаге 3; основа/окончание — от ms_product.code) ---
    by_code = defaultdict(list)
    for ms_id, code, name, val, ms_article in pairs:
        by_code[norm(val)].append((ms_id, code or '', name or '', val))
    ambiguous = {k: v for k, v in by_code.items() if len({m for m, *_ in v}) > 1}
    clean = {k: v for k, v in by_code.items() if k not in ambiguous}

    prod = {}          # ms_id -> запись моста
    for code, rows in clean.items():
        for ms_id, art, name, val in rows:
            base, suf = split_article(art)
            prod[ms_id] = dict(art=art, name=name, base=base, suf=suf, code=code, raw=val,
                               hits=price_idx.get(code, []))

    # --- A.2: артикулы, не подходящие под правило «4 цифры + окончание» ---
    bad = [(m, (c or '').strip(), n) for m, c, n in arts
           if (c or '').strip() and not BASE_RE.match((c or '').strip())]
    bad_kind = Counter()
    for _, c, _ in bad:
        if re.match(r'^\d{4}\d', c):
            bad_kind['4+ цифр подряд в начале (5-я цифра)'] += 1
        elif re.match(r'^\d{1,3}(?!\d)', c):
            bad_kind['менее 4 цифр в начале'] += 1
        elif re.match(r'^[A-Za-z]', c):
            bad_kind['начинается с латиницы'] += 1
        elif re.match(r'^[А-Яа-я]', c):
            bad_kind['начинается с кириллицы'] += 1
        else:
            bad_kind['прочее'] += 1
    bad_examples = [{'code': c, 'наименование_МС': n[:70]} for _, c, n in bad[:20]]

    # --- A.3: сведение общих чисел ---
    import os

    import psycopg2
    from dotenv import load_dotenv
    load_dotenv('/opt/mp-analytics/.env')
    _cn = psycopg2.connect(os.environ['DATABASE_URL'])
    _cur = _cn.cursor()
    _cur.execute("""select count(*) filter (where coalesce(trim(article), '') <> ''),
                           count(*) filter (where coalesce(trim(external_code), '') <> ''),
                           count(*) filter (where archived),
                           count(*) filter (where coalesce(trim(code), '') = ''
                                              and coalesce(trim(article), '') <> '')
                    from ms_product""")
    n_article, n_extcode, n_archived, n_code_empty_art_ok = _cur.fetchone()
    _cur.close()
    _cn.close()

    # --- размеры товара из прайсов ---------------------------------------------
    # (ms_id, прайс) -> набор различных отсортированных троек мм + указатель
    sizes = defaultdict(dict)
    multi_size_in_price = 0
    for ms_id, p in prod.items():
        for h in p['hits']:
            if h['прайс'] not in PRICES_WITH_MM:
                continue
            try:
                d = tuple(sorted((float(h['Д_мм']), float(h['Ш_мм']), float(h['В_мм'])), reverse=True))
            except (TypeError, ValueError):
                continue
            if not all(d):
                continue
            key = (ms_id, h['прайс'])
            if d not in sizes[key]:
                if sizes[key]:
                    multi_size_in_price += 1
                sizes[key][d] = h
    sized_products = {ms for ms, _ in sizes}

    # --- C.1/C.2: потенциал основы ---------------------------------------------
    base_members = defaultdict(set)      # основа -> ms_id (все товары с непустым code)
    for m, c, n in arts:
        b, _ = split_article((c or '').strip())
        if b:
            base_members[b].add(m)
    bases_with_size = {b for b, ms in base_members.items() if ms & sized_products}
    potential = Counter()
    potential_total = 0
    for b in bases_with_size:
        members = base_members[b]
        k = len(members)
        key = '2' if k == 2 else '3' if k == 3 else '4' if k == 4 else '5+' if k >= 5 else '1'
        n_wo = len(members - sized_products)
        potential[key] += n_wo
        potential_total += n_wo

    # --- C.3/C.4/C.5: расхождение между братьями --------------------------------
    recs = defaultdict(list)             # основа -> [(ms_id, прайс, dims, hit)]
    for (ms_id, price), dd in sizes.items():
        b = prod[ms_id]['base']
        if not b:
            continue
        for d, h in dd.items():
            recs[b].append((ms_id, price, d, h))
    cmp_pairs = []
    for b, lst in recs.items():
        for a, c in combinations(lst, 2):
            if a[0] == c[0] or a[1] == c[1]:
                continue                # тот же товар либо тот же прайс — не сравниваем
            diff = max(abs(x - y) for x, y in zip(a[2], c[2]))
            cmp_pairs.append((b, a, c, diff))
    bases_cmp = len({p[0] for p in cmp_pairs})
    diffs = sorted(p[3] for p in cmp_pairs)
    dist = Counter(bucket(d) for d in diffs)
    med = diffs[len(diffs) // 2] if diffs else 0
    p90 = diffs[int(len(diffs) * 0.9)] if diffs else 0

    def pair_row(b, a, c, diff):
        return {
            'основа': b, 'макс_расхождение_мм': round(diff, 1),
            'артикул_1': prod[a[0]]['art'], 'окончание_1': prod[a[0]]['suf'], 'прайс_1': a[1],
            'код_1': prod[a[0]]['raw'], 'размер_1_мм': '×'.join(f'{x:g}' for x in a[2]),
            'src_locator_1': a[3]['src_locator'], 'src_file_1': a[3]['src_file'],
            'src_field_1': a[3]['src_field'], 'src_value_raw_1': a[3]['src_value_raw'],
            'src_sha256_1': a[3]['src_sha256'], 'наименование_прайса_1': a[3]['наименование_строки_прайса'],
            'артикул_2': prod[c[0]]['art'], 'окончание_2': prod[c[0]]['suf'], 'прайс_2': c[1],
            'код_2': prod[c[0]]['raw'], 'размер_2_мм': '×'.join(f'{x:g}' for x in c[2]),
            'src_locator_2': c[3]['src_locator'], 'src_file_2': c[3]['src_file'],
            'src_field_2': c[3]['src_field'], 'src_value_raw_2': c[3]['src_value_raw'],
            'src_sha256_2': c[3]['src_sha256'], 'наименование_прайса_2': c[3]['наименование_строки_прайса'],
        }

    worst = [pair_row(*p) for p in sorted(cmp_pairs, key=lambda p: -p[3])[:30]]
    tight = [p for p in cmp_pairs if p[3] < 2]
    rnd = random.Random(4)
    tight30 = [pair_row(*p) for p in (rnd.sample(tight, 30) if len(tight) > 30 else tight)]
    for fname, rows in (('step4_pairs_worst30.csv', worst), ('step4_pairs_tight30.csv', tight30)):
        if rows:
            with (DATA / fname).open('w', encoding='utf-8', newline='') as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=';')
                w.writeheader()
                w.writerows(rows)
    def preview(rows, path):
        head = (f"{'осн':<6}{'Δмм':<7}{'артикул_1':<11}{'прайс_1':<14}{'размер_1 мм':<16}{'локатор_1':<22}"
                f"{'артикул_2':<11}{'прайс_2':<14}{'размер_2 мм':<16}локатор_2")
        with (DATA / path).open('w', encoding='utf-8') as fh:
            fh.write(head + '\n')
            for r in rows:
                fh.write(f"{r['основа']:<6}{r['макс_расхождение_мм']:<7g}{r['артикул_1'][:10]:<11}"
                         f"{r['прайс_1']:<14}{r['размер_1_мм']:<16}{r['src_locator_1'][:21]:<22}"
                         f"{r['артикул_2'][:10]:<11}{r['прайс_2']:<14}{r['размер_2_мм']:<16}"
                         f"{r['src_locator_2'][:21]}\n")

    preview(worst, 'step4_worst30_preview.txt')
    preview(tight30, 'step4_tight30_preview.txt')

    # A.2: сколько из «не подходящих» вообще картриджи (по наименованию МС) — факт, без домыслов
    CART = re.compile(r'картридж|тонер|фотобарабан|чернил|драм|drum', re.I)
    bad_cart = sum(1 for _, _, n in bad if CART.search(n or ''))
    bad_latin_cart = sum(1 for _, c, n in bad if re.match(r'^[A-Za-z]', c) and CART.search(n or ''))

    with (DATA / 'step4_pairs_all.csv').open('w', encoding='utf-8', newline='') as fh:
        rows = [pair_row(*p) for p in sorted(cmp_pairs, key=lambda p: (-p[3], p[0]))]
        if rows:
            w = csv.DictWriter(fh, fieldnames=list(rows[0]), delimiter=';')
            w.writeheader()
            w.writerows(rows)

    # --- B: окончание × прайс vs окончание × контрагент --------------------------
    suf_price = defaultdict(Counter)     # окончание -> прайс -> товаров
    for ms_id, p in prod.items():
        if not p['base']:
            continue
        for pr in {h['прайс'] for h in p['hits']}:
            suf_price[p['suf']][pr] += 1
    suf_sup = defaultdict(Counter)
    for m, c, n in arts:
        b, s = split_article((c or '').strip())
        if b and sup_of.get(m):
            suf_sup[s][sup_of[m]] += 1

    def purity(d, thr):
        one, dom = 0, 0
        for s, cnt in d.items():
            tot = sum(cnt.values())
            if len(cnt) == 1:
                one += 1
            if tot and max(cnt.values()) / tot >= thr:
                dom += 1
        return one, dom

    price_one, price_dom = purity(suf_price, 0.9)
    sup_one, sup_dom = purity(suf_sup, 0.9)
    # честное сравнение — на одном и том же наборе окончаний (есть и прайс, и контрагент)
    both = set(suf_price) & set(suf_sup)
    b_price_one, b_price_dom = purity({s: suf_price[s] for s in both}, 0.9)
    b_sup_one, b_sup_dom = purity({s: suf_sup[s] for s in both}, 0.9)
    with (DATA / 'step4_suffix_price.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['окончание', 'товаров_с_попаданием_в_прайсы'] + list(PRICES) + ['доля_главного_прайса_%'])
        for s in sorted(suf_price, key=lambda s: -sum(suf_price[s].values())):
            cnt = suf_price[s]
            tot = sum(cnt.values())
            w.writerow([s or '(пусто)', tot] + [cnt.get(p, 0) for p in PRICES] +
                       [pct(max(cnt.values()), tot)])
    top_suf_price = []
    for s in sorted(suf_price, key=lambda s: -sum(suf_price[s].values()))[:12]:
        cnt = suf_price[s]
        tot = sum(cnt.values())
        top_suf_price.append({'окончание': s or '(пусто)', 'товаров': tot,
                              'прайсы': cnt.most_common(3),
                              'доля_главного': pct(max(cnt.values()), tot),
                              'контрагентов': len(suf_sup.get(s, {})),
                              'доля_главного_контрагента': pct(max(suf_sup[s].values()),
                                                              sum(suf_sup[s].values())) if suf_sup.get(s) else None})

    # --- D: коды моста, не найденные ни в одном прайсе ---------------------------
    no_price = [(ms_id, p) for ms_id, p in prod.items() if not p['hits']]
    d_suf = Counter(p['suf'] if p['base'] else '(нет основы)' for _, p in no_price)
    d_suf_sup = {}
    for s, _ in d_suf.most_common(20):
        c = Counter(sup_of[m] for m, p in no_price
                    if (p['suf'] if p['base'] else '(нет основы)') == s and sup_of.get(m))
        d_suf_sup[s or '(пусто)'] = c.most_common(2)

    stats = {
        'A_универсум': {
            'ms_product всего': total_products,
            'с непустым code': total_code,
            'с непустым article': n_article,
            'с непустым external_code': n_extcode,
            'archived=true': n_archived,
            'code пуст, но article есть': n_code_empty_art_ok,
            'товаров с размером из прайса': None,   # заполняется ниже
        },
        'A2_не_подходят_под_правило': len(bad),
        'A2_виды': bad_kind.most_common(),
        'A2_из_них_картриджи_по_наименованию': bad_cart,
        'A2_латиница_из_них_картриджи': bad_latin_cart,
        'A2_примеры20': bad_examples,
        'B_окончаний_с_прайсом': len(suf_price),
        'B_окончаний_один_прайс': price_one, 'B_окончаний_прайс_≥90%': price_dom,
        'B_окончаний_с_контрагентом': len(suf_sup),
        'B_окончаний_один_контрагент': sup_one, 'B_окончаний_контрагент_≥90%': sup_dom,
        'B_общих_окончаний': len(both),
        'B_на_общих_один_прайс': b_price_one, 'B_на_общих_прайс_≥90%': b_price_dom,
        'B_на_общих_один_контрагент': b_sup_one, 'B_на_общих_контрагент_≥90%': b_sup_dom,
        'B_топ12': top_suf_price,
        'C1_основ_всего': len(base_members),
        'C1_основ_с_размером': len(bases_with_size),
        'C2_товаров_без_размера_в_таких_основах': potential_total,
        'C2_разбивка_по_размеру_основы': dict(sorted(potential.items())),
        'C3_основ_со_сравнением': bases_cmp,
        'C3_пар': len(cmp_pairs),
        'C3_распределение': [(f'{lo}–{hi} мм' if hi < 10 ** 9 else '>100 мм',
                              dist.get(f'{lo}–{hi} мм' if hi < 10 ** 9 else '>100 мм', 0))
                             for lo, hi in BUCKETS],
        'C3_медиана_мм': med, 'C3_p90_мм': p90,
        'C3_товаров_с_размером': len(sized_products),
        'C3_разные_размеры_внутри_одного_прайса': multi_size_in_price,
        'C5_пар_в_группе_0_2мм': len(tight),
        'D_кодов_без_прайса': len(no_price),
        'D_топ20_окончаний': [{'окончание': s or '(пусто)', 'товаров': n,
                               'контрагенты': d_suf_sup.get(s or '(пусто)', [])}
                              for s, n in d_suf.most_common(20)],
    }
    stats['A_универсум']['товаров с размером из прайса'] = len(sized_products)
    (DATA / 'step4_stats.json').write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding='utf-8')
    print('шаг 4:', len(cmp_pairs), 'пар сравнения в', bases_cmp, 'основах; медиана',
          med, 'мм, p90', p90, 'мм; потенциал', potential_total, 'товаров')
    return 0


if __name__ == '__main__':
    sys.exit(main())
