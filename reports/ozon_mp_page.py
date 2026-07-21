# -*- coding: utf-8 -*-
"""reports/ozon_mp_page.py — генератор страницы «Отчёты МП · Ozon» (web/static/reports.html)
из reports/data/mp_ozon_hist.json. Продуктивизация scratchpad/gen_reports.py: пути
__file__-относительные, `render()` пишет файл АТОМАРНО (os.replace из temp), поддержка
provisional-месяцев (заморожены из оценки, ещё не сверены с Отчётом о реализации → сплит «—»,
метка `*` в шапке, исключены из эталона подсветки).

Вызывается из reports/ozon_mp_freeze при каждой заморозке/сверке. Оболочка дашборда (тёмная
палитра, сайдбар, вкладки) + таблицы Финансы→Баланс под #mpr; два правых столбца (тек.*, прогноз)
дорисовывает JS из /api/ozon/mp-current.
"""
import json
import os
import tempfile
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
HIST_PATH = BASE_DIR / "reports" / "data" / "mp_ozon_hist.json"
OUT = BASE_DIR / "web" / "static" / "reports.html"

ORG = {"oz_acc1": "Цифровой квадрат", "oz_acc2": "Дисквэр"}
EXP = ["returns", "commission", "delivery", "partners", "fbo", "promo", "penalty"]

# контекст рендера (заполняется в render(), читается хелперами — как globals в оригинале)
_C = {"data": None, "M": [], "N": 0, "base": [], "prov": set()}


def money(v, neg=False):
    if v is None:
        return "—"
    v = round(v); s = f"{abs(v):,}".replace(",", " ")
    return ("−" if (neg or v < 0) else "") + s


def _base_stats(comp):
    """(mean, std) по НЕ-provisional индексам эталона; None-значения игнорируются."""
    xs = [comp[i] for i in _C["base"] if comp[i] is not None]
    if not xs:
        return None, None
    mean = sum(xs) / len(xs)
    std = (sum((x - mean) ** 2 for x in xs) / len(xs)) ** 0.5
    return mean, std


def spark(vals, good_up):
    pts_vals = [(i, v) for i, v in enumerate(vals) if v is not None]
    if len(pts_vals) < 2:
        return ""
    xs = [v for _, v in pts_vals]
    lo, hi = min(xs), max(xs); rng = (hi - lo) or 1
    N = _C["N"]
    pts = [f"{i * (62 / (N - 1)):.1f},{15 - ((v - lo) / rng) * 13 + 1:.1f}" for i, v in pts_vals]
    up = xs[-1] > xs[0]
    good = up if good_up else (not up)
    col = "var(--pos)" if good else "var(--neg)"
    lx, ly = pts[-1].split(",")
    return (f'<svg width="62" height="17" viewBox="0 0 62 17"><polyline points="{" ".join(pts)}" '
            f'fill="none" stroke="{col}" stroke-width="1.5"/><circle cx="{lx}" cy="{ly}" r="2" fill="{col}"/></svg>')


def bands(comp, good_up):
    """Три блока относительно среднего СВЕРЕННЫХ месяцев (±0.5σ). Классы ячеек для всех столбцов;
    provisional-эталон исключён (см. _base_stats), None → без класса."""
    mean, std = _base_stats(comp)
    out = []
    for v in comp:
        if v is None or mean is None or std == 0 or abs(v - mean) <= 0.5 * std:
            out.append("")
        else:
            d = v - mean
            qual = ("g" if d > 0 else "a") if good_up else ("a" if d > 0 else "g")
            out.append(f"{qual} {'up' if d > 0 else 'dn'}")
    return out


def row(label, vals, kind, oborot, tag="", showpc=False, sub=False, subtot=False, sect_pct=None, k=""):
    """kind: inflow|expense|margin|count_up|count_dn|check. None в vals → ячейка «—» (provisional
    сплит). k — line_key для JS-дозагрузки живых столбцов из /api/ozon/mp-current."""
    N = _C["N"]
    shares = [(vals[i] / oborot[i] * 100 if (vals[i] is not None and oborot[i]) else None) for i in range(N)]
    if kind == "expense":
        comp, good_up = shares, False
    elif kind == "count_dn":
        comp, good_up = vals, False
    else:                                    # inflow | margin | count_up | check
        comp, good_up = vals, True
    bd = bands(comp, good_up)
    tds = []
    for i in range(N):
        if vals[i] is None:
            tds.append('<td class="num muted">—</td>')
            continue
        if kind == "margin":
            txt = f"{vals[i]:.1f}%"
        elif kind in ("count_up", "count_dn"):
            txt = f"{round(vals[i]):,}".replace(",", " ")
        else:
            txt = money(vals[i], neg=(kind == "expense"))
        pc = f'<span class="pc">{shares[i]:.1f}%</span>' if (showpc and shares[i] is not None) else ""
        tds.append(f'<td class="num {bd[i]}">{txt}{pc}</td>')
    if kind in ("margin", "count_up", "count_dn", "check"):
        rp = "—"
    else:
        tot = sum(v for v in vals if v is not None); tob = sum(oborot)
        rp = f"{(tot / tob * 100 if tob else 0):.1f}%" if sect_pct is None else sect_pct
    sp = spark(comp, good_up)
    trclass = "sub" if sub else ("subtot" if subtot else "")
    lblcls = "lbl ind" if sub else "lbl"
    tg = f' <span class="tag {tag}">{tag}</span>' if tag else ""
    live = ('<td class="num live" data-c="jul">·</td>'
            '<td class="num live fc" data-c="fc">·</td>')
    dk = f' data-k="{k}"' if k else ""
    return (f'<tr class="{trclass}"{dk}><td class="{lblcls}">{label}{tg}</td>' + "".join(tds) +
            live + f'<td class="num pctcol">{rp}</td><td class="spk">{sp}</td></tr>')


def sect(t):
    return f'<tr class="sect"><td colspan="12">{t}</td></tr>'


def bars_line(oborot, net):
    M, N = _C["M"], _C["N"]
    hi = max(oborot); W, H = 500, 140
    out = []
    for i in range(N):
        x = 39.1 + i * 75.3; h = (oborot[i] / hi) * 96 + 2; y = 128 - h
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="45.2" height="{h:.1f}" rx="3" fill="var(--acc)" opacity=".85"/>'
                   f'<text x="{x + 22.6:.1f}" y="138" text-anchor="middle" class="axt">{M[i]}</text>')
    nl = []
    for i in range(N):
        x = 61.7 + i * 75.3; y = 128 - (net[i] / hi) * 96 - 2; nl.append(f"{x:.1f},{y:.1f}")
        out.append(f"<circle cx={x:.1f} cy={y:.1f} r=2.3 fill='var(--warn)'/>")
    poly = f'<polyline points="{" ".join(nl)}" fill="none" stroke="var(--warn)" stroke-width="2"/>'
    return (f'<svg viewBox="0 0 {W} {H}" width="100%">' + "".join(out[:2 * N]) + poly +
            "".join(o for o in out if o.startswith("<circle")) + '</svg>')


def line2(a, b, c=None, amax=None):
    M, N = _C["M"], _C["N"]
    W, H = 500, 140
    ser = [(a, "var(--pos)"), (b, "var(--neg)")] + ([(c, "var(--acc)")] if c is not None else [])
    hi = amax or max(max(s) for s, _ in ser); lo = 0

    def pl(vals, col):
        pts = [f"{24 + i * 90.4:.1f},{128 - ((v - lo) / ((hi - lo) or 1)) * 95:.1f}" for i, v in enumerate(vals)]
        cs = "".join(f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="2.3" fill="{col}"/>' for p in pts)
        return f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" stroke-width="2"/>' + cs
    ax = "".join(f'<text x="{24 + i * 90.4:.1f}" y="138" text-anchor="middle" class="axt">{M[i]}</text>' for i in range(N))
    return (f'<svg viewBox="0 0 {W} {H}" width="100%">' + "".join(pl(s, col) for s, col in ser)
            + ax + '</svg>')


def build(acc):
    data, M, N, prov = _C["data"], _C["M"], _C["N"], _C["prov"]
    a = data["accounts"][acc]; L = a["lines"]; ob = L["sales"]
    cogs = a["cogs"]; net = a["net"]; margin = a["margin"]
    cogs_pct = [(cogs[i] / ob[i] * 100 if ob[i] else 0) for i in range(N)]
    itog = [sum(L[k][i] for k in EXP) for i in range(N)]
    itog_pct = [(itog[i] / ob[i] * 100 if ob[i] else 0) for i in range(N)]
    # сплит может быть null у provisional-месяцев (нет Отчёта о реализации) → None → «—»
    rev = [(s["rev"] if s else None) for s in a["split"]]
    bon = [(s["bonus"] if s else None) for s in a["split"]]
    par = [(s["part"] if s else None) for s in a["split"]]
    orders = a["orders"]; retc = a["returns_cnt"]
    check = [(ob[i] / orders[i] if orders[i] else 0) for i in range(N)]
    H = []
    # агрегаты hero/чартов — по сверённым месяцам (provisional-оценка не искажает средние)
    base = _C["base"] or list(range(N))
    tot_ob = sum(ob[i] for i in base); tot_net = sum(net[i] for i in base)
    avg_m = tot_net / tot_ob * 100 if tot_ob else 0
    avg_cogs = sum(cogs[i] for i in base) / tot_ob * 100 if tot_ob else 0
    nmon = len(base)
    H.append(f'<section class="org"><h2><span class="orgdot"></span>{ORG[acc]} '
             f'<span class="muted" style="font-weight:400;font-size:13px">· Ozon</span></h2>')
    H.append('<div class="hero">'
             f'<div class="cell"><div class="big">{money(tot_ob)} ₽</div><div class="lbl">оборот {nmon} мес</div></div>'
             f'<div class="cell"><div class="big">{money(tot_net)} ₽</div><div class="lbl">чистая {nmon} мес</div></div>'
             f'<div class="cell"><div class="big" style="color:var(--warn)">{avg_m:.1f}%</div><div class="lbl">маржа средняя</div></div>'
             f'<div class="cell"><div class="big" style="color:var(--neg)">{avg_cogs:.1f}%</div><div class="lbl">COGS от оборота</div></div></div>')
    H.append('<div class="charts">'
             f'<div class="chart"><h3>Оборот и чистая</h3>{bars_line(ob, net)}'
             '<div class="leg"><span><i style="border-color:var(--acc)"></i>оборот</span>'
             '<span><i style="border-color:var(--warn)"></i>чистая</span></div></div>'
             f'<div class="chart"><h3>Маржа, COGS и расходы Ozon</h3>{line2(margin, cogs_pct, itog_pct)}'
             '<div class="leg"><span><i style="border-color:var(--pos)"></i>маржа %</span>'
             '<span><i style="border-color:var(--neg)"></i>COGS %</span>'
             '<span><i style="border-color:var(--acc)"></i>расходы Ozon %</span></div></div></div>')
    jul_ttl = "Текущий месяц — оценка по транзакциям до выхода Отчёта о реализации"
    fc_ttl = "Прогноз на конец месяца (факт + дневная ставка за скользящее окно × остаток дней)"
    prov_ttl = "Оценка по транзакциям — ещё не сверено с Отчётом о реализации"
    mth = "".join((f'<th title="{prov_ttl}">{M[i]}*</th>' if M[i] and i < N and _C["provkeys"][i] in prov
                   else f"<th>{M[i]}</th>") for i in range(N))
    H.append('<div class="card"><table><thead><tr><th>Статья Финансы → Баланс</th>'
             + mth
             + f'<th class="live" title="{jul_ttl}">тек.*</th>'
             + f'<th class="live" title="{fc_ttl}">прогноз</th>'
             + '<th>% Об.</th><th>Тренд</th></tr></thead><tbody>')
    H.append(sect("Операционные показатели"))
    H.append(row("Заказы, шт", orders, "count_up", ob, k="orders"))
    H.append(row("Возвраты, шт", retc, "count_dn", ob, k="returns_cnt"))
    H.append(row("Средний чек, ₽", check, "check", ob, tag="расчёт", k="check"))
    H.append(sect("Продажи (оборот)"))
    H.append(row("Продажи — оборот", ob, "inflow", ob, sect_pct="100.0%", k="sales"))
    H.append(row("Выручка (деньги покупателя)", rev, "inflow", ob, sub=True, k="rev"))
    H.append(row("Баллы за скидки (за счёт Озон)", bon, "inflow", ob, sub=True, showpc=True, k="bon"))
    H.append(row("Программы партнёров", par, "inflow", ob, sub=True, k="par"))
    H.append(sect("Расходы площадки (удержания)"))
    H.append(row("Возвраты", L["returns"], "expense", ob, k="returns"))
    H.append(row("Вознаграждение Ozon", L["commission"], "expense", ob, showpc=True, k="commission"))
    H.append(row("Услуги доставки (логистика)", L["delivery"], "expense", ob, showpc=True, k="delivery"))
    H.append(row("Услуги партнёров", L["partners"], "expense", ob, k="partners"))
    H.append(row("Услуги ФБО", L["fbo"], "expense", ob, k="fbo"))
    H.append(row("Продвижение и реклама", L["promo"], "expense", ob, showpc=True, k="promo"))
    H.append(row("Другие услуги и штрафы", L["penalty"], "expense", ob, k="penalty"))
    H.append(row("Итого расходы Ozon", itog, "expense", ob, tag="расчёт", showpc=True, subtot=True, k="itog"))
    H.append(sect("Начисления в плюс"))
    H.append(row("Компенсации и декомпенсации", L["compensation"], "inflow", ob, k="compensation"))
    H.append(row("Прочие начисления", L["other"], "inflow", ob, k="other"))
    H.append(sect("Наши данные (не из отчёта МП)"))
    H.append(row("Себестоимость (COGS)", cogs, "expense", ob, tag="наша", showpc=True, k="cogs"))
    H.append(sect("Итог (расчёт над константами)"))
    H.append(row("Чистая прибыль", net, "inflow", ob, tag="расчёт", k="net"))
    H.append(row("Маржа", margin, "margin", ob, tag="расчёт", k="margin"))
    H.append('</tbody></table></div></section>')
    return "".join(H)


SHELL_CSS = """
:root{--bg:#0f1720;--card:#172230;--line:#243449;--txt:#e6edf3;--mut:#8aa0b5;--acc:#3fa7ff;--pos:#37c871;--neg:#ff5d5d;--warn:#ffb454;
 --panel:#172230;--ink:#e6edf3;--acc-s:#16273c;--pos-s:#13291d;--neg-s:#2f1719;--warn-s:#2f2611}
*{box-sizing:border-box}
body{margin:0;font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--txt);display:flex;min-height:100vh}
header{width:216px;flex:0 0 216px;height:100vh;position:sticky;top:0;overflow:auto;padding:16px 12px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:3px}
.lvlnav{display:flex;flex-direction:column;gap:3px}
.lvlnav a{color:var(--mut);text-decoration:none;border-radius:8px;padding:8px 11px;font-size:14px}
.lvlnav a:hover{background:var(--bg);color:var(--txt)}
.lvlnav a.cur{color:var(--txt);background:var(--bg);box-shadow:inset 3px 0 0 var(--acc)}
.logo{font-size:15px;font-weight:700;color:var(--txt);text-decoration:none;padding:9px 11px;border-radius:8px;margin-bottom:4px}
.logo:hover{background:var(--card)}
.navgroup{color:var(--mut);font-size:11px;text-transform:uppercase;letter-spacing:.04em;margin:13px 0 2px;padding:0 11px}
main{flex:1;min-width:0;padding:22px 26px 60px;max-width:1200px}
.mptabs{display:flex;gap:4px;flex-wrap:wrap;margin-bottom:2px}
.mptabs a{color:var(--mut);text-decoration:none;padding:7px 13px;border-radius:8px;font-weight:600}
.mptabs a:hover{background:var(--card);color:var(--txt)}
.mptabs a.cur{background:var(--card);color:var(--txt);box-shadow:inset 0 -2px 0 var(--acc)}
.rtabs{display:flex;gap:4px;flex-wrap:wrap;margin:16px 0 2px;border-bottom:1px solid var(--line)}
.rtab{color:var(--mut);text-decoration:none;padding:7px 13px;border-radius:8px 8px 0 0;font-weight:600;font-size:13px}
.rtab:hover{color:var(--txt);background:var(--card)}
.rtab.cur{color:var(--txt);background:var(--card);box-shadow:inset 0 -2px 0 var(--acc)}
.rtab.soon{opacity:.45;cursor:default}
.rtab.soon:hover{background:none;color:var(--mut)}
"""

REPORT_CSS = """
#mpr{padding-top:4px}
#mpr .eyebrow{font-size:11.5px;letter-spacing:.15em;text-transform:uppercase;color:var(--acc);font-weight:700;margin:14px 0 9px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
#mpr h1{font-size:clamp(22px,3vw,30px);line-height:1.1;margin:0 0 8px;font-weight:720;letter-spacing:-.02em}
#mpr .sub{color:var(--mut);font-size:14px;margin:0 0 12px;max-width:84ch}
#mpr .muted{color:var(--mut)}
#mpr section.org{margin-top:30px;padding-top:8px}
#mpr h2{font-size:18px;margin:0 0 14px;font-weight:680;letter-spacing:-.01em;display:flex;align-items:center;gap:9px}
#mpr .orgdot{width:10px;height:10px;border-radius:3px;background:var(--acc);display:inline-block}
#mpr .hero{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1px;background:var(--line);border:1px solid var(--line);border-radius:14px;overflow:hidden;margin-bottom:16px}
#mpr .cell{background:var(--card);padding:15px 17px}
#mpr .big{font-size:clamp(19px,2.5vw,25px);font-weight:720;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
#mpr .cell .lbl{font-size:12px;color:var(--mut);margin-top:3px}
#mpr .charts{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:760px){#mpr .charts{grid-template-columns:1fr}}
#mpr .chart{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:13px 15px}
#mpr .chart h3{margin:0 0 8px;font-size:13px;font-weight:640;color:var(--txt)}
#mpr .axt{fill:var(--mut);font-size:9px}
#mpr .leg{display:flex;gap:13px;font-size:11.5px;color:var(--mut);margin-top:5px;flex-wrap:wrap}
#mpr .leg i{display:inline-block;width:18px;height:0;border-top:2px solid;vertical-align:middle;margin-right:4px}
#mpr .card{background:var(--card);border:1px solid var(--line);border-radius:14px;overflow-x:auto}
#mpr table{width:100%;border-collapse:collapse;font-size:13px;min-width:860px}
#mpr th,#mpr td{padding:7px 11px;text-align:right;border-bottom:1px solid var(--line);white-space:nowrap;font-variant-numeric:tabular-nums}
#mpr thead th{position:sticky;top:0;background:var(--card);font-size:11px;text-transform:uppercase;letter-spacing:.03em;color:var(--mut);font-weight:650;z-index:2}
#mpr th:first-child,#mpr td.lbl{text-align:left;white-space:normal}
#mpr td.lbl{font-weight:560;min-width:205px}
#mpr td.lbl.ind{font-weight:430}
#mpr tr.sub td{color:var(--mut)}
#mpr tr.sub td.lbl.ind{padding-left:24px;position:relative}
#mpr tr.sub td.lbl.ind::before{content:"└";position:absolute;left:10px;color:var(--line)}
#mpr tr.sect td{background:var(--acc-s);color:var(--acc);font-weight:700;font-size:11px;text-transform:uppercase;letter-spacing:.06em;text-align:left;padding:6px 11px}
#mpr td.num{position:relative;padding-right:22px}
#mpr td.num.g{background:var(--pos-s)}
#mpr td.num.a{background:var(--warn-s)}
/* стрелка тренда — в фикс. правом жёлобе (position:absolute), чтобы число было выровнено
   одинаково и в ячейках со стрелкой, и без неё (иначе inline-стрелка сдвигает число влево) */
#mpr td.num.up::after{content:"▲";position:absolute;right:7px;top:7px;font-size:9px}
#mpr td.num.dn::after{content:"▼";position:absolute;right:7px;top:7px;font-size:9px}
#mpr td.num.g.up::after,#mpr td.num.g.dn::after{color:var(--pos)}
#mpr td.num.a.up::after,#mpr td.num.a.dn::after{color:var(--warn)}
#mpr tr.subtot td{border-top:2px solid var(--acc);font-weight:700}
#mpr tr.subtot td.lbl{color:var(--txt)}
#mpr .pc{display:block;font-size:10px;color:var(--mut);font-weight:600;margin-top:1px}
#mpr td.pctcol{color:var(--mut);font-weight:600;border-left:1px solid var(--line)}
#mpr td.spk{padding:2px 8px}
#mpr .tag{font-size:9.5px;padding:1px 6px;border-radius:20px;margin-left:6px;font-weight:600;vertical-align:middle}
#mpr .tag.наша{background:var(--acc-s);color:var(--acc)}
#mpr .tag.расчёт{background:var(--neg-s);color:var(--neg)}
#mpr .tlegend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin:12px 2px 0}
#mpr .tlegend b{color:var(--txt);font-weight:600}
#mpr .sw{display:inline-block;width:11px;height:11px;border-radius:3px;vertical-align:middle;margin-right:4px}
#mpr .foot{margin-top:30px;padding-top:16px;border-top:1px solid var(--line);color:var(--mut);font-size:12.5px;line-height:1.7}
#mpr th.live{color:var(--acc)}
#mpr td.num.live{border-left:1px solid var(--line)}
#mpr td.num.fc{color:var(--mut);font-style:italic}
#mpr .livenote{margin:14px 2px 0;font-size:12.5px;color:var(--mut);line-height:1.6}
#mpr .livenote b{color:var(--txt);font-weight:600}
#mpr details.howto{margin:0 0 12px}
#mpr details.howto>summary{cursor:pointer;color:var(--acc);font-size:13px;font-weight:600;
  list-style:none;display:inline-flex;align-items:center;gap:6px;user-select:none}
#mpr details.howto>summary::-webkit-details-marker{display:none}
#mpr details.howto>summary::before{content:"▸";font-size:11px}
#mpr details.howto[open]>summary::before{content:"▾"}
#mpr details.howto>summary:hover{text-decoration:underline}
#mpr details.howto .sub{margin-top:8px}
"""

SIDEBAR = """  <a href="/" class="logo">🏢 Пульт бизнеса</a>
  <nav class="lvlnav">
    <div class="navgroup">Инвентарь и расходы</div>
    <a href="/warehouse">🏭 Склад</a>
    <a href="/suppliers">📦 Поставщики</a>
    <a href="/stale">🧊 Залежи</a>
    <a href="/brak">♻️ Брак/возвраты</a>
    <a href="/opex">💼 Опер. расходы</a>
    <div class="navgroup">Отчёты</div>
    <a href="/reports" class="cur">📋 Отчёты МП</a>
  </nav>"""

MPTABS = """    <a href="/">🏢 Главная</a>
    <a href="/dashboard">🟣 Wildberries</a>
    <a href="/ozon">🟦 Ozon</a>
    <a href="/market">🟡 Маркет</a>
    <a href="/sites">🌐 Сайты</a>"""

SUB = ('Данные <b>1:1 из раздела Финансы → Баланс</b> личного кабинета Ozon (сверено с ЛК до рубля) '
       '+ операционные показатели (заказы, возвраты, средний чек) и себестоимость из МойСклад. '
       '<b>Каждое юрлицо — своя таблица, не суммируем.</b> Строки зеркалят Баланс; столбцы — месяцы; '
       'справа доля от оборота и тренд. <b>Подсветка — три блока относительно среднего сверенных месяцев:</b> '
       '<b style="color:var(--pos)">зелёное = выше среднего (хорошо)</b>, '
       '<b style="color:var(--warn)">янтарное = ниже среднего (обратить внимание)</b>, без заливки — норма. '
       'Для расходов инверсия (ниже — лучше). Для ключевых статей — % оборота в ячейке. '
       'Закрытые месяцы — статика (со <b>*</b> — заморожены из оценки, ещё не сверены с Отчётом о реализации); '
       'два правых столбца — <b>текущий месяц (оценка)</b> и <b>прогноз на конец месяца</b>, живьём из БД.')

FOOT = ('Все строки воспроизводят <b>Финансы → Баланс</b> Ozon 1:1. Продажи / Возвраты / Вознаграждение — '
        'из официального <b>отчёта о реализации</b> (он же даёт сплит Продаж); расходные услуги — '
        'реконструкция из транзакций (сверка с ЛК ЦК: январь Σ|Δ|=2 ₽, июнь Σ|Δ|=215 ₽ на 10 строках). '
        '«Итого расходы Ozon» = сумма 7 расходных статей (компенсации и прочие — отдельно, в плюс). '
        'Эквайринг и звёзды входят в «Услуги партнёров», подписки/баллы за отзывы/продвижение — в '
        '«Продвижение и рекламу» (как в ЛК). Себестоимость — FIFO из МойСклад, покрытие 97–100%. '
        'Месяц замораживается в статику в конце последнего дня из оценки по транзакциям (сплит «—», помечен '
        '<b>*</b>); после выхода Отчёта о реализации (~8–10 числа) сверяется и правится. Дальше — WB и Яндекс.')

JS = """<script>
(function(){
  fetch('/api/ozon/mp-current').then(function(r){return r.json();}).then(function(d){
    if(!d||!d.month) return;
    var mo=d.month, accs=['oz_acc1','oz_acc2'];
    document.querySelectorAll('#mpr section.org table').forEach(function(t,ti){
      var jh=t.querySelector('thead th.live'); if(jh) jh.textContent=mo.label+'*';
      var cells=(d.accounts||{})[accs[ti]]||{};
      t.querySelectorAll('tr[data-k]').forEach(function(tr){
        var c=cells[tr.getAttribute('data-k')]||{};
        ['jul','fc'].forEach(function(cc){
          var td=tr.querySelector('td.live[data-c="'+cc+'"]'); if(!td) return;
          var base='num live'+(cc==='fc'?' fc':'');
          if(c[cc]&&c[cc].txt&&c[cc].txt!=='—'){ td.className=base+(c[cc].cls?' '+c[cc].cls:''); td.innerHTML=c[cc].txt; }
          else { td.className=base+' muted'; td.textContent='—'; }
        });
      });
    });
    var fn=document.getElementById('mpr-live-note');
    if(fn){ fn.innerHTML='<b>'+mo.label+'*</b> — текущий месяц по данным на '+(mo.last_date||'')+
      ' (оценка по транзакциям до выхода Отчёта о реализации ~8–10 числа следующего месяца). '+
      '<b>прогноз</b> — как закроется месяц: факт с начала месяца ('+mo.elapsed_days+' дн) + дневная ставка за скользящие '+mo.window_days+' дней × оставшиеся '+mo.remaining_days+' дн. '+
      'Статьи, идущие по дням (продажи, логистика, комиссия, реклама, партнёры, COGS, заказы), проецируются потоком; фиксированная абонплата подписки — разово раз в месяц, берётся фактически. '+
      'Окно непрерывно переходит через границу месяца, поэтому смена месяца прогноз не ломает. '+
      'Подсветка «тек.» — только по относительным статьям (доли расходов, маржа, чек), т.к. абсолютные суммы за неполный месяц заведомо ниже; прогноз подсвечен целиком. '+
      'Сплит Продаж (Выручка/Баллы/Программы) для текущего месяца появится после отчёта.'; }
  }).catch(function(){});
})();
</script>"""


def _atomic_write(path, text):
    """Запись через temp + os.replace — читатель (/reports) никогда не увидит частичный файл."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, str(path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def render(hist=None):
    """Собрать reports.html из hist JSON (по умолчанию из HIST_PATH) и записать атомарно. → путь."""
    data = hist if hist is not None else json.loads(HIST_PATH.read_text(encoding="utf-8"))
    keys = data.get("period_keys", [])
    prov = set(data.get("provisional", []))
    N = len(data["months"])
    _C.update({"data": data, "M": data["months"], "N": N,
               "provkeys": keys + [""] * (N - len(keys)),
               "base": [i for i in range(N) if i >= len(keys) or keys[i] not in prov],
               "prov": prov})
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Отчёты МП · Пульт бизнеса</title>
<style>{SHELL_CSS}{REPORT_CSS}</style>
</head>
<body>
<header>
{SIDEBAR}
</header>
<main id="mpr">
  <nav class="mptabs">
{MPTABS}
  </nav>
  <div class="rtabs">
    <a class="rtab cur">🟦 Ozon</a>
    <a class="rtab" href="/reports/wb">🟣 Wildberries</a>
    <span class="rtab soon">🟡 Яндекс Маркет · скоро</span>
  </div>
  <p class="eyebrow">Отчёты МП · Ozon</p>
  <h1>Ozon — сводный отчёт по месяцам</h1>
  <details class="howto"><summary>Как читать этот отчёт</summary><p class="sub">{SUB}</p></details>
  <div class="tlegend">
    <span><span class="sw" style="background:var(--pos-s)"></span>выше среднего — хорошо</span>
    <span><span class="sw" style="background:var(--warn-s)"></span>ниже среднего — обратить внимание</span>
    <span><span class="sw" style="border:1px solid var(--line);background:transparent"></span>в норме (около среднего)</span>
    <span><span class="tag наша" style="margin:0">наша</span> не из отчёта МП (МойСклад)</span>
    <span><span class="tag расчёт" style="margin:0">расчёт</span> производная над константами</span>
  </div>
  {build("oz_acc1")}
  {build("oz_acc2")}
  <p class="livenote" id="mpr-live-note"></p>
  <div class="foot">{FOOT}</div>
</main>
{JS}
</body>
</html>"""
    _atomic_write(OUT, html)
    return OUT


if __name__ == "__main__":
    p = render()
    print("OK →", p)
