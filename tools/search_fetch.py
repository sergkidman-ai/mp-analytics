#!/usr/bin/env python3
# поток: gab
"""tools/search_fetch.py — поиск и отбор страниц с РАЗМЕРАМИ УПАКОВКИ, без WebSearch Claude.

Что делает:
 1) на каждую модель строит 4 запроса («<OEM> package dimensions», «box size»,
    «размеры упаковки», прицел в Amazon/Icecat) и берёт HTML-выдачу поисковика
    своими руками: DuckDuckGo (POST html.duckduckgo.com — единственный рабочий метод,
    GET отдаёт 202), при пустой выдаче — Brave, затем Bing;
 2) чистит мусорные домены, ранжирует по приоритету источника
    (официальные сайты производителей, в т.ч. региональные → Amazon/Walmart/Newegg
    с полем Package Dimensions → Icecat → магазины);
 3) скачивает кандидатов готовым обходом блокировок (FallbackFetcher из
    deepseek_extract: прямой → браузерный UA → r.jina.ai → кэш) и оставляет
    top-5 страниц на модель, где реально есть сигнал размера упаковки;
 4) пишет вход для tools/deepseek_extract.py: positions.csv (vendorCode;model;
    manufacturer;oem) и urls.json ({vendorCode: [url, ...]}).

Сам размер тут НЕ извлекается и никуда не отправляется — только поиск и отбор URL.

Запуск:
  ./venv/bin/python -m tools.search_fetch --models docs/selling_uncovered_models.csv \
      --from 1 --to 20 --out docs/web_search_v2/hunt/batch_001
"""
from __future__ import annotations

import argparse
import csv
import html
import io
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote, urlparse, quote_plus

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from tools.deepseek_extract import FallbackFetcher  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
HDRS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
        "Accept": "text/html,application/xhtml+xml"}

# ── домены ───────────────────────────────────────────────────────────────────
# запрещены: самоописания маркетплейсов (габарит там заявляет продавец, не источник),
# соцсети, агрегаторы объявлений
BANNED = re.compile(
    r"(wildberries|ozon\.|avito|aliexpress|joom|ebay\.|youtube|pinterest|facebook|"
    r"instagram|twitter|x\.com|tiktok|vk\.com|reddit|quora|linkedin|"
    r"duckduckgo|bing\.com|brave\.com|google\.)", re.I)

# официальные сайты производителей (в т.ч. региональные зоны)
OFFICIAL = re.compile(
    r"(^|\.)(hp\.com|hp\.ru|canon\.[a-z.]+|usa\.canon\.com|brother\.[a-z.]+|"
    r"samsung\.com|kyoceradocumentsolutions\.[a-z.]+|kyocera\.[a-z.]+|xerox\.[a-z.]+|"
    r"panasonic\.[a-z.]+|epson\.[a-z.]+|pantum\.[a-z.]+|ricoh\.[a-z.]+|lexmark\.com|"
    r"oki\.[a-z.]+|sharp\.[a-z.]+|toshiba[a-z.]*\.[a-z.]+|dell\.com|katun\.com)$", re.I)

# витрины с честным полем Package Dimensions
RETAIL = re.compile(r"(^|\.)(amazon\.[a-z.]+|walmart\.com|newegg\.com|bhphotovideo\.com|"
                    r"target\.com|staples\.com|officedepot\.com|bestbuy\.com|cdw\.com|"
                    r"insight\.com|quill\.com)$", re.I)

ICECAT = re.compile(r"(^|\.)(icecat\.[a-z.]+|.*\.icecat\.biz)$", re.I)

# рус. каталоги и B2B-магазины, где поле «Габариты упаковки» встречается чаще всего
RU_CATALOG = re.compile(
    r"(^|\.)(orgprint\.com|lazerka\.net|komus\.ru|citilink\.ru|dns-shop\.ru|onlinetrade\.ru|"
    r"regard\.ru|nix\.ru|ulmart\.ru|xcom-shop\.ru|positronica\.ru|technopoint\.ru|"
    r"sculptor\.ru|kotofoto\.ru|vseinstrumenti\.ru|officemag\.ru|relef\.ru|merlion\.ru)$", re.I)

DIM_RE = re.compile(r"\d{1,3}[.,]?\d*\s*[x×хX*]\s*\d{1,3}[.,]?\d*\s*[x×хX*]\s*\d{1,3}[.,]?\d*")
PKG_RE = re.compile(
    r"(package dimensions|packaged dimensions|shipping dimensions|box dimensions|"
    r"package size|box size|packaging dimensions|gross dimensions|carton dimensions|"
    r"размер[ыа]?\s+упаковки|габарит[ыа]?\s+упаковки|размер\s+коробки|"
    r"упаковка\s*[,:(]?\s*(д|ш|в)|verpackungsma|dimensions de l.emballage)", re.I)

# ── производитель по OEM-коду ────────────────────────────────────────────────
VENDOR_RULES = [
    (re.compile(r"^(MLT|CLT|SCX|ML-|SL-)", re.I), "Samsung"),
    (re.compile(r"^(CE|CB|CF|CC|Q\d|W\d|C\d{4}[AX]|\d{2,3}[AX]$)", re.I), "HP"),
    (re.compile(r"^(CRG|PG-|CL-\d|C-EXV|GPR|NPG|E\d{2}$|7\d{2}$|0?4\d{2}[AB]?$)", re.I), "Canon"),
    (re.compile(r"^(TN-|DR-|LC-|TN\d)", re.I), "Brother"),
    (re.compile(r"^(TK-|DK-|MK-|WT-)", re.I), "Kyocera"),
    (re.compile(r"^KX-", re.I), "Panasonic"),
    (re.compile(r"^(PC-|PA-|PB-|PD-|TL-|CTL-|DL-|CDL-)", re.I), "Pantum"),
    (re.compile(r"^(10[0-9]R|013R|108R|006R|101R)", re.I), "Xerox"),
    (re.compile(r"^(SP\s?\d|MP\s?C|407\d|408\d|514\d)", re.I), "Ricoh"),
    (re.compile(r"^(MX-|AR-)", re.I), "Sharp"),
    (re.compile(r"^(T0\d|T1\d|C13S|T6\d)", re.I), "Epson"),
    (re.compile(r"^(5[0-9]F|6[0-9]F|[0-9]{2}C\d)", re.I), "Lexmark"),
    (re.compile(r"^4[34567]\d{3}$", re.I), "OKI"),
]


def guess_vendor(oem: str) -> str:
    for rx, name in VENDOR_RULES:
        if rx.search(oem or ""):
            return name
    return ""


# ── поисковики ───────────────────────────────────────────────────────────────
def _clean(u: str) -> str:
    """DDG иногда отдаёт редирект //duckduckgo.com/l/?uddg=<encoded>."""
    if "uddg=" in u:
        u = unquote(u.split("uddg=", 1)[1].split("&", 1)[0])
    if u.startswith("//"):
        u = "https:" + u
    return html.unescape(u).split("#", 1)[0]


# страницы поиска/каталога вместо карточки товара — размера упаковки там не бывает
LISTING_RE = re.compile(r"(/s\?k=|[?&]k=|/search|/sch/|/category|/catalog|/brand/|/c/)", re.I)


def ddg(query: str, session: requests.Session, limit: int = 10) -> list[str]:
    """DuckDuckGo: работает ТОЛЬКО POST-ом на html-эндпоинт (GET → HTTP 202).

    Регион задаём явно: русский запрос — ru-ru, остальные — us-en, иначе выдача
    уезжает в случайную страну (в первом прогоне пришли сплошь .co.za).
    """
    ru = bool(re.search(r"[а-яё]", query, re.I))
    r = session.post("https://html.duckduckgo.com/html/",
                     data={"q": query, "kl": "ru-ru" if ru else "us-en"},
                     headers=HDRS, timeout=25)
    if r.status_code != 200:
        return []
    hits = re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text)
    return [_clean(h) for h in hits][:limit]


def brave(query: str, session: requests.Session, limit: int = 10) -> list[str]:
    r = session.get("https://search.brave.com/search?q=" + quote_plus(query),
                    headers=HDRS, timeout=25)
    if r.status_code != 200:
        return []
    hits = re.findall(r'href="(https?://[^"]+)"', r.text)
    return [_clean(h) for h in hits][:limit * 4]


def bing(query: str, session: requests.Session, limit: int = 10) -> list[str]:
    r = session.get("https://www.bing.com/search?q=" + quote_plus(query) + "&setlang=en",
                    headers=HDRS, timeout=25)
    if r.status_code != 200:
        return []
    hits = re.findall(r'<a[^>]+href="(https?://[^"]+)"[^>]*class="[^"]*tilk', r.text)
    if not hits:
        hits = re.findall(r'<h2><a[^>]+href="(https?://[^"]+)"', r.text)
    return [_clean(h) for h in hits][:limit * 2]


ENGINES = (("ddg", ddg), ("brave", brave), ("bing", bing))


def search_all(queries: list[str], session: requests.Session, log: list,
               pace: float = 6.0) -> list[str]:
    """Последовательный поиск с паузами.

    Параллельные запросы поисковики режут почти сразу (первый прогон: два движка
    молча отдали 0 после двух моделей), поэтому здесь строго по одному запросу
    с паузой, а подряд идущие пустые ответы трактуем как блокировку и ждём дольше.
    """
    seen, out = set(), []
    empty_streak = 0
    for q in queries:
        got = []
        for name, fn in ENGINES:
            try:
                raw = fn(q, session)
            except requests.RequestException as exc:
                log.append({"query": q, "engine": name, "error": str(exc)[:120]})
                raw = []
            got = [u for u in raw if u.startswith("http")
                   and not BANNED.search(urlparse(u).netloc)
                   and not LISTING_RE.search(u)]
            log.append({"query": q, "engine": name, "raw": len(raw), "found": len(got)})
            if got:
                empty_streak = 0
                break
            empty_streak += 1
            # похоже на троттлинг: остываем, а не долбим следующий движок сразу
            time.sleep(pace if empty_streak < 3 else 45.0)
        for u in got:
            k = u.rstrip("/")
            if k not in seen:
                seen.add(k)
                out.append(u)
        time.sleep(pace)
    return out


# ── ранжирование ─────────────────────────────────────────────────────────────
def domain_tier(url: str) -> int:
    host = urlparse(url).netloc.lower().replace("www.", "")
    if OFFICIAL.search(host):
        return 0
    if RU_CATALOG.search(host):   # «Габариты упаковки» отдельным полем — самый плотный источник
        return 1
    if RETAIL.search(host):
        return 2
    if ICECAT.search(host):
        return 3
    return 4


def page_signal(text: str, oem: str) -> tuple[int, str]:
    """Оценка страницы по тексту: есть ли код и явный размер УПАКОВКИ."""
    score, why = 0, []
    low = text.lower()
    code = (oem or "").lower()
    core = re.sub(r"[^a-z0-9]", "", code)
    if code and (code in low or (core and core in re.sub(r"[^a-z0-9]", "", low))):
        score += 40
        why.append("код")
    m = PKG_RE.search(text)
    if m:
        score += 60
        why.append("поле упаковки")
    if DIM_RE.search(text):
        score += 20
        why.append("три числа")
    if m and DIM_RE.search(text[max(0, m.start() - 200): m.start() + 400]):
        score += 40
        why.append("размер рядом с полем")
    return score, ",".join(why)


def build_queries(oem: str, vendor: str) -> list[str]:
    q = [f'"{oem}" package dimensions',
         f'"{oem}" cartridge box size cm',
         f'{oem} картридж габариты упаковки',
         f'{oem} картридж "размер упаковки" вес брутто']
    if vendor:
        q[1] = f'{vendor} "{oem}" package dimensions box'
    return q


# ── фаза 1: поиск (строго последовательно) ───────────────────────────────────
def search_model(row: dict, session: requests.Session, cand: int, pace: float) -> dict:
    oem = (row.get("OEM_модель") or "").strip()
    vc = (row.get("семья_мать") or "").strip()
    vendor = guess_vendor(oem)
    log: list = []
    urls = search_all(build_queries(oem, vendor), session, log, pace=pace)
    # приоритет источника, но не более 3 ссылок с одного домена — иначе вся квота
    # уходит на Amazon и модель остаётся без второго независимого источника
    urls.sort(key=lambda u: domain_tier(u))
    diverse, per_host = [], {}
    for u in urls:
        h = urlparse(u).netloc.lower().replace("www.", "")
        if per_host.get(h, 0) >= 3:
            continue
        per_host[h] = per_host.get(h, 0) + 1
        diverse.append(u)
    urls = diverse[:cand]
    print(f"  [поиск] {row.get('ранг')}. {oem}: {len(urls)} URL", flush=True)
    return {"vendorCode": vc, "oem": oem, "manufacturer": vendor,
            "revenue": row.get("выручка_год_₽", ""), "rank": row.get("ранг", ""),
            "urls": urls, "search_log": log}


# ── фаза 2: скачивание и отбор (параллельно) ─────────────────────────────────
def fetch_model(found: dict, out_dir: Path, top: int) -> dict:
    oem, vc, urls = found["oem"], found["vendorCode"], found["urls"]
    fetcher = FallbackFetcher(timeout=25.0)
    pages_dir = out_dir / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    def _one(u: str):
        fr = fetcher.fetch(u)
        if not fr.ok:
            return {"url": u, "ok": False, "score": -1, "why": fr.error[:100], "tier": domain_tier(u)}
        s, why = page_signal(fr.doc.text, oem)
        name = re.sub(r"[^a-zA-Z0-9]+", "_", urlparse(u).netloc + urlparse(u).path)[:80]
        (pages_dir / f"{vc}__{name}.txt").write_text(fr.doc.text[:200_000], encoding="utf-8")
        return {"url": u, "ok": True, "score": s, "why": why, "tier": domain_tier(u),
                "method": fr.method, "title": (fr.doc.title or "")[:120]}

    with ThreadPoolExecutor(max_workers=6) as ex:
        scored = list(ex.map(_one, urls))

    good = [x for x in scored if x["ok"] and x["score"] >= 60]
    good.sort(key=lambda x: (-x["score"], x["tier"]))
    # доменное разнообразие: не более 2 страниц с одного домена
    picked, per_host = [], {}
    for x in good:
        h = urlparse(x["url"]).netloc
        if per_host.get(h, 0) >= 2:
            continue
        per_host[h] = per_host.get(h, 0) + 1
        picked.append(x)
        if len(picked) >= top:
            break
    out = dict(found)
    out.pop("urls", None)
    out.update({"searched": len(urls), "fetched_ok": sum(1 for x in scored if x["ok"]),
                "picked": picked, "all": scored})
    print(f"  [качаем] {oem}: ok {out['fetched_ok']}/{len(urls)}, отобрано {len(picked)}",
          flush=True)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="docs/selling_uncovered_models.csv")
    ap.add_argument("--from", dest="rank_from", type=int, default=1)
    ap.add_argument("--to", dest="rank_to", type=int, default=20)
    ap.add_argument("--out", required=True)
    ap.add_argument("--top", type=int, default=5, help="сколько страниц на модель")
    ap.add_argument("--candidates", type=int, default=14, help="сколько URL качать до отбора")
    ap.add_argument("--workers", type=int, default=4, help="моделей параллельно на скачивании")
    ap.add_argument("--pace", type=float, default=6.0, help="пауза между запросами к поисковику")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    with io.open(args.models, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f, delimiter=";")
                if args.rank_from <= int(r["ранг"]) <= args.rank_to]

    # фаза 1 — поиск: одна сессия, по очереди, с паузами (иначе движки режут выдачу)
    session = requests.Session()
    found = [search_model(r, session, args.candidates, args.pace) for r in rows]
    (out_dir / "found_urls.json").write_text(json.dumps(found, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    # фаза 2 — скачивание: здесь параллель безопасна, домены разные
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        res = list(ex.map(lambda f_: fetch_model(f_, out_dir, args.top), found))

    positions = out_dir / "positions.csv"
    with io.open(positions, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["vendorCode", "model", "manufacturer", "oem"],
                           delimiter=";")
        w.writeheader()
        for r in res:
            if r["picked"]:
                w.writerow({"vendorCode": r["vendorCode"], "model": r["oem"],
                            "manufacturer": r["manufacturer"], "oem": r["oem"]})

    urls_json = {r["vendorCode"]: [p["url"] for p in r["picked"]] for r in res if r["picked"]}
    (out_dir / "urls.json").write_text(json.dumps(urls_json, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    (out_dir / "search_log.json").write_text(json.dumps(res, ensure_ascii=False, indent=1),
                                             encoding="utf-8")

    empty = [r["oem"] for r in res if not r["picked"]]
    print(json.dumps({
        "моделей": len(res),
        "с_URL": len(urls_json),
        "без_URL": empty,
        "страниц_всего": sum(len(v) for v in urls_json.values()),
        "positions": str(positions),
        "urls": str(out_dir / "urls.json"),
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
