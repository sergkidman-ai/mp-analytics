# поток: gab
"""Шаг 3 — мост «код поставщика (доп. поле МойСклада) → наш товар» + структура нашего артикула.

Только чтение: БД читается, МойСклад по API НЕ дёргается, в маркетплейсы ничего не пишется.
Размеры из доп. полей МС «Длина/Ширина/Высота, см.» НЕ используются (решение Сергея 2026-07-31:
это наши же прошлые заливки в WB, истиной не являются).
"""
from __future__ import annotations

import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

DATA = Path(__file__).resolve().parents[2] / 'docs' / 'rebuild' / 'data'
PRICES = ('cactus', 'izi', 'sakura_laser', 'sakura_inkjet', 'rapid', 'profiline')
# прайсы, дающие размер ИУ в мм (rapid — карантин: единица не доказана; profiline — без размеров)
PRICES_WITH_MM = ('cactus', 'izi', 'sakura_laser', 'sakura_inkjet')
ATTR = 'Код поставщика'
BASE_RE = re.compile(r'^(\d{4})(?!\d)(.*)$')

norm = lambda s: re.sub(r'[^0-9A-ZА-Я]', '', (s or '').upper())


def db_rows():
    load_dotenv('/opt/mp-analytics/.env')
    cn = psycopg2.connect(os.environ['DATABASE_URL'])
    cur = cn.cursor()
    q = lambda s: (cur.execute(s), cur.fetchall())[1]
    total_products = q('select count(*) from ms_product')[0][0]
    total_article = q('select count(*) from ms_product where code is not null')[0][0]
    pairs = q(f"""select r.ms_id, p.code, p.name, trim(a->>'value'), p.article
                  from raw_moysklad_product r
                  join ms_product p on p.ms_id = r.ms_id,
                       jsonb_array_elements(r.payload->'attributes') a
                  where a->>'name' = '{ATTR}'
                    and coalesce(trim(a->>'value'), '') <> ''""")
    arts = q('select ms_id, code, name from ms_product where code is not null')
    sup = q("""select ms_id, supplier from supplier_stock
               where captured_at = (select max(captured_at) from supplier_stock)""")
    cur.close()
    cn.close()
    return total_products, total_article, pairs, arts, dict(sup)


def load_prices():
    """код_поставщика(норм) -> список строк прайса (указатель + размеры ИУ в мм)."""
    idx = defaultdict(list)
    per_price_rows = {}
    for src in PRICES:
        f = DATA / f'{src}.csv'
        n = 0
        with f.open(encoding='utf-8') as fh:
            for row in csv.DictReader(fh, delimiter=';'):
                code = norm(row.get('supplier_code'))
                if not code:
                    continue
                n += 1
                idx[code].append({
                    'прайс': src,
                    'наименование_строки_прайса': row.get('name', ''),
                    'Д_мм': row.get('iu_l_mm', '') if src in PRICES_WITH_MM else '',
                    'Ш_мм': row.get('iu_w_mm', '') if src in PRICES_WITH_MM else '',
                    'В_мм': row.get('iu_h_mm', '') if src in PRICES_WITH_MM else '',
                    'карантин': row.get('quarantine', ''),
                    'src_locator': row.get('src_locator', ''),
                    'src_file': row.get('src_file', ''),
                    'src_field': row.get('code_field', ''),
                    'src_value_raw': row.get('supplier_code', ''),
                    'src_sha256': row.get('src_sha256', ''),
                })
        per_price_rows[src] = n
    return idx, per_price_rows


def split_article(a: str):
    m = BASE_RE.match((a or '').strip())
    return (m.group(1), m.group(2)) if m else ('', '')


def main() -> int:
    total_products, total_article, pairs, arts, sup_of = db_rows()
    price_idx, per_price_rows = load_prices()

    # --- ЧАСТЬ A: мост -------------------------------------------------------
    by_code = defaultdict(list)
    for ms_id, art, name, val, ms_article in pairs:
        by_code[norm(val)].append((ms_id, art or '', name or '', val, ms_article or ''))
    ambiguous = {k: v for k, v in by_code.items() if len({m for m, *_ in v}) > 1}
    clean = {k: v for k, v in by_code.items() if k not in ambiguous}

    with (DATA / 'ambiguous.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['код_поставщика_норм', 'товаров_кандидатов', 'код_сырой',
                    'ms_id', 'наш_артикул', 'наименование_МС', 'поставщик_supplier_stock'])
        for code in sorted(ambiguous):
            rows = ambiguous[code]
            for ms_id, art, name, val, ms_article in rows:
                w.writerow([code, len({m for m, *_ in rows}), val, ms_id, art, name,
                            sup_of.get(ms_id, 'не определён')])

    bridge = []
    for code, rows in clean.items():
        for ms_id, art, name, val, ms_article in rows:
            base, suf = split_article(art)
            hits = price_idx.get(code, [])
            common = dict(наш_артикул=art, article_МС=ms_article, наименование_МС=name, ms_id=ms_id,
                          код_поставщика=val, поставщик_supplier_stock=sup_of.get(ms_id, 'не определён'),
                          основа_4=base, окончание=suf)
            if not hits:
                bridge.append({**common, 'прайс': '', 'наименование_строки_прайса': '',
                               'Д_мм': '', 'Ш_мм': '', 'В_мм': '', 'карантин': '',
                               'src_locator': '', 'src_file': '', 'src_field': '',
                               'src_value_raw': '', 'src_sha256': ''})
            for h in hits:
                bridge.append({**common, **h})

    cols = ['наш_артикул', 'article_МС', 'наименование_МС', 'ms_id', 'код_поставщика', 'поставщик_supplier_stock',
            'основа_4', 'окончание', 'прайс', 'наименование_строки_прайса', 'Д_мм', 'Ш_мм', 'В_мм',
            'src_locator', 'src_file', 'src_field', 'src_value_raw', 'src_sha256', 'карантин']
    bridge.sort(key=lambda r: (r['основа_4'] == '', r['основа_4'], r['окончание'],
                               r['наш_артикул'], r['прайс']))
    with (DATA / 'bridge.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=';', extrasaction='ignore')
        w.writeheader()
        w.writerows(bridge)

    # превью первых 50 строк (узкие колонки — для чтения в терминале)
    with (DATA / 'bridge_preview.txt').open('w', encoding='utf-8') as fh:
        head = f"{'артикул':<12}{'осн':<6}{'оконч':<8}{'код поставщика':<20}{'поставщик':<20}{'прайс':<14}{'Д×Ш×В мм':<18}наименование МС"
        fh.write(head + '\n' + '-' * len(head) + '\n')
        for r in bridge[:50]:
            sup = r['поставщик_supplier_stock'].replace('ООО ', '').replace('"', '')[:18]
            dims = 'x'.join(x for x in (r['Д_мм'], r['Ш_мм'], r['В_мм'])) if r['Д_мм'] else ''
            fh.write(f"{r['наш_артикул'][:11]:<12}{r['основа_4']:<6}{r['окончание'][:7]:<8}"
                     f"{r['код_поставщика'][:19]:<20}{sup:<20}{r['прайс']:<14}{dims:<18}"
                     f"{r['наименование_МС'][:48]}\n")

    # --- ЧАСТЬ B: структура артикула ----------------------------------------
    # какое из трёх полей МС вообще несёт структуру «4 цифры + окончание» — проверено, не принято на веру
    load_dotenv('/opt/mp-analytics/.env')
    _cn = psycopg2.connect(os.environ['DATABASE_URL'])
    _cur = _cn.cursor()
    _cur.execute('select article, code, external_code from ms_product')
    _all = _cur.fetchall()
    _cur.close()
    _cn.close()
    fields_probe = {}
    for fname, i in (('article', 0), ('code', 1), ('external_code', 2)):
        vals = [(r[i] or '').strip() for r in _all if (r[i] or '').strip()]
        m = [BASE_RE.match(v) for v in vals]
        good = [x for x in m if x]
        fields_probe[fname] = {
            'непустых': len(vals), 'уникальных': len(set(vals)),
            'ровно_4_цифры_в_начале': len(good),
            'доля': round(100 * len(good) / len(vals), 1),
            'окончаний_уник': len({x.group(2) for x in good}),
            'основ_уник': len({x.group(1) for x in good}),
        }

    art_rows = [(m, a.strip(), n) for m, a, n in arts if (a or '').strip()]
    ok, bad = [], []
    for m, a, n in art_rows:
        base, suf = split_article(a)
        (ok if base else bad).append((m, a, n, base, suf))
    suffix_freq = Counter(s for *_, s in ok)
    by_base = defaultdict(list)
    for m, a, n, base, suf in ok:
        by_base[base].append(a)
    base_size_dist = Counter(min(len(v), 5) for v in by_base.values())
    top_bases = sorted(by_base.items(), key=lambda kv: -len(kv[1]))[:10]

    cross = defaultdict(Counter)
    for m, a, n, base, suf in ok:
        s = sup_of.get(m)
        if s:
            cross[suf][s] += 1
    with (DATA / 'suffix_supplier.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['окончание', 'контрагент', 'товаров'])
        for suf in sorted(cross, key=lambda s: -sum(cross[s].values())):
            for c, n in cross[suf].most_common():
                w.writerow([suf or '(пусто)', c, n])
    multi_suf = {s: c for s, c in cross.items() if len(c) > 1}

    # --- ЧАСТЬ D: покрытие ---------------------------------------------------
    prod_with_size = defaultdict(set)
    for r in bridge:
        if r['прайс'] in PRICES_WITH_MM and r['Д_мм']:
            prod_with_size[r['прайс']].add(r['ms_id'])
    all_sized = set().union(*prod_with_size.values()) if prod_with_size else set()
    codes_no_price = {k for k in clean if k not in price_idx}

    stats = {
        'товаров_в_ms_product': total_products,
        'товаров_с_непустым_code': total_article,
        'какое_поле_несёт_структуру': fields_probe,
        'пар_с_кодом_поставщика': len(pairs),
        'уникальных_кодов': len(by_code),
        'кодов_неоднозначных_в_ambiguous': len(ambiguous),
        'товаров_в_неоднозначных': sum(len({m for m, *_ in v}) for v in ambiguous.values()),
        'кодов_в_мосте': len(clean),
        'товаров_в_мосте': len({r['ms_id'] for r in bridge}),
        'строк_bridge_csv': len(bridge),
        'поставщик_определён': len({r['ms_id'] for r in bridge if r['поставщик_supplier_stock'] != 'не определён'}),
        'товаров_с_размером_из_прайса': len(all_sized),
        'размер_по_прайсам': {k: len(v) for k, v in sorted(prod_with_size.items(), key=lambda kv: -len(kv[1]))},
        'товаров_моста_без_размера': len({r['ms_id'] for r in bridge}) - len(all_sized),
        'товаров_МС_без_размера_всего': total_products - len(all_sized),
        'кодов_моста_не_найдено_ни_в_одном_прайсе': len(codes_no_price),
        'строк_прайсов_с_кодом': per_price_rows,
        'артикулов_всего': len(art_rows),
        'артикулов_с_основой_4цифры': len(ok),
        'артикулов_без_основы': len(bad),
        'примеры_без_основы': [a for _, a, *_ in bad[:10]],
        'окончаний_различных': len(suffix_freq),
        'только_4_цифры_без_окончания': suffix_freq.get('', 0),
        'окончания_топ40': suffix_freq.most_common(40),
        'основ_различных': len(by_base),
        'распределение_товаров_на_основу': {('5+' if k == 5 else str(k)): v for k, v in sorted(base_size_dist.items())},
        'топ10_основ': [{'основа': b, 'товаров': len(v), 'артикулы': sorted(v)} for b, v in top_bases],
        'окончаний_у_нескольких_контрагентов': len(multi_suf),
        'окончаний_с_известным_контрагентом': len(cross),
        'товаров_с_известным_контрагентом': sum(sum(c.values()) for c in cross.values()),
        'multi_suf_топ20': [{'окончание': s or '(пусто)', 'контрагентов': len(c), 'товаров': sum(c.values()),
                             'разбивка': c.most_common(6)}
                            for s, c in sorted(multi_suf.items(), key=lambda kv: -sum(kv[1].values()))[:20]],
    }
    (DATA / 'step3_stats.json').write_text(json.dumps(stats, ensure_ascii=False, indent=1), encoding='utf-8')
    print('шаг 3 готов:', stats['строк_bridge_csv'], 'строк моста,',
          stats['кодов_неоднозначных_в_ambiguous'], 'неоднозначных кодов')
    return 0


if __name__ == '__main__':
    sys.exit(main())
