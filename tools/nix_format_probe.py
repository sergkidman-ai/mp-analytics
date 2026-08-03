# поток: gab
"""Разведка ФОРМАТА внутреннего запроса nix.ru. Запуск ТОЛЬКО на домашнем компьютере.

Прошлый пробник (nix_probe.py) записывал лишь адрес, метод, код и размер ответа. Стало
известно, что список товаров отдаёт POST /scripts/action.php/FastSearch/goods (JSON),
но НЕ известно, что в него посылать: параметры лежат в теле POST, а тело не писалось.
Этот пробник закрывает ровно эту дыру — пишет тела запросов целиком.

Собирается (в папке рядом со скриптом):
  nix_requests.jsonl          — по строке на каждый запрос страницы: артикул, метод, адрес,
                                ПОЛНЫЕ заголовки запроса, ПОЛНОЕ тело (request.post_data),
                                код ответа, тип содержимого;
  nix_fastsearch_<арт>.json   — тело ответа FastSearch/goods целиком, как есть, без разбора;
  nix_cookies.json            — ТОЛЬКО имена cookie и домены (значения не сохраняются) —
                                чтобы понять, нужна ли сессия из браузера;
  nix_format_probe.log        — ход работы и ошибки.

Ничего не парсится и не пересчитывается — только сбор. Один поток, пауза 5-8 с,
обычное окно браузера, обычный User-Agent, никаких обходов защиты. Ошибка на одном
артикуле не роняет прогон. Повторный запуск продолжает с места остановки.

Установка:  pip install playwright  и  playwright install chromium
Запуск:     python nix_format_probe.py
"""
import csv
import json
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
REQ = HERE / 'nix_requests.jsonl'
COOKIES = HERE / 'nix_cookies.json'
LOG = HERE / 'nix_format_probe.log'

SEARCH_URL = ('https://www.nix.ru/price/price_list.html'
              '?section=cartridges_toner_paper_ink_all'
              '#c_id=110&fn=110&g_id=59&keywords={kw}&new_goods=0&page=1&sort=0'
              '&spoiler=&store=msk-0_1721_1&thumbnail_view=2')
CARD_LINK = 'a[href*="/autocatalog/"]'
GOODS = 'FastSearch/goods'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36')
LIMIT_ARTICLES = 5
WAIT_RESULTS_MS = 30000
PAUSE = (5.0, 8.0)


def log(msg: str):
    line = f'{datetime.now():%H:%M:%S} {msg}'
    print(line)
    with LOG.open('a', encoding='utf-8') as fh:
        fh.write(line + '\n')


def safe(s: str) -> str:
    return re.sub(r'[^0-9A-Za-z_.-]', '_', s)[:40]


def read_articles():
    """Первые 5 строк nix_input.csv с непустым oem_article — читаем из файла,
    руками ничего не набираем."""
    if not INPUT.exists():
        sys.exit(f'Нет файла {INPUT}. Положите nix_input.csv рядом со скриптом.')
    out, seen = [], set()
    with INPUT.open(encoding='utf-8-sig') as fh:
        for r in csv.DictReader(fh, delimiter=';'):
            a = (r.get('oem_article') or '').strip()
            if not a or a in seen:
                continue
            seen.add(a)
            out.append((a, (r.get('external_code') or '').strip()))
            if len(out) >= LIMIT_ARTICLES:
                break
    return out


def done_articles() -> set:
    """Что уже записано в прошлый раз — по журналу запросов."""
    if not REQ.exists():
        return set()
    done = set()
    with REQ.open(encoding='utf-8') as fh:
        for line in fh:
            try:
                done.add(json.loads(line)['article'])
            except Exception:
                pass
    return done


def open_results(page, article: str):
    """Открыть выдачу прямым адресом. Смена хвоста после # страницу не перезагружает,
    поэтому сначала about:blank, затем адрес, затем reload."""
    page.goto('about:blank', wait_until='domcontentloaded', timeout=30000)
    page.goto(SEARCH_URL.format(kw=quote(article)), wait_until='domcontentloaded', timeout=60000)
    page.reload(wait_until='domcontentloaded', timeout=60000)
    page.wait_for_selector(CARD_LINK, timeout=WAIT_RESULTS_MS)


def main() -> int:
    articles = read_articles()
    skip = done_articles()
    log(f'Артикулов в работе: {len(articles)} ({", ".join(a for a, _ in articles)}); '
        f'собрано ранее: {len([a for a, _ in articles if a in skip])}')
    stat = {'обработано': 0, 'записей': 0, 'тел_запросов': 0, 'ответов_goods': 0, 'ошибок': 0}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(locale='ru-RU', user_agent=UA,
                                  viewport={'width': 1360, 'height': 900})
        page = ctx.new_page()

        rows = []                       # журнал запросов текущего артикула
        goods = []                      # объекты ответов FastSearch/goods
        state = {'on': False, 'article': ''}

        def on_response(resp):
            """В обработчике НЕ читаем тело ответа: в синхронном Playwright это ведёт
            к ошибке протокола. Тело читаем позже, пока страница жива."""
            if not state['on']:
                return
            try:
                req = resp.request
                try:
                    headers = dict(req.headers)
                except Exception:
                    headers = {}
                body = None
                try:
                    body = req.post_data          # ради этого всё и делается
                except Exception:
                    body = None
                rows.append({
                    'article': state['article'],
                    'method': req.method,
                    'url': resp.url,
                    'resource_type': req.resource_type,
                    'request_headers': headers,
                    'post_data': body,
                    'status': resp.status,
                    'content_type': resp.headers.get('content-type', ''),
                })
                if GOODS in resp.url:
                    goods.append(resp)
            except Exception:
                pass

        page.on('response', on_response)

        for article, ext in articles:
            if article in skip:
                log(f'· {article} — уже собран, пропуск')
                continue
            log(f'=== {article} (модель {ext})')
            stat['обработано'] += 1
            rows.clear()
            goods.clear()
            state.update(on=True, article=article)
            try:
                open_results(page, article)
            except Exception as e:
                stat['ошибок'] += 1
                log(f'   ошибка загрузки выдачи: {str(e).splitlines()[0][:160]} — иду дальше')
            time.sleep(2)                        # дать долететь запоздавшим ответам
            state['on'] = False

            # тела ответов FastSearch/goods — как есть, без разбора
            for i, resp in enumerate(goods):
                dst = HERE / (f'nix_fastsearch_{safe(article)}.json' if i == 0
                              else f'nix_fastsearch_{safe(article)}_{i}.json')
                try:
                    dst.write_text(resp.text(), encoding='utf-8')
                    stat['ответов_goods'] += 1
                    log(f'   сохранён ответ {dst.name}')
                except Exception as e:
                    log(f'   тело ответа не прочитано: {str(e).splitlines()[0][:120]}')

            with REQ.open('a', encoding='utf-8') as fh:
                for r in rows:
                    fh.write(json.dumps(r, ensure_ascii=False) + '\n')
            stat['записей'] += len(rows)
            stat['тел_запросов'] += sum(1 for r in rows if r['post_data'])
            log(f'   запросов записано: {len(rows)}, из них с телом: '
                f'{sum(1 for r in rows if r["post_data"])}')
            time.sleep(random.uniform(*PAUSE))

        # только имена cookie и домены — значения НЕ сохраняем
        try:
            names = [{'name': c.get('name'), 'domain': c.get('domain'),
                      'есть_значение': bool(c.get('value'))} for c in ctx.cookies()]
            COOKIES.write_text(json.dumps(names, ensure_ascii=False, indent=1), encoding='utf-8')
            log(f'   cookie: {len(names)} шт., сохранены только имена')
        except Exception as e:
            log(f'   cookie не сохранены: {str(e).splitlines()[0][:120]}')

        ctx.close()
        browser.close()

    log('ИТОГ: ' + ', '.join(f'{k} {v}' for k, v in stat.items()))
    log(f'Пришлите файлы {REQ.name}, nix_fastsearch_*.json, {COOKIES.name}, {LOG.name}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('\nПрервано. Собранное сохранено, повторный запуск продолжит с места остановки.')
