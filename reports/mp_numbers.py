# поток: fin
"""Числовой фасад «Отчётов МП»: те же цифры, что нарисованы на вкладке, но числами.

Зачем. Вкладка «Отчёты МП» — единственное место, где месяц собран по форме площадки и сверен
с личным кабинетом. Главная страница до этого считала свои показатели вторым путём (витрина
`margin_by_sku` для ВБ, транзакции для Ozon, сырые колонки `yandex_finance_monthly` для Маркета),
и по Маркету расходилась с отчётом в разы (июль-2026: чистая 1 141 347 против 115 955 — в выручку
попадала субсидия, а половина услуг и возвраты не вычитались). Здесь один вход для верхнего
уровня: `month(platform, period)`.

Форма — общая для трёх площадок (решение 19.08.2026, «единая форма Отчётов МП»):
    oborot  — оборот по НАШЕЙ цене (ВБ `own_price`, Ozon `sales`, Маркет `own` = выручка + зачёт);
    payout  — итого к перечислению после удержаний площадки и возвратов;
    net     — чистая = payout − COGS;
    margin  — чистая к обороту.
Страницы вкладки строят те же числа теми же `balance()/op_counts()/_cogs()/_derive()`.

Закрытые месяцы модули читают из снапшотов (`reports/data/mp_*_hist.json`), живой месяц считают
из БД (ВБ ~1,3 с). Поэтому здесь TTL-кэш: дашборд обновляется ночью, минуты расхождения не важны.
"""
from __future__ import annotations

import threading
import time

from reports import ozon_mp_report as _oz
from reports import wb_mp_report as _wb
from reports import yandex_mp_report as _ya

ACCOUNTS = {"wb": ("wb_acc1", "wb_acc2"), "ozon": ("oz_acc1", "oz_acc2"), "yandex": ("ya_acc1",)}
# ключ строки «Оборот» в каждой площадке (на вкладке — «Продажи — оборот (наша цена)»)
_OBOROT = {"wb": "own_price", "ozon": "sales", "yandex": "own"}
# «Итого к перечислению»: у ВБ это itog, у Ozon и Маркета — payout
_PAYOUT = {"wb": "itog", "ozon": "payout", "yandex": "payout"}

TTL_LIVE = 300       # текущий месяц ещё доезжает — обновляем чаще
TTL_CLOSED = 3600    # закрытый месяц меняется только пересбором снапшота
_cache: dict = {}
_lock = threading.Lock()


def _ttl(y, m):
    t = time.localtime()
    return TTL_LIVE if (y, m) == (t.tm_year, t.tm_mon) else TTL_CLOSED


def _derive_acc(platform, acc, y, m):
    """Строки Баланса одного аккаунта за месяц, прогнанные через ту же `_derive`, что и страница."""
    if platform == "wb":
        return _wb._derive(_wb.balance(acc, y, m), *_wb.op_counts(acc, y, m), _wb._cogs(acc, y, m))
    if platform == "ozon":
        return _oz._derive(_oz.balance(acc, y, m), *_oz.op_counts(acc, y, m), _oz._cogs(acc, y, m))
    return _ya._derive(_ya.balance(y, m), *_ya.op_counts(y, m), _ya._cogs(y, m))


def _pack(platform, d):
    ob = float(d.get(_OBOROT[platform]) or 0)
    net = float(d.get("net") or 0)
    cogs = float(d.get("cogs") or 0)
    return {"oborot": round(ob, 2), "net": round(net, 2), "cogs": round(cogs, 2),
            "payout": round(float(d.get(_PAYOUT[platform]) or 0), 2),
            "itog": round(float(d.get("itog") or 0), 2),
            "orders": int(d.get("orders") or 0), "returns_cnt": int(d.get("returns_cnt") or 0),
            "margin_pct": round(net / ob * 100, 1) if ob else None,
            "cogs_pct": round(cogs / ob * 100, 1) if ob else None}


def _compute(platform, y, m):
    accs = {}
    for a in ACCOUNTS[platform]:
        try:
            accs[a] = _pack(platform, _derive_acc(platform, a, y, m))
        except Exception:                      # нет данных по аккаунту за месяц — не роняем главную
            continue
    if not accs:
        return None
    out = {k: round(sum(v[k] or 0 for v in accs.values()), 2)
           for k in ("oborot", "net", "cogs", "payout", "itog")}
    out["orders"] = sum(v["orders"] for v in accs.values())
    out["returns_cnt"] = sum(v["returns_cnt"] for v in accs.values())
    ob = out["oborot"]
    out["margin_pct"] = round(out["net"] / ob * 100, 1) if ob else None
    out["cogs_pct"] = round(out["cogs"] / ob * 100, 1) if ob else None
    out["accounts"] = accs
    return out


def month(platform: str, period: str):
    """Числа вкладки «Отчёты МП» за месяц. `period` — 'YYYY-MM-DD' или 'YYYY-MM'.

    Возвращает None, если за месяц по площадке нет ни одного аккаунта с данными."""
    if platform not in ACCOUNTS or not period or len(period) < 7:
        return None
    y, m = int(period[:4]), int(period[5:7])
    key = (platform, y, m)
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _ttl(y, m):
            return hit[1]
    val = _compute(platform, y, m)
    with _lock:
        _cache[key] = (now, val)
    return val


def business(period: str):
    """Все три площадки + ИТОГ по бизнесу на одной базе — то, что показывает главная."""
    parts = {p: month(p, period) for p in ("wb", "ozon", "yandex")}
    have = {p: v for p, v in parts.items() if v}
    tot = {k: round(sum(v[k] for v in have.values()), 2) for k in ("oborot", "net", "cogs", "payout")}
    ob = tot["oborot"]
    tot["margin_pct"] = round(tot["net"] / ob * 100, 1) if ob else None
    tot["cogs_pct"] = round(tot["cogs"] / ob * 100, 1) if ob else None
    return {"platforms": parts, "total": tot}
