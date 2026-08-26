# поток: prc
# -*- coding: utf-8 -*-
"""
Наименование товара Булата по артикулу — с публичного сайта bulat-group.ru.

Зачем. Прайс Булата к нам не приходит: его грузит внешний загрузчик, а нам письмом достаётся
только список несопоставленного, где от товара есть ОДИН артикул вида `TC-XRX-DC240-K-GRFT`.
Из него читаются тип, вендор, модель и цвет — четыре признака из шести; ресурс и чип не
читаются никак, а без них `catalog.compare` сравнивать нечего. Сайт по тому же артикулу отдаёт
полное наименование: «Тонер-картридж для Xerox DocuColor 240/250/242/252/260, 006R01449,
Black, 30K, Grafit» — здесь и OEM-код (ключ связи с ТК), и цвет, и ресурс, и бренд.

Сайт — OpenCart, публичный, логина и оплаты не требует (проверено 23.07.2026 в потоке
габаритов). Ходим вежливо: пауза между запросами, один User-Agent, найденное сразу в кэш
`prc_supplier_item` — письма приходят несколько раз в день, а состав артикулов почти не
меняется.
"""
import re
import time
import urllib.parse
import urllib.request

from core import db

SUPPLIER_KEY = "bulat"
SOURCE = "bulat-group.ru"
SEARCH_URL = "https://bulat-group.ru/index.php?route=product/search&search=%s"
UA = "Mozilla/5.0 (compatible; mp-analytics/1.0)"
TIMEOUT = 30
PAUSE_SEC = 1.0

# Карточка в выдаче: имя и артикул лежат в соседних блоках одного `product-item`.
ITEM_RE = re.compile(r'class="product-item"')
NAME_RE = re.compile(r'class="product-item__name">\s*(.+?)\s*</div>', re.S)
ART_RE = re.compile(r'Артикул:\s*</span>\s*<span>\s*(\S+?)\s*</span>', re.S)


def cached(articles):
    """Что по этим артикулам уже лежит в кэше. -> {артикул: имя}."""
    if not articles:
        return {}
    rows = db.query("SELECT article, name FROM prc_supplier_item"
                    " WHERE supplier_key = %s AND article = ANY(%s)",
                    (SUPPLIER_KEY, list(articles)))
    return {r["article"]: r["name"] for r in rows}


def fetch_one(article):
    """Наименование с сайта по ТОЧНОМУ артикулу или None.

    Берём только выдачу ровно из одной карточки: ноль — товара на сайте нет, несколько —
    поиск ушёл в однокоренные артикулы, и угадывать, который наш, нельзя. Такая строка
    поедет дальше без имени и будет видна в сводке отдельно, а не притворится сопоставленной.
    """
    url = SEARCH_URL % urllib.parse.quote(article)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            html = resp.read().decode("utf-8", "replace")
    except Exception:
        # Сетевая ошибка на одном артикуле не должна ронять разбор всего письма.
        return None
    if len(ITEM_RE.findall(html)) != 1:
        return None
    name = NAME_RE.search(html)
    if not name:
        return None
    got = ART_RE.search(html)
    if got and got.group(1).upper() != article.upper():
        return None                       # выдача не про наш артикул — не берём
    return re.sub(r"\s+", " ", name.group(1)).strip() or None


def resolve(articles, pause=PAUSE_SEC, progress=None):
    """{артикул: имя} — из кэша, недостающее с сайта. Ненайденных в словаре просто нет."""
    articles = list(dict.fromkeys(articles))
    out = cached(articles)
    todo = [a for a in articles if a not in out]
    fresh = []
    for i, art in enumerate(todo):
        name = fetch_one(art)
        if name:
            out[art] = name
            fresh.append({"supplier_key": SUPPLIER_KEY, "article": art,
                          "name": name, "source": SOURCE})
        if progress and (i + 1) % 20 == 0:
            progress(i + 1, len(todo))
        if pause and i + 1 < len(todo):
            time.sleep(pause)
    if fresh:
        db.upsert("prc_supplier_item", fresh, ["supplier_key", "article"], ["name", "source"])
    return out
