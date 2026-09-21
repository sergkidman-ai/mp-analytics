# поток: prc
# -*- coding: utf-8 -*-
"""Блоссом: прайс без цен — новое оприходование из свежих остатков и СТАРЫХ цен.

Блоссом прислал (через дропбокс-бота) остатки без цен: в прайсе колонка «Дил.цена» пустая
во всех строках. Профиля прайса у него нет — его документы кладёт внешний загрузчик, а он
молчит с 14.07.2026 (поставщик не отгружал). Поэтому разовый инструмент: остаток берём из
файла, цену — из текущего оприходования Блоссома, замена документов как у загрузчика
(пометить `_old` → создать новые → удалить старые).

Решения Сергея 21.09.2026:
- цена из карточки МС (закупочная) берётся ТОЛЬКО там, где в новом прайсе есть остаток,
  и только если позиции нет в текущем оприходовании;
- позиция без цены и там и там — пропуск (приходовать по нулю нельзя);
- артикул без карточки в МС — в новинки НЕ заводим, ждём цену; список в отчёт.

Цены остаются июльскими: прайс Блоссома в USD по ЦБ, но цен он не прислал, а в документе
и карточках лежат рубли по курсу на 14.07.2026. Курс с тех пор ушёл — знать об этом надо.

    ./venv/bin/python -m tools.prc.blossom_stock_only            # сухой прогон: отчёт + сводка
    ./venv/bin/python -m tools.prc.blossom_stock_only --apply    # запись в МойСклад
"""
import argparse
import csv
import sys
from pathlib import Path

import pandas as pd

from core import ms_api
from prices import loader, unlinked
from prices.loader import now_msk
from prices.profiles import ORG_DIGITAL, POSITIONS_PER_DOC, STORE_REMOTE

ROOT = Path(__file__).resolve().parents[2]
DROPBOX = Path("/opt/mp-analytics/dropbox")   # общий чекаут: бот кладёт файлы только туда
REPORT_DIR = ROOT / "reports" / "data"
KEY = "blossom"
HEADER_ROW = 5                      # строки 1–5 — шапка поставщика, заголовки в шестой
COL_ART, COL_NAME, COL_QTY = "Артикул", "Наименование", "Свободный остаток"


def latest_file():
    """Свежий файл прайса из дропбокса: бот кладёт их как `<дата>_<кто>_PRC_<номер>.xls`."""
    files = sorted(DROPBOX.glob("*PRC_*.xls*"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise SystemExit(f"в {DROPBOX} нет файлов *PRC_*.xls")
    return files[-1]


def read_stock(path):
    """{артикул -> остаток}. Нулевые и пустые остатки не берём: это не позиция документа."""
    df = pd.ExcelFile(path).parse(0, header=HEADER_ROW)
    df = df[df[COL_ART].notna()]
    out, names = {}, {}
    for row in df.itertuples(index=False):
        rec = dict(zip(df.columns, row))
        art = str(rec[COL_ART]).strip()
        qty = pd.to_numeric(str(rec[COL_QTY]).replace(",", "."), errors="coerce")
        if art and qty and qty > 0:
            out[art] = float(qty)
            names[art] = str(rec.get(COL_NAME) or "").strip()
    return out, names


def current_prices():
    """({id карточки -> цена в копейках}, [документы Блоссома на «Удаленном»])."""
    docs = [doc for key, _date, doc in unlinked.current(unlinked.enters(STORE_REMOTE))
            if key == KEY]
    price = {}
    for doc in docs:
        for pos in unlinked.positions(doc):
            price[ms_api.meta_id(pos, "assortment")] = int(pos.get("price") or 0)
    return price, docs


def plan(path):
    stock, names = read_stock(path)
    price, stale = current_prices()
    cards = unlinked.cards(price)
    by_art = {}
    for card_id, card in cards.items():
        art = (card.get("article") or "").strip()
        if art:
            by_art[art] = card
    ready, skipped = [], []
    need = [a for a in stock if a not in by_art]
    found = loader.lookup_by_article(need) if need else {}
    for art, qty in stock.items():
        card = by_art.get(art)
        if card:                                     # цена из текущего оприходования
            kop, src = price.get(card["id"], 0), "оприходование"
        else:
            hits = [c for c in (found.get(art) or []) if not c.get("archived")]
            if not hits:
                skipped.append((art, names.get(art, ""), qty, "нет карточки в МС"))
                continue
            if len(hits) > 1:
                skipped.append((art, names.get(art, ""), qty, "артикул неоднозначен"))
                continue
            card = hits[0]                           # цена из закупочной карточки
            kop, src = int((card.get("buyPrice") or {}).get("value") or 0), "карточка"
        if card.get("archived"):
            skipped.append((art, names.get(art, ""), qty, "карточка в архиве"))
            continue
        if kop <= 0:
            skipped.append((art, names.get(art, ""), qty, "цены нет ни в документе, ни в карточке"))
            continue
        ready.append({"article": art, "qty": qty, "price_kop": kop, "card": card, "src": src,
                      "ms_name": card.get("name", "")})
    dropped = [(art, cards[cid].get("name", "")) for cid, art in
               ((cid, (c.get("article") or "").strip()) for cid, c in cards.items())
               if art and art not in stock]
    return ready, skipped, stale, dropped


def build_docs(ready, moment):
    """Документы по 100 позиций — как у загрузчика, но профиля у Блоссома нет."""
    stamp = moment.strftime("%Y-%m-%d")
    docs = []
    for page, start in enumerate(range(0, len(ready), POSITIONS_PER_DOC), start=1):
        chunk = ready[start:start + POSITIONS_PER_DOC]
        docs.append({
            "name": f"{KEY}_{stamp}_p{page}",
            "description": KEY,
            "moment": moment.strftime("%Y-%m-%d %H:%M:%S"),
            "applicable": True,
            "organization": ms_api.ref("organization", ORG_DIGITAL),
            "store": ms_api.ref("store", STORE_REMOTE),
            "positions": [{
                "quantity": i["qty"], "price": i["price_kop"],
                "assortment": {"meta": i["card"]["meta"]},
            } for i in chunk],
        })
    return docs


def report(ready, skipped, dropped, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["раздел", "артикул", "наименование", "остаток", "цена_руб", "источник_цены"])
        for i in ready:
            w.writerow(["грузим", i["article"], i["ms_name"], i["qty"],
                        i["price_kop"] / 100, i["src"]])
        for art, name, qty, why in skipped:
            w.writerow(["пропуск", art, name, qty, "", why])
        for art, name in dropped:
            w.writerow(["ушло из прайса", art, name, 0, "", "остаток обнулится"])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--file", help="файл прайса (по умолчанию свежий *PRC_*.xls из dropbox/)")
    ap.add_argument("--apply", action="store_true", help="записать в МойСклад")
    args = ap.parse_args(argv)

    path = Path(args.file) if args.file else latest_file()
    moment = now_msk()
    ready, skipped, stale, dropped = plan(path)
    rep = REPORT_DIR / f"prc_blossom_stock_{moment:%Y-%m-%d_%H%M}.csv"
    report(ready, skipped, dropped, rep)

    why = {}
    for *_, reason in skipped:
        why[reason] = why.get(reason, 0) + 1
    src = {}
    for i in ready:
        src[i["src"]] = src.get(i["src"], 0) + 1
    print(f"файл: {path.name}")
    print(f"грузим {len(ready)} позиций, {sum(i['qty'] for i in ready):.0f} шт, "
          f"на {sum(i['qty'] * i['price_kop'] for i in ready) / 100:,.2f} ₽".replace(",", " "))
    print("  цена: " + ", ".join(f"{k} {v}" for k, v in src.items()))
    print(f"пропуск {len(skipped)}: " + ", ".join(f"{k} {v}" for k, v in why.items()))
    print(f"было в прайсе, теперь нет: {len(dropped)} — остаток обнулится")
    print(f"документов к созданию: {-(-len(ready) // POSITIONS_PER_DOC)}, "
          f"на замену: {len(stale)}")
    print(f"отчёт: {rep}")
    if not args.apply:
        return 0

    docs = build_docs(ready, moment)
    loader.apply_to_ms(docs, stale, [], moment=moment)
    return 0


if __name__ == "__main__":
    sys.exit(main())
