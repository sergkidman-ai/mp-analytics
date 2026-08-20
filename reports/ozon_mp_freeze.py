# -*- coding: utf-8 -*-
"""reports/ozon_mp_freeze.py — жизненный цикл месяца вкладки «Отчёты МП · Ozon».

Политика:
1. В конце последнего дня месяца (по МСК) месяц замораживается в статику из ОЦЕНКИ по
   транзакциям (Отчёт о реализации ещё не вышел) → сплит «—», помечается provisional; сразу
   начинается новый живой месяц (его отдаёт /api/ozon/mp-current).
2. После выхода Отчёта о реализации (~8–10 числа след. месяца) provisional-месяц сверяется
   ПОАККАУНТНО: у кого отчёт вышел — Продажи/Возвраты/Вознаграждение + сплит берутся из
   реализации, расходные строки остаются из транзакций, и этот аккаунт больше не переоценивается
   (иначе следующий refresh откатил бы официальные цифры в оценку). Пометка provisional у месяца
   снимается, когда сверены ВСЕ аккаунты; поаккаунтный прогресс — в hist["reconciled"].

Источник истины — reports/data/mp_ozon_hist.json (RUNTIME STATE). Все мутации: под fcntl-локом,
с бэкапом, атомарной записью JSON (os.replace), затем перерисовкой страницы (ozon_mp_page.render).
Идемпотентно. Запускается из run_daily (шаг) и по cron `1 0 1 * *` (граница месяца). CLI:
    python -m reports.ozon_mp_freeze [advance|freeze YYYY-MM|reconcile YYYY-MM|cogs [YYYY-MM …]|status]
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

from reports import ozon_mp_report as R
from reports import ozon_mp_page as P
from collectors import ozon_realization as OZR

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
HIST_PATH = R.HIST_PATH
DATA_DIR = HIST_PATH.parent
LOCK_PATH = DATA_DIR / ".mp_ozon_hist.lock"
BACKUP_DIR = DATA_DIR / "backups"
MSK = ZoneInfo("Europe/Moscow")

ACCOUNTS = R.ACCOUNTS
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
    shutil.copy2(HIST_PATH, BACKUP_DIR / f"mp_ozon_hist_{ts}.json")
    backups = sorted(BACKUP_DIR.glob("mp_ozon_hist_*.json"))
    for old in backups[:-30]:                      # держим последние 30
        try:
            old.unlink()
        except OSError:
            pass


# ---------- helpers ----------
def _key(y, m):
    return f"{y}-{m:02d}"


def _next_month(y, m):
    return (y + 1, 1) if m == 12 else (y, m + 1)


def _det_arr(a, ln, code, n):
    """Массив помесячных значений подстроки детализации; создаётся нулями на всю историю."""
    d = a.setdefault("detail", {}).setdefault(ln, {})
    arr = d.get(code)
    if arr is None:
        arr = d[code] = [0] * n
    while len(arr) < n:
        arr.append(0)
    return arr


def _slot(hist, key):
    """Индекс месяца по period_key; если нет — создаёт слот, расширяя ВСЕ параллельные массивы."""
    keys = hist["period_keys"]
    if key in keys:
        return keys.index(key)
    i = len(keys)
    keys.append(key)
    hist["months"].append(MONTHS_RU[int(key[5:7]) - 1])
    for acc in ACCOUNTS:
        a = hist["accounts"][acc]
        for k in _BAL_KEYS:
            a["lines"][k].append(0)
        a["split"].append(None)
        for k in _DERIVED:
            a[k].append(0)
        for codes in (a.get("detail") or {}).values():
            for arr in codes.values():
                arr.append(0)
    return i


def _reconciled(hist):
    """hist["reconciled"] = {acc: [period_key…]} — аккаунты, УЖЕ сверенные с реализацией по
    ещё не закрытым (provisional) месяцам. Ключ выбрасывается, когда сверены все аккаунты:
    список хранит только незавершённые месяцы и не растёт бесконечно."""
    r = hist.setdefault("reconciled", {})
    for acc in ACCOUNTS:
        r.setdefault(acc, [])
    return r


def _estimate_record(y, m, accounts=None):
    """per-acc запись месяца из ТРАНЗАКЦИЙ (оценка): 10 строк + split=None + производные
    + detail (разрез 4 расходных строк по услугам/операциям Ozon)."""
    out = {}
    for acc in (accounts if accounts is not None else ACCOUNTS):
        mags = R.balance(acc, y, m)
        det = R.balance_detail(acc, y, m)
        orders, retc = R.op_counts(acc, y, m)
        cogs = R._cogs(acc, y, m)
        d = R._derive(mags, orders, retc, cogs)
        out[acc] = {"lines": {k: round(mags[k]) for k in _BAL_KEYS}, "split": None,
                    "detail": {ln: {c: round(v) for c, v in det.get(ln, {}).items()}
                               for ln in R.DETAIL_LINES},
                    "cogs": round(cogs), "net": round(d["net"]), "margin": round(d["margin"], 1),
                    "orders": orders, "returns_cnt": retc}
    return out


def _put(hist, i, per_acc):
    for acc in per_acc:
        a = hist["accounts"][acc]; rec = per_acc[acc]
        for k in _BAL_KEYS:
            a["lines"][k][i] = rec["lines"][k]
        a["split"][i] = rec["split"]
        for k in _DERIVED:
            a[k][i] = rec[k]
        _put_detail(hist, a, i, rec.get("detail"))


def _put_detail(hist, a, i, detail):
    """Записать разрез месяца i. Коды, которых в этом месяце нет, обнуляются (а не тянут
    прошлое значение) — иначе повторная заморозка оставила бы призрак старой услуги."""
    if not detail:
        return
    n = len(hist["period_keys"])
    for ln, codes in detail.items():
        known = a.setdefault("detail", {}).setdefault(ln, {})
        for code in set(known) | set(codes):
            _det_arr(a, ln, code, n)[i] = codes.get(code, 0)


# ---------- freeze / reconcile (мутируют hist in-memory) ----------
def _freeze_estimate(hist, y, m):
    """Заморозить месяц из оценки по транзакциям, пометить provisional. → period_key.
    Уже сверенные с реализацией аккаунты НЕ трогаем: их цифры официальные, переоценка была бы
    откатом назад (та же причина, что у refresh_cogs)."""
    key = _key(y, m)
    i = _slot(hist, key)
    done = _reconciled(hist)
    todo = [a for a in ACCOUNTS if key not in done[a]]
    if todo:
        _put(hist, i, _estimate_record(y, m, todo))
    prov = hist.setdefault("provisional", [])
    if key not in prov:
        prov.append(key)
    return key


def _reconcile_final(hist, y, m):
    """Сверить provisional-месяц с Отчётом о реализации ПОАККАУНТНО: у кого отчёт вышел, тот
    получает официальные Продажи/Возвраты/Вознаграждение + сплит сразу, не дожидаясь второго
    аккаунта (расходы остаются из оценки, net/margin пересчитываются). Отсутствие отчёта у одного
    аккаунта больше не блокирует второй. provisional снимается, только когда сверены ВСЕ.
    → (period_key|None, [сверённые в этот раз аккаунты]); key не None ⇔ месяц закрыт полностью."""
    key = _key(y, m)
    done = _reconciled(hist)
    rs, sp = {}, {}
    for acc in [a for a in ACCOUNTS if key not in done[a]]:
        r = R.realiz_sales(acc, y, m)
        s = OZR.sales_split(acc, y, m)
        if r is None or s is None:
            continue                                   # отчёта ещё нет — ждём, остальных не держим
        rs[acc], sp[acc] = r, s
    if not rs and any(key not in done[a] for a in ACCOUNTS):
        return None, []                                # ждём первый отчёт — менять нечего
    i = _slot(hist, key)
    for acc in rs:
        a = hist["accounts"][acc]
        prod, ret, comm = rs[acc]
        mags = {k: a["lines"][k][i] for k in _BAL_KEYS}      # оценочные 10 строк
        mags["sales"], mags["returns"], mags["commission"] = prod, ret, comm   # 3 из реализации
        cogs, orders, retc = a["cogs"][i], a["orders"][i], a["returns_cnt"][i]
        d = R._derive(mags, orders, retc, cogs)
        for k in _BAL_KEYS:
            a["lines"][k][i] = round(mags[k])
        a["split"][i] = {"rev": round(sp[acc]["revenue"]), "bonus": round(sp[acc]["bonus"]),
                         "part": round(sp[acc]["partners"])}
        a["net"][i] = round(d["net"]); a["margin"][i] = round(d["margin"], 1)
        done[acc].append(key)
    if any(key not in done[a] for a in ACCOUNTS):       # ждём оставшиеся аккаунты
        return None, list(rs)
    prov = hist.setdefault("provisional", [])
    if key in prov:
        prov.remove(key)
    for acc in ACCOUNTS:                               # месяц закрыт — маркер прогресса не нужен
        done[acc].remove(key)
    return key, list(rs)


# ---------- orchestration ----------
def advance_and_reconcile():
    """(а) заморозить все ЗАВЕРШИВШИЕСЯ месяцы вне period_keys; (б) сверить provisional с готовой
    реализацией. Под локом, с бэкапом, атомарно JSON → перерисовка страницы. → список изменений."""
    with _lock():
        hist = _load()
        cur = datetime.datetime.now(MSK)
        cur_key = _key(cur.year, cur.month)
        changed = []
        structural = False                                   # заморозка/сверка → делаем бэкап
        for _ in range(36):                                  # предохранитель от рант-луп
            mx = max(hist["period_keys"])
            ny, nm = _next_month(int(mx[:4]), int(mx[5:7]))
            if _key(ny, nm) >= cur_key:                      # текущий/будущий месяц не морозим
                break
            changed.append(("freeze", _freeze_estimate(hist, ny, nm)))
            structural = True
        for key in list(hist.get("provisional", [])):
            yy, mm = int(key[:4]), int(key[5:7])
            full, done_acc = _reconcile_final(hist, yy, mm)   # реализация вышла → сверка (поаккаунтно)
            if done_acc:
                changed.append(("reconcile", f"{key} {'+'.join(done_acc)}"))
                structural = True
            if not full:                                     # у кого отчёта нет — освежаем оценку
                _freeze_estimate(hist, yy, mm)               # (доберём поздние транзакции месяца)
                changed.append(("refresh", key))
        if changed:
            if structural:
                _backup()
            _save_atomic(hist)
            P.render(hist)
        return changed


def refresh_cogs(keys=None):
    """Обновить в статике ТОЛЬКО себестоимость (и производные net/margin) уже замороженных месяцев.

    Нужно, когда изменился метод расчёта COGS, а деньги площадки не менялись: полный
    freeze_estimate откатил бы сверенные из Отчёта о реализации Продажи/Возвраты/Вознаграждение
    обратно в оценку по транзакциям. Здесь строки денег остаются как есть, из витрины берётся
    только cogs. keys — список 'YYYY-MM' (по умолчанию все замороженные). → список изменённых.
    """
    with _lock():
        hist = _load()
        todo = [k for k in hist["period_keys"] if keys is None or k in keys]
        changed = []
        for key in todo:
            y, m = int(key[:4]), int(key[5:7])
            i = hist["period_keys"].index(key)
            for acc in ACCOUNTS:
                a = hist["accounts"][acc]
                cogs = R._cogs(acc, y, m)
                if round(cogs) == a["cogs"][i]:
                    continue
                mags = {k: a["lines"][k][i] for k in _BAL_KEYS}
                d = R._derive(mags, a["orders"][i], a["returns_cnt"][i], cogs)
                a["cogs"][i] = round(cogs)
                a["net"][i] = round(d["net"])
                a["margin"][i] = round(d["margin"], 1)
                changed.append((acc, key))
        if changed:
            _backup()
            _save_atomic(hist)
            P.render(hist)
        return changed


def refresh_detail(keys=None):
    """Пересобрать в статике ТОЛЬКО разрез расходных строк по услугам (detail).

    Безопасно для сверённых месяцев: сверка с Отчётом о реализации переписывает лишь
    Продажи/Возвраты/Вознаграждение, а Доставка/Услуги партнёров/Реклама/Другие всегда остаются
    оценкой по транзакциям — тем же источником, из которого считается разрез.
    keys — список 'YYYY-MM' (по умолчанию все замороженные). → список изменённых (acc, key)."""
    with _lock():
        hist = _load()
        todo = [k for k in hist["period_keys"] if keys is None or k in keys]
        changed = []
        for key in todo:
            y, m = int(key[:4]), int(key[5:7])
            i = hist["period_keys"].index(key)
            for acc in ACCOUNTS:
                a = hist["accounts"][acc]
                det = R.balance_detail(acc, y, m)
                new = {ln: {c: round(v) for c, v in det.get(ln, {}).items()}
                       for ln in R.DETAIL_LINES}
                old = {ln: {c: arr[i] for c, arr in ((a.get("detail") or {}).get(ln) or {}).items()
                            if arr[i]} for ln in R.DETAIL_LINES}
                if old == {ln: {c: v for c, v in new[ln].items() if v} for ln in R.DETAIL_LINES}:
                    continue
                _put_detail(hist, a, i, new)
                changed.append((acc, key))
        if changed:
            _backup()
            _save_atomic(hist)
            P.render(hist)
        return changed


def freeze_estimate(y, m):
    """Ручная заморозка одного месяца (CLI/тест)."""
    with _lock():
        hist = _load()
        _backup()
        key = _freeze_estimate(hist, y, m)
        _save_atomic(hist)
        P.render(hist)
        return key


def reconcile_final(y, m):
    """Ручная сверка одного месяца (CLI/тест). → key, если месяц закрыт полностью (сверены все
    аккаунты); None — если сверить пока нечего ИЛИ сверилась только часть аккаунтов (их цифры
    всё равно сохраняются, см. второй элемент кортежа _reconcile_final)."""
    with _lock():
        hist = _load()
        key, done_acc = _reconcile_final(hist, y, m)
        if done_acc:
            _backup()
            _save_atomic(hist)
            P.render(hist)
        return key


def _status():
    hist = _load()
    cur = datetime.datetime.now(MSK)
    print("period_keys:", hist["period_keys"])
    prov = hist.get("provisional", [])
    print("provisional:", prov)
    rec = hist.get("reconciled", {})
    for key in prov:
        acc_done = [a for a in ACCOUNTS if key in rec.get(a, [])]
        print(f"  {key}: сверено с реализацией {acc_done or '—'}, "
              f"ждём {[a for a in ACCOUNTS if a not in acc_done]}")
    print("live month:", R._live_month(), "| МСК сейчас:", _key(cur.year, cur.month))


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "advance"
    if cmd == "advance":
        print("changed:", advance_and_reconcile())
    elif cmd == "freeze" and len(args) >= 2:
        y, m = map(int, args[1].split("-"))
        print("frozen:", freeze_estimate(y, m))
    elif cmd == "reconcile" and len(args) >= 2:
        y, m = map(int, args[1].split("-"))
        print("reconciled:", reconcile_final(y, m))
    elif cmd == "detail":
        ch = refresh_detail(args[1:] or None)
        print(f"detail обновлён: {len(ch)} (аккаунт, месяц)")
    elif cmd == "cogs":
        print("обновлено:", refresh_cogs(args[1:] or None))
    elif cmd == "status":
        _status()
    else:
        print(__doc__)
        sys.exit(2)
