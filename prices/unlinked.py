# поток: prc
# -*- coding: utf-8 -*-
"""Несопоставленные карточки: лежат в оприходовании, но без нашего внешнего кода.

Внешний код (4 цифры) — ключ, по которому остаток уезжает из МойСклада дальше, в ТК.
Карточка, заведённая мимо кода (в поле стоит автогенерация МС), держит остаток поставщика
внутри МС и в продажу его не отдаёт. Задача вкладки — свести такую карточку с НАШЕЙ уже
существующей карточкой того же товара (решение Сергея 17.08.2026: новых кодов не выдавать).

Остаток берём из позиций АКТУАЛЬНОГО оприходования, а не из прайса: у Булата, ВТТ, Рамис и
Блоссома прайсы к нам не приходят вовсе — их грузит внешний загрузчик, и единственный след
остатка — сам документ в МС.

    ./venv/bin/python -m prices.unlinked            # собрать и положить в БД
    ./venv/bin/python -m prices.unlinked --dry      # только посчитать, в БД не писать
    ./venv/bin/python -m prices.unlinked --samples 15   # выгрузить примеры с кандидатами
"""
import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path

from core import db, ms_api
from prices import catalog, features as F
from prices.profiles import PROFILES, STORE_REMOTE, get_profile

ROOT = Path(__file__).resolve().parents[1]

# Имя оприходования: `<группа>_<ГГГГ-ММ-ДД>_p<N>`. У группы внутри бывают подчёркивания
# (`s_print_msk`), поэтому дату и номер страницы отрезаем с хвоста, а не режем по первому `_`.
DOC_RE = re.compile(r"^(?P<key>.+)_(?P<date>\d{4}-\d{2}-\d{2})(?:_p\d+)?(?:_old\d+)?$")
OUR_CODE_RE = re.compile(r"^\d{4}$")
BATCH = 40                       # повтор поля в filter МС трактуется как ИЛИ; больше — 414


def enters(store=STORE_REMOTE):
    """Оприходования на складе. Фильтр МС перепроверяем сами: имена соседних складов похожи."""
    rows = ms_api.get("/entity/enter", {
        "limit": 1000, "filter": [f"store={ms_api.BASE}/entity/store/{store}"],
    }).get("rows", [])
    return [d for d in rows if ms_api.meta_id(d, "store") == store]


def current(docs):
    """Только АКТУАЛЬНЫЕ документы каждой группы — последняя дата.

    Загрузчик заменяет документы целиком (пометить `_old`, создать новые, удалить старые),
    но `_old` живёт минуты, а бывают и залежавшиеся. Вчерашний остаток — не остаток.
    """
    parsed = []
    for doc in docs:
        m = DOC_RE.match(doc.get("name", ""))
        if not m or "_old" in doc.get("name", ""):
            continue                      # ручные номерные документы сюда не относятся
        parsed.append((m.group("key"), m.group("date"), doc))
    last = defaultdict(str)
    for key, date, _ in parsed:
        last[key] = max(last[key], date)
    return [(key, date, doc) for key, date, doc in parsed if date == last[key]]


def positions(doc):
    """Позиции документа отдельным запросом: `expand=positions` в списке отдаёт только meta."""
    got = ms_api.get(f"/entity/enter/{doc['id']}/positions", {"limit": 1000})
    rows = got.get("rows", [])
    size = got.get("meta", {}).get("size")
    if size is not None and size != len(rows):
        raise RuntimeError(f"{doc['name']}: позиций {len(rows)} из {size} — страница потерялась")
    return rows


def cards(ids):
    """{id -> карточка}. Архивные `/entity/assortment` МОЛЧА не отдаёт — добираем поштучно."""
    out, ids = {}, list(ids)
    for i in range(0, len(ids), BATCH):
        chunk = ids[i:i + BATCH]
        for row in ms_api.get("/entity/assortment", {
                "limit": 1000, "filter": [f"id={x}" for x in chunk]}).get("rows", []):
            out[row["id"]] = row
        for miss in (x for x in chunk if x not in out):
            try:
                out[miss] = ms_api.get(f"/entity/product/{miss}")
            except Exception:
                continue                  # удалённая карточка — не находится нигде
    return out


def collect(store=STORE_REMOTE):
    """Безкодовые карточки актуальных оприходований с положительным остатком."""
    acc = {}
    for key, date, doc in current(enters(store)):
        for pos in positions(doc):
            href = pos.get("assortment", {}).get("meta", {}).get("href", "")
            ms_id = href.rsplit("/", 1)[-1].split("?")[0]
            qty = float(pos.get("quantity") or 0)
            if qty <= 0:
                continue
            row = acc.setdefault(ms_id, {"ms_id": ms_id, "supplier_key": key, "qty": 0.0,
                                         "docs": 0, "doc_name": doc["name"], "doc_date": date})
            row["qty"] += qty
            row["docs"] += 1
    found = cards(acc)
    out = []
    for ms_id, row in acc.items():
        card = found.get(ms_id)
        if card is None:
            continue
        ext = (card.get("externalCode") or "").strip()
        if OUR_CODE_RE.match(ext):
            continue                      # наш 4-значный код — карточка сопоставлена
        out.append({**row,
                    "ms_code": (card.get("code") or "").strip() or None,
                    "article": (card.get("article") or "").strip() or None,
                    "name": card.get("name") or "",
                    "ext_raw": ext or None,
                    "archived": bool(card.get("archived")),
                    "category": card.get("pathName") or None,
                    "store": store})
    return out


def suggest(rows):
    """Подсказки из нашего каталога тем же матчингом, что у новинок.

    Свою же карточку и карточки без 4-значного кода из кандидатов убираем: первая — это сама
    строка, вторые той же болезнью больны и проблему не закрывают.
    """
    tc_all = catalog.load_tc()
    cat = catalog.load_catalog(tc_all)
    by_id = {item["ms_id"]: item for item in cat}
    index = catalog.build_index(cat)
    art_index = catalog.build_article_index(cat)
    chips, arts = {}, {}
    for key in PROFILES:
        try:
            profile = get_profile(key)
        except Exception:
            continue
        chips[key], arts[key] = profile.default_chip, profile.article_re
    out = {}
    for src in rows:
        row = {"name": src["name"], "article": src["article"] or "", "price": None}
        row["kind"] = catalog.kind(row["name"])
        row.update(F.parse(row["name"], row["article"]))
        if row["chip"] is None:
            row["chip"] = chips.get(src["supplier_key"], "chip")
        hits = (catalog.by_article(row, art_index, arts.get(src["supplier_key"]))
                + catalog.match(row, index, by_id))
        seen, shown = set(), []
        for hit in hits:
            item = hit["item"]
            code = (item.get("external_code") or "").strip()
            if item["ms_id"] == src["ms_id"] or not OUR_CODE_RE.match(code) or code in seen:
                continue
            seen.add(code)
            shown.append(hit)
        out[src["ms_id"]] = shown[:8]
    return out


def save(rows, hits):
    """Строки и подсказки — в БД. Решение человека не трогаем: строка приходит каждый прогон."""
    for row in rows:
        db.execute("""
            INSERT INTO prc_unlinked (ms_id, ms_code, article, name, supplier_key, ext_raw,
                                      archived, category, qty, docs, doc_name, doc_date, store)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (ms_id) DO UPDATE
               SET ms_code = excluded.ms_code, article = excluded.article,
                   name = excluded.name, supplier_key = excluded.supplier_key,
                   ext_raw = excluded.ext_raw, archived = excluded.archived,
                   category = excluded.category, qty = excluded.qty, docs = excluded.docs,
                   doc_name = excluded.doc_name, doc_date = excluded.doc_date,
                   store = excluded.store, last_seen = now()
        """, (row["ms_id"], row["ms_code"], row["article"], row["name"], row["supplier_key"],
              row["ext_raw"], row["archived"], row["category"], row["qty"], row["docs"],
              row["doc_name"], row["doc_date"], row["store"]))
        db.execute("DELETE FROM prc_unlinked_candidate WHERE ms_id = %s", (row["ms_id"],))
        for rank, hit in enumerate(hits.get(row["ms_id"], ()), start=1):
            item = hit["item"]
            db.execute("""
                INSERT INTO prc_unlinked_candidate
                    (ms_id, rank, cand_ms_id, cand_code, external_code, cand_name, color,
                     measure, chip, brand, kind, shared_code, verdict, model_ok, kind_ok,
                     brand_ok, color_ok, resource_ok, chip_ok, score, by_article, feat_src)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s)
            """, (row["ms_id"], rank, item["ms_id"], item["code"] or None,
                  item.get("external_code") or None, item["name"], item["color"],
                  catalog.measure(item), item["chip"], F.brand_text(item["brand"]), item["kind"],
                  hit["code"], catalog.verdict(hit), hit["model_ok"], hit["kind_ok"],
                  hit["brand_ok"], hit["color_ok"], hit["resource_ok"], hit["chip_ok"],
                  hit["score"], bool(hit.get("by_article")),
                  ",".join(item.get("feat_src") or ()) or None))


def report(rows, hits, path):
    """Отчёт человеку: одна строка — карточка, до трёх лучших вариантов рядом."""
    with Path(path).open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["поставщик", "артикул", "наименование", "штук", "документ", "категория",
                    "код МС", "внешний код (мусор)", "вариантов",
                    "вариант 1 код", "вариант 1 наименование", "вариант 1 вердикт",
                    "вариант 2 код", "вариант 2 наименование",
                    "вариант 3 код", "вариант 3 наименование"])
        for row in sorted(rows, key=lambda r: (r["supplier_key"], r["article"] or "")):
            got = hits.get(row["ms_id"], ())
            cells = []
            for hit in got[:3]:
                item = hit["item"]
                cells += [item.get("external_code"), item["name"]]
                if len(cells) == 2:
                    cells.append(catalog.verdict(hit))
            cells += [""] * (8 - len(cells))
            w.writerow([row["supplier_key"], row["article"], row["name"], row["qty"],
                        row["doc_name"], row["category"] or "", row["ms_code"] or "",
                        row["ext_raw"] or "", len(got)] + cells)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Несопоставленные карточки в оприходованиях")
    ap.add_argument("--dry", action="store_true", help="не писать в БД")
    ap.add_argument("--out", default="docs/reports/prc_unlinked.csv")
    args = ap.parse_args(argv)

    rows = collect()
    hits = suggest(rows)
    by_sup = defaultdict(int)
    for row in rows:
        by_sup[row["supplier_key"]] += 1
    withhits = sum(1 for r in rows if hits.get(r["ms_id"]))
    strong = sum(1 for r in rows if (hits.get(r["ms_id"]) or [{}])[0].get("by_article"))
    if not args.dry:
        save(rows, hits)
    report(rows, hits, ROOT / args.out)
    print(f"карточек без внешнего кода в актуальных оприходованиях: {len(rows)}, "
          f"штук {sum(r['qty'] for r in rows):.0f}")
    print("по поставщикам: " + ", ".join(f"{k} {v}" for k, v in
                                         sorted(by_sup.items(), key=lambda x: -x[1])))
    print(f"есть варианты у {withhits}, из них по совпавшему АРТИКУЛУ — {strong}; "
          f"без вариантов {len(rows) - withhits}")
    print(f"архивных среди них {sum(1 for r in rows if r['archived'])}; отчёт → {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
