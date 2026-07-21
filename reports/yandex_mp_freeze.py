# -*- coding: utf-8 -*-
# поток: fin
"""reports/yandex_mp_freeze.py — жизненный цикл месяца вкладки «Отчёты МП · Яндекс».

Проще, чем у Ozon/WB: витрина `yandex_finance_monthly` пересобирается в run_daily из сырья
(заказы+услуги Партнёр-API + order-based COGS МС), а закрытие месяца у ЯМ окончательно (raw_yandex_
closure). Поэтому нет отдельного «отчёта о реализации» для сверки и нет правок задним числом,
как у ВБ. Заморозка просто переносит завершившиеся месяцы из витрины в статику
(reports/data/mp_yandex_hist.json), которую читает генератор страницы.

  1. advance(): заморозить все завершившиеся месяцы вне period_keys + освежить последний
     замороженный (услуги/закрытие месяца могут дойти в начале следующего). Живой месяц (следующий
     за max period_keys) отдаёт /api/yandex/mp-current.
  2. Мутации под fcntl-локом, с бэкапом, атомарной записью (os.replace), затем перерисовка
     (yandex_mp_page.render). Идемпотентно.

CLI: python -m reports.yandex_mp_freeze [advance | freeze YYYY-MM | bootstrap YYYY-MM YYYY-MM | status]
"""
import sys
import json
import os
import tempfile
import shutil
import fcntl
import pathlib
import datetime
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from reports import yandex_mp_report as R
from reports import yandex_mp_page as P

HIST_PATH = R.HIST_PATH
DATA_DIR = HIST_PATH.parent
LOCK_PATH = DATA_DIR / ".mp_yandex_hist.lock"
BACKUP_DIR = DATA_DIR / "backups"
MSK = ZoneInfo("Europe/Moscow")

ACC = "ya_acc1"
_BAL_KEYS = R._BAL_KEYS
MONTHS_RU = R.MONTHS_RU
_DERIVED = ("cogs", "net", "margin", "orders", "returns_cnt")


# ---------- io / lock / backup ----------
@contextmanager
def _lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield


def _load():
    return json.loads(HIST_PATH.read_text(encoding="utf-8"))


def _save_atomic(hist):
    text = json.dumps(hist, ensure_ascii=False, indent=1)
    fd, tmp = tempfile.mkstemp(dir=str(HIST_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, str(HIST_PATH))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _backup():
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now(MSK).strftime("%Y%m%d_%H%M%S")
    shutil.copy2(HIST_PATH, BACKUP_DIR / f"mp_yandex_hist_{ts}.json")
    for old in sorted(BACKUP_DIR.glob("mp_yandex_hist_*.json"))[:-30]:
        try:
            old.unlink()
        except OSError:
            pass


# ---------- helpers ----------
def _key(y, m):
    return f"{y}-{m:02d}"


def _next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _empty_hist():
    return {"months": [], "period_keys": [], "provisional": [],
            "accounts": {ACC: {"lines": {k: [] for k in _BAL_KEYS},
                               **{k: [] for k in _DERIVED}}}}


def _slot(hist, key):
    keys = hist["period_keys"]
    if key in keys:
        return keys.index(key)
    keys.append(key)
    hist["months"].append(MONTHS_RU[int(key[5:7]) - 1])
    a = hist["accounts"][ACC]
    for k in _BAL_KEYS:
        a["lines"][k].append(0)
    for k in _DERIVED:
        a[k].append(0)
    return keys.index(key)


def _record(y, m):
    """Запись месяца из yandex_finance_monthly: сырые строки + производные."""
    mags = R.balance(y, m)
    orders, retc = R.op_counts(y, m)
    cogs = R._cogs(y, m)
    d = R._derive(mags, orders, retc, cogs)
    return {"lines": {k: round(mags[k]) for k in _BAL_KEYS},
            "cogs": round(cogs), "net": round(d["net"]), "margin": round(d["margin"], 1),
            "orders": round(orders), "returns_cnt": round(retc)}


def _put(hist, i, rec):
    a = hist["accounts"][ACC]
    for k in _BAL_KEYS:
        a["lines"][k][i] = rec["lines"][k]
    for k in _DERIVED:
        a[k][i] = rec[k]


def _freeze(hist, y, m):
    key = _key(y, m)
    i = _slot(hist, key)
    _put(hist, i, _record(y, m))
    return key


# ---------- orchestration ----------
def advance():
    """Заморозить завершившиеся месяцы вне period_keys + освежить последний. → список изменений."""
    with _lock():
        hist = _load()
        cur = datetime.datetime.now(MSK)
        cur_key = _key(cur.year, cur.month)
        changed = []
        for _ in range(36):
            mx = max(hist["period_keys"]) if hist["period_keys"] else None
            if mx is None:
                break
            ny, nm = _next_month(int(mx[:4]), int(mx[5:7]))
            if _key(ny, nm) >= cur_key:
                break
            changed.append(("freeze", _freeze(hist, ny, nm)))
        if hist["period_keys"]:
            mx = max(hist["period_keys"])
            _freeze(hist, int(mx[:4]), int(mx[5:7]))
            changed.append(("refresh", mx))
        _backup()
        _save_atomic(hist)
        P.render(hist)
        return changed


def bootstrap(y1m1, y2m2):
    """Собрать hist с нуля за [y1m1 … y2m2] включительно (оба 'YYYY-MM'). Живой месяц (следующий
    за y2m2) НЕ морозим. → period_keys."""
    with _lock():
        hist = _empty_hist()
        y, m = int(y1m1[:4]), int(y1m1[5:7])
        while _key(y, m) <= y2m2:
            _freeze(hist, y, m)
            y, m = _next_month(y, m)
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _save_atomic(hist)
        P.render(hist)
        return hist["period_keys"]


def freeze_month(y, m):
    with _lock():
        hist = _load()
        _backup()
        key = _freeze(hist, y, m)
        _save_atomic(hist)
        P.render(hist)
        return key


def _status():
    hist = _load()
    cur = datetime.datetime.now(MSK)
    print("period_keys:", hist["period_keys"])
    print("live month:", R._live_month(), "| МСК сейчас:", _key(cur.year, cur.month))


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "advance"
    if cmd == "advance":
        print("changed:", advance())
    elif cmd == "freeze" and len(args) >= 2:
        y, m = map(int, args[1].split("-"))
        print("frozen:", freeze_month(y, m))
    elif cmd == "bootstrap" and len(args) >= 3:
        print("bootstrapped:", bootstrap(args[1], args[2]))
    elif cmd == "status":
        _status()
    else:
        print(__doc__)
        sys.exit(2)
