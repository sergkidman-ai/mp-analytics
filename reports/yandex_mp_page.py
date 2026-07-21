# -*- coding: utf-8 -*-
# поток: fin
"""reports/yandex_mp_page.py — генератор страницы «Отчёты МП · Яндекс» (web/static/reports_yandex.html)
из reports/data/mp_yandex_hist.json. Сестра ozon_mp_page/wb_mp_page: та же тёмная оболочка и
подсветка, но витрина Яндекс.Маркета и ОДНА таблица (одно юрлицо ya_acc1).

Водопад: Оплата покупателя + Субсидия = Оборот − Комиссия − Логистика − Перевод − Продвижение −
Агентское − Прочие − Подписка − Баллы = Итого к перечислению − COGS = Чистая. Два правых столбца
(тек., прогноз) дорисовывает JS из /api/yandex/mp-current. render() пишет файл атомарно.
"""
import json
import os
import tempfile
import pathlib

from reports.ozon_mp_page import SHELL_CSS, REPORT_CSS, SIDEBAR, MPTABS
from reports.yandex_mp_report import MP_EXP

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
HIST_PATH = BASE_DIR / "reports" / "data" / "mp_yandex_hist.json"
OUT = BASE_DIR / "web" / "static" / "reports_yandex.html"

ORG = {"ya_acc1": "Цифровой квадрат"}

_C = {"data": None, "M": [], "N": 0, "base": []}


def money(v, neg=False):
    if v is None:
        return "—"
    v = round(v); s = f"{abs(v):,}".replace(",", " ")
    return ("−" if (neg or v < 0) else "") + s


def _base_stats(comp):
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
    N = _C["N"]
    shares = [(vals[i] / oborot[i] * 100 if (vals[i] is not None and oborot[i]) else None) for i in range(N)]
    if kind == "expense":
        comp, good_up = shares, False
    elif kind == "count_dn":
        comp, good_up = vals, False
    else:
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
    live = ('<td class="num live" data-c="cur">·</td>'
            '<td class="num live fc" data-c="fc">·</td>')
    dk = f' data-k="{k}"' if k else ""
    return (f'<tr class="{trclass}"{dk}><td class="{lblcls}">{label}{tg}</td>' + "".join(tds) +
            live + f'<td class="num pctcol">{rp}</td><td class="spk">{sp}</td></tr>')


def sect(t):
    return f'<tr class="sect"><td colspan="{_C["N"] + 5}">{t}</td></tr>'


def bars_line(oborot, net):
    M, N = _C["M"], _C["N"]
    hi = max(oborot) or 1; W, H = 500, 140
    step = (W - 40) / N
    out = []
    for i in range(N):
        x = 20 + i * step + step * 0.12; h = (oborot[i] / hi) * 96 + 2; y = 128 - h
        bw = step * 0.6
        out.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" rx="3" fill="var(--acc)" opacity=".85"/>'
                   f'<text x="{x + bw / 2:.1f}" y="138" text-anchor="middle" class="axt">{M[i]}</text>')
    nl = []
    for i in range(N):
        x = 20 + i * step + step * 0.42; y = 128 - (net[i] / hi) * 96 - 2; nl.append(f"{x:.1f},{y:.1f}")
    circ = "".join(f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="2.3" fill="var(--warn)"/>' for p in nl)
    poly = f'<polyline points="{" ".join(nl)}" fill="none" stroke="var(--warn)" stroke-width="2"/>'
    return f'<svg viewBox="0 0 {W} {H}" width="100%">' + "".join(out) + poly + circ + '</svg>'


def line2(a, b, c=None, amax=None):
    M, N = _C["M"], _C["N"]
    W, H = 500, 140
    ser = [(a, "var(--pos)"), (b, "var(--neg)")] + ([(c, "var(--acc)")] if c is not None else [])
    hi = amax or max(max(s) for s, _ in ser) or 1; lo = 0
    step = (W - 48) / (N - 1 or 1)

    def pl(vals, col):
        pts = [f"{24 + i * step:.1f},{128 - ((v - lo) / ((hi - lo) or 1)) * 95:.1f}" for i, v in enumerate(vals)]
        cs = "".join(f'<circle cx="{p.split(",")[0]}" cy="{p.split(",")[1]}" r="2.3" fill="{col}"/>' for p in pts)
        return f'<polyline points="{" ".join(pts)}" fill="none" stroke="{col}" stroke-width="2"/>' + cs
    ax = "".join(f'<text x="{24 + i * step:.1f}" y="138" text-anchor="middle" class="axt">{M[i]}</text>' for i in range(N))
    return (f'<svg viewBox="0 0 {W} {H}" width="100%">' + "".join(pl(s, col) for s, col in ser)
            + ax + '</svg>')


def build(acc):
    data, M, N = _C["data"], _C["M"], _C["N"]
    a = data["accounts"][acc]; L = a["lines"]
    rev = L["revenue"]; sub = L["subsidy"]
    ob = [rev[i] + sub[i] for i in range(N)]               # ОБОРОТ = выручка + субсидия (база всех %)
    cogs = a["cogs"]; net = a["net"]; margin = a["margin"]
    cogs_pct = [(cogs[i] / ob[i] * 100 if ob[i] else 0) for i in range(N)]
    itog = [sum(L[k][i] for k in MP_EXP) for i in range(N)]      # Итого расходы Маркета
    payout = [ob[i] - itog[i] for i in range(N)]                 # Итого к перечислению
    itog_pct = [(itog[i] / ob[i] * 100 if ob[i] else 0) for i in range(N)]
    orders = a["orders"]; retc = a["returns_cnt"]
    check = [(ob[i] / orders[i] if orders[i] else 0) for i in range(N)]
    base = _C["base"] or list(range(N))
    tot_ob = sum(ob[i] for i in base); tot_net = sum(net[i] for i in base)
    avg_m = tot_net / tot_ob * 100 if tot_ob else 0
    avg_cogs = sum(cogs[i] for i in base) / tot_ob * 100 if tot_ob else 0
    nmon = len(base)
    H = []
    H.append(f'<section class="org"><h2><span class="orgdot"></span>{ORG[acc]} '
             f'<span class="muted" style="font-weight:400;font-size:13px">· Яндекс Маркет</span></h2>')
    H.append('<div class="hero">'
             f'<div class="cell"><div class="big">{money(tot_ob)} ₽</div><div class="lbl">оборот {nmon} мес</div></div>'
             f'<div class="cell"><div class="big">{money(tot_net)} ₽</div><div class="lbl">чистая {nmon} мес</div></div>'
             f'<div class="cell"><div class="big" style="color:var(--warn)">{avg_m:.1f}%</div><div class="lbl">маржа средняя</div></div>'
             f'<div class="cell"><div class="big" style="color:var(--neg)">{avg_cogs:.1f}%</div><div class="lbl">COGS от оборота</div></div></div>')
    H.append('<div class="charts">'
             f'<div class="chart"><h3>Оборот и чистая</h3>{bars_line(ob, net)}'
             '<div class="leg"><span><i style="border-color:var(--acc)"></i>оборот</span>'
             '<span><i style="border-color:var(--warn)"></i>чистая</span></div></div>'
             f'<div class="chart"><h3>Маржа, COGS и расходы Маркета</h3>{line2(margin, cogs_pct, itog_pct)}'
             '<div class="leg"><span><i style="border-color:var(--pos)"></i>маржа %</span>'
             '<span><i style="border-color:var(--neg)"></i>COGS %</span>'
             '<span><i style="border-color:var(--acc)"></i>расходы Маркета %</span></div></div></div>')
    cur_ttl = "Текущий месяц — факт с начала месяца (оценка, неполный месяц)"
    fc_ttl = "Прогноз на конец месяца (проекция по юнит-экономике закрытых месяцев × ожидаемое число заказов)"
    mth = "".join(f"<th>{M[i]}</th>" for i in range(N))
    H.append('<div class="card"><table><thead><tr><th>Статья отчёта Яндекс.Маркета</th>'
             + mth
             + f'<th class="live" title="{cur_ttl}">тек.</th>'
             + f'<th class="live" title="{fc_ttl}">прогноз</th>'
             + '<th>% Об.</th><th>Тренд</th></tr></thead><tbody>')
    H.append(sect("Операционные показатели"))
    H.append(row("Продажи, шт", orders, "count_up", ob, k="orders"))
    H.append(row("Возвраты, шт", retc, "count_dn", ob, k="returns_cnt"))
    H.append(row("Средний чек, ₽", check, "check", ob, tag="расчёт", k="check"))
    H.append(sect("Продажи и субсидия"))
    H.append(row("Оборот (выручка + субсидия)", ob, "inflow", ob, sect_pct="100.0%", tag="расчёт", subtot=True, k="own"))
    H.append(row("Оплата покупателя", rev, "inflow", ob, showpc=True, k="revenue"))
    H.append(row("Субсидия Маркета", sub, "inflow", ob, showpc=True, k="subsidy"))
    H.append(sect("Удержания площадки"))
    H.append(row("Комиссия Маркета", L["fee"], "expense", ob, showpc=True, k="fee"))
    H.append(row("Логистика / доставка", L["delivery"], "expense", ob, showpc=True, k="delivery"))
    H.append(row("Перевод / эквайринг", L["transfer"], "expense", ob, k="transfer"))
    H.append(row("Продвижение (буст + полки)", L["promotion"], "expense", ob, showpc=True, k="promotion"))
    H.append(row("Буст-продажи", L["boost_sales"], "expense", ob, sub=True, k="boost_sales"))
    H.append(row("Буст-показы", L["boost_shows"], "expense", ob, sub=True, k="boost_shows"))
    H.append(row("Полки", L["shelf"], "expense", ob, sub=True, k="shelf"))
    H.append(row("Агентское вознаграждение", L["agency"], "expense", ob, k="agency"))
    H.append(row("Прочие удержания", L["other_fee"], "expense", ob, k="other_fee"))
    H.append(row("Подписка (Маркет)", L["subscription_cost"], "expense", ob, k="subscription_cost"))
    H.append(row("Баллы за отзывы", L["reviews_cost"], "expense", ob, k="reviews_cost"))
    H.append(row("Итого расходы Маркета", itog, "expense", ob, tag="расчёт", showpc=True, subtot=True, k="itog"))
    H.append(row("Итого к перечислению", payout, "inflow", ob, tag="расчёт", showpc=True, subtot=True, k="payout"))
    H.append(sect("Наши данные (не из отчёта МП)"))
    H.append(row("Себестоимость (COGS)", cogs, "expense", ob, tag="наша", showpc=True, k="cogs"))
    H.append(sect("Итог (расчёт над константами)"))
    H.append(row("Чистая прибыль", net, "inflow", ob, tag="расчёт", k="net"))
    H.append(row("Маржа", margin, "margin", ob, tag="расчёт", k="margin"))
    H.append('</tbody></table></div></section>')
    return "".join(H)


SUB = ('Данные из <b>витрины Яндекс.Маркета</b> (Партнёр-API: заказы + отчёт услуг + закрытие '
       'месяца) + себестоимость из МойСклад. <b>Оборот = «Оплата покупателя» + «Субсидия Маркета»</b> '
       '— от него считаются все % и подсветка (субсидия у ЯМ значимая, до ~64% сверх оплаты). '
       'Строки — водопад: оборот → минус комиссия, логистика, перевод, продвижение, агентское, '
       'прочие, подписка, баллы → Итого к перечислению → минус COGS → Чистая. '
       '<b>Одно юрлицо (Цифровой квадрат) — одна таблица.</b> Столбцы — <b>календарные месяцы</b>. '
       'Справа — доля от оборота и тренд. <b>Подсветка — три блока относительно среднего:</b> '
       '<b style="color:var(--pos)">зелёное = выше среднего (хорошо)</b>, '
       '<b style="color:var(--warn)">янтарное = ниже среднего (обратить внимание)</b>, без заливки — норма. '
       'Для расходов инверсия (ниже — лучше). Два правых столбца — <b>текущий месяц (оценка)</b> и '
       '<b>прогноз на конец месяца</b>, живьём из БД.')

FOOT = ('Все строки — из витрины <b>Яндекс.Маркета</b> (Партнёр-API). <b>Оборот</b> = Оплата '
        'покупателя (payment) + Субсидия Маркета (subsidy) — субсидия у ЯМ идёт сверх оплаты и '
        'значима. <b>«Итого расходы Маркета»</b> = комиссия + логистика + перевод/эквайринг + '
        'продвижение (буст-продажи + буст-показы + полки) + агентское + прочие + подписка + баллы '
        'за отзывы. <b>«Итого к перечислению»</b> = Оборот − Итого расходы Маркета. Себестоимость — '
        'order-based из МойСклад (Σ себест×кол-во по позициям заказа в месяц заказа; сторно '
        'возвратов в месяц заказа, кроме склада «Брак»). '
        '<b>⚠ Живой месяц Яндекса структурно занижает маржу:</b> COGS списывается в месяц заказа '
        'целиком, а выручка/субсидия реализуются по мере доставки — часть заказов ещё «в пути» '
        '(несут себест до реализации выручки). Поэтому «тек.» — заниженный факт «пока набрано», а '
        '«прогноз» проецирует полный месяц по юнит-экономике закрытых месяцев × ожидаемое число '
        'заказов (маржа-прогноз сходится к норме). Данные Ozon и WB — на соседних вкладках.')

JS = """<script>
(function(){
  fetch('/api/yandex/mp-current').then(function(r){return r.json();}).then(function(d){
    if(!d||!d.month) return;
    var mo=d.month;
    document.querySelectorAll('#mpr section.org table').forEach(function(t){
      var jh=t.querySelector('thead th.live'); if(jh) jh.textContent=mo.label;
      var cells=(d.accounts||{})['ya_acc1']||{};
      t.querySelectorAll('tr[data-k]').forEach(function(tr){
        var c=cells[tr.getAttribute('data-k')]||{};
        ['cur','fc'].forEach(function(cc){
          var td=tr.querySelector('td.live[data-c="'+cc+'"]'); if(!td) return;
          var base='num live'+(cc==='fc'?' fc':'');
          if(c[cc]&&c[cc].txt&&c[cc].txt!=='—'){ td.className=base+(c[cc].cls?' '+c[cc].cls:''); td.innerHTML=c[cc].txt; }
          else { td.className=base+' muted'; td.textContent='—'; }
        });
      });
    });
    var fn=document.getElementById('mpr-live-note');
    if(fn){ fn.innerHTML='<b>'+mo.label+'</b> — текущий месяц по данным на '+(mo.last_date||'')+
      ' ('+mo.elapsed_days+' дн из '+mo.days_in_month+'). '+
      '<b>«тек.» — заниженный факт:</b> у Яндекса себест списывается в месяц заказа целиком, а выручка/субсидия добираются по мере доставки — заказы «в пути» несут COGS до реализации → маржа неполного месяца ниже нормы. '+
      '<b>«прогноз»</b> — проекция на полный месяц по юнит-экономике закрытых месяцев × ожидаемое число заказов (заказы — дневная ставка за '+mo.window_days+' дн × оставшиеся '+mo.remaining_days+' дн), поэтому маржа-прогноз сходится к норме, а не к заниженной MTD.'; }
  }).catch(function(){});
})();
</script>"""


def _atomic_write(path, text):
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
    """Собрать reports_yandex.html из hist JSON (по умолчанию из HIST_PATH), записать атомарно. → путь."""
    data = hist if hist is not None else json.loads(HIST_PATH.read_text(encoding="utf-8"))
    N = len(data["months"])
    _C.update({"data": data, "M": data["months"], "N": N, "base": list(range(N))})
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Отчёты МП · Яндекс · Пульт бизнеса</title>
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
    <a class="rtab" href="/reports">🟦 Ozon</a>
    <a class="rtab" href="/reports/wb">🟣 Wildberries</a>
    <a class="rtab cur">🟡 Яндекс Маркет</a>
  </div>
  <p class="eyebrow">Отчёты МП · Яндекс Маркет</p>
  <h1>Яндекс Маркет — сводный отчёт по месяцам</h1>
  <details class="howto"><summary>Как читать этот отчёт</summary><p class="sub">{SUB}</p></details>
  <div class="tlegend">
    <span><span class="sw" style="background:var(--pos-s)"></span>выше среднего — хорошо</span>
    <span><span class="sw" style="background:var(--warn-s)"></span>ниже среднего — обратить внимание</span>
    <span><span class="sw" style="border:1px solid var(--line);background:transparent"></span>в норме (около среднего)</span>
    <span><span class="tag наша" style="margin:0">наша</span> не из отчёта МП (МойСклад)</span>
    <span><span class="tag расчёт" style="margin:0">расчёт</span> производная над константами</span>
  </div>
  {build("ya_acc1")}
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
