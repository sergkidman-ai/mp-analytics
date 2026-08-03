# поток: gab
"""Массовый сбор габаритов с nix.ru браузером. Запуск ТОЛЬКО на домашнем компьютере.

Путь выбран окончательно: браузер + карточки /autocatalog/. Внутренний JSON мы САМИ
не формируем и в /scripts/ не ходим: запрос FastSearch/goods делает сама страница,
мы лишь дожидаемся его завершения и читаем уже пришедшую выдачу.

ВАЖНО (дефект прогона 03.08): ссылки нельзя брать из DOM «когда появятся» —
до прихода выдачи в разметке лежит 101 посторонняя ссылка, одинаковая для всех
артикулов. Поэтому: ждём ответ FastSearch/goods, ссылки и заголовки берём из
поля goods.html этого ответа. Ответа нет за 30 с — в журнал «выдача не получена»
и дальше; содержимое страницы вместо выдачи НЕ подставляем.

Что делает по каждой модели:
  1) ищет по ключу `article` (артикул поставщика из МойСклада, как есть, посимвольно);
     если после отбора не осталось ни одной подходящей карточки — повторяет поиск
     по `oem_article` (тоже как есть). Оба поля пусты — модель пропускается, причина в журнал;
  2) из пришедшей выдачи забирает все ссылки /autocatalog/ вместе с заголовками и отсеивает
     ДО открытия: искомый код обязан стоять в заголовке; тип товара обязан совпасть с нашим;
     главная и «Архив каталога» — не карточки;
  3) открывает до пяти прошедших отбор карточек, сохраняет HTML в nix_raw/<external_code>/,
     имя файла — из canonical-ссылки внутри самой страницы (заголовок Windows режет);
  4) читает «Размеры упаковки (измерено в НИКСе)» и «Вес брутто (измерено в НИКСе)».
     Сантиметры в миллиметры — только точным умножением на 10, исходная строка рядом.
     Ничего не округляется, не усредняется и не выводится из похожих моделей.
     Размеров нет — это нормальный результат, так и пишется.

Файлы рядом со скриптом:
  nix_collect_log.csv  — по строке на модель: пришёл ли ответ выдачи и сколько в нём позиций
                         (total), ключ, ссылок, прошло отбор, карточек открыто, размеров
                         прочитано (по нему же resume);
  nix_links.csv        — все ссылки выдачи с вердиктом отбора (проверяемость фильтра);
  nix_dims.csv         — найденные величины с источником: файл, канонический адрес,
                         заголовок карточки, точная строка;
  nix_raw/<модель>/*.html — сохранённые карточки;
  nix_collect.log      — ход работы.

Модель считается пройденной только если выдача РЕАЛЬНО получена (или обе строки-ключа
пусты / наш тип не читается). Строки с «выдача не получена» при следующем запуске
переигрываются, а не пропускаются.

Один поток, пауза 5-8 с, обычное окно браузера, обычный User-Agent, никаких обходов защиты.
Ошибка на одной модели не роняет прогон.

Установка:  pip install playwright  и  playwright install chromium
Запуск:     python nix_collect.py 100          (число — сколько моделей за запуск)
"""
import csv
import html as H
import json
import random
import re
import sys
import time
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit('Не установлен playwright. Выполните: pip install playwright '
             'и затем playwright install chromium')

HERE = Path(__file__).resolve().parent
INPUT = HERE / 'nix_input.csv'              # положить рядом со скриптом
RAW = HERE / 'nix_raw'
LOGCSV = HERE / 'nix_collect_log.csv'
LINKS = HERE / 'nix_links.csv'
DIMS = HERE / 'nix_dims.csv'
LOG = HERE / 'nix_collect.log'

SEARCH_URL = ('https://www.nix.ru/price/price_list.html'
              '?section=cartridges_toner_paper_ink_all'
              '#c_id=110&fn=110&g_id=59&keywords={kw}&new_goods=0&page=1&sort=0'
              '&spoiler=&store=msk-0_1721_1&thumbnail_view=2')
GOODS = 'FastSearch/goods'                   # запрос делает сама страница, мы его ждём
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
DEFAULT_LIMIT = 100                          # моделей за запуск, можно задать аргументом
OPEN_MAX = 5                                 # сколько прошедших отбор карточек открывать
WAIT_RESULTS_S = 30                          # ждём выдачу столько и не дольше
PAUSE = (5.0, 8.0)                           # между моделями и между карточками

# ---- разбор карточки (тот же парсер, семантика не менялась) ----
DIM_LABEL = re.compile(
    r'>\s*((?:Размеры упаковки|Габариты)[^<]*)</td>\s*<td[^>]*>\s*(?:<div[^>]*>)?\s*([^<]+?)\s*<')
WEIGHT_LABEL = re.compile(r'>\s*(Вес[^<]*)</td>\s*<td[^>]*>\s*(?:<div[^>]*>)?\s*([^<]+?)\s*<')
DIM_VALUE = re.compile(
    r'^\s*([\d.,]+)\s*[xх×]\s*([\d.,]+)\s*[xх×]\s*([\d.,]+)\s*(мм|см|m|cm)?\s*$', re.I)
TITLE = re.compile(r'<title[^>]*>(.*?)</title>', re.S | re.I)
CANON = re.compile(r'<link[^>]+rel=["\']canonical["\'][^>]+href=["\']([^"\']+)', re.I)
OGURL = re.compile(r'og:url["\'][^>]+content=["\']([^"\']+)', re.I)
# ссылка на карточку в выдаче: кавычки в вёрстке никса бывают обоих видов
A_TAG = re.compile(r'<a\b[^>]*href=[\'"]([^\'"]*/autocatalog/[^\'"]*)[\'"][^>]*>(.*?)</a>', re.S | re.I)
TAGS = re.compile(r'<[^>]+>')

# Тип предмета по смыслу, а не по точному совпадению строки. Порядок важен:
# «чип к картриджу» — это чип, «тонер-картридж» — это картридж.
TYPES = [('чип', r'\bчип\b|\bchip\b'),
         ('девелопер', r'девелопер|developer|девелопир'),
         ('термоплёнка', r'термоплен|термоплён|термо-плен|термо плен|т/плен|т/плён'),
         ('барабан', r'драм[- ]?юнит|drum|фотобарабан|барабан|фотовал|фотокондуктор|photoconductor'),
         ('тонер-туба', r'тонер-туба|тонер в тубе|\bтуба\b'),
         ('картридж', r'тонер-картридж|картридж|cartridge|\bк-ж\b|картр\.'),
         ('тонер', r'\bтонер\b|бутыл'),
         ('запчасть', r'ролик|шестерн|\bвал\b|плат[аы]\b|узел|печк|фьюзер|термоузел')]
NOT_CARD = re.compile(r'/autocatalog/cc/main|/autocatalog/?$|archive|архив', re.I)


def log(msg: str):
    line = f'{datetime.now():%H:%M:%S} {msg}'
    print(line)
    with LOG.open('a', encoding='utf-8') as fh:
        fh.write(line + '\n')


def norm(s: str) -> str:
    return re.sub(r'[^0-9A-Z]', '', (s or '').upper())


def safe(s: str) -> str:
    return re.sub(r'[^0-9A-Za-z_.-]', '_', s)[:80]


def text_of(chunk: str) -> str:
    return re.sub(r'\s+', ' ', H.unescape(TAGS.sub(' ', chunk))).strip()


def kind(name: str) -> str:
    """Тип предмета по названию. 'не определён' = тип не читается однозначно."""
    low = (name or '').lower()
    for label, pat in TYPES:
        if re.search(pat, low):
            return label
    return 'не определён'


def file_name(canon: str, href: str) -> str:
    """Имя файла — из canonical-ссылки внутри HTML (заголовок Windows режет и удваивает)."""
    src = canon or href
    path = urlsplit(src).path.strip('/')
    path = re.sub(r'^autocatalog/', '', path)
    path = re.sub(r'\.html?$', '', path)
    name = safe(path.replace('/', '_')) or 'card'
    return name + '.html'


def parse_card(text: str, path: Path):
    """Величины карточки как они записаны. Ничего не досчитываем."""
    m = TITLE.search(text)
    title = unescape(re.sub(r'\s+', ' ', m.group(1))).strip() if m else ''
    title = re.sub(r'\s*\|\s*(Купить|НИКС|nix\.ru).*$', '', title, flags=re.I).strip()
    c = CANON.search(text) or OGURL.search(text)
    row = {'файл': path.name, 'адрес_canonical': c.group(1) if c else '',
           'заголовок_карточки': title[:180], 'тип_карточки': kind(title),
           'метка': '', 'строка_размеров': '', 'единицы': '',
           'Д_мм': '', 'Ш_мм': '', 'В_мм': '', 'строка_веса': '', 'вес': ''}
    dm = DIM_LABEL.search(text)
    if dm:
        row['метка'] = unescape(dm.group(1)).strip()
        row['строка_размеров'] = unescape(dm.group(2)).strip()
        v = DIM_VALUE.match(row['строка_размеров'])
        if v:
            unit = (v.group(4) or '').lower()
            row['единицы'] = unit or '(не указаны)'
            mult = {'см': 10.0, 'cm': 10.0, 'мм': 1.0, '': 1.0, 'm': 1.0}.get(unit)
            if mult:                       # см→мм только точным ×10, без округлений
                d = [float(v.group(i).replace(',', '.')) * mult for i in (1, 2, 3)]
                row['Д_мм'], row['Ш_мм'], row['В_мм'] = (f'{x:g}' for x in d)
    else:
        row['строка_размеров'] = 'РАЗМЕРОВ В ФАЙЛЕ НЕТ'
    wm = WEIGHT_LABEL.search(text)
    if wm:
        row['строка_веса'] = unescape(wm.group(1)).strip()
        row['вес'] = unescape(wm.group(2)).strip()
    return row


def links_from_goods(goods_html: str):
    """Ссылки на карточки и их заголовки ИЗ ОТВЕТА ВЫДАЧИ. У одной карточки в строке
    несколько ссылок (картинка и название) — берём самый длинный текст на адрес."""
    best = {}
    for m in A_TAG.finditer(goods_html or ''):
        url = urljoin('https://www.nix.ru/', m.group(1)).split('?')[0]
        t = text_of(m.group(2))
        if len(t) > len(best.get(url, '')):
            best[url] = t
    return [{'адрес': u, 'заголовок': t[:250]} for u, t in best.items()]


# ---- вход и журнал --------------------------------------------------------------
def read_models():
    if not INPUT.exists():
        sys.exit(f'Нет файла {INPUT}. Положите nix_input.csv рядом со скриптом.')
    out = []
    with INPUT.open(encoding='utf-8-sig') as fh:
        for r in csv.DictReader(fh, delimiter=';'):
            out.append({'external_code': (r.get('external_code') or '').strip(),
                        'наше_название': (r.get('наше_название') or '').strip(),
                        # ключи посылаются КАК ЕСТЬ: без нормализации, без отбрасывания
                        # префиксов/суффиксов, без приведения регистра
                        'article': (r.get('article') or '').strip(),
                        'oem_article': (r.get('oem_article') or '').strip()})
    return out


LOG_COLS = ['время', 'external_code', 'наше_название', 'наш_тип', 'ключ_сработал', 'значение_ключа',
            'article_ответ', 'article_total', 'article_ссылок', 'article_прошло',
            'oem_ответ', 'oem_total', 'oem_ссылок', 'oem_прошло',
            'выдача_получена', 'карточек_открыто', 'размеров_прочитано', 'итог']
LINK_COLS = ['external_code', 'ключ', 'значение_ключа', 'адрес', 'заголовок',
             'тип_карточки', 'вердикт', 'открыта']
DIM_COLS = ['external_code', 'ключ', 'значение_ключа', 'файл', 'адрес_canonical',
            'заголовок_карточки', 'тип_карточки', 'метка', 'строка_размеров', 'единицы',
            'Д_мм', 'Ш_мм', 'В_мм', 'строка_веса', 'вес']


def done_models() -> set:
    """Пройденные модели. Строка, где выдача НЕ получена, пройденной не считается —
    иначе неудача первого прогона молча потеряла бы модель навсегда."""
    if not LOGCSV.exists():
        return set()
    with LOGCSV.open(encoding='utf-8-sig') as fh:
        rd = csv.DictReader(fh, delimiter=';')
        if rd.fieldnames != LOG_COLS:        # журнал старого формата (прогон до правки)
            old = LOGCSV.with_suffix('.old.csv')
            fh.close()
            LOGCSV.replace(old)
            log(f'журнал старого формата переименован в {old.name}; '
                f'записанные в нём модели будут пройдены заново')
            return set()
        done = set()
        for r in rd:
            if (r.get('выдача_получена') == 'да') or str(r.get('итог', '')).startswith('пропуск'):
                done.add((r.get('external_code') or '').strip())
    return done


def append(path: Path, cols, rows):
    new = not path.exists()
    with path.open('a', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=';', extrasaction='ignore')
        if new:
            w.writeheader()
        w.writerows(rows)


# ---- работа с выдачей -----------------------------------------------------------
def fetch_goods(page, goods_buf, keyword: str):
    """Открыть выдачу и дождаться ОТВЕТА FastSearch/goods, который делает сама страница.
    Возвращает разобранный объект goods или None, если за WAIT_RESULTS_S не пришёл.
    Содержимое страницы вместо выдачи не подставляется — молчание лучше чужих ссылок."""
    goods_buf.clear()
    page.goto('about:blank', wait_until='domcontentloaded', timeout=30000)
    page.goto(SEARCH_URL.format(kw=quote(keyword)), wait_until='domcontentloaded', timeout=60000)
    # смена хвоста после # страницу не перезагружает, поэтому reload — как в рабочем пробнике
    page.reload(wait_until='domcontentloaded', timeout=60000)
    deadline = time.time() + WAIT_RESULTS_S
    tries = {}                                # сколько раз не удалось прочитать тело ответа
    while time.time() < deadline:
        for resp in list(goods_buf):
            if tries.get(id(resp), 0) >= 2:   # тело первого ответа умирает при reload — бросаем
                continue
            try:
                d = json.loads(resp.text()).get('goods')
            except Exception:                 # тела ещё нет или оно уже недоступно
                tries[id(resp)] = tries.get(id(resp), 0) + 1
                continue
            tries[id(resp)] = 9
            if isinstance(d, dict) and 'html' in d:
                return d
        page.wait_for_timeout(500)
    return None


def select(links, code: str, our_kind: str):
    """Отбор ДО открытия карточки. Пропускаем только то, что читается однозначно:
    лучше пусто, чем чужой размер."""
    for it in links:
        title = it['заголовок']
        k = kind(title)
        it['тип_карточки'] = k
        if NOT_CARD.search(it['адрес']) or 'архив каталога' in title.lower():
            it['вердикт'] = 'не карточка (главная / архив каталога)'
        elif norm(code) not in norm(title):
            it['вердикт'] = f'кода {code} нет в заголовке'
        elif k == 'не определён':
            it['вердикт'] = 'тип карточки не читается однозначно'
        elif our_kind and k != our_kind:
            it['вердикт'] = f'у никса «{k}», у нас «{our_kind}»'
        else:
            it['вердикт'] = 'прошла'
        it['открыта'] = ''
    return [it for it in links if it['вердикт'] == 'прошла']


def try_key(page, goods_buf, model, key_name, code):
    """Один заход: выдача по ключу -> ссылки из ответа -> отбор.
    Возвращает (пришёл ли ответ, total, все ссылки, прошедшие)."""
    try:
        d = fetch_goods(page, goods_buf, code)
    except Exception as e:
        log(f'   выдача по {key_name}={code}: {str(e).splitlines()[0][:120]}')
        d = None
    if d is None:
        return False, '', [], []
    links = links_from_goods(d.get('html'))
    good = select(links, code, model['наш_тип'])
    for it in links:
        it.update(external_code=model['external_code'], ключ=key_name, значение_ключа=code)
    return True, str(d.get('total', '')), links, good


def main() -> int:
    limit = DEFAULT_LIMIT
    if len(sys.argv) > 1:
        try:
            limit = int(sys.argv[1])
        except ValueError:
            sys.exit('Аргумент — число моделей за запуск, например: python nix_collect.py 100')

    models = read_models()
    for m in models:
        m['наш_тип'] = kind(m['наше_название'])
    skip = done_models()
    queue = [m for m in models if m['external_code'] not in skip][:limit]
    RAW.mkdir(exist_ok=True)
    log(f'=== запуск: всего моделей {len(models)}, пройдено ранее {len(skip)}, '
        f'в этот заход {len(queue)} (лимит {limit})')
    stat = {'моделей': 0, 'выдача_есть': 0, 'выдачи_нет': 0, 'по_article': 0, 'по_oem': 0,
            'без_ключей': 0, 'карточек': 0, 'размеров': 0, 'пусто': 0}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(locale='ru-RU', user_agent=UA,
                                  viewport={'width': 1360, 'height': 900})
        page = ctx.new_page()
        goods_buf = []                        # ответы FastSearch/goods текущего захода
        page.on('response', lambda r: goods_buf.append(r) if GOODS in r.url else None)

        for n, m in enumerate(queue, 1):
            ext = m['external_code']
            stat['моделей'] += 1
            rec = {'время': f'{datetime.now():%Y-%m-%d %H:%M:%S}', 'external_code': ext,
                   'наше_название': m['наше_название'][:120], 'наш_тип': m['наш_тип'],
                   'ключ_сработал': '', 'значение_ключа': '',
                   'article_ответ': '', 'article_total': '', 'article_ссылок': '', 'article_прошло': '',
                   'oem_ответ': '', 'oem_total': '', 'oem_ссылок': '', 'oem_прошло': '',
                   'выдача_получена': 'нет', 'карточек_открыто': 0, 'размеров_прочитано': 0, 'итог': ''}
            log(f'[{n}/{len(queue)}] {ext} | {m["наше_название"][:60]} | наш тип: {m["наш_тип"]}')

            if not m['article'] and not m['oem_article']:
                rec['итог'] = 'пропуск: нет ни article, ни oem_article'
                stat['без_ключей'] += 1
                log('   пропуск: оба ключа пусты')
                append(LOGCSV, LOG_COLS, [rec])
                continue
            if m['наш_тип'] == 'не определён':
                rec['итог'] = 'пропуск: наш тип товара не читается однозначно'
                log('   пропуск: наш тип не читается однозначно')
                append(LOGCSV, LOG_COLS, [rec])
                continue

            all_links, good, used_key, used_val, any_resp = [], [], '', '', False
            for key_name in ('article', 'oem_article'):
                code = m[key_name]
                if not code:
                    continue
                got, total, links, passed = try_key(page, goods_buf, m, key_name, code)
                any_resp = any_resp or got
                all_links += links
                pfx = 'article' if key_name == 'article' else 'oem'
                rec[f'{pfx}_ответ'] = 'да' if got else 'нет'
                rec[f'{pfx}_total'] = total
                rec[f'{pfx}_ссылок'] = len(links)
                rec[f'{pfx}_прошло'] = len(passed)
                log(f'   ключ {key_name}={code}: ответ выдачи {"да" if got else "НЕТ"}, '
                    f'total {total or "-"}, ссылок {len(links)}, прошло отбор {len(passed)}')
                if passed:
                    good, used_key, used_val = passed, key_name, code
                    break
                time.sleep(random.uniform(*PAUSE))

            rec['выдача_получена'] = 'да' if any_resp else 'нет'
            rec['ключ_сработал'] = used_key or 'ни один'
            rec['значение_ключа'] = used_val
            stat['выдача_есть' if any_resp else 'выдачи_нет'] += 1

            # ---- открываем до пяти прошедших отбор карточек ----------------------
            dims_rows, opened, got_dims = [], 0, 0
            folder = RAW / safe(ext)
            for it in good[:OPEN_MAX]:
                try:
                    page.goto(it['адрес'], wait_until='domcontentloaded', timeout=60000)
                    card_html = page.content()
                except Exception as e:
                    log(f'   карточка не открылась: {str(e).splitlines()[0][:110]}')
                    continue
                c = CANON.search(card_html) or OGURL.search(card_html)
                folder.mkdir(parents=True, exist_ok=True)
                dst = folder / file_name(c.group(1) if c else '', it['адрес'])
                dst.write_text(card_html, encoding='utf-8')
                it['открыта'] = dst.name
                opened += 1
                row = parse_card(card_html, dst)
                row.update(external_code=ext, ключ=used_key, значение_ключа=used_val)
                if not row['адрес_canonical']:
                    row['адрес_canonical'] = it['адрес']
                if row['Д_мм']:
                    got_dims += 1
                dims_rows.append(row)
                time.sleep(random.uniform(*PAUSE))

            rec['карточек_открыто'] = opened
            rec['размеров_прочитано'] = got_dims
            if not any_resp:
                rec['итог'] = 'выдача не получена (ответ FastSearch/goods не пришёл за 30 с)'
            elif not good:
                rec['итог'] = 'подходящих карточек в выдаче нет'
                stat['пусто'] += 1
            elif not got_dims:
                rec['итог'] = f'карточки открыты ({opened}), размеров в них нет'
            else:
                rec['итог'] = f'размеров получено {got_dims}'
                stat['по_article' if used_key == 'article' else 'по_oem'] += 1
            stat['карточек'] += opened
            stat['размеров'] += got_dims
            log(f'   открыто карточек {opened}, размеров прочитано {got_dims} — {rec["итог"]}')

            append(LOGCSV, LOG_COLS, [rec])
            if all_links:
                append(LINKS, LINK_COLS, all_links)
            if dims_rows:
                append(DIMS, DIM_COLS, dims_rows)
            time.sleep(random.uniform(*PAUSE))

        ctx.close()
        browser.close()

    log('ИТОГ: ' + ', '.join(f'{k} {v}' for k, v in stat.items()))
    log(f'Пришлите файлы: {LOGCSV.name}, {LINKS.name}, {DIMS.name}, {LOG.name} '
        f'и папку nix_raw (можно архивом).')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nПрервано. Собранное сохранено, повторный запуск продолжит с места остановки.')
