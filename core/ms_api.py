# поток: prc
# -*- coding: utf-8 -*-
"""
Тонкий HTTP-клиент МойСклад (JSON API remap/1.2) для общего пользования.

Заведён потоком `prc` как НОВЫЙ файл: invoice_bot/ms.py (контур счетов/УПД/банка,
территория `inv`) намеренно не трогали — рефакторить живой контур ради переиспользования
шестидесяти строк невыгодно. Поток `inv` может перейти на этот модуль отдельной задачей.

Отличия от invoice_bot/ms.py: ретраи на 429/5xx, постраничный обход, DELETE и массовые
операции, query-параметры отдельным аргументом.
"""
import os
import json
import gzip
import time
import urllib.parse
import urllib.request
import urllib.error

from dotenv import load_dotenv

load_dotenv(os.getenv("MP_ENV_PATH", "/opt/mp-analytics/.env"))

BASE = "https://api.moysklad.ru/api/remap/1.2"
TOKEN = os.getenv("MOYSKLAD_TOKEN")
RETRIES = 4
TIMEOUT = 180


class MsError(RuntimeError):
    """Ошибка API МойСклад: код + разобранное тело ответа."""

    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"MoySklad HTTP {status}: {json.dumps(body, ensure_ascii=False)[:400]}")


def _url(path, params=None):
    url = path if path.startswith("http") else BASE + path
    if params:
        # filter приходит списком пар: МойСклад ждёт их через ';' в одном параметре
        flat = []
        for key, val in params.items():
            if val is None:
                continue
            if key == "filter" and isinstance(val, (list, tuple)):
                val = ";".join(val)
            flat.append((key, val))
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(flat, quote_via=urllib.parse.quote)
    return url


def _request(method, path, payload=None, params=None):
    url = _url(path, params)
    data = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None
    last = None
    for attempt in range(RETRIES):
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {TOKEN}",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                if exc.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                body = json.loads(raw.decode(errors="replace"))
            except Exception:
                body = {"raw": raw[:400].decode(errors="replace")}
            # 429 — лимит запросов, 5xx — временная беда на стороне МС: ретраим
            if exc.code in (429, 500, 502, 503, 504) and attempt < RETRIES - 1:
                time.sleep(2 ** attempt)
                last = MsError(exc.code, body)
                continue
            raise MsError(exc.code, body) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < RETRIES - 1:
                time.sleep(2 ** attempt)
                last = exc
                continue
            raise
    raise last


def get(path, params=None):
    return _request("GET", path, params=params)


def post(path, payload, params=None):
    return _request("POST", path, payload=payload, params=params)


def put(path, payload, params=None):
    return _request("PUT", path, payload=payload, params=params)


def delete(path, params=None):
    return _request("DELETE", path, params=params)


def iter_rows(path, params=None, page=1000):
    """Постраничный обход коллекции: отдаёт строки по одной, страницами по `page`."""
    offset = 0
    while True:
        args = dict(params or {})
        args.update({"limit": page, "offset": offset})
        chunk = get(path, args)
        rows = chunk.get("rows", [])
        for row in rows:
            yield row
        offset += len(rows)
        if not rows or offset >= chunk.get("meta", {}).get("size", 0):
            return


def meta_id(obj, key):
    """id вложенной сущности из meta.href (store, organization, supplier ...)."""
    href = ((obj or {}).get(key) or {}).get("meta", {}).get("href", "")
    return href.rsplit("/", 1)[-1].split("?")[0] if href else None


def ref(entity, obj_id):
    """Ссылка на сущность в формате, который МойСклад ждёт в теле документа."""
    return {"meta": {
        "href": f"{BASE}/entity/{entity}/{obj_id}",
        "type": entity,
        "mediaType": "application/json",
    }}


if __name__ == "__main__":
    org = get("/entity/organization", {"limit": 5})
    print("organizations:", org.get("meta", {}).get("size"))
    for row in org.get("rows", []):
        print("  ", row.get("name"), row.get("id"))
