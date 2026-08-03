# поток: gab
"""Шаг 10 — разбор записи формата внутреннего запроса nix.ru (пробник nix_format_probe.py).

Читает docs/rebuild/data/nix_fmt/: nix_requests.jsonl (журнал запросов с телами),
nix_fastsearch_*.json (ответы FastSearch/goods), nix_cookies.json (только имена cookie).
Отвечает на: какие поля тела меняются, что постоянно, есть ли подпись/сессия,
есть ли в ответе ссылки на карточки и названия, есть ли габариты.

Ничего не пересчитывает и не додумывает — только читает записанное.
Пишет docs/rebuild/data/nix_fastsearch_summary.csv, в консоль — сводку.
"""
import csv
import json
import re
import sys
import urllib.parse as up
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from step3_bridge import DATA                                    # noqa: E402

FMT = DATA / 'nix_fmt'
GOODS = 'FastSearch/goods'
HREF = re.compile(r'href=[\'"]([^\'"]*/autocatalog/[^\'"]+)')      # в вёрстке никса кавычки обоих видов
DIM_WORDS = ('Размеры упаковки', 'Габариты', 'Вес брутто')


def main() -> int:
    rows = [json.loads(l) for l in (FMT / 'nix_requests.jsonl').open(encoding='utf-8')]
    g = [r for r in rows if GOODS in r['url']]
    bodies = [dict(up.parse_qsl(r['post_data'] or '', keep_blank_values=True)) for r in g]
    keys = set().union(*[set(b) for b in bodies])
    var = sorted(k for k in keys if len({b.get(k) for b in bodies}) > 1)
    const = sorted(keys - set(var))

    print(f'журнал: {len(rows)} запросов, артикулов {len({r["article"] for r in rows})}, '
          f'с телом {sum(1 for r in rows if r["post_data"])}, вызовов goods {len(g)}')
    print(f'ответов не 2xx: {sum(1 for r in rows if r["status"] >= 300)}')
    print(f'тело goods: полей {len(keys)} | МЕНЯЮТСЯ {var} | постоянных {len(const)}')
    print('  ps_id =', bodies[0].get('ps_id'), '| sign у каждого свой:',
          len({b.get("sign") for b in bodies}) == len({b.get("keywords") for b in bodies}))
    ck = json.loads((FMT / 'nix_cookies.json').read_text(encoding='utf-8'))
    print('  cookie:', ', '.join(c['name'] for c in ck))

    out = []
    for p in sorted(FMT.glob('nix_fastsearch_*.json')):
        d = json.loads(p.read_text(encoding='utf-8'))['goods']
        h = d['html']
        cards = {u for u in HREF.findall(h)}
        dims = sum(h.count(w) for w in DIM_WORDS)
        out.append([p.name, p.stat().st_size, d.get('total'), len(d.get('navigation') or {}),
                    len(cards), len(re.findall(r'data-page=', d.get('pages') or '')), dims])
        print(f'  {p.name:<32} байт {p.stat().st_size:>7} | total {str(d.get("total")):>4} | '
              f'карточек {len(cards):>3} | страниц {len(re.findall(r"data-page=", d.get("pages") or ""))} | '
              f'габаритов {dims}')

    with (DATA / 'nix_fastsearch_summary.csv').open('w', encoding='utf-8', newline='') as fh:
        w = csv.writer(fh, delimiter=';')
        w.writerow(['файл', 'байт', 'total', 'navigation', 'ссылок_autocatalog', 'страниц', 'слов_о_габаритах'])
        w.writerows(out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
