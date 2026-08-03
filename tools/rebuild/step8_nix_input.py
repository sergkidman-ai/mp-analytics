# поток: gab
"""Шаг 8, задача 1 — входной список для сверки по nix.ru.

Из docs/rebuild/data/step7_web_batch.csv собирает nix_input.csv.
oem_article берётся ТОЛЬКО оттуда, где он буквально записан:
  A) поле прайса, ЯВНО названное оригинальным артикулом — у sakura «Номер
     оригинального картриджа» / «Артикул оригинального картриджа». Указатель:
     файл + лист!строка. Поля izi «аналог» и rapid «CodeShort» НЕ берём: там
     вперемешку OEM-коды и модели принтеров (Q5949A рядом с «LBP 6000», «P1102»),
     однозначно прочитать нельзя;
  B) OEM-код, буквально стоящий в названии карточки МойСклада, в части ДО слова
     «для» (после «для» перечислены принтеры). Свой совместимый артикул из прайса
     (CS-Q5949AS, CS-EXV6) исключается — выводить из него OEM запрещено.
Берётся только при ЕДИНСТВЕННОМ кандидате; два и больше — пусто, «не определён».
Ничего не достраивается по шаблону.
"""
import csv
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_bridge import DATA, PRICES                                # noqa: E402

csv.field_size_limit(10 ** 7)
TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9\-]{2,}')
# поля прайсов, ЯВНО объявленные оригинальным артикулом
OEM_FIELDS = {'Номер оригинального картриджа', 'Артикул оригинального картриджа'}
# «до слова для» — дальше в названии идёт перечень принтеров
TAIL = re.compile(r'\bдля\b|\bfor\b', re.I)
# ресурс/фасовка, а не артикул: 2000k, 1000стр, 12x
RESOURCE = re.compile(r'^\d+\s*(k|к|шт|стр|x|х)$', re.I)
# производители оборудования: код сразу за именем бренда — это оригинальный артикул
OEM_BRAND = {'HP', 'CANON', 'SAMSUNG', 'XEROX', 'EPSON', 'BROTHER', 'KYOCERA', 'PANASONIC',
             'RICOH', 'OKI', 'LEXMARK', 'SHARP', 'KONICA', 'MINOLTA', 'TOSHIBA', 'PANTUM',
             'DELL', 'PHILIPS', 'SONY', 'NEC'}
# маркер «это совместимка с таким-то оригиналом»
COMPAT = re.compile(r'совместим|аналог|compatible', re.I)
# приставки артикулов совместимок: NV-106R03770, CS-CF360A, SG-C610M-DRUM — это чужой
# или наш совместимый код, а не оригинальный
COMPAT_PREFIX = {'NV', 'NVP', 'CS', 'HB', 'SG', 'T2', 'TC', 'EP', 'SF', 'SFR', 'BS',
                 'GG', 'GP', 'CT', 'SP', 'PL', 'SA', 'SK', 'UN'}
WORD = re.compile(r"[A-Za-zА-Яа-я0-9№'][A-Za-zА-Яа-я0-9№'\-]*")


def norm(s: str) -> str:
    return re.sub(r'[^0-9A-Z]', '', (s or '').upper())


def is_code(s: str) -> bool:
    """Форма OEM-кода: есть и буква, и цифра, длина ≥5. Отсекает «3160», «i-Sensys»,
    «1210» — они читаются как модель принтера, а не как код картриджа."""
    n = norm(s)
    if len(n) < 5 or RESOURCE.match(s.strip()):
        return False
    if '-' in s and s.split('-', 1)[0].upper() in COMPAT_PREFIX:
        return False                      # артикул совместимки, не оригинал
    return any(c.isalpha() for c in n) and any(c.isdigit() for c in n)


def split_oem(raw: str):
    """Строка OEM-поля прайса -> список кодов. Несколько разных кодов = не однозначно."""
    return [p.strip() for p in re.split(r'[,;/]|\s+или\s+', raw or '') if is_code(p)]


def read_oem(name: str, ours: set):
    """Кандидаты в OEM, буквально прочитанные из названия карточки. Два правила:
    B1 — код стоит сразу за именем производителя оборудования («HP CC388A»,
         «HP 29 (51629A)» — пропускаем не-коды в пределах двух слов);
    B2 — карточка объявлена совместимой («Совместимый картридж Colortek ML-1610D2»),
         и в части до слова «для» ровно один код, и он не наш артикул.
    Всё остальное — не читается однозначно, кандидата нет."""
    head = TAIL.split(name)[0]
    words = WORD.findall(head)
    out = []
    for i, w in enumerate(words):
        if norm(w) in OEM_BRAND:
            for nxt in words[i + 1:i + 3]:
                if not is_code(nxt):
                    continue
                # первый же код за брендом решает: если он наш — читать дальше нельзя,
                # следующий токен уже не «код сразу за производителем»
                if norm(nxt) not in ours:
                    out.append((nxt.strip('-'), 'код за именем производителя'))
                break
    if not out and COMPAT.search(name):
        codes = {norm(w): w.strip('-') for w in words if is_code(w) and norm(w) not in ours}
        if len(codes) == 1:
            out.append((next(iter(codes.values())), 'единственный код в совместимке'))
    return out


def rd(name):
    with (DATA / name).open(encoding='utf-8') as fh:
        return list(csv.DictReader(fh, delimiter=';'))


def main() -> int:
    web = rd('step7_web_batch.csv')
    bridge = rd('bridge.csv')

    # ---- строки прайсов: указатель -> OEM и наш совместимый артикул ----------
    price_oem = {}                       # (прайс, локатор) -> (коды, поле, файл)
    price_code = {}                      # (прайс, локатор) -> артикул поставщика
    ours = set()                         # ВСЕ совместимые артикулы наших прайсов
    for p in PRICES:
        for r in rd(f'{p}.csv'):
            key = (p, r['src_locator'])
            sc = (r.get('supplier_code') or '').strip()
            price_code[key] = sc
            if norm(sc):
                ours.add(norm(sc))
            o = (r.get('oem_code') or '').strip()
            if o and (r.get('oem_field') or '').strip() in OEM_FIELDS:
                codes = split_oem(o)
                if codes:
                    price_oem[key] = (codes, r['oem_field'], r['src_file'])

    # ---- модель -> строки прайса и окончания ---------------------------------
    by_model = defaultdict(list)
    suffix = defaultdict(set)
    for r in bridge:
        e = r['основа_4']
        if r['окончание']:
            suffix[e].add(r['окончание'])
        if r['прайс']:
            by_model[e].append(r)

    # ---- названия карточек МойСклада -----------------------------------------
    load_dotenv('/opt/mp-analytics/.env')
    cn = psycopg2.connect(os.environ['DATABASE_URL'])
    cur = cn.cursor()
    cur.execute("""select coalesce(external_code,''), ms_id, coalesce(name,''), coalesce(code,''),
                          coalesce(article,'')
                     from ms_product where coalesce(external_code,'') <> ''""")
    ms = defaultdict(list)
    for e, ms_id, name, code, article in cur.fetchall():
        ms[e].append((ms_id, name, code, article))
    cur.close()
    cn.close()

    out, with_oem, without = [], [], []
    for w in web:
        e = w['external_code']
        name = w['название']
        oem = field = loc = ''
        # карточка МойСклада этой модели: та, чьё название стоит в этой же строке;
        # из неё берём и артикул поставщика (поле «Артикул» МС), и кандидатов в OEM
        cards = [c for c in ms.get(e, []) if c[1].startswith(name[:60])] or ms.get(e, [])[:1]
        # article — как он лежит в МС, посимвольно; нет карточки или пусто поле — пусто
        article = (cards[0][3] if cards else '').strip()
        # A) OEM объявлен строкой прайса. Несколько разных кодов в поле — не однозначно.
        for r in by_model.get(e, []):
            hit = price_oem.get((r['прайс'], r['src_locator']))
            if not hit:
                continue
            codes, fld, src_file = hit
            if len({norm(c) for c in codes}) != 1:
                continue
            oem = codes[0]
            loc = f"{Path(src_file).name} :: {r['src_locator']}"
            field = f"прайс {r['прайс']}, поле «{fld}»"
            break
        # B) OEM буквально стоит в названии карточки МС, в части ДО слова «для».
        # Свои совместимые артикулы вычёркиваем — выводить из них OEM запрещено.
        if not oem:
            mine = {norm(price_code.get((r['прайс'], r['src_locator']), ''))
                    for r in by_model.get(e, [])} | ours
            cands = {}                                    # norm -> (код, ms_id, правило)
            for ms_id, nm, code, _art in cards[:1]:
                bad = (mine | {norm(code)}) - {''}
                for c, rule in read_oem(nm, bad):
                    cands.setdefault(norm(c), (c, ms_id, rule))
            if len(cands) == 1:
                oem, ms_id, rule = next(iter(cands.values()))
                field, loc = f'наименование карточки МойСклада ({rule})', f'ms_id {ms_id}'
        sfx = ','.join(sorted(suffix.get(e, set()))) or ''
        row = [e, name, article, oem, field or 'не определён', loc, sfx]
        out.append(row)
        (with_oem if oem else without).append(row)

    with (DATA / 'nix_input.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['external_code', 'наше_название', 'article', 'oem_article', 'src_поле',
                    'src_строка', 'окончание_бренда'])
        w.writerows(out)

    art_n = sum(1 for r in out if r[2])
    print(f'всего {len(out)} | с OEM {len(with_oem)} | без OEM {len(without)}')
    print(f'с article (МС): {art_n} | без article: {len(out) - art_n} | '
          f'есть хотя бы один ключ: {sum(1 for r in out if r[2] or r[3])}')
    src = defaultdict(int)
    for r in with_oem:
        src['прайс' if r[4].startswith('прайс') else 'название МС'] += 1
    print('источник OEM:', dict(src))
    print('--- 10 с OEM ---')
    for r in with_oem[:10]:
        print(f'  {r[0]} | арт {r[2][:16]:<16} | {r[3]:<16} | {r[1][:40]}')
    print('--- 10 без OEM ---')
    for r in without[:10]:
        print(f'  {r[0]} | {r[1][:70]}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
