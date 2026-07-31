# поток: gab
"""Шаг 6 — максимальное покрытие моделей (external_code) реальными размерами.

Правило выбора: у модели берём коробку с НАИБОЛЬШИМ объёмом целиком (три габарита
из одной строки одного файла). Ничего не считаем, не усредняем, не смешиваем оси.
Приоритет источников: прайс > наш ручной размер (own_manual) > НЕ НАЙДЕНО.
rapid остаётся в карантине — считается отдельно, в покрытие не входит.
"""
import csv
import json
import os
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_bridge import DATA, PRICES, PRICES_WITH_MM, ATTR, norm, load_prices   # noqa: E402
from step5_model_key import norm_bc, dims_of                                     # noqa: E402

csv.field_size_limit(10 ** 7)
P4 = re.compile(r'^(\d{4})(?!\d)')
IS4 = re.compile(r'^\d{4}$')
PROFILINE = Path('/opt/mp-analytics/incoming/gab/Профилайн.xls')
DIMPAT = re.compile(r'\d+\s*[xх*×]\s*\d+\s*[xх*×]\s*\d+')


def vol_l(d):
    return d[0] * d[1] * d[2] / 1e6


def gap(d1, d2):
    """максимальное расхождение по одной оси, мм (габариты уже отсортированы)."""
    return max(abs(a - b) for a, b in zip(d1, d2))


def basket(mm):
    return 'до 20 мм' if mm <= 20 else ('20-50 мм' if mm <= 50 else 'больше 50 мм')


def mm3(d):
    return '×'.join(str(round(x)) for x in d)


def db():
    load_dotenv('/opt/mp-analytics/.env')
    cn = psycopg2.connect(os.environ['DATABASE_URL'])
    cur = cn.cursor()
    q = lambda s: (cur.execute(s), cur.fetchall())[1]
    d = {
        'prods': q("""select ms_id, coalesce(code,''), coalesce(external_code,''),
                             coalesce(article,''), coalesce(name,'') from ms_product"""),
        'attrs': q(f"""select r.ms_id, trim(a->>'value') from raw_moysklad_product r,
                            jsonb_array_elements(r.payload->'attributes') a
                       where a->>'name' = '{ATTR}' and coalesce(trim(a->>'value'),'') <> ''"""),
        # наш ручной размер в МойСкладе: атрибуты «Длина, см.» / «Ширина, см.» / «Высота, см.»
        'ms_dims': q("""select r.ms_id, a->>'name', a->>'value', r.loaded_at::date
                          from raw_moysklad_product r, jsonb_array_elements(r.payload->'attributes') a
                         where a->>'name' in ('Длина, см.','Ширина, см.','Высота, см.')"""),
        'brand': q("""select r.ms_id, trim(a->>'value') from raw_moysklad_product r,
                           jsonb_array_elements(r.payload->'attributes') a
                      where a->>'name' = 'Бренд' and coalesce(trim(a->>'value'),'') <> ''"""),
        'bcs': q('select ms_id, barcode from ms_barcode'),
        'wb': q("""select account, nm_id, coalesce(vendor_code,''), length_cm, width_cm, height_cm,
                          updated_at::date from wb_cards
                    where length_cm > 0 and width_cm > 0 and height_cm > 0"""),
        'sup': q("""select s.ms_id, s.supplier from supplier_stock s
                     join (select max(captured_at) m from supplier_stock) t on s.captured_at = t.m
                    where coalesce(s.supplier,'') <> ''"""),
    }
    cur.close(); cn.close()
    return d


def main() -> int:
    d = db()
    prods = d['prods']
    price_idx, _ = load_prices()
    ext_of = {m: e for m, c, e, a, n in prods}
    st = {}

    # ---------- связи товар → строка прайса (article > Код поставщика > ШК) ----
    attr_of = defaultdict(list)
    for m, v in d['attrs']:
        attr_of[m].append(v)
    bc_of = defaultdict(list)
    for m, b in d['bcs']:
        bc_of[m].append(b)
    bcp = defaultdict(list)
    rapid_dims = {}
    for src in PRICES:
        with (DATA / f'{src}.csv').open(encoding='utf-8') as fh:
            for row in csv.DictReader(fh, delimiter=';'):
                b = norm_bc(row.get('barcode_raw'))
                if b:
                    bcp[b].append({
                        'прайс': src, 'карантин': row.get('quarantine', ''),
                        'Д_мм': row.get('iu_l_mm', '') if src in PRICES_WITH_MM else '',
                        'Ш_мм': row.get('iu_w_mm', '') if src in PRICES_WITH_MM else '',
                        'В_мм': row.get('iu_h_mm', '') if src in PRICES_WITH_MM else '',
                        'src_locator': row.get('src_locator', ''), 'src_file': row.get('src_file', ''),
                        'src_field': row.get('code_field', ''), 'src_value_raw': row.get('barcode_raw', ''),
                        'src_sha256': row.get('src_sha256', '')})
                if src == 'rapid':
                    try:
                        v = [float(x) for x in (row.get('iu_raw') or '').split('|')]
                    except ValueError:
                        continue
                    if len(v) == 3 and all(x > 0 for x in v):
                        rapid_dims[norm(row.get('supplier_code'))] = (
                            tuple(sorted((x * 1000 for x in v), reverse=True)),
                            {'src_locator': row.get('src_locator', ''), 'iu_raw': row.get('iu_raw', ''),
                             'volume_raw': row.get('volume_raw', ''), 'qty': row.get('qty_in_pack', ''),
                             'name': (row.get('name') or '')[:60]})

    boxes = defaultdict(list)          # ext -> [(code, суффикс, прайс, dims, ptr)]
    hit_any = defaultdict(set)         # ext -> прайсы, где товар вообще нашёлся
    rapid_by_model = defaultdict(list)
    for ms_id, code, ext, art, name in prods:
        if not ext:
            continue
        seen = set()
        for key, raws in (('k1', [art] if art.strip() else []), ('k2', attr_of.get(ms_id, [])),
                          ('k3', bc_of.get(ms_id, []))):
            for raw in raws:
                n = norm_bc(raw) if key == 'k3' else norm(raw)
                if not n:
                    continue
                if key != 'k3' and n in rapid_dims:
                    rapid_by_model[ext].append((code, rapid_dims[n]))
                for h in (bcp.get(n, []) if key == 'k3' else price_idx.get(n, [])):
                    ident = (h['прайс'], h['src_locator'])
                    if ident in seen:
                        continue
                    seen.add(ident)
                    hit_any[ext].add(h['прайс'])
                    if h['прайс'] not in PRICES_WITH_MM:
                        continue
                    dd = dims_of(h)
                    if dd and str(h.get('карантин', '')).strip().lower() not in ('1', 'true', 'да'):
                        boxes[ext].append((code, code[len(ext):] if code.startswith(ext) else '',
                                           h['прайс'], dd, h))

    # ---------- 1. выбор коробки: максимальная по объёму ----------------------
    chosen = {}
    n_one = n_multi = 0
    for e, v in boxes.items():
        best = max(v, key=lambda b: vol_l(b[3]))
        chosen[e] = best
        if len({b[3] for b in v}) > 1:
            n_multi += 1
        else:
            n_one += 1
    st['1'] = {'моделей закрыто прайсами': len(chosen),
               'из них коробка была одна': n_one, 'выбирали из нескольких': n_multi,
               'из них четырёхзначных': sum(1 for e in chosen if IS4.match(e))}

    with (DATA / 'step6_chosen_box.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['external_code', 'наш_код', 'прайс', 'Д_мм', 'Ш_мм', 'В_мм', 'объём_л',
                    'коробок_у_модели', 'src_file', 'src_locator', 'src_field',
                    'src_value_raw', 'src_sha256'])
        for e in sorted(chosen):
            code, suf, src, dd, h = chosen[e]
            w.writerow([e, code, src, *[round(x) for x in dd], round(vol_l(dd), 3),
                        len({b[3] for b in boxes[e]}), h.get('src_file', ''), h.get('src_locator', ''),
                        h.get('src_field', ''), h.get('src_value_raw', ''), h.get('src_sha256', '')])

    # ---------- 2. грубая проверка расхождений --------------------------------
    bask = Counter()
    big_gap, same_code = [], []
    for e, v in boxes.items():
        ds = {b[3] for b in v}
        if len(ds) < 2:
            continue
        pairs = [(b1, b2) for i, b1 in enumerate(v) for b2 in v[i + 1:] if b1[3] != b2[3]]
        worst = max(pairs, key=lambda p: gap(p[0][3], p[1][3]))
        g = gap(worst[0][3], worst[1][3])
        bask[basket(g)] += 1
        if g > 50:
            big_gap.append((g, e, worst))
        for b1, b2 in pairs:
            if b1[0] == b2[0] and b1[2] != b2[2] and gap(b1[3], b2[3]) > 50:
                same_code.append((gap(b1[3], b2[3]), e, b1, b2))
    st['2'] = {'моделей с несколькими коробками': sum(bask.values()),
               'корзины': dict(bask), 'расхождений >50 мм': len(big_gap),
               'один и тот же code, два прайса, >50 мм': len(same_code)}

    def dump(rows, path, head):
        with (DATA / path).open('w', encoding='utf-8', newline='') as fh:
            w = csv.writer(fh, delimiter=';')
            w.writerow(head)
            for g, e, b1, b2 in rows:
                w.writerow([e, round(g), b1[0], b1[2], mm3(b1[3]), Path(b1[4]['src_file']).name,
                            b1[4]['src_locator'], b2[0], b2[2], mm3(b2[3]),
                            Path(b2[4]['src_file']).name, b2[4]['src_locator']])
    H = ['external_code', 'расхождение_мм', 'код_1', 'прайс_1', 'мм_1', 'файл_1', 'строка_1',
         'код_2', 'прайс_2', 'мм_2', 'файл_2', 'строка_2']
    dump(sorted(((g, e, w[0], w[1]) for g, e, w in big_gap), key=lambda x: -x[0]),
         'step6_gap_over50.csv', H)
    dump(sorted(same_code, key=lambda x: -x[0]), 'step6_samecode_gap.csv', H)

    # ---------- 3. наш ручной размер (own_manual) -----------------------------
    ms_raw = defaultdict(dict)
    ms_date = None
    for ms_id, nm, val, ld in d['ms_dims']:
        try:
            ms_raw[ms_id][nm] = float(str(val).replace(',', '.'))
        except (TypeError, ValueError):
            continue
        ms_date = max(ms_date, ld) if ms_date else ld
    own = {}                       # ext -> (dims_mm, ms_id, число товаров)
    own_cnt = Counter()
    for ms_id, code, ext, art, name in prods:
        a = ms_raw.get(ms_id)
        if not ext or not a or len(a) < 3:
            continue
        dd = tuple(sorted((a['Длина, см.'] * 10, a['Ширина, см.'] * 10, a['Высота, см.'] * 10),
                          reverse=True))
        if min(dd) <= 0:
            continue
        own_cnt[ext] += 1
        if ext not in own or vol_l(dd) > vol_l(own[ext][0]):
            own[ext] = (dd, ms_id, code)
    own_only = [e for e in own if e not in chosen]
    ob = Counter()
    for e, (dd, ms_id, code) in own.items():
        if e in chosen:
            ob[basket(gap(dd, chosen[e][3]))] += 1
    # тот же ручной размер, но уже залитый на карточки WB — контрольный срез
    wb_by_model = {}
    for acc, nm, vc, l, wd, h, upd in d['wb']:
        m = P4.match((vc or '').strip())
        if m:
            wb_by_model.setdefault(m.group(1), (float(l) * 10, float(wd) * 10, float(h) * 10))
    st['3'] = {'источник': 'МойСклад, атрибуты «Длина, см.»/«Ширина, см.»/«Высота, см.» '
                           '(raw_moysklad_product.payload→attributes, указатель = ms_id + имя атрибута)',
               'товаров с тремя атрибутами': len([1 for a in ms_raw.values() if len(a) >= 3]),
               'дата среза (loaded_at)': str(ms_date),
               'моделей с нашим размером': len(own),
               'из них закрываются ИМ ОДНИМ (в прайсах размера нет)': len(own_only),
               'четырёхзначных среди них': sum(1 for e in own_only if IS4.match(e)),
               'расхождение с выбранной коробкой прайса': dict(ob),
               'контроль — карточки WB с габаритами': {
                   'строк': len(d['wb']), 'моделей по 4-значному префиксу': len(wb_by_model),
                   'дата': str(max(r[6] for r in d['wb']))}}

    # ---------- 4. rapid ------------------------------------------------------
    conf = {}
    for e, v in boxes.items():
        cs = [b[3] for b in v if b[2] in ('cactus', 'sakura_laser', 'sakura_inkjet')]
        if cs:
            conf[e] = cs
    rr, band = [], Counter()
    for e, lst in rapid_by_model.items():
        if e not in conf:
            continue
        rd = lst[0][1][0]
        base = min(conf[e], key=vol_l)
        if not vol_l(base):
            continue
        r = round(vol_l(rd) / vol_l(base), 2)
        rr.append((r, e, lst[0][0], rd, base, lst[0][1][1]))
        if 0.7 <= r <= 1.4:
            band['0.7-1.4 (та же ИУ)'] += 1
        elif any(abs(r - k) <= 0.15 * k for k in (2, 4, 6, 8, 10, 12)):
            band['около чётного 2..12 (МК)'] += 1
        else:
            band['не ложится'] += 1
    rapid_new = [e for e in rapid_by_model if e not in chosen]
    st['4'] = {'строк rapid с размерами': len(rapid_dims),
               'моделей rapid×подтверждённая ИУ': len(rr), 'распределение': dict(band),
               'медиана отношения': round(statistics.median([x[0] for x in rr]), 2) if rr else None,
               'rapid добавит моделей сверх прайсов': len(rapid_new),
               'из них четырёхзначных': sum(1 for e in rapid_new if IS4.match(e)),
               'из них нет и ручного размера': len([e for e in rapid_new if e not in own])}
    with (DATA / 'step6_rapid_ratio.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['модель', 'наш_код', 'отношение_объёмов', 'rapid_мм', 'ИУ_мм', 'rapid_iu_raw',
                    'rapid_volume_raw', 'qty_in_pack', 'локатор_rapid', 'наименование'])
        for r, e, code, rd, base, ptr in sorted(rr, key=lambda x: -x[0]):
            w.writerow([e, code, r, mm3(rd), mm3(base), ptr['iu_raw'], ptr['volume_raw'],
                        ptr['qty'], ptr['src_locator'], ptr['name']])

    # ---------- 5. profiline --------------------------------------------------
    import xlrd
    bk = xlrd.open_workbook(str(PROFILINE))
    hits, ex = 0, []
    for sh in bk.sheets():
        for r in range(sh.nrows):
            for c in range(sh.ncols):
                val = sh.cell_value(r, c)
                if isinstance(val, str) and DIMPAT.search(val):
                    hits += 1
                    if len(ex) < 3:
                        ex.append(f'{sh.name} r{r}c{c}: {val[:80]}')
    sh = bk.sheet_by_index(0)
    rows_a = w_n = v_n = 0
    for r in range(10, sh.nrows):
        if not str(sh.cell_value(r, 4)).strip():
            continue
        rows_a += 1
        w_n += bool(str(sh.cell_value(r, 9)).strip())
        v_n += bool(str(sh.cell_value(r, 10)).strip())
    st['5'] = {'листов': bk.nsheets,
               'листы': [f'{s.name}/visibility={s.visibility}/{s.nrows} строк' for s in bk.sheets()],
               'шапка (строка 9)': ' | '.join(str(sh.cell_value(9, c)).strip() for c in range(sh.ncols)),
               'ячеек с шаблоном ЧxЧxЧ': hits, 'примеры': ex,
               'строк с артикулом': rows_a, 'Вес непустой': w_n, 'Объём непустой': v_n}

    # ---------- 6. итоговая таблица покрытия ----------------------------------
    all_ext = {e for m, c, e, a, n in prods if e}
    ext4 = {e for e in all_ext if IS4.match(e)}
    cover = {'закрыты прайсами': set(chosen), 'закрыты только ручным': set(own_only),
             'потенциально добавит rapid': {e for e in rapid_new if e not in own}}
    cover['без размера'] = all_ext - cover['закрыты прайсами'] - cover['закрыты только ручным']
    st['6'] = {'моделей всего (уникальных external_code)': len(all_ext),
               'четырёхзначных': len(ext4),
               'строки': {k: {'всего': len(v), '4-значных': len(v & ext4),
                              '% от 4-значных': round(100 * len(v & ext4) / len(ext4), 1)}
                          for k, v in cover.items()}}

    # ---------- 7. остаток ----------------------------------------------------
    sup_of = dict(d['sup'])
    brand_of = dict(d['brand'])
    by_ext = defaultdict(list)
    for ms_id, code, ext, art, name in prods:
        if ext:
            by_ext[ext].append((ms_id, code, art, name))
    with (DATA / 'models_no_dims.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['external_code', 'article', 'название', 'бренд', 'окончания_code',
                    'поставщик_со_склада', 'причина'])
        for e in sorted(cover['без размера']):
            items = by_ext[e]
            arts = sorted({a for _, _, a, _ in items if a.strip()})
            sufs = sorted({c[len(e):] for _, c, _, _ in items if c.startswith(e) and c[len(e):]})
            brs = sorted({brand_of[m] for m, _, _, _ in items if m in brand_of})
            sups = sorted({sup_of[m] for m, _, _, _ in items if m in sup_of})
            hits_p = sorted(hit_any.get(e, ()))
            reason = f'есть в прайсе без габаритов ({", ".join(hits_p)})' if hits_p else 'нет в прайсах'
            w.writerow([e, ', '.join(arts[:6]), items[0][3][:80], ', '.join(brs[:3]),
                        ', '.join(sufs[:8]), ', '.join(sups[:3]), reason])
    st['7'] = {'моделей без размера': len(cover['без размера']),
               'из них 4-значных': len(cover['без размера'] & ext4),
               'причина: есть в прайсе без габаритов': sum(1 for e in cover['без размера'] if hit_any.get(e)),
               'причина: нет в прайсах': sum(1 for e in cover['без размера'] if not hit_any.get(e))}

    (DATA / 'step6_stats.json').write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding='utf-8')
    print('1:', st['1'], '\n3:', st['3']['моделей с нашим размером'],
          'только им:', st['3']['из них закрываются ИМ ОДНИМ (в прайсах размера нет)'],
          '\n6:', {k: v['4-значных'] for k, v in st['6']['строки'].items()})
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
