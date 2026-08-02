#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# поток: inv
"""Ротация `client_secret` SberBusinessAPI (ООО «ДИСКВЭР») + напоминание в TG.

ЗАЧЕМ. У Сбера `client_secret` живёт **40 дней** (у Альфы такого нет). Протух — перестаёт
работать `/oauth/token`, то есть встаёт весь контур: выписка, платёжки. Дата активации в
ЛК — 2026-08-02, первый дедлайн ≈ 2026-09-11.

КОНТРАКТ БАНКА (спецификация + проверено на живом ПРОМе):

    POST {API_HOST}/ic/sso/api/v1/change-client-secret
         ?access_token=<живой>&client_secret=<текущий>&new_client_secret=<наш новый>&client_id=<id>
    → 200 {"clientSecretExpiration": 40}

**Новое значение придумываем МЫ** — банк его не возвращает. Отсюда главный риск: если запрос
дошёл до банка, а ответ до нас не дошёл (таймаут, обрыв), секрет УЖЕ сменился, а мы его не
знаем → контур мёртв до ручного перевыпуска в ЛК. Защита: кандидат пишется на диск
(`secrets/sber/secret_pending.json`, 0600) **ДО** запроса и удаляется только после того, как
новый секрет доказал работоспособность. При старте pending обнаруживается и лечится `--resolve`.

ПОРЯДОК (менять только понимая, зачем):
  1. взять живой access_token (нужен как параметр запроса);
  2. сгенерировать кандидат, записать pending на диск;
  3. POST в банк;
  4. записать новый секрет в .env (атомарно, права сохраняются);
  5. доказать живость: refresh токенов на НОВОМ секрете;
  6. снять pending, обновить состояние, отчитаться в TG.

Секреты не печатаются никогда: в лог и TG идут только длина и хвост из 4 символов.

Запуск:
    ./venv/bin/python ops/sber_secret_rotate.py --status     # сколько дней осталось
    ./venv/bin/python ops/sber_secret_rotate.py --rotate     # боевая ротация
    ./venv/bin/python ops/sber_secret_rotate.py --resolve    # разбор незавершённой ротации
    ./venv/bin/python ops/sber_secret_rotate.py --cron       # для crontab: напомнить/ротировать

Ежедневно из crontab (время сервера = UTC), тег SBER_SECRET_ROTATE:
    17 6 * * * cd /opt/mp-analytics && ./venv/bin/python ops/sber_secret_rotate.py --cron \
               >> ops/sber_secret_rotate.log 2>&1   # SBER_SECRET_ROTATE
"""
import os
import sys
import json
import uuid
import string
import secrets as pysecrets
import pathlib
import datetime as dt
import urllib.parse
import urllib.request

BASE = pathlib.Path("/opt/mp-analytics")
sys.path.insert(0, str(BASE))

from dotenv import load_dotenv                                   # noqa: E402
load_dotenv(BASE / ".env")

from collectors import sber_auth as sa                           # noqa: E402

ENV_FILE = BASE / ".env"
ENV_KEY = "SBER_CLIENT_SECRET"
STATE = BASE / "secrets" / "sber" / "secret_state.json"
PENDING = BASE / "secrets" / "sber" / "secret_pending.json"
CHANGE_PATH = "/ic/sso/api/v1/change-client-secret"

LIFETIME_DAYS = 40           # срок жизни секрета по спецификации
ROTATE_AFTER = 30            # ротируем на 30-й день: 10 дней запаса на разбор аварии
WARN_AFTER = 25              # с 25-го дня — предупреждать в TG
ACTIVATED_DEFAULT = dt.date(2026, 8, 2)      # активация в ЛК (см. docs/SBER_BANK_API.md)


# ── утилиты ──────────────────────────────────────────────────────────────────
def tail(s):
    """Безопасное представление секрета для логов: длина + 4 последних символа."""
    return f"len={len(s)}, …{s[-4:]}" if s else "пусто"


def new_secret(n=48):
    """Кандидат: требований к формату банк не публикует, берём консервативный
    алфавит без спецсимволов — секрет уходит в URL-параметре, лишние экранирования
    не нужны."""
    alphabet = string.ascii_letters + string.digits
    return "".join(pysecrets.choice(alphabet) for _ in range(n))


def load_state():
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"activated": ACTIVATED_DEFAULT.isoformat(), "rotations": 0}


def save_state(st):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(STATE)


def age_days(st=None):
    st = st or load_state()
    d0 = dt.date.fromisoformat(st.get("activated") or ACTIVATED_DEFAULT.isoformat())
    return (dt.date.today() - d0).days


def write_env_secret(value):
    """Атомарная подмена одной строки в .env. Права и остальные ключи сохраняются;
    файл не пересобирается из окружения (иначе потеряются комментарии и порядок)."""
    text = ENV_FILE.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    done = False
    for i, ln in enumerate(lines):
        if ln.strip().startswith(f"{ENV_KEY}="):
            nl = "\n" if ln.endswith("\n") else ""
            lines[i] = f"{ENV_KEY}={value}{nl}"
            done = True
            break
    if not done:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{ENV_KEY}={value}\n")
    mode = ENV_FILE.stat().st_mode & 0o777
    tmp = ENV_FILE.with_suffix(".tmp")
    tmp.write_text("".join(lines), encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(ENV_FILE)
    os.environ[ENV_KEY] = value          # чтобы sber_auth в этом же процессе взял новый


def tg(msg):
    """Отчёт в Telegram (тот же бот, что у ops/wb_token_reminder.py). Молча пропускаем,
    если бот не настроен: отсутствие TG не должно ронять ротацию."""
    token = os.getenv("TG_BOT_TOKEN", "")
    ids = [x.strip() for x in os.getenv("TG_ALLOWED_IDS", "").split(",") if x.strip()]
    if not token or not ids:
        print("TG не настроен — сообщение не отправлено", flush=True)
        return
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    for uid in ids:
        try:
            data = urllib.parse.urlencode(
                {"chat_id": uid, "text": msg, "parse_mode": "Markdown"}).encode()
            urllib.request.urlopen(api, data=data, timeout=20).read()
        except Exception as e:                                   # noqa: BLE001
            print(f"TG {uid}: не доставлено ({type(e).__name__})", flush=True)


def _refresh_works(secret):
    """Доказательство живости секрета: обновляем токены на нём. Пара токенов при этом
    штатно ротируется. Если банк отказал — секрет не тот (refresh_token в этом случае
    не тратится: запрос отклоняется до его погашения)."""
    old = os.environ.get(ENV_KEY, "")
    os.environ[ENV_KEY] = secret
    try:
        sa.refresh()
        return True                       # секрет рабочий — оставляем его в окружении
    except Exception as e:                                       # noqa: BLE001
        print(f"  проверка не прошла: {str(e)[:160]}", flush=True)
        os.environ[ENV_KEY] = old         # не тот — возвращаем как было
        return False


# ── ротация ──────────────────────────────────────────────────────────────────
def rotate():
    cur = os.getenv(ENV_KEY, "")
    if not cur:
        print(f"в .env нет {ENV_KEY} — нечего ротировать")
        return 2
    if PENDING.exists():
        print("есть незавершённая ротация — сначала ./ops/sber_secret_rotate.py --resolve")
        return 2

    token = sa.access_token()                    # нужен живым ДО смены секрета

    # Требований к формату банк не публикует, а сам выдаёт короткий секрет (8 знаков).
    # Пробуем стойкий длинный, при отказе по формату — длиной как у банковского.
    lengths = [32] if len(cur) >= 32 else [32, len(cur)]
    r = cand = None
    for attempt, ln in enumerate(lengths, 1):
        cand = new_secret(ln)
        # кандидат на диск ДО запроса: если ответ потеряется, секрет уже может быть сменён
        PENDING.parent.mkdir(parents=True, exist_ok=True)
        tmp = PENDING.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "candidate": cand,
            "previous": cur,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(PENDING)
        print(f"попытка {attempt}: кандидат {tail(cand)} сохранён в {PENDING.name}")

        params = {"access_token": token, "client_secret": cur,
                  "new_client_secret": cand, "client_id": sa.cfg()["client_id"]}
        r = sa.session().post(f"{sa.API_HOST}{CHANGE_PATH}", params=params,
                              headers={"Accept": "*/*", "RqUID": uuid.uuid4().hex}, timeout=60)
        if r.status_code == 200:
            break
        print(f"  отказ: HTTP {r.status_code} {r.text[:160]}")
        if r.status_code != 400:
            break                       # не про формат — второй заход бессмыслен

    if r.status_code != 200:
        # банк отказал — секрет НЕ сменён, кандидат не нужен
        PENDING.unlink(missing_ok=True)
        print(f"смена отклонена: HTTP {r.status_code}")
        tg(f"⚠️ Сбер: ротация client_secret ОТКЛОНЕНА (HTTP {r.status_code}). "
           f"Контур пока жив на старом секрете, но дедлайн не двигается.")
        return 1

    exp = ""
    try:
        exp = str((r.json() or {}).get("clientSecretExpiration", ""))
    except ValueError:
        pass
    print(f"банк принял смену (clientSecretExpiration={exp or '?'}, {tail(cand)})")

    write_env_secret(cand)
    print(f".env обновлён: {ENV_KEY} {tail(cand)}")

    if not _refresh_works(cand):
        print("НОВЫЙ СЕКРЕТ НЕ РАБОТАЕТ — pending оставлен, разбирать вручную")
        tg("🚨 Сбер: секрет сменён, но проверка НЕ прошла.\n"
           "Контур может быть мёртв. Файл `secrets/sber/secret_pending.json` хранит значение. "
           "Разбор: `ops/sber_secret_rotate.py --resolve`.")
        return 1

    PENDING.unlink(missing_ok=True)
    st = load_state()
    st.update({"activated": dt.date.today().isoformat(),
               "rotations": int(st.get("rotations", 0)) + 1,
               "last_rotated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
    save_state(st)
    dl = dt.date.today() + dt.timedelta(days=LIFETIME_DAYS)
    print(f"готово. следующий дедлайн ≈ {dl.isoformat()}")
    tg(f"🔑 Сбер (Дисквэр): `client_secret` обновлён автоматически.\n"
       f"Проверено обновлением токенов. Следующий дедлайн ≈ {dl.strftime('%d.%m.%Y')} "
       f"(ротация на {ROTATE_AFTER}-й день).")
    return 0


def resolve():
    """Разбор незавершённой ротации: какой из двух секретов действует."""
    if not PENDING.exists():
        print("незавершённых ротаций нет")
        return 0
    p = json.loads(PENDING.read_text(encoding="utf-8"))
    cand, prev = p.get("candidate", ""), p.get("previous", "")
    print(f"кандидат {tail(cand)}, предыдущий {tail(prev)}; начато {p.get('started_at')}")
    for name, val in (("кандидат", cand), ("предыдущий", prev)):
        if not val:
            continue
        print(f"проверяю {name}…")
        if _refresh_works(val):
            write_env_secret(val)
            PENDING.unlink(missing_ok=True)
            st = load_state()
            if val == cand:                       # смена всё-таки прошла
                st.update({"activated": dt.date.today().isoformat(),
                           "rotations": int(st.get("rotations", 0)) + 1})
                save_state(st)
            print(f"действует {name}: записан в .env, pending снят")
            tg(f"✅ Сбер: незавершённая ротация разобрана — действует {name}.")
            return 0
    print("НИ ОДИН из секретов не работает — нужен перевыпуск в ЛК СберБизнес")
    tg("🚨 Сбер: ни старый, ни новый `client_secret` не работают. "
       "Нужен ручной перевыпуск в ЛК СберБизнес (вкладка «Промышленный сервис»).")
    return 1


def status():
    st = load_state()
    a = age_days(st)
    left = LIFETIME_DAYS - a
    dl = dt.date.fromisoformat(st["activated"]) + dt.timedelta(days=LIFETIME_DAYS)
    print(f"секрет активирован {st['activated']}, возраст {a} дн., осталось {left} дн.")
    print(f"дедлайн {dl.isoformat()}, ротаций сделано: {st.get('rotations', 0)}")
    print(f"плановая ротация на {ROTATE_AFTER}-й день "
          f"(через {max(0, ROTATE_AFTER - a)} дн.), предупреждение с {WARN_AFTER}-го")
    print(f"текущий {ENV_KEY}: {tail(os.getenv(ENV_KEY, ''))}")
    if PENDING.exists():
        print("⚠️ ЕСТЬ незавершённая ротация — нужен --resolve")
    return 0


def cron():
    """Ежедневный вызов: тихий no-op, пока не подошёл срок."""
    if PENDING.exists():
        return resolve()
    a = age_days()
    if a >= ROTATE_AFTER:
        print(f"возраст {a} дн. ≥ {ROTATE_AFTER} — ротирую")
        return rotate()
    if a >= WARN_AFTER:
        left = LIFETIME_DAYS - a
        tg(f"⏳ Сбер (Дисквэр): `client_secret` живёт {a} дн., осталось {left}. "
           f"Автоматическая ротация — на {ROTATE_AFTER}-й день.")
        print(f"возраст {a} дн. — предупреждение отправлено")
        return 0
    print(f"возраст {a} дн. — рано, no-op")
    return 0


def main(argv):
    if "--rotate" in argv:
        return rotate()
    if "--resolve" in argv:
        return resolve()
    if "--cron" in argv:
        return cron()
    return status()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
