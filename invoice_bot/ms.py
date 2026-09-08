import os, sys, json, gzip, time, urllib.request, urllib.parse, urllib.error
sys.path.insert(0, "/opt/mp-analytics")
from dotenv import load_dotenv
load_dotenv("/opt/mp-analytics/.env")
MS = "https://api.moysklad.ru/api/remap/1.2"
TOK = os.getenv("MOYSKLAD_TOKEN")

# ── Ретраи на временные сбои шлюза МойСклада ────────────────────────────────
# Инцидент 27.08.2026: счёт Одиссея ОД00005205 упал с «HTTP Error 502: Bad Gateway»,
# заказ не создался, письмо ушло в ошибку. Шлюз моргнул на секунду — повтор лечит.
RETRY_CODES = {429, 500, 502, 503, 504}
RETRY_TRIES = 3
RETRY_PAUSE = (2, 5, 10)

def _retryable(e):
    if isinstance(e, urllib.error.HTTPError):
        return e.code in RETRY_CODES
    # сеть отвалилась / TLS-handshake не уложился в таймаут
    return isinstance(e, (urllib.error.URLError, TimeoutError, OSError))

def _sleep_before_retry(what, attempt, e):
    pause = RETRY_PAUSE[min(attempt, len(RETRY_PAUSE) - 1)]
    print(f"[ms] {what} → {e}; повтор через {pause}с ({attempt + 2}/{RETRY_TRIES})",
          file=sys.stderr, flush=True)
    time.sleep(pause)

def _body(e):
    """Тело HTTPError → dict; если пришёл HTML от шлюза, заворачиваем в форму ошибки МС."""
    d = e.read()
    try:
        if e.headers.get("Content-Encoding") == "gzip":
            d = gzip.decompress(d)
    except Exception:
        pass
    txt = d.decode(errors="replace")
    try:
        return json.loads(txt)
    except Exception:
        return {"errors": [{"error": f"HTTP {e.code}: {txt[:300]}"}]}

def get(path):
    """GET идемпотентен — повторяем свободно."""
    url = MS + path
    for attempt in range(RETRY_TRIES):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {TOK}",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    d = gzip.decompress(d)
                return json.loads(d)
        except Exception as e:
            if attempt + 1 >= RETRY_TRIES or not _retryable(e):
                raise
            _sleep_before_retry(f"GET {path}", attempt, e)

if __name__ == "__main__":
    # sanity + orgs
    orgs = get("/entity/organization")
    print("ORG_COUNT", orgs.get("meta",{}).get("size"))
    for o in orgs.get("rows", []):
        print(json.dumps({"name":o.get("name"),"inn":o.get("inn"),"kpp":o.get("kpp"),
                          "legalTitle":o.get("legalTitle"),"id":o.get("id")[:8]+"..."}, ensure_ascii=False))

def post(path, payload, retry=False):
    """POST НЕ идемпотентен: при 502 документ мог успеть создаться на той стороне,
    и повтор даст дубль заказа/приёмки. Поэтому по умолчанию НЕ повторяем — возвращаем
    код наверх, пусть решает вызывающий. retry=True — только для заведомо безопасных POST.

    Исключение — 429: лимит запросов МС отвергает запрос ДО обработки, документ на той
    стороне не появляется, дубля от повтора быть не может. Инцидент 08.09.2026: счёт
    Одиссея ОД00005464 не создался («HTTP 429: превышен лимит»), когда рядом обрабатывались
    ещё два счёта того же поставщика; человек об этом узнал только из сообщения в боте.
    """
    data = json.dumps(payload, ensure_ascii=False).encode()
    tries = RETRY_TRIES          # 429 повторяем всегда; остальные коды — только при retry=True
    for attempt in range(tries):
        req = urllib.request.Request(MS + path, data=data, method="POST", headers={
            "Authorization": f"Bearer {TOK}", "Accept-Encoding": "gzip",
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    d = gzip.decompress(d)
                return r.status, json.loads(d)
        except urllib.error.HTTPError as e:
            can = e.code == 429 or (retry and e.code in RETRY_CODES)
            if attempt + 1 >= tries or not can:
                return e.code, _body(e)
            _sleep_before_retry(f"POST {path}", attempt, e)
        except Exception as e:
            if attempt + 1 >= tries or not retry or not _retryable(e):
                raise
            _sleep_before_retry(f"POST {path}", attempt, e)

def put(path, payload):
    """PUT здесь — полная замена документа по id, идемпотентен: повторяем."""
    data = json.dumps(payload, ensure_ascii=False).encode()
    for attempt in range(RETRY_TRIES):
        req = urllib.request.Request(MS + path, data=data, method="PUT", headers={
            "Authorization": f"Bearer {TOK}", "Accept-Encoding": "gzip",
            "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                d = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    d = gzip.decompress(d)
                return r.status, json.loads(d)
        except urllib.error.HTTPError as e:
            if attempt + 1 >= RETRY_TRIES or e.code not in RETRY_CODES:
                return e.code, _body(e)
            _sleep_before_retry(f"PUT {path}", attempt, e)
        except Exception as e:
            if attempt + 1 >= RETRY_TRIES or not _retryable(e):
                raise
            _sleep_before_retry(f"PUT {path}", attempt, e)
