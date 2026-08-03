# поток: gab
"""Массовый сбор габаритов с nix.ru браузером. Запуск ТОЛЬКО на домашнем компьютере.

Путь выбран окончательно: браузер + карточки /autocatalog/. Внутренний JSON мы САМИ
не формируем и в /scripts/ не ходим: запрос FastSearch/goods делает сама страница,
мы лишь наблюдаем его ответ.

Два урока прошлых прогонов, оба зашиты здесь:
  * ссылки нельзя брать из DOM — до прихода выдачи там лежит 101 посторонняя ссылка,
    одинаковая для всех артикулов. Берём их из ответа выдачи (поле goods.html);
  * ни одна ветка не должна ждать бесконечно. У каждого шага жёсткий предел STEP_MAX_S,
    у модели целиком — MODEL_MAX_S; тело ответа читается через страницу (у Playwright
    есть таймаут), а не методом Response.text(), который ждёт без ограничения.

Режимы поиска. У НИКСа под строкой ввода переключатели «товар / артикул / драйвер /
статьи». По умолчанию включён «товар» — поиск по названию. Наши article — это артикулы
поставщиков (NV-CC388A, CS-Q5949AS), они лежат в поле артикула, поэтому по названию
дают пусто. Имя параметра, включающего поиск по артикулу, нам неизвестно (в записанных
телах запросов нет ни одного примера с включённым переключателем), поэтому мы его НЕ
подделываем, а включаем сам переключатель на странице — как это делает человек.
Тела запросов выдачи пишутся в nix_search_probe.jsonl: после первого прогона параметр
будет виден оттуда буквально.

Порядок по модели:
  1) article с включённым переключателем «артикул»;
  2) если карточек не нашлось — oem_article в обычном режиме (по названию).
  Обе строки посылаются как есть, посимвольно.

Подтверждение карточки (сравнения буквальные, посимвольные, без нормализации):
  * найдена по article  -> артикул должен встретиться В САМОЙ КАРТОЧКЕ;
  * найдена по oem      -> код должен стоять в заголовке карточки.
  Проверка типа товара (картридж / барабан / чип / ...) действует в обоих случаях.
  Не подтверждено — размеры не берутся: лучше пусто, чем чужой размер.

Разбор карточек: «Размеры упаковки (измерено в НИКСе)» и «Вес брутто (измерено в НИКСе)».
Сантиметры в миллиметры — только точным умножением на 10, исходная строка рядом.
Ничего не округляется, не усредняется и не выводится из похожих моделей.

Файлы рядом со скриптом:
  nix_collect_log.csv    — по строке на модель (по нему же resume);
  nix_links.csv          — все ссылки выдачи с вердиктом отбора;
  nix_dims.csv           — найденные величины с источником и признаком подтверждения;
  nix_search_probe.jsonl — тела запросов выдачи (снять параметр режима и store);
  nix_raw/<модель>/*.html — сохранённые карточки;
  nix_collect.log        — ход работы.

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
PROBE = HERE / 'nix_search_probe.jsonl'
LOG = HERE / 'nix_collect.log'

# store=msk-0_1721_1 — то же значение, что уходило в удачных прогонах 03.08
# (проверено по nix_requests.jsonl: другого значения там не встречается).
SEARCH_URL = ('https://www.nix.ru/price/price_list.html'
              '?section=cartridges_toner_paper_ink_all'
              '#c_id=110&fn=110&g_id=59&keywords={kw}&new_goods=0&page=1&sort=0'
              '&spoiler=&store=msk-0_1721_1&thumbnail_view=2')
GOODS = 'FastSearch/goods'                   # запрос делает сама страница, мы его ждём
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
DEFAULT_LIMIT = 100                          # моделей за запуск, можно задать аргументом
OPEN_MAX = 5                                 # сколько карточек открывать на модель
STEP_MAX_S = 60                              # предел на ОДИН шаг (выдача / одна карточка)
MODEL_MAX_S = 180                            # предел на модель целиком, включая паузы
PAUSE = (5.0, 8.0)                           # между моделями и между карточками

# ---- разбор карточки (парсер прежний, семантика не менялась) ----
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
NEAR = 2000                                  # окно текста вокруг ссылки в выдаче, символов

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

# Наблюдатель за ответом выдачи. Запрос делает страница, мы его не формируем и не меняем:
# только запоминаем тело ответа (и тело запроса — чтобы снять параметр режима поиска).
INIT_JS = r"""
(() => {
  if (window.__nix) return;
  window.__nix = {goods: null, reqs: [], err: ''};
  const mine = u => String(u || '').indexOf('FastSearch/goods') >= 0;
  const take = t => { try { const j = JSON.parse(t); if (j && j.goods) window.__nix.goods = j.goods; }
                      catch (e) { window.__nix.err = String(e).slice(0, 200); } };
  const X = XMLHttpRequest.prototype, open = X.open, send = X.send;
  X.open = function (m, u) { this.__nixUrl = u; return open.apply(this, arguments); };
  X.send = function (body) {
    if (mine(this.__nixUrl)) {
      window.__nix.reqs.push(String(body || ''));
      this.addEventListener('load', () => take(this.responseText));
    }
    return send.apply(this, arguments);
  };
  const of = window.fetch;
  if (of) window.fetch = function (input, init) {
    const u = (typeof input === 'string') ? input : (input && input.url) || '';
    const p = of.apply(this, arguments);
    if (mine(u)) {
      window.__nix.reqs.push(String((init && init.body) || ''));
      p.then(r => { try { r.clone().text().then(take); } catch (e) {} });
    }
    return p;
  };
})();
"""
# Переключатели режима поиска: собираем все флажки с подписями, чтобы найти «артикул».
JS_TOGGLES = r"""
() => {
  const out = [];
  document.querySelectorAll('input[type=checkbox], input[type=radio]').forEach((el, i) => {
    let t = '';
    if (el.id) { const l = document.querySelector('label[for="' + el.id.replace(/"/g, '\\"') + '"]');
                 if (l) t = l.textContent; }
    if (!t && el.closest('label')) t = el.closest('label').textContent;
    if (!t && el.parentElement) t = el.parentElement.textContent;
    out.push({i: i, id: el.id || '', name: el.name || '', value: el.value || '',
              checked: !!el.checked, text: (t || '').replace(/\s+/g, ' ').trim().slice(0, 40)});
  });
  return out;
}
"""


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
           'заголовок_карточки': title[:180], 'тип_карточки': kind(title), 'подтверждение': '',
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


def confirm_in_card(card_html: str, code: str) -> str:
    """Буквальная, посимвольная проверка: стоит ли строка в самой карточке."""
    if not code:
        return ''
    if code in text_of(card_html):
        return 'артикул найден в тексте карточки'
    if code in card_html:
        return 'артикул найден в разметке карточки'
    return ''


def links_from_goods(goods_html: str):
    """Ссылки на карточки, их заголовки и текст вокруг — ИЗ ОТВЕТА ВЫДАЧИ.
    У одной карточки несколько ссылок (картинка и название) — берём самый длинный текст."""
    h = goods_html or ''
    best = {}
    for m in A_TAG.finditer(h):
        url = urljoin('https://www.nix.ru/', m.group(1)).split('?')[0]
        t = text_of(m.group(2))
        near = text_of(h[max(0, m.start() - NEAR):m.end() + NEAR])
        cur = best.get(url)
        if cur is None or len(t) > len(cur['заголовок']):
            best[url] = {'адрес': url, 'заголовок': t[:250], '_около': near}
        elif cur['заголовок'] == '' and t:
            cur['заголовок'] = t[:250]
    return list(best.values())


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
            'переключатель_артикула',
            'article_ответ', 'article_total', 'article_ссылок', 'article_прошло',
            'oem_ответ', 'oem_total', 'oem_ссылок', 'oem_прошло',
            'выдача_получена', 'карточек_открыто', 'подтверждено', 'размеров_прочитано',
            'секунд', 'итог']
LINK_COLS = ['external_code', 'ключ', 'значение_ключа', 'адрес', 'заголовок',
             'тип_карточки', 'вердикт', 'открыта']
DIM_COLS = ['external_code', 'ключ', 'значение_ключа', 'файл', 'адрес_canonical',
            'заголовок_карточки', 'тип_карточки', 'подтверждение', 'метка', 'строка_размеров',
            'единицы', 'Д_мм', 'Ш_мм', 'В_мм', 'строка_веса', 'вес']


def done_models() -> set:
    """Пройденные модели. Строка, где выдача НЕ получена (или упёрлись в предел времени),
    пройденной не считается — иначе неудача молча потеряла бы модель навсегда."""
    if not LOGCSV.exists():
        return set()
    with LOGCSV.open(encoding='utf-8-sig') as fh:
        rd = csv.DictReader(fh, delimiter=';')
        if rd.fieldnames != LOG_COLS:        # журнал старого формата (прогон до правки)
            fh.close()
            old = LOGCSV.with_name('nix_collect_log.old.csv')
            LOGCSV.replace(old)
            log(f'журнал старого формата переименован в {old.name}; '
                f'записанные в нём модели будут пройдены заново')
            return set()
        done = set()
        for r in rd:
            итог = str(r.get('итог', ''))
            if итог.startswith('превышен предел'):
                continue
            if r.get('выдача_получена') == 'да' or итог.startswith('пропуск'):
                done.add((r.get('external_code') or '').strip())
    return done


def append(path: Path, cols, rows):
    new = not path.exists()
    with path.open('a', encoding='utf-8', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=cols, delimiter=';', extrasaction='ignore')
        if new:
            w.writeheader()
        w.writerows(rows)


# ---- работа со страницей --------------------------------------------------------
def left(deadline: float, cap: float = STEP_MAX_S) -> float:
    """Сколько секунд ещё можно ждать: не больше остатка по модели и не больше предела шага."""
    return max(0.0, min(cap, deadline - time.time()))


def wait_goods(page, deadline: float):
    """Дождаться ответа выдачи, который делает сама страница. Ждём не дольше предела —
    у wait_for_function таймаут жёсткий, зависнуть здесь нельзя. Нет ответа — None."""
    ms = int(left(deadline) * 1000)
    if ms < 1000:
        return None
    try:
        page.wait_for_function('() => window.__nix && window.__nix.goods', timeout=ms)
    except Exception:
        return None
    try:
        return page.evaluate("() => { const g = window.__nix.goods;"
                             "  return g ? {html: String(g.html || ''), total: g.total} : null; }")
    except Exception:
        return None


def reset_goods(page):
    try:
        page.evaluate('() => { if (window.__nix) window.__nix.goods = null; }')
    except Exception:
        pass


def save_probe(page, ext: str, key: str, mode: str):
    """Тела запросов выдачи — чтобы снять параметр режима поиска и фактический store."""
    try:
        reqs = page.evaluate('() => (window.__nix && window.__nix.reqs) || []')
    except Exception:
        return
    if not reqs:
        return
    with PROBE.open('a', encoding='utf-8') as fh:
        for b in reqs[-3:]:
            fh.write(json.dumps({'external_code': ext, 'ключ': key, 'режим': mode,
                                 'тело_запроса': str(b)[:2000]}, ensure_ascii=False) + '\n')
    try:
        page.evaluate('() => { if (window.__nix) window.__nix.reqs = []; }')
    except Exception:
        pass


def turn_on_article(page, deadline: float):
    """Включить переключатель «артикул» на самой странице (как это делает человек).
    Имя параметра нам неизвестно и мы его не подделываем. Возвращает текст для журнала."""
    try:
        items = page.evaluate(JS_TOGGLES)
    except Exception as e:
        return f'переключатели не прочитаны: {str(e).splitlines()[0][:60]}'
    hit = [t for t in items if t['text'].lower().startswith('артикул')]
    if not hit:
        hit = [t for t in items if 'артикул' in t['text'].lower()]
    if not hit:
        return 'переключатель «артикул» не найден'
    t = hit[0]
    подпись = f"«{t['text']}» (name={t['name'] or '-'}, id={t['id'] or '-'}, value={t['value'] or '-'})"
    if t['checked']:
        return f'уже включён: {подпись}'
    box = page.locator('input[type=checkbox], input[type=radio]').nth(t['i'])
    try:
        box.check(timeout=int(min(8, left(deadline)) * 1000))
        return f'включён: {подпись}'
    except Exception:
        pass
    try:                                     # кастомный флажок бывает скрыт — жмём подпись
        page.evaluate("""i => { const el = document.querySelectorAll(
                                 'input[type=checkbox], input[type=radio]')[i];
                               const l = el.closest('label') ||
                                 (el.id && document.querySelector('label[for="' + el.id + '"]'));
                               (l || el).click(); }""", t['i'])
        return f'включён кликом по подписи: {подпись}'
    except Exception as e:
        return f'найден, но не включился: {подпись}; {str(e).splitlines()[0][:60]}'


def submit_again(page, keyword: str, deadline: float) -> bool:
    """Перезапустить поиск после смены режима, если страница не сделала этого сама."""
    for sel in ('input[name*="keyword" i]', '#keywords', 'input[type="search"]',
                'input[name*="search" i][type="text"]'):
        try:
            box = page.locator(sel).first
            if box.count() == 0:
                continue
            box.fill(keyword, timeout=int(min(8, left(deadline)) * 1000))
            box.press('Enter', timeout=int(min(8, left(deadline)) * 1000))
            return True
        except Exception:
            continue
    return False


def fetch_goods(page, keyword: str, deadline: float, by_article: bool):
    """Открыть выдачу и дождаться ответа FastSearch/goods.
    Возвращает (данные|None, отчёт о переключателе). Содержимое страницы вместо
    выдачи не подставляется — молчание лучше чужих ссылок."""
    toggle = ''
    page.goto('about:blank', wait_until='domcontentloaded', timeout=int(left(deadline, 20) * 1000) or 1)
    page.goto(SEARCH_URL.format(kw=quote(keyword)), wait_until='domcontentloaded',
              timeout=int(left(deadline) * 1000) or 1)
    # смена хвоста после # страницу не перезагружает, поэтому reload — как в рабочем пробнике
    page.reload(wait_until='domcontentloaded', timeout=int(left(deadline) * 1000) or 1)
    if by_article:
        toggle = turn_on_article(page, deadline)
        reset_goods(page)                    # ответ режима «товар» нам не нужен
        d = wait_goods(page, min(deadline, time.time() + 12))
        if d is None and left(deadline) > 3:
            submit_again(page, keyword, deadline)
            d = wait_goods(page, deadline)
        return d, toggle
    return wait_goods(page, deadline), toggle


def select(links, code: str, our_kind: str, by_article: bool):
    """Отбор ДО открытия карточки.
    По article: тип обязан совпасть; артикул в выдаче — сразу «прошла», иначе «кандидат»
                (артикул проверяется в самой карточке после открытия).
    По oem:     тип обязан совпасть И код обязан стоять в заголовке."""
    for it in links:
        title = it['заголовок']
        k = kind(title)
        it['тип_карточки'] = k
        if NOT_CARD.search(it['адрес']) or 'архив каталога' in title.lower():
            it['вердикт'] = 'не карточка (главная / архив каталога)'
        elif k == 'не определён':
            it['вердикт'] = 'тип карточки не читается однозначно'
        elif our_kind and k != our_kind:
            it['вердикт'] = f'у никса «{k}», у нас «{our_kind}»'
        elif by_article:
            # сравнение буквальное, посимвольное: артикул как есть
            it['вердикт'] = ('прошла (артикул в выдаче)' if code in it.get('_около', '')
                             else 'кандидат (артикул проверяется в карточке)')
        elif norm(code) not in norm(title):
            it['вердикт'] = f'кода {code} нет в заголовке'
        else:
            it['вердикт'] = 'прошла'
        it['открыта'] = ''
    прошли = [it for it in links if it['вердикт'].startswith('прошла')]
    кандидаты = [it for it in links if it['вердикт'].startswith('кандидат')]
    if прошли:                               # артикул в выдаче нашёлся — гадать не о чем
        for it in кандидаты:
            it['вердикт'] = 'кандидат не понадобился (артикул нашёлся в других строках)'
        return прошли
    return кандидаты


def try_key(page, model, key_name, code, deadline):
    """Один заход: выдача по ключу -> ссылки из ответа -> отбор.
    Возвращает (ответ пришёл, total, все ссылки, отобранные, отчёт о переключателе)."""
    by_article = (key_name == 'article')
    try:
        d, toggle = fetch_goods(page, code, deadline, by_article)
    except Exception as e:
        log(f'   выдача по {key_name}={code}: {str(e).splitlines()[0][:120]}')
        d, toggle = None, ''
    save_probe(page, model['external_code'], key_name, 'артикул' if by_article else 'название')
    if d is None:
        return False, '', [], [], toggle
    links = links_from_goods(d.get('html'))
    good = select(links, code, model['наш_тип'], by_article)
    for it in links:
        it.update(external_code=model['external_code'], ключ=key_name, значение_ключа=code)
    return True, str(d.get('total', '')), links, good, toggle


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
        f'в этот заход {len(queue)} (лимит {limit}); предел на шаг {STEP_MAX_S} с, '
        f'на модель {MODEL_MAX_S} с')
    stat = {'моделей': 0, 'выдача_есть': 0, 'выдачи_нет': 0, 'пусто_на_сайте': 0,
            'по_article': 0, 'по_oem': 0, 'без_ключей': 0, 'предел_времени': 0,
            'карточек': 0, 'подтверждено': 0, 'размеров': 0}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(locale='ru-RU', user_agent=UA,
                                  viewport={'width': 1360, 'height': 900})
        ctx.set_default_timeout(STEP_MAX_S * 1000)   # ни один вызов не ждёт дольше предела
        ctx.add_init_script(INIT_JS)                 # наблюдатель за ответом выдачи
        page = ctx.new_page()

        for n, m in enumerate(queue, 1):
            ext = m['external_code']
            started = time.time()
            deadline = started + MODEL_MAX_S
            stat['моделей'] += 1
            rec = {'время': f'{datetime.now():%Y-%m-%d %H:%M:%S}', 'external_code': ext,
                   'наше_название': m['наше_название'][:120], 'наш_тип': m['наш_тип'],
                   'ключ_сработал': '', 'значение_ключа': '', 'переключатель_артикула': '',
                   'article_ответ': '', 'article_total': '', 'article_ссылок': '', 'article_прошло': '',
                   'oem_ответ': '', 'oem_total': '', 'oem_ссылок': '', 'oem_прошло': '',
                   'выдача_получена': 'нет', 'карточек_открыто': 0, 'подтверждено': 0,
                   'размеров_прочитано': 0, 'секунд': '', 'итог': ''}
            log(f'[{n}/{len(queue)}] {ext} | {m["наше_название"][:60]} | наш тип: {m["наш_тип"]}')

            def close(итог):
                rec['итог'] = итог
                rec['секунд'] = f'{time.time() - started:.0f}'
                append(LOGCSV, LOG_COLS, [rec])
                log(f'   {итог} ({rec["секунд"]} с)')

            if not m['article'] and not m['oem_article']:
                stat['без_ключей'] += 1
                close('пропуск: нет ни article, ни oem_article')
                continue
            if m['наш_тип'] == 'не определён':
                close('пропуск: наш тип товара не читается однозначно')
                continue

            all_links, good, used_key, used_val, any_resp, empty = [], [], '', '', False, False
            for key_name in ('article', 'oem_article'):
                code = m[key_name]
                if not code or left(deadline, MODEL_MAX_S) < 10:
                    continue
                got, total, links, passed, toggle = try_key(page, m, key_name, code, deadline)
                if toggle:
                    rec['переключатель_артикула'] = toggle
                    log(f'   переключатель: {toggle}')
                any_resp = any_resp or got
                empty = empty or (got and str(total) in ('0', ''))
                all_links += links
                pfx = 'article' if key_name == 'article' else 'oem'
                rec[f'{pfx}_ответ'] = 'да' if got else 'нет'
                rec[f'{pfx}_total'] = total
                rec[f'{pfx}_ссылок'] = len(links)
                rec[f'{pfx}_прошло'] = len(passed)
                log(f'   ключ {key_name}={code} ({"по артикулу" if key_name == "article" else "по названию"}): '
                    f'ответ {"да" if got else "НЕТ"}, total {total or "-"}, ссылок {len(links)}, '
                    f'отобрано {len(passed)}')
                if passed:
                    good, used_key, used_val = passed, key_name, code
                    break
                time.sleep(min(random.uniform(*PAUSE), max(0.0, deadline - time.time())))

            rec['выдача_получена'] = 'да' if any_resp else 'нет'
            rec['ключ_сработал'] = used_key or 'ни один'
            rec['значение_ключа'] = used_val
            stat['выдача_есть' if any_resp else 'выдачи_нет'] += 1

            # ---- открываем до пяти отобранных карточек ---------------------------
            dims_rows, opened, confirmed, got_dims = [], 0, 0, 0
            folder = RAW / safe(ext)
            for it in good[:OPEN_MAX]:
                if time.time() >= deadline:
                    log('   предел времени на модель — карточки дальше не открываю')
                    break
                try:
                    page.goto(it['адрес'], wait_until='domcontentloaded',
                              timeout=int(left(deadline) * 1000) or 1)
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
                if used_key == 'article':
                    row['подтверждение'] = confirm_in_card(card_html, used_val)
                    if not row['подтверждение']:
                        it['вердикт'] = 'артикул в карточке не подтверждён'
                        log(f'   {dst.name}: артикул {used_val} в карточке не найден — размер не беру')
                        time.sleep(min(random.uniform(*PAUSE), max(0.0, deadline - time.time())))
                        continue
                else:
                    row['подтверждение'] = f'код {used_val} в заголовке карточки'
                confirmed += 1
                if row['Д_мм']:
                    got_dims += 1
                dims_rows.append(row)
                time.sleep(min(random.uniform(*PAUSE), max(0.0, deadline - time.time())))

            rec['карточек_открыто'] = opened
            rec['подтверждено'] = confirmed
            rec['размеров_прочитано'] = got_dims
            stat['карточек'] += opened
            stat['подтверждено'] += confirmed
            stat['размеров'] += got_dims
            if not any_resp:
                итог = 'выдача не получена (ответ FastSearch/goods не пришёл)'
            elif not good and empty:
                итог = 'выдача получена, total=0 — на сайте по этим ключам ничего нет'
                stat['пусто_на_сайте'] += 1
            elif not good:
                итог = 'подходящих карточек в выдаче нет'
            elif not confirmed:
                итог = f'карточки открыты ({opened}), ни одна не подтверждена'
            elif not got_dims:
                итог = f'подтверждено {confirmed}, размеров в них нет'
            else:
                итог = f'размеров получено {got_dims}'
                stat['по_article' if used_key == 'article' else 'по_oem'] += 1
            if time.time() - started >= MODEL_MAX_S:
                итог = f'превышен предел времени на модель ({MODEL_MAX_S} с); {итог}'
                stat['предел_времени'] += 1
            close(итог)

            if all_links:
                append(LINKS, LINK_COLS, all_links)
            if dims_rows:
                append(DIMS, DIM_COLS, dims_rows)
            time.sleep(random.uniform(*PAUSE))

        ctx.close()
        browser.close()

    log('ИТОГ: ' + ', '.join(f'{k} {v}' for k, v in stat.items()))
    log(f'Пришлите файлы: {LOGCSV.name}, {LINKS.name}, {DIMS.name}, {PROBE.name}, '
        f'{LOG.name} и папку nix_raw (можно архивом).')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nПрервано. Собранное сохранено, повторный запуск продолжит с места остановки.')
