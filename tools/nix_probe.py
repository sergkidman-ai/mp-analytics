# поток: gab
"""Разведка nix.ru. Запускается ТОЛЬКО на домашнем компьютере (Windows).

Что делает: по первым 10 артикулам из nix_input.csv открывает главную nix.ru,
вводит артикул в поле поиска как человек, ждёт результаты и складывает материал:
  nix_network.csv — все сетевые обращения страницы во время поиска (адрес, метод,
                    тип ответа, размер). Главная цель: найти внутренний адрес,
                    который отдаёт список данными — тогда сбор пойдёт без браузера;
  nix_raw/        — отрисованный HTML страницы результатов и до 3 карточек;
  nix_found.csv   — артикул запроса, название из выдачи, адрес карточки, раздел.

Размеры здесь НЕ разбираются — только собирается материал.
Один поток, пауза 4-6 с между действиями, обычное окно браузера, никаких обходов
защиты. Прогон можно прервать и запустить снова: уже сохранённое не повторяется.

Установка:  pip install playwright  и  playwright install chromium
Запуск:     python nix_probe.py
"""
import csv
import random
import re
import sys
import time
from pathlib import Path

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
HOME = 'https://www.nix.ru/'
LIMIT_ARTICLES = 10
LIMIT_CARDS = 3
PAUSE = (4.0, 6.0)

SEARCH_BOX = ['input[name="searchStr"]', 'input[name="search"]', 'input[type="search"]',
              'input[placeholder*="оиск"]', '#search_input', 'input#searchStr']


def pause(why=''):
    t = random.uniform(*PAUSE)
    print(f'   пауза {t:.1f} с {why}')
    time.sleep(t)


def safe(s: str) -> str:
    return re.sub(r'[^0-9A-Za-zА-Яа-я_.-]', '_', s)[:60]


def append(path: Path, header, rows):
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
                out.append((a, r.get('external_code', '')))
            if len(out) >= LIMIT_ARTICLES:
                break
    return out


def done_articles() -> set:
    """Что уже сделано в прошлый раз — по файлу находок."""
    if not FOUND.exists():
        return set()
    with FOUND.open(encoding='utf-8-sig') as fh:
        return {r['артикул_запроса'] for r in csv.DictReader(fh, delimiter=';')}


def type_search(page, article: str) -> bool:
    for sel in SEARCH_BOX:
        box = page.query_selector(sel)
        if box:
            box.click()
            box.fill('')
            box.type(article, delay=120)      # набор посимвольно, как человек
            page.keyboard.press('Enter')
            return True
    return False


def main() -> int:
    articles = read_articles()
    skip = done_articles()
    RAW.mkdir(exist_ok=True)
    print(f'Артикулов в работе: {len(articles)}, из них уже собрано ранее: '
          f'{len([a for a, _ in articles if a in skip])}')

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
                size = resp.headers.get('content-length', '')
                ctype = resp.headers.get('content-type', '')
            except Exception:
                size, ctype = '', ''
            netlog.append([collecting['article'], resp.request.method, resp.status,
                           ctype, size, resp.url[:500]])

        page.on('response', on_response)

        for article, ext in articles:
            if article in skip:
                print(f'· {article} — уже собран, пропуск')
                continue
            print(f'\n=== {article} (модель {ext}) ===')
            try:
                page.goto(HOME, wait_until='domcontentloaded', timeout=60000)
            except Exception as e:
                print(f'   главная не открылась: {e}')
                continue
            pause('после главной')

            collecting.update(on=True, article=article)
            if not type_search(page, article):
                collecting['on'] = False
                print('   поле поиска не найдено — селектор изменился, '
                      'пришлите HTML главной из nix_raw')
                (RAW / f'home_{safe(article)}.html').write_text(page.content(), encoding='utf-8')
                continue
            try:
                page.wait_for_load_state('networkidle', timeout=45000)
            except Exception:
                pass
            pause('после поиска')
            collecting['on'] = False

            html = page.content()
            (RAW / f'search_{safe(article)}.html').write_text(html, encoding='utf-8')

            links, seen = [], set()
            for a in page.query_selector_all('a[href*="/autocatalog/"]'):
                href = a.get_attribute('href') or ''
                if not href or href in seen:
                    continue
                seen.add(href)
                url = href if href.startswith('http') else 'https://www.nix.ru' + href
                sect = url.split('/autocatalog/')[1].split('/')[0] if '/autocatalog/' in url else ''
                links.append([article, (a.inner_text() or '').strip()[:200], url, sect])

            append(FOUND, ['артикул_запроса', 'название_из_выдачи', 'адрес_карточки', 'раздел'],
                   links or [[article, 'НИЧЕГО НЕ НАЙДЕНО', '', '']])
            append(NET, ['артикул_запроса', 'метод', 'код_ответа', 'тип_ответа',
                         'размер', 'адрес'], netlog)
            netlog.clear()
            print(f'   ссылок на карточки: {len(links)}')

            for row in links[:LIMIT_CARDS]:
                url = row[2]
                dst = RAW / f'card_{safe(url.rsplit("/", 1)[-1])}.html'
                if dst.exists():
                    continue
                pause('перед карточкой')
                try:
                    page.goto(url, wait_until='domcontentloaded', timeout=60000)
                    dst.write_text(page.content(), encoding='utf-8')
                    print(f'   сохранена карточка {dst.name}')
                except Exception as e:
                    print(f'   карточка не открылась: {e}')
            pause('перед следующим артикулом')

        ctx.close()
        browser.close()
    print(f'\nГотово. Пришлите папку {RAW.name} и файлы {NET.name}, {FOUND.name}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nПрервано. Собранное сохранено, повторный запуск продолжит с места остановки.')
