# поток: rev
"""reports/ya_cogs_page.py — раздел «Себестоимость» (Яндекс.Маркет): себест по отгрузкам.

Два уровня из кэша ya_cogs_demand (collectors/ya_cogs_demand.py):
  overview_html — таблица по отчётам-месяцам (заказов/возвратов/сумма/себест/валовая маржа);
  detail_html   — провал внутрь месяца: по каждой отгрузке (№, дата, статус, сумма, себест, способ, маржа).
Маржа здесь ВАЛОВАЯ (наша цена − себест, до комиссий Маркета). Рендер динамический (запрос к БД).
"""
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from reports.ozon_mp_page import SHELL_CSS, REPORT_CSS, SIDEBAR, MPTABS  # noqa: E402

# левое меню: активен пункт «Себестоимость» (не «Отчёты МП»)
SIDEBAR_COST = (SIDEBAR
    .replace('<a href="/reports" class="cur">📋 Отчёты МП</a>', '<a href="/reports">📋 Отчёты МП</a>')
    .replace('<a href="/reports/cost">💰 Себестоимость</a>',
             '<a href="/reports/cost" class="cur">💰 Себестоимость</a>'))

RET_STATUSES = ("return_stock", "return_defect", "unredeemed")

STATUS_LABEL = {
    "done": ("✅", "Выполнен", ""),
    "return_stock": ("↩️", "Возврат → наш склад", "warn"),
    "return_defect": ("♻️", "Возврат → наш склад (брак)", "warn"),
    "unredeemed": ("🔙", "Невыкуп → передан нам · в МС нет возврата", "warn"),
    "other": ("•", "В пути / в обработке", "mut"),
}
# статусы, где Маркет вернул товар нам, но в МС возврат не проведён → подсветить на проверку
FLAG_STATUSES = ("unredeemed",)
# товар вернулся в ПРОДАВАЕМЫЙ сток → себест сторнируется, строка net-neutral (оборот и себест = 0).
# Брак (return_defect) НЕ сторнируется: товар нельзя перепродать → себест остаётся убытком.
STORNO_STATUSES = ("return_stock", "unredeemed")
METHOD_LABEL = {"ms_fifo": "МС (FIFO)", "imputed": "импутация"}

PAGE_CSS = """
.ct{width:100%;border-collapse:collapse;margin:8px 0 4px;font-size:14px}
.ct th,.ct td{padding:9px 12px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
.ct th{color:var(--mut);font-weight:600;text-align:right;border-bottom:2px solid var(--line)}
.ct th:first-child,.ct td:first-child{text-align:left}
.ct td.l,.ct th.l{text-align:left}
.ct tbody tr.row-link{cursor:pointer}
.ct tbody tr.row-link:hover{background:var(--card)}
.ct td.num,.ct th.num{font-variant-numeric:tabular-nums}
.ct tfoot td{font-weight:700;border-top:2px solid var(--line);border-bottom:none}
.ct .pos{color:var(--pos)}
.ct .neg{color:var(--neg)}
.ct .warn{color:var(--warn)}
.ct .mut{color:var(--mut)}
.stleg{display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 0;font-size:12.5px;color:var(--mut)}
.method{font-size:12px;color:var(--mut)}
.backlink{display:inline-block;margin:2px 0 10px;color:var(--acc);text-decoration:none;font-weight:600}
.backlink:hover{text-decoration:underline}
.mnote{color:var(--mut);font-size:12.5px;margin:6px 0 0}
.tblwrap{overflow-x:auto}
.ftbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:4px 0 10px}
.ftbar label{font-size:13px;color:var(--mut);display:flex;gap:5px;align-items:center}
.ftbar select,.ftbar input{padding:5px 8px;border:1px solid var(--line);border-radius:7px;
  background:var(--card);color:var(--txt);font-size:13px}
.ftbar .cnt{color:var(--mut);font-size:12.5px}
.ct th.srt{cursor:pointer;user-select:none}
.ct th.srt:hover{color:var(--txt)}
.ct th[data-dir]:not([data-dir=""])::after{content:" " attr(data-dir);color:var(--acc)}
.ct tr.flag td{background:var(--warn-s)}
.ct .strk{text-decoration:line-through;color:var(--mut)}
.stmark{font-size:10.5px;color:var(--warn);border:1px solid var(--warn);border-radius:4px;
  padding:0 4px;margin-left:5px;text-decoration:none;white-space:nowrap}
.stleg .fl{display:inline-block;width:11px;height:11px;border-radius:3px;background:var(--warn-s);
  border:1px solid var(--warn);vertical-align:-1px;margin-right:4px}
"""


def _fmt(x):
    return f"{(x or 0):,.0f}".replace(",", " ")


def _pct(m, s):
    return f"{(m / s * 100):.1f}%" if s else "—"


def _shell(title, eyebrow, h1, body, rtab_cur):
    tabs = {
        "cost": ('<a class="rtab cur">🟡 Яндекс Маркет</a>'),
    }
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
    {tabs[rtab_cur]}
  </div>
  <p class="eyebrow">{eyebrow}</p>
  <h1>{h1}</h1>
{body}
</main>
</body>
</html>"""


def overview_html(account="ya_acc1"):
    rows = db.query("""
        SELECT to_char(ym,'YYYY-MM') ym,
               count(*)                                                        orders,
               count(*) FILTER (WHERE status = ANY(%s))                        returns,
               coalesce(sum(our_sum) FILTER (WHERE status = ANY(%s)),0)::float ret_sales,
               coalesce(sum(cogs)    FILTER (WHERE status = ANY(%s)),0)::float ret_cogs,
               coalesce(sum(our_sum) FILTER (WHERE status='done'),0)::float    sales,
               coalesce(sum(cogs) FILTER (WHERE status IN ('done','return_defect')),0)::float cogs
        FROM ya_cogs_demand WHERE account=%s
        GROUP BY ym ORDER BY ym DESC
    """, (list(RET_STATUSES), list(RET_STATUSES), list(RET_STATUSES), account))
    body = ['<table class="ct"><thead><tr>'
            '<th class="l">Отчёт</th><th class="num">Заказов</th><th class="num">Возвратов</th>'
            '<th class="num">Возвр.: сумма<br>(наша цена)</th><th class="num">Возвр.: себест</th>'
            '<th class="num">Продажи (наша цена)</th><th class="num">Себестоимость</th>'
            '<th class="num">Валовая маржа</th><th class="num">Маржа&nbsp;%</th></tr></thead><tbody>']
    t_ord = t_ret = 0
    t_sales = t_cogs = t_rs = t_rc = 0.0
    for r in rows:
        m = r["sales"] - r["cogs"]
        t_ord += r["orders"]; t_ret += r["returns"]
        t_sales += r["sales"]; t_cogs += r["cogs"]
        t_rs += r["ret_sales"]; t_rc += r["ret_cogs"]
        mcls = "pos" if m >= 0 else "neg"
        body.append(
            f'<tr class="row-link" onclick="location.href=\'/reports/cost/{r["ym"]}\'">'
            f'<td class="l">▸ {r["ym"]}</td>'
            f'<td class="num">{r["orders"]}</td>'
            f'<td class="num">{r["returns"]}</td>'
            f'<td class="num mut">{_fmt(r["ret_sales"])}</td>'
            f'<td class="num mut">{_fmt(r["ret_cogs"])}</td>'
            f'<td class="num">{_fmt(r["sales"])}</td>'
            f'<td class="num">{_fmt(r["cogs"])}</td>'
            f'<td class="num {mcls}">{_fmt(m)}</td>'
            f'<td class="num {mcls}">{_pct(m, r["sales"])}</td></tr>')
    tm = t_sales - t_cogs
    body.append('</tbody><tfoot><tr>'
                f'<td class="l">ИТОГО</td><td class="num">{t_ord}</td><td class="num">{t_ret}</td>'
                f'<td class="num mut">{_fmt(t_rs)}</td><td class="num mut">{_fmt(t_rc)}</td>'
                f'<td class="num">{_fmt(t_sales)}</td><td class="num">{_fmt(t_cogs)}</td>'
                f'<td class="num">{_fmt(tm)}</td><td class="num">{_pct(tm, t_sales)}</td>'
                '</tr></tfoot></table>')
    body.append('<p class="mnote">Отчёт = месяц отгрузки. Маржа <b>валовая</b> (наша цена − себестоимость, '
                'ДО комиссий Маркета; полная чистая — во вкладке «Отчёты МП»). Себест — FIFO конкретной '
                'отгрузки из МойСклад. <b>Сторно:</b> возвраты в сток и невыкупы (товар вернулся к нам) '
                'исключены из «Продажи»/«Себестоимость» (net-neutral); Брак — себест остаётся убытком. '
                'Колонки <b>«Возвр.»</b> — сумма и себест по всем возвратам (сток + невыкуп + брак), '
                'т.е. сколько сторнировано/ушло в возвраты. Клик по строке — детализация по заказам.</p>')
    if not rows:
        body = ['<p class="mnote">Данных нет. Запустите сбор: '
                '<code>./venv/bin/python -m collectors.ya_cogs_demand</code></p>']
    return _shell("Себестоимость · Яндекс · Пульт бизнеса",
                  "Себестоимость · Яндекс Маркет", "Себестоимость по отчётам (месяцам)",
                  "\n".join(body), "cost")


_STATUS_FILTER = [
    ("", "все статусы"),
    ("done", "✅ Выполнен"),
    ("return_stock", "↩️ Возврат → наш склад"),
    ("return_defect", "♻️ Брак"),
    ("unredeemed", "🔙 Невыкуп → склад Маркета"),
    ("other", "• В пути / в обработке"),
]

# фильтр по столбцам + сортировка + пересчёт ИТОГО по видимым строкам (vanilla JS, self-contained)
DETAIL_JS = """<script>
(function(){
  var tbl=document.getElementById('dtbl'); if(!tbl) return;
  var tb=tbl.tBodies[0], rows=[].slice.call(tb.rows);
  var fst=document.getElementById('fst'), fmt=document.getElementById('fmt'),
      fq=document.getElementById('fq'), foot=document.getElementById('dfoot'),
      cnt=document.getElementById('fcnt');
  var RET={return_stock:1,return_defect:1,unredeemed:1};
  function nf(x){return Math.round(x).toLocaleString('ru-RU').replace(/\\u00A0/g,' ').replace(/,/g,' ');}
  function apply(){
    var s=fst.value, m=fmt.value, q=fq.value.trim().toLowerCase();
    var n=0,ret=0,sales=0,cogs=0;
    rows.forEach(function(r){
      var ok=(!s||r.dataset.status===s)&&(!m||r.dataset.method===m)&&
             (!q||r.cells[0].textContent.toLowerCase().indexOf(q)>=0);
      r.style.display=ok?'':'none';
      if(ok){n++; sales+=+r.cells[3].dataset.v; cogs+=+r.cells[4].dataset.v;
             if(RET[r.dataset.status])ret++;}
    });
    var mg=sales-cogs;
    foot.innerHTML='<td class="l" colspan="3">ИТОГО: заказов '+n+' · возвратов '+ret+'</td>'+
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
  fq.addEventListener('input',apply); apply();
})();
</script>"""


def detail_html(account, ym):
    """ym в формате YYYY-MM."""
    rows = db.query("""
        SELECT demand_name, to_char(demand_date,'YYYY-MM-DD') d, status, status_raw,
               coalesce(our_sum,0)::float our_sum, coalesce(cogs,0)::float cogs,
               coalesce(qty,0)::float qty, method
        FROM ya_cogs_demand
        WHERE account=%s AND to_char(ym,'YYYY-MM')=%s
        ORDER BY demand_date, demand_name
    """, (account, ym))
    opts = "".join(f'<option value="{v}">{lbl}</option>' for v, lbl in _STATUS_FILTER)
    body = ['<a class="backlink" href="/reports/cost">◀ Назад к отчётам</a>',
            '<div class="ftbar">'
            f'<label>Статус <select id="fst">{opts}</select></label>'
            '<label>Способ <select id="fmt">'
            '<option value="">все</option><option value="ms_fifo">МС (FIFO)</option>'
            '<option value="imputed">импутация</option></select></label>'
            '<input id="fq" placeholder="поиск по № отгрузки…" size="18">'
            '<span class="cnt" id="fcnt"></span></div>',
            '<div class="tblwrap"><table class="ct" id="dtbl"><thead><tr>'
            '<th class="l srt" data-col="0" data-type="text">№ отгрузки</th>'
            '<th class="l srt" data-col="1" data-type="text">Дата</th>'
            '<th class="l srt" data-col="2" data-type="text">Статус (Маркет)</th>'
            '<th class="num srt" data-col="3" data-type="num">Сумма</th>'
            '<th class="num srt" data-col="4" data-type="num">Себест.</th>'
            '<th class="l srt" data-col="5" data-type="text">Способ</th>'
            '<th class="num srt" data-col="6" data-type="num">Маржа&nbsp;₽</th>'
            '<th class="num srt" data-col="7" data-type="num">Маржа&nbsp;%</th></tr></thead><tbody>']
    t_sales = t_cogs = 0.0
    n_ret = 0
    for r in rows:
        st = r["status"]
        our_sum, cogs = r["our_sum"], r["cogs"]
        emoji, label, cls = STATUS_LABEL.get(st, ("•", r["status_raw"] or "—", "mut"))
        if st in RET_STATUSES:
            n_ret += 1
        # эффективные (реализованные) значения: сторно возвратов в сток/невыкупов, Брак = убыток
        if st in STORNO_STATUSES:
            eff_sum, eff_cogs = 0.0, 0.0           # товар вернулся в сток → net-neutral
        elif st == "return_defect":
            eff_sum, eff_cogs = 0.0, cogs          # Брак: оборот 0, себест — убыток
        else:
            eff_sum, eff_cogs = our_sum, cogs      # done
        eff_m = eff_sum - eff_cogs
        t_sales += eff_sum; t_cogs += eff_cogs
        flagcls = ' class="flag"' if st in FLAG_STATUSES else ''
        # ячейка «Сумма»: у не-продаж оборот не реализован (перечёркнут), data-v = эффективный
        sum_cell = (f'<td class="num strk" data-v="0.00">{_fmt(our_sum)}</td>' if st != "done"
                    else f'<td class="num" data-v="{our_sum:.2f}">{_fmt(our_sum)}</td>')
        # ячейка «Себест»
        if st in STORNO_STATUSES:
            cogs_cell = f'<td class="num strk" data-v="0.00">{_fmt(cogs)}<span class="stmark">сторно</span></td>'
        elif st == "return_defect":
            cogs_cell = f'<td class="num neg" data-v="{cogs:.2f}">{_fmt(cogs)}</td>'
        else:
            cogs_cell = f'<td class="num" data-v="{cogs:.2f}">{_fmt(cogs)}</td>'
        # ячейки маржи
        if st in STORNO_STATUSES:
            m_cell = '<td class="num mut" data-v="0.00">—</td>'
            mp_cell = '<td class="num mut" data-v="0.00">сторно</td>'
        elif st == "return_defect":
            m_cell = f'<td class="num neg" data-v="{eff_m:.2f}">{_fmt(eff_m)}</td>'
            mp_cell = '<td class="num neg" data-v="-100.00">убыток</td>'
        else:
            mcls = "pos" if eff_m >= 0 else "neg"
            m_cell = f'<td class="num {mcls}" data-v="{eff_m:.2f}">{_fmt(eff_m)}</td>'
            mp_cell = (f'<td class="num {mcls}" data-v="{(eff_m/eff_sum*100):.2f}">{_pct(eff_m, eff_sum)}</td>'
                       if eff_sum else '<td class="num mut" data-v="0.00">—</td>')
        body.append(
            f'<tr{flagcls} data-status="{st}" data-method="{r["method"]}">'
            f'<td class="l">{r["demand_name"]}</td><td class="l">{r["d"]}</td>'
            f'<td class="l {cls}">{emoji} {label}</td>'
            f'{sum_cell}{cogs_cell}'
            f'<td class="l method">{METHOD_LABEL.get(r["method"], r["method"])}</td>'
            f'{m_cell}{mp_cell}</tr>')
    tm = t_sales - t_cogs
    body.append('</tbody><tfoot><tr id="dfoot">'
                f'<td class="l" colspan="3">ИТОГО: заказов {len(rows)} · возвратов {n_ret}</td>'
                f'<td class="num">{_fmt(t_sales)}</td><td class="num">{_fmt(t_cogs)}</td>'
                f'<td></td><td class="num">{_fmt(tm)}</td><td class="num">{_pct(tm, t_sales)}</td>'
                '</tr></tfoot></table></div>')
    body.append('<div class="stleg">'
                '<span>✅ Выполнен — доставлен/продан</span>'
                '<span>↩️ Возврат → наш склад (в сток)</span>'
                '<span>♻️ Брак — наш склад, дефект</span>'
                '<span><span class="fl"></span>🔙 Невыкуп → передан нам, но в МС возврат не проведён '
                '(на проверку — себест не сторнирован)</span></div>')
    body.append('<p class="mnote">Клик по заголовку — сортировка. Способ: «МС (FIFO)» — себест конкретной '
                'отгрузки из МойСклад; «импутация» — фолбэк по закупочной, когда FIFO по отгрузке нет. '
                '«ИТОГО» пересчитывается по отфильтрованным строкам.</p>')
    if not rows:
        body.append(f'<p class="mnote">За {ym} отгрузок нет.</p>')
    return _shell(f"Себестоимость · {ym} · Пульт бизнеса",
                  f"Себестоимость · Яндекс Маркет · {ym}",
                  f"Себестоимость по заказам — {ym}", "\n".join(body) + DETAIL_JS, "cost")


if __name__ == "__main__":
    print(overview_html()[:200], "...")
