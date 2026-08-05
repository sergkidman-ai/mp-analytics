# поток: fin
"""reports/oz_cogs_page.py — раздел «Себестоимость» → Ozon: себест по отгрузкам, два юрлица.

Зеркало reports/ya_cogs_page.py (та же вёрстка, те же правила чтения), источник — oz_cogs_demand:
  overview_html — таблица по отчётам-месяцам (заказов/возвратов/сумма/себест/валовая маржа);
  detail_html   — провал внутрь месяца: по каждой отгрузке (№, дата, статус, сумма, себест, способ).
Маржа ВАЛОВАЯ (наша цена − себест, ДО комиссий Ozon). Юрлица не смешиваются: у каждого свой подтаб,
своя таблица и своё закрытие месяца.
"""
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from reports.ozon_mp_page import SHELL_CSS, REPORT_CSS, MPTABS  # noqa: E402
from reports.cost_tabs import tabs_html  # noqa: E402
from reports.ya_cogs_page import SIDEBAR_COST, PAGE_CSS, _fmt, _pct  # noqa: E402

# ключ URL → аккаунт БД → (юрлицо, ключ подтаба)
ACCOUNTS = {
    "acc1": ("oz_acc1", "Цифровой квадрат", "oz1"),
    "acc2": ("oz_acc2", "Дисквэр", "oz2"),
}

RET_STATUSES = ("return_stock", "return_defect", "return_ozon", "unredeemed")

STATUS_LABEL = {
    "done": ("✅", "Доставлен", ""),
    "return_stock": ("↩️", "Возврат → наш склад", "warn"),
    "return_defect": ("♻️", "Возврат → наш склад (брак)", "warn"),
    "return_ozon": ("🏬", "Возврат на склад Озон", "warn"),
    "unredeemed": ("🔙", "Отменён/невыкуп · в МС нет возврата", "warn"),
    "other": ("•", "В пути / в обработке", "mut"),
}
# Озон отменил отправление, а возврат в МС не проведён → товар «в воздухе»: подсветить на проверку.
FLAG_STATUSES = ("unredeemed",)
# Товар вернулся в ПРОДАВАЕМЫЙ сток → себест сторнируется, строка net-neutral (оборот и себест = 0).
# Склад «Озон» тоже сторнируем (решение Сергея 2026-08-05): товар остаётся в стоке FBO и будет продан
# оттуда, а себест по FBO-заказам списывается в момент той продажи. Не сторновать здесь = списать
# себестоимость дважды. НЕ сторнируем только «Брак» — перепродать нельзя, себест остаётся расходом.
STORNO_STATUSES = ("return_stock", "unredeemed", "return_ozon")
# себест этих статусов остаётся расходом месяца (товар потерян для перепродажи)
LOSS_STATUSES = ("return_defect",)
METHOD_LABEL = {"ms_fifo": "МС (FIFO)", "imputed": "импутация", "manual": "ручной"}


def _js_head(acc_key, account):
    return f"var OZ_ACCOUNT='{account}';var OZ_KEY='{acc_key}';"


def _overview_js(acc_key, account):
    return """<script>
""" + _js_head(acc_key, account) + """
function freezeMonth(ym){
  if(!confirm('Закрыть месяц '+ym+'? Себест станет финальным, коллектор перестанет его пересобирать.'))return;
  fetch('/api/oz-cost/freeze',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({account:OZ_ACCOUNT,ym:ym})})
   .then(function(r){return r.json();}).then(function(d){
     if(d.ok){location.reload();}
     else{alert((d.msg||'Не удалось закрыть')+(d.zero?'\\n'+d.zero.map(function(z){return z.demand_name;}).join(', '):''));}});
}
function unfreezeMonth(ym){
  if(!confirm('Разморозить '+ym+'? Следующий прогон коллектора соберёт месяц заново из МойСклад.'))return;
  fetch('/api/oz-cost/unfreeze',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({account:OZ_ACCOUNT,ym:ym})})
   .then(function(r){return r.json();}).then(function(){location.reload();});
}
</script>"""


def _shell(title, eyebrow, h1, body, rtab_cur):
    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{SHELL_CSS}{REPORT_CSS}{PAGE_CSS}</style>
</head>
<body>
<header>
{SIDEBAR_COST}
</header>
<main id="mpr">
  <nav class="mptabs">
{MPTABS}
  </nav>
  <div class="rtabs">
    {tabs_html(rtab_cur)}
  </div>
  <p class="eyebrow">{eyebrow}</p>
  <h1>{h1}</h1>
{body}
</main>
</body>
</html>"""


def overview_html(acc_key="acc1"):
    account, org, tab = ACCOUNTS[acc_key]
    rows = db.query("""
        SELECT to_char(ym,'YYYY-MM') ym,
               count(*)                                                        orders,
               count(*) FILTER (WHERE status = ANY(%s))                        returns,
               coalesce(sum(our_sum) FILTER (WHERE status = ANY(%s)),0)::float ret_sales,
               coalesce(sum(cogs)    FILTER (WHERE status = ANY(%s)),0)::float ret_cogs,
               coalesce(sum(our_sum) FILTER (WHERE status='done'),0)::float    sales,
               coalesce(sum(cogs) FILTER (WHERE status='done' OR status = ANY(%s)),0)::float cogs
        FROM oz_cogs_demand WHERE account=%s
        GROUP BY ym ORDER BY ym DESC
    """, (list(RET_STATUSES), list(RET_STATUSES), list(RET_STATUSES), list(LOSS_STATUSES), account))
    frozen = {r["ym"].strftime("%Y-%m"): r["closed_at"] for r in db.query(
        "SELECT ym, closed_at FROM oz_cogs_frozen WHERE account=%s", (account,))}
    body = ['<table class="ct"><thead><tr>'
            '<th class="l">Отчёт</th><th class="num">Заказов</th><th class="num">Возвратов</th>'
            '<th class="num">Возвр.: сумма<br>(наша цена)</th><th class="num">Возвр.: себест</th>'
            '<th class="num">Продажи (наша цена)</th><th class="num">Себестоимость</th>'
            '<th class="num">Валовая маржа</th><th class="num">Маржа&nbsp;%</th>'
            '<th class="l">Состояние</th></tr></thead><tbody>']
    t_ord = t_ret = 0
    t_sales = t_cogs = t_rs = t_rc = 0.0
    for r in rows:
        m = r["sales"] - r["cogs"]
        t_ord += r["orders"]; t_ret += r["returns"]
        t_sales += r["sales"]; t_cogs += r["cogs"]
        t_rs += r["ret_sales"]; t_rc += r["ret_cogs"]
        mcls = "pos" if m >= 0 else "neg"
        ym = r["ym"]
        closed_at = frozen.get(ym)
        if closed_at:
            state = (f'<span class="stbadge closed">🔒 закрыт {closed_at.strftime("%d.%m")}</span>'
                     f'<button class="mbtn warn" onclick="event.stopPropagation();unfreezeMonth(\'{ym}\')">'
                     f'Разморозить</button>')
        else:
            state = ('<span class="stbadge open">🔓 открыт</span>'
                     f'<button class="mbtn" onclick="event.stopPropagation();freezeMonth(\'{ym}\')">'
                     f'Закрыть месяц</button>')
        body.append(
            f'<tr class="row-link" onclick="location.href=\'/reports/cost/ozon/{acc_key}/{ym}\'">'
            f'<td class="l">▸ {ym}</td>'
            f'<td class="num">{r["orders"]}</td>'
            f'<td class="num">{r["returns"]}</td>'
            f'<td class="num mut">{_fmt(r["ret_sales"])}</td>'
            f'<td class="num mut">{_fmt(r["ret_cogs"])}</td>'
            f'<td class="num">{_fmt(r["sales"])}</td>'
            f'<td class="num">{_fmt(r["cogs"])}</td>'
            f'<td class="num {mcls}">{_fmt(m)}</td>'
            f'<td class="num {mcls}">{_pct(m, r["sales"])}</td>'
            f'<td class="l">{state}</td></tr>')
    tm = t_sales - t_cogs
    body.append('</tbody><tfoot><tr>'
                f'<td class="l">ИТОГО</td><td class="num">{t_ord}</td><td class="num">{t_ret}</td>'
                f'<td class="num mut">{_fmt(t_rs)}</td><td class="num mut">{_fmt(t_rc)}</td>'
                f'<td class="num">{_fmt(t_sales)}</td><td class="num">{_fmt(t_cogs)}</td>'
                f'<td class="num">{_fmt(tm)}</td><td class="num">{_pct(tm, t_sales)}</td>'
                '<td></td></tr></tfoot></table>')
    body.append(f'<p class="mnote">Юрлицо <b>{org}</b> ({account}) — отдельная таблица, с другим '
                'юрлицом Ozon не суммируется. Отчёт = месяц отгрузки. Маржа <b>валовая</b> (наша цена − '
                'себестоимость, ДО комиссии, логистики и рекламы Ozon; полная чистая — во вкладке '
                '«Отчёты МП»). Себест — FIFO конкретной отгрузки из МойСклад. <b>Сторно:</b> возвраты '
                'в продаваемый сток — наш склад <i>и склад Озон</i> (товар останется в FBO и будет '
                'продан оттуда) — плюс отменённые отправления исключены из «Продажи»/«Себестоимость» '
                '(net-neutral). Не сторнируется только <b>Брак</b>: перепродать нельзя, себест '
                'остаётся расходом. Колонки <b>«Возвр.»</b> — сумма и себест по всем '
                'возвратам. Клик по строке — детализация по отправлениям.</p>')
    body.append('<p class="mnote"><b>Месяц отгрузки ≠ месяц отчёта Ozon.</b> Здесь месяц = <b>дата '
                'отгрузки</b> (документ «Отгрузка» в МойСклад): себест берётся как FIFO конкретной '
                'отгрузки, поэтому оборот, себест и месяц привязаны к дате физической отгрузки. '
                'Во вкладке <b>«Отчёты МП · Ozon»</b> тот же заказ считается по <b>отчёту о реализации '
                'и транзакциям Ozon</b>, а оборот там — деньги площадки (после комиссии и СПП), а не '
                'наша цена. Поэтому цифры двух разделов не обязаны совпадать: это разные точки '
                'привязки и разные деньги; внутри каждого раздела всё консистентно.</p>')
    body.append('<p class="mnote"><b>🔒 Закрытый месяц</b> — себест финальный, коллектор его не '
                'пересобирает (МойСклад лишний раз не дёргается). Закрывать имеет смысл после проверки '
                'месяца; «Разморозить» открывает месяц обратно — следующий прогон соберёт его заново. '
                'Закрыть нельзя, пока в месяце есть продажи с нулевым себестом (сначала заполнить).</p>')
    if not rows:
        body = ['<p class="mnote">Данных нет. Запустите сбор: '
                '<code>./venv/bin/python -m collectors.oz_cogs_demand</code></p>']
    return _shell(f"Себестоимость · Ozon · {org} · Пульт бизнеса",
                  f"Себестоимость · Ozon · {org}", "Себестоимость по отчётам (месяцам)",
                  "\n".join(body) + _overview_js(acc_key, account), tab)


_STATUS_FILTER = [
    ("", "все статусы"),
    ("done", "✅ Доставлен"),
    ("return_stock", "↩️ Возврат → наш склад"),
    ("return_defect", "♻️ Брак"),
    ("return_ozon", "🏬 Возврат на склад Озон"),
    ("unredeemed", "🔙 Отменён · в МС нет возврата"),
    ("other", "• В пути / в обработке"),
]

# фильтр по наличию себеста (для поиска пустых → ручной ввод)
_COST_FILTER = [
    ("", "все"),
    ("zero", "= 0 (нужен ввод)"),
    ("pos", "> 0"),
]

# фильтр по столбцам + сортировка + пересчёт ИТОГО по видимым строкам (vanilla JS, self-contained)
_DETAIL_JS_BODY = """
function saveCost(nm){
  var inp=document.getElementById('ci_'+nm); if(!inp) return;
  var v=parseFloat(inp.value); if(isNaN(v)||v<0){alert('Введите себест ≥ 0');return;}
  fetch('/api/oz-cost/manual',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({account:OZ_ACCOUNT,demand_name:nm,cogs:v,author:'сотрудник'})})
   .then(function(r){return r.json();}).then(function(d){
     if(d.ok){location.reload();}else{alert('Ошибка: '+(d.error||'не сохранено'));}});
}
function resetCost(nm){
  if(!confirm('Сбросить ручной себест и пересчитать по МС?'))return;
  fetch('/api/oz-cost/reset',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({account:OZ_ACCOUNT,demand_name:nm})})
   .then(function(r){return r.json();}).then(function(){location.reload();});
}
(function(){
  var tbl=document.getElementById('dtbl'); if(!tbl) return;
  var tb=tbl.tBodies[0], rows=[].slice.call(tb.rows);
  var fst=document.getElementById('fst'), fmt=document.getElementById('fmt'),
      fc=document.getElementById('fcost'),
      fq=document.getElementById('fq'), foot=document.getElementById('dfoot'),
      cnt=document.getElementById('fcnt');
  var RET={return_stock:1,return_defect:1,return_ozon:1,unredeemed:1};
  function nf(x){return Math.round(x).toLocaleString('ru-RU').replace(/\\u00A0/g,' ').replace(/,/g,' ');}
  function apply(){
    var s=fst.value, m=fmt.value, cf=fc.value, q=fq.value.trim().toLowerCase();
    var n=0,ret=0,sales=0,cogs=0;
    rows.forEach(function(r){
      var rc=+r.dataset.cogs;   // сырой себест строки (не эффективный)
      var okc=(!cf)||(cf==='zero'?rc<0.005:rc>0.005);
      var ok=(!s||r.dataset.status===s)&&(!m||r.dataset.method===m)&&okc&&
             (!q||r.cells[0].textContent.toLowerCase().indexOf(q)>=0);
      r.style.display=ok?'':'none';
      if(ok){n++; sales+=+r.cells[3].dataset.v; cogs+=+r.cells[4].dataset.v;
             if(RET[r.dataset.status])ret++;}
    });
    var mg=sales-cogs;
    foot.innerHTML='<td class="l" colspan="3">ИТОГО: отправлений '+n+' · возвратов '+ret+'</td>'+
      '<td class="num">'+nf(sales)+'</td><td class="num">'+nf(cogs)+'</td><td></td>'+
      '<td class="num">'+nf(mg)+'</td><td class="num">'+(sales?(mg/sales*100).toFixed(1)+'%':'—')+'</td>';
    cnt.textContent='показано '+n+' из '+rows.length;
  }
  var sc=-1, asc=true, ths=tbl.tHead.rows[0].cells;
  for(var i=0;i<ths.length;i++){(function(th){
    th.classList.add('srt');
    th.addEventListener('click',function(){
      var c=+th.dataset.col, t=th.dataset.type;
      asc=(sc===c)?!asc:true; sc=c;
      rows.sort(function(a,b){
        var va,vb;
        if(t==='num'){va=+a.cells[c].dataset.v; vb=+b.cells[c].dataset.v;}
        else{va=a.cells[c].textContent; vb=b.cells[c].textContent;}
        return (va<vb?-1:va>vb?1:0)*(asc?1:-1);
      });
      rows.forEach(function(r){tb.appendChild(r);});
      for(var k=0;k<ths.length;k++)ths[k].dataset.dir='';
      th.dataset.dir=asc?'▲':'▼';
    });
  })(ths[i]);}
  fst.addEventListener('change',apply); fmt.addEventListener('change',apply);
  fc.addEventListener('change',apply);
  fq.addEventListener('input',apply); apply();
})();
"""


def detail_html(acc_key, ym):
    """ym в формате YYYY-MM."""
    account, org, tab = ACCOUNTS[acc_key]
    rows = db.query("""
        SELECT demand_name, to_char(demand_date,'YYYY-MM-DD') d, status, status_raw,
               coalesce(our_sum,0)::float our_sum, coalesce(cogs,0)::float cogs,
               coalesce(qty,0)::float qty, method
        FROM oz_cogs_demand
        WHERE account=%s AND to_char(ym,'YYYY-MM')=%s
        ORDER BY demand_date, demand_name
    """, (account, ym))
    opts = "".join(f'<option value="{v}">{lbl}</option>' for v, lbl in _STATUS_FILTER)
    copts = "".join(f'<option value="{v}">{lbl}</option>' for v, lbl in _COST_FILTER)
    body = [f'<a class="backlink" href="/reports/cost/ozon/{acc_key}">◀ Назад к отчётам</a>',
            '<div class="ftbar">'
            f'<label>Статус <select id="fst">{opts}</select></label>'
            '<label>Способ <select id="fmt">'
            '<option value="">все</option><option value="ms_fifo">МС (FIFO)</option>'
            '<option value="imputed">импутация</option><option value="manual">ручной</option>'
            '</select></label>'
            f'<label>Себест <select id="fcost">{copts}</select></label>'
            '<input id="fq" placeholder="поиск по № отправления…" size="20">'
            '<span class="cnt" id="fcnt"></span></div>',
            '<div class="tblwrap"><table class="ct" id="dtbl"><thead><tr>'
            '<th class="l srt" data-col="0" data-type="text">№ отправления</th>'
            '<th class="l srt" data-col="1" data-type="text">Дата</th>'
            '<th class="l srt" data-col="2" data-type="text">Статус (Ozon)</th>'
            '<th class="num srt" data-col="3" data-type="num">Сумма</th>'
            '<th class="num srt" data-col="4" data-type="num">Себест.</th>'
            '<th class="l srt" data-col="5" data-type="text">Способ</th>'
            '<th class="num srt" data-col="6" data-type="num">Маржа&nbsp;₽</th>'
            '<th class="num srt" data-col="7" data-type="num">Маржа&nbsp;%</th></tr></thead><tbody>']
    t_sales = t_cogs = 0.0
    n_ret = 0
    for r in rows:
        st = r["status"]
        nm = r["demand_name"]
        our_sum, cogs = r["our_sum"], r["cogs"]
        emoji, label, cls = STATUS_LABEL.get(st, ("•", r["status_raw"] or "—", "mut"))
        if st in RET_STATUSES:
            n_ret += 1
        # эффективные (реализованные) значения: сторно возвратов в продаваемый сток, Брак = убыток
        if st in STORNO_STATUSES:
            eff_sum, eff_cogs = 0.0, 0.0           # товар вернулся в продаваемый сток → net-neutral
        elif st in LOSS_STATUSES:
            eff_sum, eff_cogs = 0.0, cogs          # брак: оборот 0, себест остаётся убытком
        elif st == "other":
            eff_sum, eff_cogs = 0.0, 0.0           # в пути: деньги ещё не реализованы
        else:
            eff_sum, eff_cogs = our_sum, cogs      # done
        eff_m = eff_sum - eff_cogs
        t_sales += eff_sum; t_cogs += eff_cogs
        flagcls = ' class="flag"' if st in FLAG_STATUSES else ''
        # ячейка «Сумма»: у не-продаж оборот не реализован (перечёркнут), data-v = эффективный
        sum_cell = (f'<td class="num strk" data-v="0.00">{_fmt(our_sum)}</td>' if st != "done"
                    else f'<td class="num" data-v="{our_sum:.2f}">{_fmt(our_sum)}</td>')
        # ячейка «Себест». Редактируемая для: реальных продаж с 0 себеста (нужен ввод) и ручных
        # (можно поправить/сбросить). Сторно не редактируем (net-neutral).
        need_input = (cogs == 0 and (st == "done" or st in LOSS_STATUSES))
        is_manual = (r["method"] == "manual")
        editable = (st not in STORNO_STATUSES) and (need_input or is_manual)
        if editable:
            prefill = f"{cogs:.2f}" if cogs else ""
            badge = ('<span class="stmark man">ручной</span>' if is_manual else '')
            reset = (f'<a class="stmark rst" onclick="resetCost(\'{nm}\')" title="сбросить к МС">↺</a>'
                     if is_manual else '')
            cogs_cell = (f'<td class="num" data-v="{eff_cogs:.2f}">'
                         f'<input id="ci_{nm}" class="cinp" type="number" step="0.01" min="0" '
                         f'value="{prefill}" placeholder="0.00">'
                         f'<button class="cbtn" onclick="saveCost(\'{nm}\')">💾</button>{badge}{reset}</td>')
        elif st in STORNO_STATUSES:
            cogs_cell = f'<td class="num strk" data-v="0.00">{_fmt(cogs)}<span class="stmark">сторно</span></td>'
        elif st in LOSS_STATUSES:
            cogs_cell = f'<td class="num neg" data-v="{cogs:.2f}">{_fmt(cogs)}</td>'
        elif st == "other":
            cogs_cell = f'<td class="num strk" data-v="0.00">{_fmt(cogs)}</td>'
        else:
            cogs_cell = f'<td class="num" data-v="{cogs:.2f}">{_fmt(cogs)}</td>'
        # ячейки маржи
        if need_input:                                 # себест неизвестен → маржа не считается
            m_cell = '<td class="num mut" data-v="0.00">—</td>'
            mp_cell = '<td class="num mut" data-v="0.00">нужен себест</td>'
        elif st in STORNO_STATUSES:
            m_cell = '<td class="num mut" data-v="0.00">—</td>'
            mp_cell = '<td class="num mut" data-v="0.00">сторно</td>'
        elif st == "other":
            m_cell = '<td class="num mut" data-v="0.00">—</td>'
            mp_cell = '<td class="num mut" data-v="0.00">в пути</td>'
        elif st in LOSS_STATUSES:
            m_cell = f'<td class="num neg" data-v="{eff_m:.2f}">{_fmt(eff_m)}</td>'
            mp_cell = '<td class="num neg" data-v="-100.00">убыток</td>'
        else:
            mcls = "pos" if eff_m >= 0 else "neg"
            m_cell = f'<td class="num {mcls}" data-v="{eff_m:.2f}">{_fmt(eff_m)}</td>'
            mp_cell = (f'<td class="num {mcls}" data-v="{(eff_m/eff_sum*100):.2f}">{_pct(eff_m, eff_sum)}</td>'
                       if eff_sum else '<td class="num mut" data-v="0.00">—</td>')
        body.append(
            f'<tr{flagcls} data-status="{st}" data-method="{r["method"]}" data-cogs="{cogs:.2f}">'
            f'<td class="l">{r["demand_name"]}</td><td class="l">{r["d"]}</td>'
            f'<td class="l {cls}">{emoji} {label}</td>'
            f'{sum_cell}{cogs_cell}'
            f'<td class="l method">{METHOD_LABEL.get(r["method"], r["method"])}</td>'
            f'{m_cell}{mp_cell}</tr>')
    tm = t_sales - t_cogs
    body.append('</tbody><tfoot><tr id="dfoot">'
                f'<td class="l" colspan="3">ИТОГО: отправлений {len(rows)} · возвратов {n_ret}</td>'
                f'<td class="num">{_fmt(t_sales)}</td><td class="num">{_fmt(t_cogs)}</td>'
                f'<td></td><td class="num">{_fmt(tm)}</td><td class="num">{_pct(tm, t_sales)}</td>'
                '</tr></tfoot></table></div>')
    body.append('<div class="stleg">'
                '<span>✅ Доставлен — продан</span>'
                '<span>↩️ Возврат → наш склад (в сток, себест сторнируется)</span>'
                '<span>♻️ Брак — наш склад, дефект (себест = убыток)</span>'
                '<span>🏬 Возврат на склад Озон — остаётся в стоке FBO, себест сторнируется '
                '(спишется при продаже с FBO)</span>'
                '<span><span class="fl"></span>🔙 Отменён Озоном, но в МС возврат не проведён '
                '(на проверку — себест не сторнирован)</span></div>')
    body.append('<p class="mnote">Клик по заголовку — сортировка. Способ: «МС (FIFO)» — себест конкретной '
                'отгрузки из МойСклад; «импутация» — фолбэк по закупочной, когда FIFO по отгрузке нет. '
                '«ИТОГО» пересчитывается по отфильтрованным строкам. Статус Ozon есть в сырье '
                'с 2026-05; для более ранних отгрузок он берётся как «доставлен», если в МойСклад нет '
                'возврата.</p>')
    if not rows:
        body.append(f'<p class="mnote">За {ym} отгрузок нет.</p>')
    js = "<script>" + _js_head(acc_key, account) + _DETAIL_JS_BODY + "</script>"
    return _shell(f"Себестоимость · Ozon · {org} · {ym} · Пульт бизнеса",
                  f"Себестоимость · Ozon · {org} · {ym}",
                  f"Себестоимость по отправлениям — {ym}", "\n".join(body) + js, tab)


if __name__ == "__main__":
    print(overview_html()[:200], "...")
