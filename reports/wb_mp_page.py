# -*- coding: utf-8 -*-
# поток: fin
"""reports/wb_mp_page.py — генератор страницы «Отчёты МП · WB» (web/static/reports_wb.html)
из reports/data/mp_wb_hist.json. Сестра ozon_mp_page.py: та же тёмная оболочка/подсветка,
но WB-специфичная витрина (водопад Финансового отчёта ВБ) и БЕЗ сплита Продаж (у ВБ его нет).

Водопад: Продажа(оборот) − Возврат − Комиссия(ВБ+СПП) = К перечислению за товар − Логистика −
Хранение − Приёмка − Прочие удержания = Итого к оплате − COGS = Чистая. Два правых столбца
(тек., прогноз) дорисовывает JS из /api/wb/mp-current. render() пишет файл атомарно.
"""
import json
import os
import tempfile
import pathlib

from reports.ozon_mp_page import SHELL_CSS, REPORT_CSS, SIDEBAR, MPTABS

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
HIST_PATH = BASE_DIR / "reports" / "data" / "mp_wb_hist.json"
OUT = BASE_DIR / "web" / "static" / "reports_wb.html"

ORG = {"wb_acc1": "Цифровой квадрат", "wb_acc2": "Дисквэр"}
EXP = ["delivery", "storage", "acceptance", "other"]

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
    """kind: inflow|expense|margin|count_up|count_dn|check. k — line_key для JS-дозагрузки живых
    столбцов (тек./прогноз) из /api/wb/mp-current."""
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
    ob = L["own_price"]                                     # ОБОРОТ = наша цена (база всех %)
    sales = L["sales"]                                      # ВБ реализовал (retail_amount, после СПП)
    spp = [ob[i] - sales[i] for i in range(N)]              # СПП = наша цена − ВБ реализовал
    cogs = a["cogs"]; net = a["net"]; margin = a["margin"]
    commission = a["commission"]
    cogs_pct = [(cogs[i] / ob[i] * 100 if ob[i] else 0) for i in range(N)]
    itog = [L["to_pay"][i] - sum(L[k][i] for k in EXP) for i in range(N)]
    wb_exp = [ob[i] - itog[i] for i in range(N)]                 # все удержания ВБ = наша цена − Итого к оплате
    wb_exp_pct = [(wb_exp[i] / ob[i] * 100 if ob[i] else 0) for i in range(N)]
    orders = a["orders"]; retc = a["returns_cnt"]
    check = [(ob[i] / orders[i] if orders[i] else 0) for i in range(N)]
    base = _C["base"] or list(range(N))
    tot_ob = sum(ob[i] for i in base); tot_net = sum(net[i] for i in base)
    avg_m = tot_net / tot_ob * 100 if tot_ob else 0
    avg_cogs = sum(cogs[i] for i in base) / tot_ob * 100 if tot_ob else 0
    nmon = len(base)
    H = []
    H.append(f'<section class="org"><h2><span class="orgdot"></span>{ORG[acc]} '
             f'<span class="muted" style="font-weight:400;font-size:13px">· Wildberries</span></h2>')
    H.append('<div class="hero">'
             f'<div class="cell"><div class="big">{money(tot_ob)} ₽</div><div class="lbl">оборот {nmon} мес</div></div>'
             f'<div class="cell"><div class="big">{money(tot_net)} ₽</div><div class="lbl">чистая {nmon} мес</div></div>'
             f'<div class="cell"><div class="big" style="color:var(--warn)">{avg_m:.1f}%</div><div class="lbl">маржа средняя</div></div>'
             f'<div class="cell"><div class="big" style="color:var(--neg)">{avg_cogs:.1f}%</div><div class="lbl">COGS от оборота</div></div></div>')
    H.append('<div class="charts">'
             f'<div class="chart"><h3>Оборот и чистая</h3>{bars_line(ob, net)}'
             '<div class="leg"><span><i style="border-color:var(--acc)"></i>оборот</span>'
             '<span><i style="border-color:var(--warn)"></i>чистая</span></div></div>'
             f'<div class="chart"><h3>Маржа, COGS и расходы ВБ</h3>{line2(margin, cogs_pct, wb_exp_pct)}'
             '<div class="leg"><span><i style="border-color:var(--pos)"></i>маржа %</span>'
             '<span><i style="border-color:var(--neg)"></i>COGS %</span>'
             '<span><i style="border-color:var(--acc)"></i>расходы ВБ %</span></div></div></div>')
    cur_ttl = "Текущий месяц — оценка по сформированным недельным отчётам"
    fc_ttl = "Прогноз на конец месяца (факт + дневная ставка за скользящее окно × остаток дней)"
    mth = "".join(f"<th>{M[i]}</th>" for i in range(N))
    H.append('<div class="card"><table><thead><tr><th>Статья Финансового отчёта ВБ</th>'
             + mth
             + f'<th class="live" title="{cur_ttl}">тек.</th>'
             + f'<th class="live" title="{fc_ttl}">прогноз</th>'
             + '<th>% Об.</th><th>Тренд</th></tr></thead><tbody>')
    H.append(sect("Операционные показатели"))
    H.append(row("Продажи, шт", orders, "count_up", ob, k="orders"))
    H.append(row("Возвраты, шт", retc, "count_dn", ob, k="returns_cnt"))
    H.append(row("Средний чек, ₽", check, "check", ob, tag="расчёт", k="check"))
    H.append(sect("Продажи и удержания площадки"))
    H.append(row("Продажа — наша цена", ob, "inflow", ob, sect_pct="100.0%", k="own_price"))
    H.append(row("ВБ реализовал (после СПП)", sales, "inflow", ob, showpc=True, k="sales"))
    H.append(row("Возврат покупателю", L["returns"], "expense", ob, k="returns"))
    H.append(row("СПП (скидка ВБ за свой счёт)", spp, "expense", ob, tag="расчёт", showpc=True, k="spp"))
    H.append(row("Комиссия ВБ", commission, "expense", ob, tag="расчёт", showpc=True, k="commission"))
    H.append(row("К перечислению за товар", L["to_pay"], "inflow", ob, tag="расчёт", subtot=True, k="to_pay"))
    H.append(sect("Услуги и удержания"))
    H.append(row("Логистика", L["delivery"], "expense", ob, showpc=True, k="delivery"))
    H.append(row("Хранение", L["storage"], "expense", ob, k="storage"))
    H.append(row("Приёмка", L["acceptance"], "expense", ob, k="acceptance"))
    H.append(row("Прочие удержания (баллы, штрафы)", L["other"], "expense", ob, showpc=True, k="other"))
    H.append(row("Итого расходы ВБ", wb_exp, "expense", ob, tag="расчёт", showpc=True, subtot=True, k="wb_exp"))
    H.append(row("Итого к оплате", itog, "inflow", ob, tag="расчёт", showpc=True, subtot=True, k="itog"))
    H.append(sect("Наши данные (не из отчёта МП)"))
    H.append(row("Себестоимость (COGS)", cogs, "expense", ob, tag="наша", showpc=True, k="cogs"))
    H.append(sect("Итог (расчёт над константами)"))
    H.append(row("Чистая прибыль", net, "inflow", ob, tag="расчёт", k="net"))
    H.append(row("Маржа", margin, "margin", ob, tag="расчёт", k="margin"))
    H.append('</tbody></table></div></section>')
    return "".join(H)


SUB = ('Данные <b>1:1 из Финансового отчёта Wildberries (Баланс)</b> личного кабинета '
       '(сверено с ЛК: К перечислению Δ&lt;0,05%, Итого к оплате +537 ₽ / +0,03% — в допуске) '
       '+ операционные показатели (продажи, возвраты, средний чек) и себестоимость из МойСклад. '
       '<b>Оборот = «Продажа — наша цена»</b> (цена после нашей скидки, ДО СПП) — от неё считаются '
       'все % и подсветка. «ВБ реализовал» — это уже <b>после СПП</b> (что заплатил покупатель); '
       'разница = <b>СПП</b> (скидка ВБ за свой счёт). Строки — водопад: наша цена → минус СПП, '
       'возврат, комиссия ВБ → К перечислению → минус логистика, хранение, приёмка, удержания → '
       'Итого к оплате → минус COGS → Чистая. <b>Каждое юрлицо — своя таблица, не суммируем.</b> '
       'Столбцы — <b>месяцы формирования</b> отчётов (весь недельный отчёт падает в свой месяц '
       'формирования — модель данных ВБ). Справа — доля от оборота и тренд. '
       '<b>Подсветка — три блока относительно среднего:</b> '
       '<b style="color:var(--pos)">зелёное = выше среднего (хорошо)</b>, '
       '<b style="color:var(--warn)">янтарное = ниже среднего (обратить внимание)</b>, без заливки — норма. '
       'Для расходов инверсия (ниже — лучше). Два правых столбца — <b>текущий месяц (оценка)</b> и '
       '<b>прогноз на конец месяца</b>, живьём из БД.')

FOOT = ('Все строки воспроизводят <b>Финансовый отчёт ВБ → Баланс</b>. <b>Оборот</b> = «Продажа — '
        'наша цена» (retail_price_withdisc_rub, ДО СПП). <b>«ВБ реализовал»</b> = retail_amount '
        '(после СПП, что заплатил покупатель); <b>«СПП»</b> = наша цена − ВБ реализовал (скидка ВБ за '
        'свой счёт, ≈28–29%). «Возврат» в сырье ВБ лежит с положительным ppvz_for_pay — в «К '
        'перечислению» он вычитается (иначе завышение 2×). <b>«Комиссия ВБ»</b> = ВБ реализовал − '
        'Возврат − К перечислению (реальная удержка площадки, без СПП; формулой не моделируется — '
        'берём фактическую разницу). «Прочие удержания» = штрафы + удержания + баллы лояльности '
        '(cashback). <b>«Итого расходы ВБ»</b> = наша цена − Итого к оплате (все удержания площадки: '
        'СПП + возврат + комиссия + логистика + хранение + приёмка + прочие). '
        '«Итого к оплате» = К перечислению − Логистика − Хранение − Приёмка − Прочие. '
        'Себестоимость — FIFO из МойСклад по assembly_id (сторно COGS возвратов в продаваемый сток, '
        'кроме склада «Брак»). Живой месяц оценивается по уже сформированным недельным отчётам; ВБ '
        'дописывает отчёты в начале следующего месяца — страница их подхватывает. Данные Ozon — на '
        'соседней вкладке; Яндекс — следующим шагом.')

JS = """<script>
(function(){
  fetch('/api/wb/mp-current').then(function(r){return r.json();}).then(function(d){
    if(!d||!d.month) return;
    var mo=d.month, accs=['wb_acc1','wb_acc2'];
    document.querySelectorAll('#mpr section.org table').forEach(function(t,ti){
      var jh=t.querySelector('thead th.live'); if(jh) jh.textContent=mo.label;
      var cells=(d.accounts||{})[accs[ti]]||{};
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
      ' (оценка по уже сформированным недельным отчётам ВБ). '+
      '<b>прогноз</b> — как закроется месяц: факт с начала месяца ('+mo.elapsed_days+' дн) + дневная ставка за скользящие '+mo.window_days+' дней × оставшиеся '+mo.remaining_days+' дн. '+
      'ВБ формирует отчёты недельными пачками, поэтому текущий месяц и прогноз — оценка; окно непрерывно переходит через границу месяца. '+
      'Подсветка «тек.» — только по относительным статьям (доли расходов, маржа, чек), т.к. абсолютные суммы за неполный месяц заведомо ниже; прогноз подсвечен целиком.'; }
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
    """Собрать reports_wb.html из hist JSON (по умолчанию из HIST_PATH), записать атомарно. → путь."""
    data = hist if hist is not None else json.loads(HIST_PATH.read_text(encoding="utf-8"))
    keys = data.get("period_keys", [])
    N = len(data["months"])
    _C.update({"data": data, "M": data["months"], "N": N,
               "base": list(range(N))})       # у ВБ все замороженные месяцы финальны (нет provisional)
    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Отчёты МП · WB · Пульт бизнеса</title>
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
    <a class="rtab cur">🟣 Wildberries</a>
    <span class="rtab soon">🟡 Яндекс Маркет · скоро</span>
  </div>
  <p class="eyebrow">Отчёты МП · Wildberries</p>
  <h1>Wildberries — сводный отчёт по месяцам</h1>
  <details class="howto"><summary>Как читать этот отчёт</summary><p class="sub">{SUB}</p></details>
  <div class="tlegend">
    <span><span class="sw" style="background:var(--pos-s)"></span>выше среднего — хорошо</span>
    <span><span class="sw" style="background:var(--warn-s)"></span>ниже среднего — обратить внимание</span>
    <span><span class="sw" style="border:1px solid var(--line);background:transparent"></span>в норме (около среднего)</span>
    <span><span class="tag наша" style="margin:0">наша</span> не из отчёта МП (МойСклад)</span>
    <span><span class="tag расчёт" style="margin:0">расчёт</span> производная над константами</span>
  </div>
  {build("wb_acc1")}
  {build("wb_acc2")}
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
