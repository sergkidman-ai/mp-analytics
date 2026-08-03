# поток: gab
"""Разведка nix.ru. Запускается ТОЛЬКО на домашнем компьютере (Windows).

Страница выдачи открывается ПРЯМЫМ адресом — поле поиска не трогаем (на главной
несколько полей, часть невидима). Часть адреса после # обрабатывает сам браузер,
поэтому Playwright её отрабатывает, а обычный загрузчик страниц — нет.
При смене артикула меняется только хвост после #, и браузер сам страницу не
перезагружает: поэтому между артикулами уходим на about:blank, открываем новый
адрес и делаем reload.

Собирается:
  nix_network.csv — все сетевые обращения во время загрузки выдачи (артикул, метод,
                    код, тип, размер, адрес). Цель — найти внутренний адрес, который
                    отдаёт список данными: тогда сбор пойдёт без браузера;
  nix_raw/        — HTML выдачи и до 3 карточек по каждому артикулу;
  nix_found.csv   — артикул запроса, название из выдачи, адрес карточки, раздел;
  nix_probe.log   — ход работы и ошибки.

Размеры здесь НЕ разбираются — только собирается материал. Один поток, пауза 4-6 с,
обычное окно браузера, никаких обходов защиты. Ошибка на одном артикуле не роняет
прогон. Прогон можно прервать и запустить снова: собранное не повторяется.

Установка:  pip install playwright  и  playwright install chromium
Запуск:     python nix_probe.py
"""
import csv
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit('Не установлен playwright. Выполните: pip install playwright '
             'и затем playwright install chromium')

HERE = Path(__file__).resolve().parent
INPUT = HERE / 'nix_input.csv'          # положить рядом со скриптом
RAW = HERE / 'nix_raw'
NET = HERE / 'nix_network.csv'
FOUND = HERE / 'nix_found.csv'
LOG = HERE / 'nix_probe.log'

SEARCH_URL = ('https://www.nix.ru/price/price_list.html'
              '?section=cartridges_toner_paper_ink_all'
              '#c_id=110&fn=110&g_id=59&keywords={kw}&new_goods=0&page=1&sort=0'
              '&spoiler=&store=msk-0_1721_1&thumbnail_view=2')
CARD_LINK = 'a[href*="/autocatalog/"]'
LIMIT_ARTICLES = 10
LIMIT_CARDS = 3
WAIT_RESULTS_MS = 30000
PAUSE = (4.0, 6.0)


def log(msg: str):
    line = f'{datetime.now():%H:%M:%S} {msg}'
    print(line)
    with LOG.open('a', encoding='utf-8') as fh:
        fh.write(line + '\n')


def pause(why=''):
    t = random.uniform(*PAUSE)
    time.sleep(t)


def safe(s: str) -> str:
    return re.sub(r'[^0-9A-Za-zА-Яа-я_.-]', '_', s)[:60]


def append(path: Path, header, rows):
    if not rows:
        return
    new = not path.exists()
    with path.open('a', encoding='utf-8-sig', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        if new:
            w.writerow(header)
        w.writerows(rows)


def read_articles():
    if not INPUT.exists():
        sys.exit(f'Нет файла {INPUT}. Положите nix_input.csv рядом со скриптом.')
    out = []
    with INPUT.open(encoding='utf-8-sig') as fh:
        for r in csv.DictReader(fh, delimiter=';'):
            a = (r.get('oem_article') or '').strip()
            if a:
                out.append((a, (r.get('external_code') or '').strip()))
            if len(out) >= LIMIT_ARTICLES:
                break
    return out


def done_articles() -> set:
    """Что уже сделано в прошлый раз — по файлу находок."""
    if not FOUND.exists():
        return set()
    with FOUND.open(encoding='utf-8-sig') as fh:
        return {r['артикул_запроса'] for r in csv.DictReader(fh, delimiter=';')}


def open_results(page, article: str):
    """Открыть выдачу прямым адресом и дождаться ссылок на карточки."""
    page.goto('about:blank', wait_until='domcontentloaded', timeout=30000)
    page.goto(SEARCH_URL.format(kw=quote(article)), wait_until='domcontentloaded', timeout=60000)
    page.reload(wait_until='domcontentloaded', timeout=60000)   # хвост после # сам не применяется
    page.wait_for_selector(CARD_LINK, timeout=WAIT_RESULTS_MS)


def collect_links(page, article: str):
    links, seen = [], set()
    for a in page.query_selector_all(CARD_LINK):
        href = a.get_attribute('href') or ''
        if not href or href in seen:
            continue
        seen.add(href)
        url = href if href.startswith('http') else 'https://www.nix.ru' + href
        sect = url.split('/autocatalog/')[1].split('/')[0] if '/autocatalog/' in url else ''
        links.append([article, (a.inner_text() or '').strip()[:200], url, sect])
    return links


def main() -> int:
    articles = read_articles()
    skip = done_articles()
    RAW.mkdir(exist_ok=True)
    log(f'Артикулов в работе: {len(articles)}, собрано ранее: '
        f'{len([a for a, _ in articles if a in skip])}')
    stat = {'обработано': 0, 'найдено': 0, 'пусто': 0, 'ошибок': 0}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(locale='ru-RU', viewport={'width': 1360, 'height': 900})
        page = ctx.new_page()

        netlog = []
        collecting = {'on': False, 'article': ''}

        def on_response(resp):
            if not collecting['on']:
                return
            try:
                h = resp.headers
                netlog.append([collecting['article'], resp.request.method, resp.status,
                               h.get('content-type', ''), h.get('content-length', ''),
                               resp.url[:500]])
            except Exception:
                pass

        page.on('response', on_response)

        for article, ext in articles:
            if article in skip:
                log(f'· {article} — уже собран, пропуск')
                continue
            log(f'=== {article} (модель {ext})')
            stat['обработано'] += 1
            netlog.clear()
            collecting.update(on=True, article=article)
            links = []
            try:
                open_results(page, article)
                links = collect_links(page, article)
            except Exception as e:
                first = str(e).splitlines()[0][:160]
                try:                                   # выдача могла отрисоваться частично
                    links = collect_links(page, article)
                except Exception:
                    links = []
                if not links:
                    stat['ошибок'] += 1
                    log(f'   ошибка: {first} — иду дальше')
            finally:
                collecting['on'] = False
                append(NET, ['артикул_запроса', 'метод', 'код_ответа', 'тип_ответа',
                             'размер', 'адрес'], netlog)

            try:
                (RAW / f'search_{safe(article)}.html').write_text(page.content(), encoding='utf-8')
            except Exception as e:
                log(f'   HTML выдачи не сохранён: {str(e).splitlines()[0][:120]}')

            if links:
                stat['найдено'] += 1
            else:
                stat['пусто'] += 1
                log('   ничего не найдено')
            append(FOUND, ['артикул_запроса', 'название_из_выдачи', 'адрес_карточки', 'раздел'],
                   links or [[article, 'НИЧЕГО НЕ НАЙДЕНО', '', '']])
            log(f'   ссылок на карточки: {len(links)}')

            for row in links[:LIMIT_CARDS]:
                url = row[2]
                dst = RAW / f'card_{safe(url.rsplit("/", 1)[-1])}.html'
                if dst.exists():
                    continue
                pause()
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=60000)
                    dst.write_text(page.content(), encoding='utf-8')
                    log(f'   сохранена карточка {dst.name}')
                except Exception as e:
                    log(f'   карточка не открылась: {str(e).splitlines()[0][:120]}')
            pause()

        ctx.close()
        browser.close()

    log('ИТОГ: ' + ', '.join(f'{k} {v}' for k, v in stat.items()))
    log(f'Пришлите папку {RAW.name} и файлы {NET.name}, {FOUND.name}, {LOG.name}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nПрервано. Собранное сохранено, повторный запуск продолжит с места остановки.')
