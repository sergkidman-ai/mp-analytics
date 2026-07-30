#!/usr/bin/env python3
# поток: gab
"""tools/url_discovery.py — поиск URL-страниц с размерами УПАКОВКИ, без Claude-сессии.

Зачем отдельный модуль: HTML-выдача поисковиков (DuckDuckGo, Bing, Brave, Startpage,
Mojeek, SearXNG) с нашего московского IP закрыта — DDG отдаёт 202 и таймаут,
Bing — капчу, Brave — 429. Поэтому URL берём из канала, который работает:

  icecat — открытый API live.icecat.biz (Open Icecat, ключ не нужен). Отдаёт
  структурную группу «Packaging data» (Package width/depth/height) ОТДЕЛЬНО от
  «Logistics data» (палета, мастер-короб) — то есть мастер-короб не подмешивается,
  чего мы боялись на sima-land (там «размер упаковки» = короб 64×36×36 см).

Модуль НЕ извлекает размеры и не решает, что подтверждено. Он пишет
positions.csv + urls.json для tools/deepseek_extract.py, который качает страницу и
достаёт размер с дословным фрагментом-доказательством; CONFIRMED ставит только
tools/deepseek_candidate_validator.py. Память любой нейросети источником не является.

В urls.json кладём именно API-URL Icecat: он публичный, стабильный и одинаково
доступен извлекателю и независимому валидатору — валидатор перекачает его сам.

  ./venv/bin/python -m tools.url_discovery --from 1 --to 20 \
      --out docs/web_search_v2/hunt/batch_001
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import time
from pathlib import Path
from typing import Iterable

import requests

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

MODELS_CSV = BASE_DIR / "docs/selling_uncovered_models.csv"
ICECAT_API = "https://live.icecat.biz/api"
ICECAT_USER = "openIcecat-live"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
      "Accept-Language": "en-US,en;q=0.9,ru;q=0.8"}

BANNED = re.compile(r"(wildberries|ozon\.ru|avito|aliexpress|joom|ebay|youtube|pinterest|"
                    r"facebook|instagram|twitter|tiktok|vk\.com|reddit|quora|linkedin)", re.I)
OFFICIAL = re.compile(r"(^|\.)(hp|canon|brother|samsung|kyocera|kyoceradocumentsolutions|xerox|"
                      r"panasonic|epson|pantum|ricoh|lexmark|oki|sharp|toshiba|dell)\.", re.I)
RETAIL = re.compile(r"(amazon|walmart|newegg|bhphotovideo|staples|officedepot|bestbuy|cdw|"
                    r"quill|printerland|cartridgesave|tonerpartner|misco|komus|citilink|"
                    r"dns-shop|onlinetrade|regard|xcom-shop|positronica)", re.I)

# производитель по префиксу OEM-кода — нужен Icecat и полезен в отчёте
VENDOR_RULES = [
    (re.compile(r"^(MLT|CLT|SCX|ML)[\-\d]", re.I), "samsung"),
    (re.compile(r"^(TN|DR|LC|LK)[\-\d]", re.I), "brother"),
    (re.compile(r"^(TK|FK|MK|DK)[\-\d]", re.I), "kyocera"),
    (re.compile(r"^KX-", re.I), "panasonic"),
    (re.compile(r"^(PC|TL|CTL|DL|PA)[\-\d]", re.I), "pantum"),
    (re.compile(r"^(CE|CF|CB|CC|Q\d|W\d|C\d{4})", re.I), "hp"),
    (re.compile(r"^(PG|CL|CRG|EP|KP|E\d|BCI|GI|PGI|CLI)", re.I), "canon"),
    (re.compile(r"^(10[0-9]R|11[0-9]R)", re.I), "xerox"),
    (re.compile(r"^(T\d{3,4}|C13T)", re.I), "epson"),
]
ALL_BRANDS = ["hp", "canon", "samsung", "brother", "kyocera", "pantum",
              "xerox", "panasonic", "epson", "ricoh", "lexmark"]


def guess_vendor(oem: str) -> str:
    for rx, v in VENDOR_RULES:
        if rx.search(oem):
            return v
    return ""


def code_variants(oem: str) -> list[str]:
    """Варианты кода: сам код, половинки набора, концы диапазона, без дефисов."""
    out = [oem]
    parts = oem.split("-")
    if len(parts) >= 4:                       # PG-460XL-CL-461XL → два кода набора
        out += ["-".join(parts[:2]), "-".join(parts[2:])]
    m = re.fullmatch(r"([A-Z]+\d{3,4})([A-Z]?)-([A-Z]+\d{3,4})([A-Z]?)", oem, re.I)
    if m:                                     # W2070X-W2073X → концы диапазона
        out += [m.group(1) + m.group(2), m.group(3) + m.group(4)]
    out.append(oem.replace("-", ""))
    seen: list[str] = []
    for x in out:
        if x and x not in seen:
            seen.append(x)
    return seen


def icecat_urls(oem: str, session: requests.Session, log: list) -> list[str]:
    """API-URL Icecat, где реально присутствует группа «Packaging data»."""
    vendor = guess_vendor(oem)
    brands = ([vendor] + [b for b in ALL_BRANDS if b != vendor]) if vendor else ALL_BRANDS
    urls: list[str] = []
    for code in code_variants(oem):
        for brand in brands[:5]:
            url = (f"{ICECAT_API}?UserName={ICECAT_USER}&Language=en"
                   f"&Brand={brand}&ProductCode={code}")
            try:
                r = session.get(url, headers=UA, timeout=25)
            except requests.RequestException as exc:
                log.append({"oem": oem, "src": "icecat", "code": code, "brand": brand,
                            "err": str(exc)[:80]})
                continue
            if r.status_code != 200:
                continue
            has_pkg = "Package width" in r.text
            log.append({"oem": oem, "src": "icecat", "code": code, "brand": brand,
                        "ok": True, "packaging": has_pkg})
            if has_pkg:
                urls.append(url)
            break                    # код нашёлся у этого бренда — прочие не перебираем
    return urls


def tier(url: str) -> int:
    host = re.sub(r"^https?://", "", url).split("/")[0].lower()
    if "icecat" in host:
        return 0
    if OFFICIAL.search(host):
        return 1
    if RETAIL.search(host):
        return 2
    return 3


def pick(urls: Iterable[str], limit: int) -> list[str]:
    clean, seen, per_host = [], set(), {}
    for u in urls:
        u = u.strip()
        if not u.startswith("http") or u in seen or BANNED.search(u):
            continue
        host = re.sub(r"^https?://", "", u).split("/")[0].lower()
        if per_host.get(host, 0) >= 3:
            continue
        seen.add(u)
        per_host[host] = per_host.get(host, 0) + 1
        clean.append(u)
    clean.sort(key=tier)
    return clean[:limit]


def load_models(frm: int, to: int) -> list[dict]:
    with io.open(MODELS_CSV, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    return [r for r in rows if frm <= int(r["ранг"]) <= to]


def extra_urls(path: str | None) -> dict:
    """Необязательный файл {vendorCode:[url,...]} — ссылки, найденные вручную."""
    if not path:
        return {}
    return json.load(io.open(path, encoding="utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", type=int, required=True)
    ap.add_argument("--to", dest="to", type=int, required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=8, help="URL на модель")
    ap.add_argument("--pace", type=float, default=1.0)
    ap.add_argument("--extra", help="JSON {vendorCode:[url,...]} с ручными ссылками")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    manual = extra_urls(args.extra)

    log: list = []
    positions, urls_map = [], {}
    for row in load_models(args.frm, args.to):
        oem = row["OEM_модель"]
        vc = row["семья_мать"]
        got = icecat_urls(oem, session, log) + list(manual.get(vc, []))
        chosen = pick(got, args.limit)
        positions.append({"vendorCode": vc, "model": oem,
                          "manufacturer": guess_vendor(oem) or "", "oem": oem})
        urls_map[vc] = chosen
        print(f"[поиск] {row['ранг']:>3}. {oem}: {len(chosen)} URL", flush=True)
        time.sleep(args.pace)

    with io.open(out / "positions.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["vendorCode", "model", "manufacturer", "oem"],
                           delimiter=";")
        w.writeheader()
        w.writerows(positions)
    (out / "urls.json").write_text(json.dumps(urls_map, ensure_ascii=False, indent=1),
                                   encoding="utf-8")
    (out / "discovery_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=1),
                                            encoding="utf-8")
    print(json.dumps({"моделей": len(positions),
                      "с_URL": sum(1 for v in urls_map.values() if v),
                      "без_URL": sorted(k for k, v in urls_map.items() if not v),
                      "всего_URL": sum(len(v) for v in urls_map.values())},
                     ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
