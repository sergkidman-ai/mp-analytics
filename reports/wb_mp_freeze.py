# -*- coding: utf-8 -*-
# поток: fin
"""reports/wb_mp_freeze.py — жизненный цикл месяца вкладки «Отчёты МП · WB».

Политика ПРОЩЕ, чем у Ozon: Финансовый отчёт ВБ (Баланс) сам является ОКОНЧАТЕЛЬНЫМ источником —
нет отдельного «отчёта о реализации» для последующей сверки. Поэтому:
1. Завершившийся месяц замораживается в статику из raw_wb_report (по формированию) — сразу
   финально, provisional НЕ используется (список всегда пуст).
2. Живой месяц отдаёт /api/wb/mp-current (оценка + прогноз run-rate).
   ⚠ ВБ дозаписывает недельные отчёты и в начале следующего месяца (отчёт, сформированный
   01–06 числа, попадает в НОВЫЙ месяц формирования). Поэтому свежезамороженный месяц ещё может
   пополниться — advance() при каждом прогоне ОБНОВЛЯЕТ последний замороженный месяц из БД
   (refresh), пока не станет старше «текущего−1».

РЕКОНСАЙЛ (правки ВБ задним числом). ВБ переписывает уже сформированные отчёты в пределах
последних ~4–5 недель. Механика в сырье: коллектор upsert-ит raw_wb_report по (account, rrd_id),
`payload` в update-колонках → при перезаборе строка ПЕРЕЗАПИСЫВАЕТСЯ на месте, `create_dt` не
меняется → правка тихо сдвигает итоги ИСХОДНОГО месяца формирования. `loaded_at` = время первого
появления (в update-колонки не входит), поэтому по нему правку НЕ поймать. Единственный способ —
пересчитать затронутые месяцы и сравнить с замороженным снапшотом. Раз в неделю (понедельник,
после выхода нового отчёта) `reconcile_recent`: (1) сам перезабирает 45-дн окно операций с ВБ и
пересобирает margin за затронутые месяцы, (2) пересчитывает замороженные месяцы окна, (3) правит
hist на свежие цифры, (4) выводит список расхождений (месяц/юрлицо/строка/было→стало/Δ) в чат и в
лог reports/data/wb_reconcile_log.jsonl для проверки.

Источник истины — reports/data/mp_wb_hist.json (RUNTIME STATE). Мутации под fcntl-локом, с
бэкапом, атомарной записью (os.replace), затем перерисовка страницы (wb_mp_page.render).
Идемпотентно. Запуск из run_daily (шаг) и cron `2 0 1 * *` (граница месяца) + понедельничный
реконсайл. CLI:
    python -m reports.wb_mp_freeze [advance | reconcile [weeks] [--no-apply] [--no-pull] |
                                    freeze YYYY-MM | bootstrap YYYY-MM YYYY-MM | status]
"""
import sys
import json
import os
import tempfile
import shutil
import fcntl
import calendar
import pathlib
import datetime
from contextlib import contextmanager
from zoneinfo import ZoneInfo

from core import db
from collectors import wb as _wb
from reports import margin_by_sku as _margin
from reports import wb_mp_report as R
from reports import wb_mp_page as P

HIST_PATH = R.HIST_PATH
DATA_DIR = HIST_PATH.parent
LOCK_PATH = DATA_DIR / ".mp_wb_hist.lock"
BACKUP_DIR = DATA_DIR / "backups"
RECON_LOG = DATA_DIR / "wb_reconcile_log.jsonl"
MSK = ZoneInfo("Europe/Moscow")

ACCOUNTS = R.ACCOUNTS
_BAL_KEYS = R._BAL_KEYS
MONTHS_RU = R.MONTHS_RU
_DERIVED = ("commission", "cogs", "net", "margin", "orders", "returns_cnt")

# человекочитаемые ярлыки строк для отчёта о расхождениях
_LABELS = {"sales": "Продажа", "returns": "Возврат", "commission": "Комиссия ВБ+СПП",
           "to_pay": "К перечислению", "delivery": "Логистика", "storage": "Хранение",
           "acceptance": "Приёмка", "other": "Прочие удержания", "cogs": "COGS",
           "net": "Чистая", "margin": "Маржа %", "orders": "Продажи шт", "returns_cnt": "Возвраты шт"}


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
    shutil.copy2(HIST_PATH, BACKUP_DIR / f"mp_wb_hist_{ts}.json")
    for old in sorted(BACKUP_DIR.glob("mp_wb_hist_*.json"))[:-30]:
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
            "accounts": {acc: {"lines": {k: [] for k in _BAL_KEYS},
                               **{k: [] for k in _DERIVED}} for acc in ACCOUNTS}}


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
        for k in _DERIVED:
            a[k].append(0)
    return i


def _record(y, m):
    """per-acc запись месяца из raw_wb_report (окончательная): строки Баланса + производные."""
    out = {}
    for acc in ACCOUNTS:
        mags = R.balance(acc, y, m)
        orders, retc = R.op_counts(acc, y, m)
        cogs = R._cogs(acc, y, m)
        d = R._derive(mags, orders, retc, cogs)
        out[acc] = {"lines": {k: round(mags[k]) for k in _BAL_KEYS},
                    "commission": round(d["commission"]), "cogs": round(cogs),
                    "net": round(d["net"]), "margin": round(d["margin"], 1),
                    "orders": round(orders), "returns_cnt": round(retc)}
    return out


def _put(hist, i, per_acc):
    for acc in ACCOUNTS:
        a = hist["accounts"][acc]; rec = per_acc[acc]
        for k in _BAL_KEYS:
            a["lines"][k][i] = rec["lines"][k]
        for k in _DERIVED:
            a[k][i] = rec[k]


def _freeze(hist, y, m):
    """Заморозить/обновить месяц из raw_wb_report. → period_key."""
    key = _key(y, m)
    i = _slot(hist, key)
    _put(hist, i, _record(y, m))
    return key


# ---------- orchestration ----------
def advance():
    """(а) заморозить все ЗАВЕРШИВШИЕСЯ месяцы вне period_keys; (б) освежить последний
    замороженный (ВБ мог дописать недельный отчёт в его месяц формирования). Под локом, бэкап,
    атомарно JSON → перерисовка. → список изменений."""
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
        # refresh последнего замороженного (доберём поздние отчёты его месяца формирования)
        if hist["period_keys"]:
            mx = max(hist["period_keys"])
            _freeze(hist, int(mx[:4]), int(mx[5:7]))
            changed.append(("refresh", mx))
        _backup()
        _save_atomic(hist)
        P.render(hist)
        return changed


# ---------- реконсайл правок ВБ задним числом ----------
def _month_last(y, m):
    return f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"


def _recent_formation_months(weeks):
    """Месяцы формирования (YYYY-MM), у которых есть отчёты с create_dt за последние weeks недель."""
    rows = db.query(
        """SELECT DISTINCT to_char((payload->>'create_dt')::date,'YYYY-MM') ym
             FROM raw_wb_report
             WHERE (payload->>'create_dt')::date >= (CURRENT_DATE - %s) ORDER BY 1""",
        (weeks * 7,))
    return [r["ym"] for r in rows]


def _pull_window(days=45):
    """Перезабрать 45-дн окно ОПЕРАЦИЙ с ВБ (правки старых отчётов приходят перезаписью payload
    по rrd_id) и пересобрать margin_by_sku за затронутые месяцы формирования. Тянем сырьё напрямую
    (fetch_report+load_raw), без normalize_sales — нам нужен только свежий raw + пересчёт COGS."""
    today = datetime.datetime.now(MSK).date()
    d_from = (today - datetime.timedelta(days=days)).isoformat()
    d_to = today.isoformat()
    for acc in ACCOUNTS:
        rows = _wb.fetch_report(acc, d_from, d_to)
        _wb.load_raw(acc, rows, d_from, d_to)
    for ym in _recent_formation_months(days // 7):
        y, m = int(ym[:4]), int(ym[5:7])
        for acc in ACCOUNTS:
            _margin.build(acc, f"{y}-{m:02d}-01", _month_last(y, m))


def _diff_month(hist, ym):
    """Список расхождений замороженного месяца ym против свежего пересчёта из сырья. Пороги:
    деньги/шт — округление до целого; маржа — 0.1 п.п. → (discs, newrec)."""
    y, m = int(ym[:4]), int(ym[5:7])
    newrec = _record(y, m)
    i = hist["period_keys"].index(ym)
    discs = []
    for acc in ACCOUNTS:
        a = hist["accounts"][acc]; nr = newrec[acc]
        pairs = [(k, a["lines"][k][i], nr["lines"][k]) for k in _BAL_KEYS] + \
                [(k, a[k][i], nr[k]) for k in _DERIVED]
        for k, old, new in pairs:
            changed = (abs(old - new) >= 0.1) if k == "margin" else (round(old) != round(new))
            if changed:
                discs.append({"month": ym, "account": acc, "line": k,
                              "label": _LABELS.get(k, k), "old": old, "new": new,
                              "delta": round(new - old, 1)})
    return discs, newrec


def reconcile_recent(weeks=6, apply=True, pull=True):
    """Понедельничный реконсайл: свериться с ВБ за последние `weeks` недель и подхватить правки
    задним числом. Живой месяц НЕ трогаем (он и так пересчитывается на каждый запрос). Под локом,
    бэкап при изменениях, атомарная запись, перерисовка. → список расхождений (для чата + лог)."""
    if pull:
        _pull_window(days=max(45, weeks * 7 + 3))
    with _lock():
        hist = _load()
        live = R._live_month()
        live_key = f"{live[0]}-{live[1]:02d}" if live else None
        months = [ym for ym in _recent_formation_months(weeks)
                  if ym in hist["period_keys"] and ym != live_key]
        all_discs, changed = [], False
        for ym in months:
            discs, newrec = _diff_month(hist, ym)
            if discs:
                all_discs.extend(discs)
                if apply:
                    _put(hist, hist["period_keys"].index(ym), newrec)
                    changed = True
        if changed:
            _backup()
            _save_atomic(hist)
            P.render(hist)
        # лог (даже если пусто — фиксируем факт прогона)
        rec = {"ts": datetime.datetime.now(MSK).isoformat(timespec="seconds"),
               "weeks": weeks, "pulled": pull, "applied": apply and changed,
               "months_checked": months, "n_disc": len(all_discs), "discs": all_discs}
        with open(RECON_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return {"months": months, "discs": all_discs, "applied": apply and changed}


def _fmt_recon(res):
    """Человекочитаемый отчёт о реконсайле для чата."""
    months, discs = res["months"], res["discs"]
    if not months:
        return "реконсайл: замороженных месяцев в окне нет (только живой) — сверять нечего."
    if not discs:
        return f"✓ реконсайл: расхождений нет (сверено {len(months)} мес: {', '.join(months)})."
    out = [f"⚠ реконсайл: {len(discs)} расхождений (сверено {', '.join(months)})"
           + (" — таблица обновлена" if res["applied"] else " — БЕЗ правки (--no-apply)")]
    money = lambda v: f"{round(v):,}".replace(",", " ")
    by = {}
    for d in discs:
        by.setdefault((d["month"], d["account"]), []).append(d)
    for (ym, acc), ds in sorted(by.items()):
        out.append(f"\n{ym} · {acc}:")
        for d in ds:
            if d["line"] == "margin":
                out.append(f"    {d['label']:<18} {d['old']:.1f}% → {d['new']:.1f}%  (Δ {d['delta']:+.1f} п.п.)")
            else:
                out.append(f"    {d['label']:<18} {money(d['old'])} → {money(d['new'])}  (Δ {d['delta']:+,.0f})".replace(",", " "))
    return "\n".join(out)


def bootstrap(y1m1, y2m2):
    """Собрать hist с нуля за диапазон замороженных месяцев [y1m1 … y2m2] включительно
    (оба 'YYYY-MM'). Живой месяц (следующий за y2m2) НЕ морозим. → period_keys."""
    with _lock():
        hist = _empty_hist()
        y, m = int(y1m1[:4]), int(y1m1[5:7])
        end = y2m2
        while _key(y, m) <= end:
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
    print("provisional:", hist.get("provisional", []))
    print("live month:", R._live_month(), "| МСК сейчас:", _key(cur.year, cur.month))


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "advance"
    if cmd == "advance":
        print("changed:", advance())
    elif cmd == "reconcile":
        wk = next((int(a) for a in args[1:] if a.isdigit()), 6)
        res = reconcile_recent(weeks=wk, apply="--no-apply" not in args,
                               pull="--no-pull" not in args)
        print(_fmt_recon(res))
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
