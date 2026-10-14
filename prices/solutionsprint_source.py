# поток: prc
"""Authenticated Solutions Print catalogue: Moscow price, Moscow + SPb stock.

The cabinet's 'export newest' spreadsheet has no stocks. The complete paginated
catalogue exposes the same customer prices and exact stocks of both warehouses.
Cookies and authenticated HTML never leave the in-memory session.
"""
import hashlib
import json
import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from lxml import html

BASE = "https://cartridge.ru"
CATEGORIES = ["/catalog/" + path + "/" for path in (
    "sale", "kartridzhi", "fotobaraban", "chernila-dlya-printera",
    "pechatayushchie-golovki", "bunker-otrabotannogo-tonera",
    "lenta-perenosa-izobrazheniya", "uzel-proyavki")]
SCOPE = "selected_categories_compatible_solutionprint_standart_v2"


def allowed_brand(value):
    return re.sub(r"[\s.-]+", "", value.casefold()) in {
        "solutionprint", "solutionsprint", "sprint", "sp", "standart", "standard",
        "солюшнспринт", "спринт", "стандарт"}


def selection_params(doc):
    labels = {n.get("for"): text(n) for n in doc.xpath('//label[@for]')}
    brands = {k: v for k, v in labels.items() if k.startswith("arrFilterCatalog_13_")}
    if not brands:
        # Sale has no manufacturer selector: verify each product's actual properties.
        return {}
    selected = {k: "Y" for k, v in brands.items() if allowed_brand(v)}
    compatible = [k for k, v in labels.items()
                  if k.startswith("arrFilterCatalog_16_") and v.casefold() == "совместимый"]
    if not selected or not compatible:
        return None  # Category has no allowed manufacturer / compatible goods.
    selected.update({k: "Y" for k in compatible})
    selected["set_filter"] = "Показать"
    return selected


def verify_selection(doc, params):
    checked = {n.get("name") for n in doc.xpath('//input[@checked]')
               if n.get("name", "").startswith(("arrFilterCatalog_13_", "arrFilterCatalog_16_"))}
    if checked != {k for k in params if k != "set_filter"}:
        raise RuntimeError("Supplier manufacturer/originality filter not applied")


def confirmed_empty(doc):
    return bool(doc.xpath('//*[normalize-space(.)="Нет товаров по заданным параметрам."]'))


def detail_scope(doc):
    verify_city(doc)
    def values(label):
        prefix = label + ":"
        return {text(n)[len(prefix):].strip() for n in doc.xpath('//div|//li|//span')
                if text(n).startswith(prefix) and text(n)[len(prefix):].strip() and len(text(n)) < 100
                and not any(text(c).startswith(prefix) and text(c)[len(prefix):].strip() for c in n)}
    brands, originality = values("Производитель"), values("Оригинальность")
    if len(brands) != 1 or len(originality) != 1:
        raise RuntimeError("Product manufacturer/originality missing or ambiguous")
    return allowed_brand(next(iter(brands))) and next(iter(originality)).casefold() == "совместимый"


def cls(value):
    return f'contains(concat(" ", normalize-space(@class), " "), " {value} ")'


def text(node):
    return re.sub(r"\s+", " ", node.text_content()).strip()


def document(content):
    return html.fromstring(content, parser=html.HTMLParser(encoding="utf-8"))


def request(session, method, path, **kwargs):
    url = urljoin(BASE, path)
    if urlparse(url).hostname != "cartridge.ru" or urlparse(url).scheme != "https":
        raise ValueError("Unexpected supplier host")
    try:
        response = session.request(method, url, timeout=(15, 60), **kwargs)
        response.raise_for_status()
    except requests.RequestException:
        raise RuntimeError("Solutions Print request failed (credentials redacted)") from None
    if urlparse(response.url).hostname != "cartridge.ru":
        raise RuntimeError("Unexpected supplier redirect")
    return response


def login(session):
    login_name = os.environ.get("SOLUTIONSPRINT_LOGIN")
    password = os.environ.get("SOLUTIONSPRINT_PASSWORD")
    if not login_name or not password:
        raise RuntimeError("SOLUTIONSPRINT_LOGIN/PASSWORD missing in .env")
    doc = document(request(session, "GET", "/personal/").content)
    forms = doc.xpath('//form[.//input[@name="USER_PASSWORD"]]')
    if len(forms) != 1:
        raise RuntimeError("Unique login form missing")
    form = forms[0]
    data = {n.get("name"): n.get("value", "") for n in
            form.xpath('.//input[@type="hidden"]') if n.get("name")}
    data.update(AUTH_FORM="Y", TYPE="AUTH", backurl="/personal/",
                USER_LOGIN=login_name, USER_PASSWORD=password, USER_REMEMBER="N")
    result = document(request(session, "POST", form.get("action") or "/personal/", data=data).content)
    verify_login(result)


def verify_login(doc):
    if (not doc.xpath('//a[contains(@href,"logout=")]') or
            doc.xpath('//input[@name="USER_PASSWORD"]')):
        raise RuntimeError("Supplier authorization not confirmed")


def verify_city(doc):
    city = doc.xpath('//*[' + cls("hdr-top__change-city") + ']')
    if len(city) != 1 or not re.search(r"\bМосква\b", text(city[0])):
        raise RuntimeError("Moscow price region not confirmed")
    if not doc.xpath('//body[' + cls("b2b-body") + ']'):
        raise RuntimeError("Authorized B2B catalogue not confirmed")


def page_count(doc):
    numbers = [1]
    for node in doc.xpath('//*[' + cls("pagin_active") + ']'):
        if text(node).isdigit():
            numbers.append(int(text(node)))
    for href in doc.xpath('//a[contains(@href,"PAGEN_1")]/@href'):
        value = parse_qs(urlparse(href).query).get("PAGEN_1", ["1"])[0]
        if not value.isdigit():
            raise RuntimeError("Unexpected catalogue pagination")
        numbers.append(int(value))
    return max(numbers)


def parse_price(block):
    # The primary new-price is the customer's RUB price. The inline price is RRP.
    nodes = block.xpath('.//span[' + cls("new-price") + ']')
    rubles = [n for n in nodes if "new-price_second" not in n.get("class", "").split()]
    if len(rubles) == 1:
        raw = text(rubles[0])
        match = re.fullmatch(r"([\d\s]+(?:[.,]\d+)?)\s*(?:р\.|руб\.?|₽)(?:\s*/\s*шт\.)?", raw)
        if match:
            return str(Decimal(re.sub(r"\s", "", match[1]).replace(",", "."))), "RUB"
    dollars = [n for n in nodes if n.xpath('.//*[' + cls("dollar-icon") + ']')]
    if len(dollars) == 1:
        raw = text(dollars[0])
        match = re.fullmatch(r"([\d\s]+(?:[.,]\d+)?)(?:\s*\$)?(?:\s*/\s*шт\.)?", raw)
        if match:
            return str(Decimal(re.sub(r"\s", "", match[1]).replace(",", "."))), "USD"
    raise RuntimeError("Unambiguous customer RUB/USD price missing")


def parse_listing(doc):
    verify_city(doc)
    # The site hides a warehouse with zero stock. Only infer that zero when the
    # product's independently supplied exact MAX_QUANTITY equals the visible stock.
    totals = {}
    for script in doc.xpath('//script'):
        code = script.text or ""
        if "new JCCatalogItem(" not in code:
            continue
        identity = re.search(r"'XML_ID'\s*:\s*'([\w./-]+)'", code)
        amount = re.search(r"'MAX_QUANTITY'\s*:\s*'([\d.]+)'", code)
        if identity and amount:
            value = Decimal(amount[1])
            if identity[1] in totals and totals[identity[1]] != value:
                raise RuntimeError("Conflicting product total quantity")
            totals[identity[1]] = value
    # container-1 is the main category, not special offers or recommendations.
    containers = doc.xpath('//*[@data-entity="container-1"]')
    if len(containers) != 1:
        raise RuntimeError("Unique catalogue container missing")
    rows = []
    for block in containers[0].xpath('.//*[@data-entity="item"]'):
        codes = block.xpath('.//*[' + cls("product-cart__item-code") + ']')
        names = block.xpath('.//*[' + cls("product-cart__title") + ']//a[@href]')
        if len(codes) != 1 or len(names) != 1:
            raise RuntimeError("Catalogue product identity missing")
        match = re.fullmatch(r"Код товара\s*:\s*([\w./-]+)", text(codes[0]))
        path = names[0].get("href")
        if not match or not re.fullmatch(r"/catalog/[^/]+/", path) or not text(names[0]):
            raise RuntimeError("Invalid catalogue product identity")
        statuses = block.xpath('.//*[' + cls("product-status") + ']')
        if len(statuses) != 1:
            raise RuntimeError("Unique product stock block missing")
        status = text(statuses[0])
        stocks = {}
        for city, count in re.findall(r"Склад\s+(Спб|Мск)\s*[-–]\s*([\d\s]+(?:[.,]\d+)?)\s*шт\.", status, re.I):
            key = city.casefold()
            if key in stocks:
                raise RuntimeError("Duplicate warehouse stock")
            number = Decimal(re.sub(r"\s", "", count).replace(",", "."))
            stocks[key] = int(number) if number == number.to_integral_value() else str(number)
        if stocks and set(stocks) != {"спб", "мск"}:
            if totals.get(match[1]) != sum(Decimal(str(v)) for v in stocks.values()):
                raise RuntimeError("Missing warehouse not confirmed zero by exact total quantity")
            stocks.setdefault("спб", 0)
            stocks.setdefault("мск", 0)
        if not stocks:
            if not re.search(r"под заказ|нет в наличии|отсутствует", status, re.I):
                raise RuntimeError("Unknown supplier stock; refusing partial price list")
            stocks = {"спб": 0, "мск": 0}
        amount, currency = parse_price(block)
        name = text(names[0])
        rows.append({"supplier_code": match[1], "name": name, "url": BASE + path,
                     "msk": stocks["мск"], "spb": stocks["спб"],
                     "price": amount, "currency": currency,
                     "defective": "брак" in name.casefold()})
    return rows


def collect(out_dir, log=print, pause=0.5):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with requests.Session() as session:
        login(session)
        root = document(request(session, "GET", "/catalog/").content)
        verify_city(root)
        available = set(root.xpath('//li[' + cls("selection-device-item") + ']/a/@href'))
        categories = CATEGORIES
        if not set(categories) <= available:
            raise RuntimeError("Required supplier category missing")
        records, pages, conflicts = {}, [], {}
        fingerprints = {path: set() for path in categories}
        local, rate_lock, sessions = threading.local(), threading.Lock(), []
        next_start = [0.0]
        selections = {}

        def fetch_page(path, page=1):
            if not hasattr(local, "session"):
                local.session = requests.Session()
                login(local.session)
                with rate_lock:
                    sessions.append(local.session)
            # Independent Bitrix sessions; at most two catalogue request starts/sec.
            with rate_lock:
                time.sleep(max(0, next_start[0] - time.monotonic()))
                next_start[0] = time.monotonic() + pause
            params = dict(selections.get(path) or {})
            if page > 1:
                params["PAGEN_1"] = page
            response = request(local.session, "GET", path, params=params)
            doc = document(response.content)
            if page == 1:
                verify_city(doc)
                selections[path] = selection_params(doc)
                if selections[path] is None:
                    return [], 0
                if selections[path]:
                    doc = document(request(local.session, "GET", path, params=selections[path]).content)
            if selections[path]:
                verify_selection(doc, selections[path])
            try:
                rows = parse_listing(doc)
                if page == 1 and not rows and selections[path] and confirmed_empty(doc):
                    return [], 0
                for row in rows:
                    row["supplier_scope_verified"] = (True if selections[path] else detail_scope(
                        document(request(local.session, "GET", row["url"]).content)))
                return rows, page_count(doc)
            except RuntimeError as exc:
                raise RuntimeError(f"{path} page {page}: {exc}") from None

        def consume(path, page, rows, count, total):
            if not rows or count != total:
                raise RuntimeError(f"Catalogue pagination changed or empty page: {path} page {page}/{total}")
            fingerprint = tuple(sorted(r["supplier_code"] for r in rows))
            if fingerprint in fingerprints[path]:
                raise RuntimeError("Repeated catalogue page; refusing partial price list")
            fingerprints[path].add(fingerprint)
            for row in rows:
                previous = records.get(row["supplier_code"])
                if previous and any(previous[k] != row[k] for k in
                                    ("name", "price", "currency", "url", "supplier_scope_verified")):
                    conflicts.setdefault(row["supplier_code"], [previous]).append(row)
                if row["supplier_code"] in conflicts:
                    row = {**row, "ambiguous": True}
                records[row["supplier_code"]] = row
            pages.append({"category": path, "page": page, "pages": total, "rows": len(rows)})
            if len(pages) % 25 == 0:
                log(f"supplier pages={len(pages)} unique products={len(records)}", flush=True)

        pool = ThreadPoolExecutor(max_workers=4)
        try:
            first = {pool.submit(fetch_page, path): path for path in categories}
            totals = {}
            for future in as_completed(first):
                path = first[future]
                rows, total = future.result()
                totals[path] = total
                if total:
                    consume(path, 1, rows, total, total)
                else:
                    pages.append({"category": path, "page": 0, "pages": 0, "rows": 0,
                                  "reason": "No allowed manufacturer/compatible goods in supplier filters"})
            remaining = {pool.submit(fetch_page, path, page): (path, page, total)
                         for path, total in totals.items() for page in range(2, total + 1)}
            for future in as_completed(remaining):
                path, page, total = remaining[future]
                rows, count = future.result()
                consume(path, page, rows, count, total)
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
            for worker_session in sessions:
                worker_session.close()
        verify_login(document(request(session, "GET", "/personal/").content))
    if len(records) < 1000:
        raise RuntimeError("Unexpectedly small selected catalogue")
    snapshot = {"collected_at": datetime.now(timezone.utc).isoformat(), "region": "Москва",
                "complete": True, "categories": categories, "pages": pages,
                "rows": list(records.values()), "conflicting_codes": conflicts,
                "scope": SCOPE, "selection_params": selections}
    encoded = json.dumps(snapshot, ensure_ascii=False, indent=2).encode()
    (out / "source.json").write_bytes(encoded)
    (out / "source.sha256").write_text(hashlib.sha256(encoded).hexdigest() + "\n")
    return snapshot
