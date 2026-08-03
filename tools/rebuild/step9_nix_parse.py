# поток: gab
"""Шаг 9 — разбор материала разведки nix.ru (задачи 2 и 3).

Задача 2: по каждому сохранённому файлу nix_raw/card_*.html вытащить название,
тип предмета, СТРОКУ размеров как есть, единицы и вес. Перевод см→мм разрешён
только точным умножением на 10, исходная строка сохраняется рядом. Ничего не
округляется, не усредняется, не досчитывается. Нет размеров — так и помечается.

Задача 3: отбор карточки под наш запрос:
  1) искомый код обязан присутствовать в названии карточки;
  2) тип предмета на никсе обязан совпадать с типом в нашем названии
     (картридж↔картридж, барабан↔барабан, чип↔чип, термоплёнка↔термоплёнка);
     чипы, драм-юниты, фотокондукторы и запчасти при нашем «картридж» отбрасываются;
  3) card_main.html — не карточка товара, а главная: отбрасывается.

Пишет docs/rebuild/data/nix_cards.csv (разбор) и nix_verdict.csv (отбор по артикулам).
"""
import csv
import re
import sys
from html import unescape
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_bridge import DATA                                    # noqa: E402

csv.field_size_limit(10 ** 7)
RAW = DATA / 'nix_raw'

# «Размеры упаковки (измерено в НИКСе)» / «Габариты (ширина x высота x глубина)»
DIM_LABEL = re.compile(
    r'>\s*((?:Размеры упаковки|Габариты)[^<]*)</td>\s*<td[^>]*>\s*(?:<div[^>]*>)?\s*([^<]+?)\s*<')
WEIGHT_LABEL = re.compile(r'>\s*(Вес[^<]*)</td>\s*<td[^>]*>\s*(?:<div[^>]*>)?\s*([^<]+?)\s*<')
DIM_VALUE = re.compile(r'^\s*([\d.,]+)\s*[xх×]\s*([\d.,]+)\s*[xх×]\s*([\d.,]+)\s*(мм|см|m|cm)?\s*$', re.I)
TITLE = re.compile(r'<title[^>]*>(.*?)</title>', re.S | re.I)
# адрес самой карточки, записанный внутри страницы: имена файлов с Windows
# усечены и продублированы (.html.html), по ним сопоставлять нельзя
CANON = re.compile(r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"', re.I)
OGURL = re.compile(r'og:url"[^>]+content="([^"]+)"', re.I)

TYPES = [('чип', r'\bчип\b|\bchip\b'),
         ('фотокондуктор', r'фотокондуктор|photoconductor'),
         ('барабан', r'драм[- ]?юнит|drum|барабан|фотобарабан|фотовал'),
         ('термоплёнка', r'термоплен|термоплён|термо-плен'),
         ('тонер', r'\bтонер\b(?!-картридж)|тонер в тубе|бутыл'),
         ('запчасть', r'ролик|шестерн|вал\b|плат[аы]\b|узел'),
         ('картридж', r'картридж|cartridge|тонер-картридж')]


def norm(s: str) -> str:
    return re.sub(r'[^0-9A-Z]', '', (s or '').upper())


def safe(s: str) -> str:
    return re.sub(r'[^0-9A-Za-zА-Яа-я_.-]', '_', s)[:60]


def kind(name: str) -> str:
    """Тип предмета по названию. Порядок важен: «чип к картриджу» — это чип."""
    low = (name or '').lower()
    for label, pat in TYPES:
        if re.search(pat, low):
            return label
    return 'не определён'


def parse_card(path: Path) -> dict:
    t = path.read_text(encoding='utf-8', errors='ignore')
    m = TITLE.search(t)
    title = unescape(re.sub(r'\s+', ' ', m.group(1))).strip() if m else ''
    title = re.sub(r'\s*\|\s*(Купить|НИКС|nix\.ru).*$', '', title, flags=re.I).strip()

    c = CANON.search(t) or OGURL.search(t)
    row = {'файл': path.name, 'адрес': (c.group(1) if c else ''),
           'название': title[:150], 'тип': kind(title),
           'строка_размеров': '', 'метка': '', 'единицы': '', 'вес': '',
           'Д_мм': '', 'Ш_мм': '', 'В_мм': ''}
    dm = DIM_LABEL.search(t)
    if dm:
        row['метка'] = unescape(dm.group(1)).strip()
        row['строка_размеров'] = unescape(dm.group(2)).strip()
        v = DIM_VALUE.match(row['строка_размеров'])
        if v:
            unit = (v.group(4) or '').lower()
            row['единицы'] = unit or '(не указаны)'
            mult = {'см': 10.0, 'cm': 10.0, 'мм': 1.0, '': 1.0, 'm': 1.0}.get(unit, None)
            if mult:                       # см→мм только точным ×10, без округлений
                d = [float(v.group(i).replace(',', '.')) * mult for i in (1, 2, 3)]
                row['Д_мм'], row['Ш_мм'], row['В_мм'] = (f'{x:g}' for x in d)
    else:
        row['строка_размеров'] = 'РАЗМЕРОВ В ФАЙЛЕ НЕТ'
    wm = WEIGHT_LABEL.search(t)
    if wm:
        row['вес'] = unescape(wm.group(2)).strip()
    return row


def main() -> int:
    # какой артикул к какой карточке: ключ — адрес карточки из выдачи,
    # он же записан внутри сохранённого HTML (canonical)
    art_of = {}                      # адрес -> артикулы запроса
    tried = []                       # артикулы, которые прогон реально запрашивал
    urls_of = {}                     # артикул -> адреса из выдачи
    nm_of = {}                       # адрес -> название строкой выдачи (полное, без усечения)
    with (DATA / 'nix_raw' / 'nix_found.csv').open(encoding='utf-8-sig') as fh:
        for r in csv.DictReader(fh, delimiter=';'):
            a = r['артикул_запроса'].strip()
            if a not in tried:
                tried.append(a)
            url = (r['адрес_карточки'] or '').strip().split('?')[0]
            if url:
                art_of.setdefault(url, []).append(a)
                urls_of.setdefault(a, []).append(url)
                nm_of[url] = (r.get('название_из_выдачи') or '').strip()

    # наш тип по артикулу запроса — из входного файла, только по опробованным
    ours = {}
    with (DATA / 'nix_input.csv').open(encoding='utf-8-sig') as fh:
        for r in csv.DictReader(fh, delimiter=';'):
            a = (r.get('oem_article') or '').strip()
            if a in tried and a not in ours:
                ours[a] = (r['наше_название'], kind(r['наше_название']), r['external_code'])

    cards = []
    for p in sorted(RAW.glob('card_*.html')):
        row = parse_card(p)
        row['артикул_запроса'] = ','.join(sorted(set(art_of.get(row['адрес'], []))))
        # заголовок страницы усечён никсом; полное название той же карточки —
        # в строке выдачи. Читаем оба текста как есть, ничего не достраивая.
        row['название_выдачи'] = nm_of.get(row['адрес'], '')
        full = f"{row['название']} {row['название_выдачи']}"
        row['тип'] = kind(full)
        cards.append(row)

    def judge(row, art):
        """Годна ли эта карточка под ЭТОТ артикул. Причины — только по ней."""
        our_kind = ours.get(art, ('', '', ''))[1]
        why = []
        if row['файл'].startswith('card_main') or '/autocatalog/cc/main' in row['адрес']:
            why.append('главная страница, не карточка')
        elif norm(art) not in norm(f"{row['название']} {row['название_выдачи']}"):
            why.append(f'кода {art} нет в названии карточки')
        if our_kind and row['тип'] != our_kind:
            why.append(f'у никса «{row["тип"]}», у нас «{our_kind}»')
        if not why and not row['Д_мм']:
            why.append('размеров в файле нет')
        return why

    # ---- задача 3: отбор ----------------------------------------------------
    for row in cards:
        arts = [a for a in row['артикул_запроса'].split(',') if a]
        row['наш_тип'] = ','.join(sorted({ours.get(a, ('', '', ''))[1] for a in arts}))
        if not arts:
            row['годен'], row['причина'] = 'нет', 'артикул запроса не сопоставлен'
            continue
        verd = {a: judge(row, a) for a in arts}
        good = [a for a, w in verd.items() if not w]
        row['годен'] = 'да' if good else 'нет'
        row['причина'] = ('годна для: ' + ', '.join(good)) if good else \
            '; '.join(f'{a}: {", ".join(w)}' for a, w in verd.items())

    cols = ['файл', 'адрес', 'артикул_запроса', 'название', 'название_выдачи', 'тип', 'наш_тип', 'метка',
            'строка_размеров', 'единицы', 'Д_мм', 'Ш_мм', 'В_мм', 'вес', 'годен', 'причина']
    with (DATA / 'nix_cards.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=';')
        w.writeheader()
        w.writerows(cards)

    # ---- итог по артикулам ---------------------------------------------------
    verdict = []
    for art in tried:
        name, k, ext = ours.get(art, ('', '', ''))
        seen = [c for c in cards if art in c['артикул_запроса'].split(',')]
        good = [c for c in seen if not judge(c, art)]
        if good:
            b = good[0]
            verdict.append([ext, art, k, 'ПРИГОДЕН', b['файл'], b['строка_размеров'],
                            b['единицы'], f"{b['Д_мм']}x{b['Ш_мм']}x{b['В_мм']}", b['вес'], ''])
        else:
            if seen:
                why = '; '.join(f'{c["файл"][:28]}: {", ".join(judge(c, art))}' for c in seen)
            elif urls_of.get(art) and all('/autocatalog/cc/main' in u for u in urls_of[art]):
                why = 'в выдаче нет карточек товара (одна ссылка «Архив каталога» на главную)'
            elif urls_of.get(art):
                why = (f'выдача дала {len(urls_of[art])} ссылок, но ни одна карточка '
                       f'не сохранена (лимит 3 / уже был такой файл)')
            else:
                why = 'выдача пуста — ничего не найдено'
            verdict.append([ext, art, k, 'НЕ ПРИГОДЕН', '', '', '', '', '', why[:300]])
    with (DATA / 'nix_verdict.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['external_code', 'артикул', 'наш_тип', 'итог', 'файл', 'строка_размеров',
                    'единицы', 'ДхШхВ_мм', 'вес', 'причина'])
        w.writerows(verdict)

    ok = sum(1 for v in verdict if v[3] == 'ПРИГОДЕН')
    print(f'карточек разобрано {len(cards)}; с размерами '
          f'{sum(1 for c in cards if c["Д_мм"])}; без размеров '
          f'{sum(1 for c in cards if not c["Д_мм"])}')
    print(f'артикулов {len(verdict)}: пригодный размер получен {ok}, не получен {len(verdict) - ok}')
    for v in verdict:
        print(f'  {v[1]:<11} {v[3]:<12} {v[5] or v[9][:70]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
