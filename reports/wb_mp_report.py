# поток: fin
"""reports/wb_mp_report.py — живой ТЕКУЩИЙ месяц + прогноз для вкладки «Отчёты МП · WB».

Реконструкция Финансового отчёта WB (Баланс) из raw_wb_report по МЕСЯЦУ ФОРМИРОВАНИЯ
(create_dt отчёта — весь недельный отчёт падает в свой месяц формирования, модель данных ВБ),
операционные показатели (продажи/возвраты шт, средний чек) и COGS из margin_by_sku
(platform='wb') — для месяца, которого ещё нет в статическом снапшоте
(reports/data/mp_wb_hist.json). Плюс прогноз на конец месяца run-rate по скользящему окну.

Маппинг строк (сверено с ЛК ВБ, июнь-2026: К перечислению Δ<0.05%, Итого к оплате +537₽ /
+0.03% — в допуске правила 6). supplier_oper_name='Продажа'/'Возврат'; «Возврат» лежит с
ПОЛОЖИТЕЛЬНЫМ ppvz_for_pay → в К перечислению вычитается (иначе завышение 2×):
  Продажа        = Σ retail_amount где op='Продажа'
  Возврат        = Σ retail_amount где op='Возврат'
  К перечислению = Σ (op='Возврат' ? −ppvz_for_pay : ppvz_for_pay)
  Логистика      = Σ delivery_rub
  Хранение       = Σ storage_fee
  Приёмка        = Σ acceptance
  Продвижение WB = Σ deduction≥0 где bonus_type_name ~ «Продвижение»  ← реклама
  Баллы          = Σ cashback_amount
  Штрафы         = Σ penalty  (минус = сторно штрафа)
  Прочие удерж.  = Σ deduction≥0 прочих типов (списание за отзыв, утилизация)
  Компенсации ВБ = Σ (−deduction<0)  ← «Добровольная выплата за товары…» — ПРИХОД нам
  Итого к оплате = К перечислению − все удержания выше + Компенсации
  COGS           = margin_by_sku platform='wb' (себест отгрузок МС по assembly_id)
  Чистая         = Итого к оплате − COGS

⚠ ВБ формирует отчёты НЕДЕЛЬНЫМИ пачками (≈4–5/мес), поэтому MTD растёт скачками, а прогноз
по 14-дн. окну — ОЦЕНКА (окно ловит ~2 последних недельных отчёта). estimate=True.

Подсветка (cls) — те же 3 блока ±0.5σ относительно сверённых (не provisional) месяцев hist,
инверсия для расходов. DB-only, вызывается на запросе из web/app.py (/api/wb/mp-current).
"""
import json
import calendar
import datetime as dt
import pathlib

from core import db

HIST_PATH = pathlib.Path(__file__).resolve().parent / "data" / "mp_wb_hist.json"

# hist JSON — RUNTIME STATE (заморозка/run_daily переписывают без рестарта uvicorn):
# кэш с перечиткой по mtime, иначе веб залипнет на старом period_keys.
_HIST_CACHE = {"mtime": None, "data": None}


def _hist():
    try:
        mtime = HIST_PATH.stat().st_mtime
    except OSError:
        return _HIST_CACHE["data"] or {}
    if _HIST_CACHE["mtime"] != mtime:
        _HIST_CACHE["data"] = json.loads(HIST_PATH.read_text(encoding="utf-8"))
        _HIST_CACHE["mtime"] = mtime
    return _HIST_CACHE["data"]


ACCOUNTS = ("wb_acc1", "wb_acc2")
EXP = ["delivery", "storage", "acceptance", "ads", "points", "penalty", "other"]
# ^ расходы, вычитаемые из К перечислению
WINDOW_DAYS = 14
# строки Баланса (величины ≥0, знак/направление задаёт KIND).
# own_price = «Продажа по нашей цене» (retail_price_withdisc_rub, ДО СПП) — это ОБОРОТ (база %);
# sales = «ВБ реализовал» (retail_amount, ПОСЛЕ СПП, что заплатил покупатель).
_BAL_KEYS = ["own_price", "sales", "returns", "returns_pay", "to_pay", "delivery", "storage",
             "acceptance", "ads", "points", "penalty", "other", "compensation"]
MONTHS_RU = ["Янв", "Фев", "Мар", "Апр", "Май", "Июн",
             "Июл", "Авг", "Сен", "Окт", "Ноя", "Дек"]

# kind по ключу строки — формат и направление подсветки
KIND = {
    "own_price": "inflow", "sales": "inflow", "spp": "expense", "returns": "expense",
    "returns_pay": "expense",   # сторно ppvz_for_pay по возвратам — уже внутри «К перечислению»
    "commission": "expense", "to_pay": "inflow",
    "delivery": "expense", "storage": "expense", "acceptance": "expense",
    "ads": "expense",       # «Оказание услуг „WB Продвижение“» — реклама, управляемый рычаг
    "points": "expense",    # cashback_amount — баллы и лояльность
    "penalty": "expense",   # штрафы (отрицательное значение = сторно штрафа)
    "other": "expense",     # остаток: списания за отзывы, утилизация и пр.
    "compensation": "inflow",   # выплаты ВБ нам (отрицательный deduction) — приход, не расход
    "wb_exp": "expense",        # Итого расходы ВБ = наша цена − Итого к оплате (все удержания площадки)
    "itog": "inflow",           # Итого к оплате = выплата (положительная, НЕ сумма расходов как у Ozon)
    "cogs": "expense", "net": "inflow", "margin": "margin",
    "orders": "count_up", "returns_cnt": "count_dn", "check": "check",
}


# ---------- агрегат Баланса из raw_wb_report по формированию ----------
def _agg(account, d1, d2):
    """Суммы строк Баланса за полуинтервал create_dt [d1, d2) (строки 'YYYY-MM-DD').
    Один SELECT — все величины разом (деньги + шт продаж/возвратов)."""
    r = db.query(
        """SELECT
             coalesce(sum(CASE WHEN op='Продажа' THEN rpw ELSE 0 END),0) own_price,
             coalesce(sum(CASE WHEN op='Продажа' THEN ra ELSE 0 END),0) sales,
             coalesce(sum(CASE WHEN op='Возврат' THEN ra ELSE 0 END),0) returns,
             coalesce(sum(CASE WHEN op='Возврат' THEN pay ELSE 0 END),0) returns_pay,
             coalesce(sum(CASE WHEN op='Возврат' THEN -pay ELSE pay END),0) to_pay,
             coalesce(sum(del),0) delivery,
             coalesce(sum(st),0) storage,
             coalesce(sum(acc),0) acceptance,
             coalesce(sum(ads),0) ads,
             coalesce(sum(points),0) points,
             coalesce(sum(pen),0) penalty,
             coalesce(sum(oth),0) other,
             coalesce(sum(comp),0) compensation,
             coalesce(sum(CASE WHEN op='Продажа' THEN q ELSE 0 END),0) orders,
             coalesce(sum(CASE WHEN op='Возврат' THEN q ELSE 0 END),0) returns_cnt
           FROM (
             SELECT payload->>'supplier_oper_name' op,
                    coalesce((payload->>'quantity')::numeric,0) q,
                    coalesce((payload->>'retail_price_withdisc_rub')::numeric,0) rpw,
                    coalesce((payload->>'retail_amount')::numeric,0) ra,
                    coalesce((payload->>'ppvz_for_pay')::numeric,0) pay,
                    coalesce((payload->>'delivery_rub')::numeric,0) del,
                    coalesce((payload->>'storage_fee')::numeric,0) st,
                    coalesce((payload->>'acceptance')::numeric,0) acc,
                    CASE WHEN payload->>'bonus_type_name' ilike '%%продвижение%%'
                         THEN greatest(coalesce((payload->>'deduction')::numeric,0),0)
                         ELSE 0 END ads,
                    coalesce((payload->>'cashback_amount')::numeric,0) points,
                    coalesce((payload->>'penalty')::numeric,0) pen,
                    CASE WHEN payload->>'bonus_type_name' ilike '%%продвижение%%'
                         THEN 0
                         ELSE greatest(coalesce((payload->>'deduction')::numeric,0),0)
                         END oth,
                    -least(coalesce((payload->>'deduction')::numeric,0),0) comp
             FROM raw_wb_report
             WHERE account=%s
               AND (payload->>'create_dt')::date>=%s
               AND (payload->>'create_dt')::date<%s
           ) t""",
        (account, d1, d2))[0]
    return {k: float(r[k] or 0) for k in
            ("own_price", "sales", "returns", "returns_pay", "to_pay", "delivery", "storage",
             "acceptance", "ads", "points", "penalty", "other", "compensation", "orders",
             "returns_cnt")}


def _month_bounds(y, m):
    d1 = f"{y}-{m:02d}-01"
    d2 = f"{y + 1}-01-01" if m == 12 else f"{y}-{m + 1:02d}-01"
    return d1, d2


def balance(account, y, m):
    """{line: magnitude} — строки Баланса за месяц формирования (величины ≥0)."""
    a = _agg(account, *_month_bounds(y, m))
    return {k: a[k] for k in _BAL_KEYS}


def op_counts(account, y, m):
    """(шт продаж, шт возвратов) за месяц формирования."""
    a = _agg(account, *_month_bounds(y, m))
    return a["orders"], a["returns_cnt"]


def _cogs(account, y, m):
    dt_end = f"{y}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
    r = db.query("""SELECT coalesce(sum(cogs),0) c FROM margin_by_sku
        WHERE platform='wb' AND account=%s AND period_from=%s AND period_to=%s""",
        (account, f"{y}-{m:02d}-01", dt_end))
    return float(r[0]["c"])


def _month_last_day(y, m, account=None):
    """Дата последнего сформированного отчёта в месяце (max create_dt в границах).

    account=None — по всем аккаунтам (есть ли данные за месяц вообще). Для прогноза дату берём
    ПО СВОЕМУ аккаунту: общий max задаёт elapsed/remaining/окно ставки по самому свежему
    аккаунту, и отставший занижается трижды — неполный MTD × ставка, размытая днями без данных,
    × remaining, посчитанный как будто всё собрано."""
    d1, d2 = _month_bounds(y, m)
    sql = """SELECT max((payload->>'create_dt')::date) mx FROM raw_wb_report
               WHERE (payload->>'create_dt')::date>=%s AND (payload->>'create_dt')::date<%s"""
    args = [d1, d2]
    if account:
        sql += " AND account=%s"
        args.append(account)
    return db.query(sql, tuple(args))[0]["mx"]


def _live_month():
    """Живой месяц = календарный месяц СРАЗУ ПОСЛЕ последнего замороженного (max period_keys).
    None — если истории нет. Реальность данных проверяется в current_report (last==None → пусто)."""
    keys = _hist().get("period_keys", [])
    if not keys:
        return None
    ym = max(keys)
    y, m = int(ym[:4]), int(ym[5:7])
    return (y + 1, 1) if m == 12 else (y, m + 1)


# ---------- форматирование / подсветка ----------
def _money(v, neg=False):
    """neg=True — расходная строка: положительная величина печатается со знаком «−».
    ОТРИЦАТЕЛЬНЫЙ расход = деньги пришли нам → печатаем «+», а не «−» (иначе колонка
    не сходится глазами: подытоги считаются по знаковому значению)."""
    v = round(v); s = f"{abs(v):,}".replace(",", " ")
    if neg:
        return ("" if v == 0 else "+" if v < 0 else "−") + s
    return ("−" if v < 0 else "") + s


def _fmt(key, v):
    if v is None:
        return "—"
    k = KIND[key]
    if k == "margin":
        return f"{v:.1f}%"
    if k in ("count_up", "count_dn"):
        return f"{round(v):,}".replace(",", " ")
    return _money(v, neg=(k == "expense"))


def _basis(key, v, oborot):
    return (v / oborot * 100 if oborot else 0) if KIND[key] == "expense" else v


def _good_up(key):
    return KIND[key] not in ("expense", "count_dn")


def _hist_series(acc, key):
    """Ряд значений (через _basis) для эталона подсветки — только по НЕ-provisional месяцам."""
    H = _hist()
    a = H["accounts"][acc]; L = a["lines"]; ob = L["own_price"]; n = len(ob)   # оборот = наша цена
    keys = H.get("period_keys", [])
    prov = set(H.get("provisional", []))
    idx = [i for i in range(n) if i >= len(keys) or keys[i] not in prov]
    comp = L.get("compensation") or [0] * n          # совместимость со снапшотом без строки
    if key in _BAL_KEYS:
        vals = L.get(key) or [0] * n
    elif key == "spp":
        vals = [L["own_price"][i] - L["sales"][i] for i in range(n)]
    elif key == "itog":
        vals = [L["to_pay"][i] - sum(L[x][i] for x in EXP) + comp[i] for i in range(n)]
    elif key == "wb_exp":
        retp = L.get("returns_pay") or [0] * n        # снапшот до 19.08.2026 — строки нет
        vals = [L["own_price"][i] - (L["to_pay"][i] - sum(L[x][i] for x in EXP) + comp[i]) - retp[i]
                for i in range(n)]
    elif key == "commission":
        vals = a["commission"]
    elif key in ("cogs", "net", "margin", "orders", "returns_cnt"):
        vals = a[key]
    elif key == "check":
        vals = [L["own_price"][i] / a["orders"][i] if a["orders"][i] else 0 for i in range(n)]
    else:
        vals = [0] * n
    return [_basis(key, vals[i], ob[i]) for i in idx]


def _band(acc, key, v, oborot_cur):
    """Класс ячейки vs исторический ряд сверённых месяцев: g/a (цвет) + up/dn (стрелка)."""
    if v is None:
        return ""
    hs = _hist_series(acc, key)
    if not hs:
        return ""
    mean = sum(hs) / len(hs)
    std = (sum((x - mean) ** 2 for x in hs) / len(hs)) ** 0.5
    if std == 0:
        return ""
    b = _basis(key, v, oborot_cur)
    d = b - mean
    if abs(d) <= 0.5 * std:
        return ""
    gu = _good_up(key)
    qual = ("g" if d > 0 else "a") if gu else ("a" if d > 0 else "g")
    return f"{qual} {'up' if d > 0 else 'dn'}"


def _derive(mags, orders, retc, cogs):
    """spp/commission/itog/net/margin/check из строк Баланса. ОБОРОТ = наша цена (own_price).
    СПП (скидка ВБ за свой счёт) = наша цена − ВБ реализовал. Комиссия ВБ (реальная удержка,
    БЕЗ СПП) = ВБ реализовал − Возврат − К перечислению. itog = К перечислению − расходы."""
    own = mags["own_price"]
    spp = own - mags["sales"]
    commission = mags["sales"] - mags["returns"] - mags["to_pay"]
    itog = mags["to_pay"] - sum(mags[k] for k in EXP) + mags.get("compensation", 0.0)
    # Удержания ВБ БЕЗ возвратов: сторно возвратов (ppvz_for_pay по операции «Возврат») уже сидит
    # внутри to_pay, поэтому вычитаем его явно — возврат не услуга площадки, а сторно продажи,
    # и внутри «Итого удержания» он гулял бы вместе с долей возвратов (единое правило со всеми МП).
    wb_exp = own - itog - mags.get("returns_pay", 0.0)
    net = itog - cogs
    return {**mags, "spp": spp, "commission": commission, "wb_exp": wb_exp, "itog": itog,
            "cogs": cogs, "orders": orders, "returns_cnt": retc, "net": net,
            "margin": (net / own * 100 if own else 0),
            "check": (own / orders if orders else 0)}


def _lag_note(last_acc):
    """Текст-предупреждение, если аккаунты собраны на РАЗНЫЕ даты (иначе пустая строка).
    Готовим на стороне Python, чтобы JS страницы остался тривиальным (см. *_mp_page)."""
    have = {a: d for a, d in last_acc.items() if d}
    if len(set(have.values())) < 2:
        return ""
    parts = ", ".join(f"{a} — по {d.isoformat()}" for a, d in sorted(have.items()))
    empty = [a for a, d in last_acc.items() if not d]
    if empty:
        parts += ", " + ", ".join(f"{a} — данных за месяц нет" for a in sorted(empty))
    return ("<b>Данные собраны неравномерно</b> (" + parts +
            "): факт и прогноз каждого аккаунта считаются от СВОЕЙ даты, поэтому столбцы аккаунтов "
            "покрывают разное число дней. ")


def current_report():
    """{"month": {...}|None, "accounts": {acc: {line_key: {"cur":{txt,cls},"fc":{txt,cls}}}}}"""
    lm = _live_month()
    if not lm:
        return {"month": None, "accounts": {}}
    y, m = lm
    last = _month_last_day(y, m)
    if not last:                       # живой месяц ещё без отчётов — не показываем пустой столбец
        return {"month": None, "accounts": {}}
    days_in = calendar.monthrange(y, m)[1]
    elapsed = last.day
    remaining = days_in - elapsed

    out, last_acc = {}, {}
    for acc in ACCOUNTS:
        # Своя дата последних данных на каждый аккаунт (см. _month_last_day). Аккаунт без данных
        # в месяце: elapsed=0, а окно ставки якорим по общей дате — иначе якорить не к чему.
        la = _month_last_day(y, m, acc)
        last_acc[acc] = la
        anchor = la or last
        elapsed_a = la.day if la else 0
        remaining_a = days_in - elapsed_a
        # окно скользящей ставки: WINDOW_DAYS дней, кончая последним днём формирования. Свободно
        # пересекает границу месяца (в начале августа окно ещё захватывает июльские отчёты).
        w1 = (anchor - dt.timedelta(days=WINDOW_DAYS - 1)).isoformat()
        w2 = (anchor + dt.timedelta(days=1)).isoformat()
        mtd = _agg(acc, *_month_bounds(y, m))           # факт MTD (по формированию)
        cogs = _cogs(acc, y, m)
        actual = _derive(mtd, mtd["orders"], mtd["returns_cnt"], cogs)
        # Прогноз = факт MTD + дневная ставка окна × оставшиеся дни. Ставка из скользящего окна
        # прошлого (WINDOW_DAYS дн) → нет взрыва factor в начале месяца, сходится к факту в конце.
        win = _agg(acc, w1, w2)
        rate = {k: win[k] / WINDOW_DAYS for k in win}
        fc = {k: mtd[k] + rate[k] * remaining_a for k in
              ("own_price", "sales", "returns", "returns_pay", "to_pay", "delivery", "storage",
               "acceptance", "ads", "points", "penalty", "other", "compensation")}
        fc_orders = mtd["orders"] + rate["orders"] * remaining_a
        fc_retc = mtd["returns_cnt"] + rate["returns_cnt"] * remaining_a
        # COGS привязана к продажам (margin_by_sku помесячный): fc_cogs = fc_sales × (COGS÷Прод MTD).
        cogs_ratio = cogs / mtd["sales"] if mtd["sales"] else 0
        fc_cogs = fc["sales"] * cogs_ratio
        forecast = _derive(fc, fc_orders, fc_retc, fc_cogs)

        cells = {}
        for key in KIND:
            cv, fv = actual.get(key), forecast.get(key)
            # столбец «тек.» (неполный месяц): подсвечиваем ТОЛЬКО относительные статьи
            # (доли расходов/маржа/чек); абсолютные MTD заведомо ниже полного месяца.
            cur_cls = _band(acc, key, cv, actual["own_price"]) if KIND[key] in ("expense", "margin", "check") else ""
            cells[key] = {
                "cur": {"txt": _fmt(key, cv), "cls": cur_cls},
                "fc": {"txt": _fmt(key, fv), "cls": _band(acc, key, fv, forecast["own_price"])},
            }
        out[acc] = cells

    return {
        "month": {"label": MONTHS_RU[m - 1], "month_key": f"{y}-{m:02d}",
                  "elapsed_days": elapsed, "days_in_month": days_in,
                  "remaining_days": remaining, "window_days": WINDOW_DAYS,
                  "last_date": last.isoformat(), "estimate": True,
                  "last_by_acc": {a: (d.isoformat() if d else None) for a, d in last_acc.items()},
                  "lag_note": _lag_note(last_acc)},
        "accounts": out,
    }


if __name__ == "__main__":
    r = current_report()
    mo = r["month"]
    print("месяц:", mo)
    for acc in ACCOUNTS:
        c = r["accounts"].get(acc)
        if not c:
            continue
        print(f"\n{acc}: Прод cur={c['sales']['cur']['txt']} fc={c['sales']['fc']['txt']} "
              f"| К_переч {c['to_pay']['cur']['txt']}→{c['to_pay']['fc']['txt']} "
              f"| Итого {c['itog']['cur']['txt']}→{c['itog']['fc']['txt']} "
              f"| шт {c['orders']['cur']['txt']}→{c['orders']['fc']['txt']} "
              f"| чек {c['check']['cur']['txt']}→{c['check']['fc']['txt']} "
              f"| COGS {c['cogs']['cur']['txt']}→{c['cogs']['fc']['txt']} "
              f"| маржа {c['margin']['cur']['txt']}→{c['margin']['fc']['txt']} "
              f"| чистая {c['net']['cur']['txt']}→{c['net']['fc']['txt']}")
