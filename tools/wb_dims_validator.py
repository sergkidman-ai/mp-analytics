#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# поток: gab
"""Независимая проверка кандидатов, собранных харвестером с карточек конкурентов WB.

Принцип тот же, что у валидатора партий Icecat: НЕ доверять файлу харвестера.
Валидатор заново скачивает каждую карточку по артикулу, сам заново вычисляет
basket-хост, сам заново достаёт габариты — и сверяет с тем, что записал харвестер.
Совпало — только тогда строка вообще рассматривается.

Правило подтверждения (железное): CONFIRMED ставится, если одну и ту же коробку
назвали **минимум два РАЗНЫХ продавца** (по `selling.supplier_id`, а не по числу
карточек: у одного продавца может быть пять карточек с одной и той же ошибкой).

Отбраковка (каждая причина пишется в rejected.csv, ничего не прячется):
  WRONG_MODEL      — кода OEM нет ни в названии, ни в описании, ни в артикуле
                     продавца: поиск WB подсунул не тот картридж;
  MISMATCH         — при перечитке карточки размер не тот, что в файле харвестера;
  MASTER_CARTON    — объём больше 25 л: это мастер-короб, а не упаковка штуки;
  ABSURD           — сторона меньше 1 см или больше 120 см;
  NO_DIMS          — при перечитке габаритов в карточке уже нет;
  ONE_SELLER       — размер назвал только один продавец, подтверждения нет;
  CARD_GONE        — карточка не открывается.

Ничего не выдумывает и не усредняет: подтверждается ровно та тройка чисел,
которую независимо назвали двое.

Порядок сторон. Продавцы подписывают оси как попало (12x33x12 и 33x12x12 —
одна коробка), поэтому для сравнения и для записи тройка упорядочивается по
величине: длина = наибольшая сторона, высота = наименьшая. Сами числа не меняются.

  python3 -m tools.wb_dims_validator --input docs/web_search_v2/competitor_wb/competitor_dims.csv
"""
from __future__ import annotations
import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict

import requests
from requests.adapters import HTTPAdapter


class BrowserTLSAdapter(HTTPAdapter):
    CIPHERS = ("ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:"
               "ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:"
               "ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
               "ECDHE-RSA-AES128-SHA:ECDHE-RSA-AES256-SHA:AES128-GCM-SHA256:"
               "AES256-GCM-SHA384:AES128-SHA:AES256-SHA")

    def init_poolmanager(self, *a, **kw):
        try:
            from urllib3.util.ssl_ import create_urllib3_context
            ctx = create_urllib3_context(ciphers=self.CIPHERS)
            ctx.options |= 0x00004000
            kw["ssl_context"] = ctx
        except Exception:
            pass
        return super().init_poolmanager(*a, **kw)


UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*"}
S = requests.Session()
S.mount("https://", BrowserTLSAdapter())

MAX_VOL_L = 25.0        # больше — мастер-короб, см. правило про загрязнение мастер-коробами
MIN_SIDE, MAX_SIDE = 1.0, 120.0
PAUSE = 0.6

DIM_KEYS = {"длина упаковки": "l", "ширина упаковки": "w", "высота упаковки": "h"}


# ---------- независимый доступ к карточке (свой код, не импорт харвестера) ----------
def _baskets(vol: int) -> list[int]:
    """Кандидаты basket-хостов: грубая оценка и перебор соседей."""
    table = [(143,1),(287,2),(431,3),(719,4),(1007,5),(1061,6),(1115,7),(1169,8),
             (1313,9),(1601,10),(1655,11),(1919,12),(2045,13),(2189,14),(2405,15),
             (2621,16),(2837,17),(3053,18),(3269,19)]
    for hi, n in table:
        if vol <= hi:
            return [n]
        start = None
    anchors = [(3272,20),(3657,21),(4138,24),(4404,25),(4816,26),(5276,28),
               (6275,31),(7157,34),(7421,35),(8068,37),(8618,38),(10746,42)]
    start, prev = anchors[-1][1], anchors[0]
    for v, b in anchors:
        if vol <= v:
            f = 0 if v == prev[0] else (vol - prev[0]) / (v - prev[0])
            start = int(round(prev[1] + f * (b - prev[1])))
            break
        prev = (v, b)
    else:
        start = min(60, anchors[-1][1] + int((vol - anchors[-1][0]) / 500) + 1)
    order = [start]
    for d in range(1, 26):
        order += [start + d, start - d]
    return [n for n in order if 1 <= n <= 70]


def fetch_card(nm: int) -> dict | None:
    vol, part = nm // 100000, nm // 1000
    for b in _baskets(vol):
        url = f"https://basket-{b:02d}.wbbasket.ru/vol{vol}/part{part}/{nm}/info/ru/card.json"
        try:
            r = S.get(url, headers=UA, timeout=20)
        except Exception:
            continue
        if r.status_code == 200:
            try:
                card = r.json()
            except Exception:
                return None
            card["_url"] = url
            return card
    return None


def card_options(card: dict) -> list[dict]:
    opts = list(card.get("options") or [])
    for g in (card.get("grouped_options") or []):
        opts += list(g.get("options") or [])
    return opts


def card_dims(card: dict) -> tuple[dict, str] | None:
    """Три характеристики упаковки → {'l':.., 'w':.., 'h':..} + дословная цитата."""
    got, ev = {}, []
    for o in card_options(card):
        name = (o.get("name") or "").strip().lower()
        key = DIM_KEYS.get(name)
        if key and key not in got:
            m = re.search(r"\d+(?:[.,]\d+)?", str(o.get("value") or ""))
            if m:
                got[key] = float(m.group(0).replace(",", "."))
                ev.append(f"{o.get('name')} = {o.get('value')}")
    if len(got) == 3:
        return got, "; ".join(ev)
    return None


def card_text(card: dict) -> str:
    parts = [card.get("imt_name") or "", card.get("description") or "",
             card.get("vendor_code") or ""]
    for o in card_options(card):
        parts.append(str(o.get("value") or ""))
    return " ".join(parts)


def norm_code(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def oem_in_card(oem: str, card: dict) -> str:
    """Есть ли код модели в карточке. Возвращает 'ok' / 'partial' / 'no'.

    'partial' — из составного кода нашлась часть, но не все. Так бывает у наборов:
    в карточке пишут «117X W2070X/71/72/73X», то есть полный код W2073X там есть,
    но в сокращённой записи. Автоматически такие НЕ подтверждаем — коробка набора
    и коробка одного картриджа отличаются в разы. Кладём в отдельную корзину,
    чтобы посмотреть глазами, а не выбрасываем вместе с настоящими промахами.
    """
    code = norm_code(oem)
    text = norm_code(card_text(card))
    if len(code) < 5:            # слишком короткий код («650») — им ничего не проверишь,
        return "ok"              # решает сверка двух продавцов
    if code in text:
        return "ok"
    parts = [norm_code(p) for p in re.split(r"[+/,\-]| и ", oem) if len(norm_code(p)) >= 5]
    if not parts:
        return "no"
    hits = [p for p in parts if p in text]
    if len(hits) == len(parts):
        return "ok"
    return "partial" if hits else "no"


# ---------------------------- проверка ----------------------------
def main():
    ap = argparse.ArgumentParser(description="Независимая проверка кандидатов с карточек WB")
    ap.add_argument("--input", required=True, nargs="+",
                    help="один или несколько competitor_dims.csv от харвестера; "
                         "карточки объединяются, повторы по (артикул-мать, номенклатура) "
                         "проверяются один раз")
    ap.add_argument("--outdir", default="", help="куда класть результат (по умолчанию рядом)")
    ap.add_argument("--limit", type=int, default=0, help="проверить только первые N моделей")
    args = ap.parse_args()

    outdir = args.outdir or os.path.dirname(os.path.abspath(args.input[0]))
    os.makedirs(outdir, exist_ok=True)

    # Из входных файлов нужны только три поля: артикул-мать, OEM и номенклатура ВБ.
    # Размеры, продавца и согласие валидатор добывает сам, поэтому файлы от разных
    # сборок харвестера (в том числе со старой шапкой) сливаются без переделки.
    rows, dup = [], set()
    for path in args.input:
        with open(path, encoding="utf-8-sig", newline="") as f:
            n = 0
            for r in csv.DictReader(f, delimiter=";"):
                nm = (r.get("competitor_article") or "").strip()
                if not nm:
                    continue
                key = (r["vendorCode"], nm)
                if key in dup:
                    continue
                dup.add(key)
                rows.append(r)
                n += 1
        print(f"  {os.path.basename(path)}: карточек взято {n}")
    by_model: dict[tuple, list] = defaultdict(list)
    for r in rows:
        by_model[(r["vendorCode"], r["oem"])].append(r)

    models = list(by_model.items())
    if args.limit:
        models = models[:args.limit]

    confirmed, rejected = [], []
    seen_cards: dict[int, dict | None] = {}
    print(f"К проверке: {len(models)} моделей, {sum(len(v) for _, v in models)} карточек")

    for i, ((vc, oem), items) in enumerate(models, 1):
        ok_cards = []                        # прошедшие перечитку: (supplier, triple, ...)
        for r in items:
            try:
                nm = int(r["competitor_article"])
            except ValueError:
                continue
            if nm not in seen_cards:
                time.sleep(PAUSE)
                seen_cards[nm] = fetch_card(nm)
            card = seen_cards[nm]

            def rej(reason, detail=""):
                rejected.append([vc, oem, nm, r.get("url", ""), r.get("dimensions", ""),
                                 reason, detail])

            if not card:
                rej("CARD_GONE", "карточка не открылась при перечитке")
                continue
            dd = card_dims(card)
            if not dd:
                rej("NO_DIMS", "в карточке нет трёх характеристик упаковки")
                continue
            got, ev = dd
            triple = tuple(sorted((got["l"], got["w"], got["h"]), reverse=True))
            was = tuple(sorted((float(x) for x in
                               re.findall(r"\d+(?:[.,]\d+)?", r["dimensions"].replace(",", "."))[:3]),
                              reverse=True))
            if triple != was:
                rej("MISMATCH", f"было {r['dimensions']}, при перечитке {triple}")
                continue
            code_hit = oem_in_card(oem, card)
            if code_hit != "ok":
                rej("WRONG_MODEL" if code_hit == "no" else "PARTIAL_CODE",
                    f"код {oem} не найден целиком в карточке "
                    f"«{(card.get('imt_name') or '')[:60]}»")
                continue
            if min(triple) < MIN_SIDE or max(triple) > MAX_SIDE:
                rej("ABSURD", f"сторона вне здравого смысла: {triple}")
                continue
            vol_l = triple[0] * triple[1] * triple[2] / 1000.0
            if vol_l > MAX_VOL_L:
                rej("MASTER_CARTON", f"объём {vol_l:.1f} л — это мастер-короб, не упаковка штуки")
                continue
            ok_cards.append({
                "nm": nm, "supplier": (card.get("selling") or {}).get("supplier_id"),
                "brand": (card.get("selling") or {}).get("brand_name", ""),
                "triple": triple, "ev": ev, "url": r.get("url", ""), "vol": vol_l,
                "name": (card.get("imt_name") or "")[:80],
            })

        # --- правило двух РАЗНЫХ продавцов ---
        by_triple: dict[tuple, list] = defaultdict(list)
        for c in ok_cards:
            by_triple[c["triple"]].append(c)
        best = None
        for triple, cards in by_triple.items():
            sellers = {c["supplier"] for c in cards if c["supplier"]}
            if len(sellers) >= 2 and (best is None or len(sellers) > best[1]):
                best = (triple, len(sellers), cards)
        if best:
            triple, nsell, cards = best
            confirmed.append([vc, oem, triple[0], triple[1], triple[2],
                              round(triple[0]*triple[1]*triple[2]/1000.0, 2), nsell,
                              " | ".join(str(c["nm"]) for c in cards),
                              " | ".join(c["url"] for c in cards),
                              " | ".join(f"[{c['nm']}] {c['ev']}" for c in cards),
                              " | ".join(f"{c['brand']}#{c['supplier']}" for c in cards)])
            mark = f"CONFIRMED {triple[0]}x{triple[1]}x{triple[2]} ({nsell} продавца)"
        else:
            for c in ok_cards:
                rejected.append([vc, oem, c["nm"], c["url"], "x".join(map(str, c["triple"])),
                                 "ONE_SELLER", "размер назвал только один продавец"])
            mark = "не подтверждено"
        print(f"  {i}/{len(models)}  {vc}  {oem:<22} {mark}")

    conf_path = os.path.join(outdir, "confirmed.csv")
    rej_path = os.path.join(outdir, "rejected.csv")
    with open(conf_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["vendorCode", "oem", "длина_см", "ширина_см", "высота_см", "объём_л",
                    "продавцов_подтвердили", "артикулы", "ссылки", "доказательство", "продавцы"])
        w.writerows(confirmed)
    with open(rej_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["vendorCode", "oem", "артикул", "ссылка", "размер", "причина", "подробность"])
        w.writerows(rejected)

    print(f"\nПодтверждено моделей: {len(confirmed)}")
    print(f"Отбраковано строк: {len(rejected)}")
    print(f"  {conf_path}\n  {rej_path}")


if __name__ == "__main__":
    main()
