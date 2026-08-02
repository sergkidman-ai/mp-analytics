# поток: inv
"""sber_auth.py — авторизация в СберБизнес API (OAuth 2.0 / OIDC) для ООО «ДИСКВЭР».

Отличие от Альфы: у Альфы статичный ApiKey, здесь — полноценный OAuth с интерактивным
входом представителя. Один раз человек логинится в браузере (СМС), банк возвращает `code`
на наш redirect_uri, дальше живём на refresh_token без участия человека.

Три срока годности, за которыми следим:
  * access_token  — 60 минут, обновляем по refresh_token;
  * refresh_token — ОДНОРАЗОВЫЙ: на каждом обновлении банк выдаёт новый, старый умирает.
    Поэтому пишем файл токенов ДО того, как отдать access_token наверх, и пишем атомарно —
    потеря нового refresh_token означает повторный поход к человеку за браузерным входом;
  * client_secret — 40 дней, ротация отдельно (`ops/sber_secret_rotate.py`).

Токены лежат в `secrets/sber/tokens.json` (0600), рядом с ключом mTLS, вне git.

Запуск:
    ./venv/bin/python collectors/sber_auth.py --url        # ссылка для входа человеку
    ./venv/bin/python collectors/sber_auth.py --status     # что в хранилище токенов
    ./venv/bin/python collectors/sber_auth.py --check      # живой вызов client-info
"""
import os
import sys
import json
import uuid
import pathlib
import datetime as dt
import urllib.parse

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv                                   # noqa: E402
load_dotenv(BASE_DIR / ".env" if (BASE_DIR / ".env").exists() else "/opt/mp-analytics/.env")

import requests                                                  # noqa: E402

# Вход человека (СМС) — без mTLS, обычный браузер. Всё остальное — только через mTLS.
SSO_HOST = "https://sbi.sberbank.ru:9443"
API_HOST = "https://fintech.sberbank.ru:9443"
TOKENS = BASE_DIR / "secrets" / "sber" / "tokens.json"
LEEWAY = 120                                                     # с запасом до истечения


def cfg():
    """Параметры подключения из .env. Секреты только отсюда, в код не попадают."""
    c = {
        "client_id": os.getenv("SBER_CLIENT_ID", ""),
        "client_secret": os.getenv("SBER_CLIENT_SECRET", ""),
        "redirect_uri": os.getenv("SBER_REDIRECT_URI", ""),
        "scope": os.getenv("SBER_SCOPE_V1", ""),
        "cert": os.getenv("SBER_CLIENT_CERT", ""),
        "key": os.getenv("SBER_CLIENT_KEY", ""),
        "ca": os.getenv("SBER_CA_BUNDLE", ""),
    }
    missing = [k for k, v in c.items() if not v]
    if missing:
        raise RuntimeError(f"в .env не заполнено: {', '.join('SBER_' + m.upper() for m in missing)}")
    return c


def session(c=None):
    """requests-сессия с нашим клиентским сертификатом и доверенной цепочкой Сбера."""
    c = c or cfg()
    s = requests.Session()
    s.cert = (c["cert"], c["key"])
    s.verify = c["ca"]
    return s


def authorize_url(state=None):
    """Ссылка, по которой представитель входит в браузере. Одноразовое действие."""
    c = cfg()
    state = state or uuid.uuid4().hex
    q = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": c["client_id"],
        "scope": c["scope"],
        "redirect_uri": c["redirect_uri"],
        "state": state,
        "nonce": uuid.uuid4().hex,
    })
    return f"{SSO_HOST}/ic/sso/api/v2/oauth/authorize?{q}", state


def _save(data):
    """Атомарная запись хранилища: потеря refresh_token = повторный вход человека."""
    TOKENS.parent.mkdir(parents=True, exist_ok=True)
    tmp = TOKENS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(TOKENS)


def load():
    return json.loads(TOKENS.read_text(encoding="utf-8")) if TOKENS.exists() else {}


def _token_call(params):
    """POST /oauth/token — параметры в query, тело пустое (так требует спецификация Сбера)."""
    c = cfg()
    params = dict(params, client_id=c["client_id"], client_secret=c["client_secret"])
    r = session(c).post(f"{API_HOST}/ic/sso/api/v2/oauth/token", params=params,
                        headers={"Accept": "application/json",
                                 "Content-Type": "application/x-www-form-urlencoded",
                                 "RqUID": uuid.uuid4().hex}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"токен не выдан: HTTP {r.status_code} {r.text[:300]}")
    t = r.json()
    now = dt.datetime.now(dt.timezone.utc)
    saved = {
        "access_token": t["access_token"],
        "refresh_token": t.get("refresh_token", ""),
        "expires_at": (now + dt.timedelta(seconds=int(t.get("expires_in", 3600)))).isoformat(),
        "obtained_at": now.isoformat(),
        "scope": t.get("scope", ""),
    }
    _save(saved)                                  # сохраняем ДО возврата: см. шапку модуля
    return saved


def exchange_code(code):
    """Первый обмен: код из браузера → пара токенов."""
    return _token_call({"grant_type": "authorization_code", "code": code,
                        "redirect_uri": cfg()["redirect_uri"]})


def refresh():
    t = load()
    if not t.get("refresh_token"):
        raise RuntimeError("нет refresh_token — нужен повторный вход представителя (--url)")
    return _token_call({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})


def access_token():
    """Живой access_token: из хранилища, при истечении — молча обновляем."""
    t = load()
    if not t:
        raise RuntimeError("хранилище токенов пусто — нужен вход представителя (--url)")
    exp = dt.datetime.fromisoformat(t["expires_at"])
    if (exp - dt.datetime.now(dt.timezone.utc)).total_seconds() > LEEWAY:
        return t["access_token"]
    return refresh()["access_token"]


def api(method, path, **kw):
    """Вызов делового API под текущим токеном."""
    h = kw.pop("headers", {})
    h.update({"Authorization": f"Bearer {access_token()}",
              "Accept": "application/json", "RqUID": uuid.uuid4().hex})
    kw.setdefault("timeout", 60)
    return session().request(method, f"{API_HOST}{path}", headers=h, **kw)


def main(argv):
    if "--url" in argv:
        url, state = authorize_url()
        print("Ссылка для входа представителя (открыть в браузере, войти по СМС):\n")
        print(url)
        print(f"\nstate={state}. После входа банк вернёт код на {cfg()['redirect_uri']} — "
              "дальше всё автоматически.")
        return 0
    if "--status" in argv:
        t = load()
        if not t:
            print("токенов нет — сначала --url")
            return 1
        exp = dt.datetime.fromisoformat(t["expires_at"])
        left = (exp - dt.datetime.now(dt.timezone.utc)).total_seconds()
        print(f"access_token: {'живой' if left > 0 else 'истёк'}, осталось {int(left)} с")
        print(f"refresh_token: {'есть' if t.get('refresh_token') else 'НЕТ'}")
        print(f"получен: {t.get('obtained_at')}")
        return 0
    if "--check" in argv:
        r = api("GET", "/fintech/api/v1/client-info")
        print(f"client-info: HTTP {r.status_code}")
        if r.ok:
            d = r.json()
            print("организация:", d.get("name") or d.get("shortName") or "—",
                  "| ИНН:", d.get("inn") or "—")
        else:
            print(r.text[:300])
        return 0 if r.ok else 1
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
