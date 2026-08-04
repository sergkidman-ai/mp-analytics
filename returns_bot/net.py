# поток: ret
"""HTTP с ретраями. Паттерн взят из collectors/ms_demand_cogs.py (throttle + 429/5xx)."""
import time

import requests

TIMEOUT = 60
MAX_TRIES = 6


def request_json(method, url, *, headers=None, params=None, json_body=None, tries=0):
    """GET/POST с ретраями на 429/5xx и на сетевые сбои. Возвращает разобранный JSON."""
    try:
        r = requests.request(method, url, headers=headers, params=params,
                             json=json_body, timeout=TIMEOUT)
    except requests.RequestException:
        if tries >= MAX_TRIES:
            raise
        time.sleep(2 + tries * 3)
        return request_json(method, url, headers=headers, params=params,
                            json_body=json_body, tries=tries + 1)

    # 420 — лимит Яндекса, 429 — общий, 5xx — временная беда на стороне площадки
    if r.status_code in (420, 429) or r.status_code >= 500:
        if tries >= MAX_TRIES:
            r.raise_for_status()
        wait = int(r.headers.get("Retry-After") or 0) or (3 + tries * 4)
        time.sleep(wait)
        return request_json(method, url, headers=headers, params=params,
                            json_body=json_body, tries=tries + 1)

    r.raise_for_status()
    return r.json()
