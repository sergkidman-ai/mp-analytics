# поток: fin
"""reports/mp_cogs_page.py — общий рендер раздела «Себестоимость» для площадок с отгрузками МС.

Вёрстка и правила чтения одни на все площадки (взяты из ya_cogs_page), различия площадки живут
в конфиге CFG, который передаёт вызывающий модуль:
  reports/oz_cogs_page.py — Ozon (два юрлица),
  reports/wb_cogs_page.py — WB   (два юрлица).

Отличаются только: таблицы БД, префикс API/URL, набор статусов и что из них сторнируется, подписи
колонок и пояснения. Всё остальное — общее, чтобы правка вёрстки или логики сторно делалась
в ОДНОМ месте, а не в трёх копиях.

  overview_html(cfg, acc_key) — таблица по отчётам-месяцам (заказов/возвратов/сумма/себест/маржа);
  detail_html(cfg, acc_key, ym) — провал внутрь месяца: по каждой отгрузке.
Маржа ВАЛОВАЯ (наша цена − себест, ДО комиссий площадки). Юрлица не смешиваются.
"""
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from reports.ozon_mp_page import SHELL_CSS, REPORT_CSS, MPTABS  # noqa: E402
from reports.cost_tabs import tabs_html  # noqa: E402
from reports.ya_cogs_page import SIDEBAR_COST, PAGE_CSS, _fmt, _pct, _ST  # noqa: E402

METHOD_LABEL = {"ms_fifo": "МС (FIFO)", "tovar_fifo": "FIFO товара", "imputed": "импутация",
                "manual": "ручной"}

# фильтр по наличию себеста (для поиска пустых → ручной ввод)
_COST_FILTER = [("", "все"), ("zero", "= 0 (нужен ввод)"), ("pos", "> 0")]


def _js_head(cfg, acc_key, account):
    return f"var ACC='{account}';var ACCKEY='{acc_key}';var API='/api/{cfg['api']}';"


def _overview_js(cfg, acc_key, account):
    return """<script>
""" + _js_head(cfg, acc_key, account) + """
function freezeMonth(ym){
  if(!confirm('Закрыть месяц '+ym+'? Себест станет финальным, коллектор перестанет его пересобирать.'))return;
  fetch(API+'/freeze',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({account:ACC,ym:ym})})
   .then(function(r){return r.json();}).then(function(d){
     if(d.ok){location.reload();}
     else{alert((d.msg||'Не удалось закрыть')+(d.zero?'\\n'+d.zero.map(function(z){return z.demand_name;}).join(', '):''));}});
}
function unfreezeMonth(ym){
  if(!confirm('Разморозить '+ym+'? Следующий прогон коллектора соберёт месяц заново из МойСклад.'))return;
  fetch(API+'/unfreeze',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({account:ACC,ym:ym})})
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


def overview_html(cfg, acc_key):
    account, org, tab = cfg["accounts"][acc_key]
    ret, loss = list(cfg["ret_statuses"]), list(cfg["loss_statuses"])
    rows = db.query(f"""
        WITH t AS (SELECT ym, our_sum, cogs, {_ST} status FROM {cfg['table']} WHERE account=%s)
        SELECT to_char(ym,'YYYY-MM') ym,
               count(*)                                                        orders,
               count(*) FILTER (WHERE status = ANY(%s))                        returns,
               coalesce(sum(our_sum) FILTER (WHERE status = ANY(%s)),0)::float ret_sales,
               coalesce(sum(cogs)    FILTER (WHERE status = ANY(%s)),0)::float ret_cogs,
               coalesce(sum(our_sum) FILTER (WHERE status='done'),0)::float    sales,
               coalesce(sum(cogs) FILTER (WHERE status='done' OR status = ANY(%s)),0)::float cogs
        FROM t
        GROUP BY ym ORDER BY ym DESC
    """, (account, ret, ret, ret, loss))
    frozen = {r["ym"].strftime("%Y-%m"): r["closed_at"] for r in db.query(
        f"SELECT ym, closed_at FROM {cfg['frozen_table']} WHERE account=%s", (account,))}
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
            f'<tr class="row-link" onclick="location.href=\'{cfg["url"]}/{acc_key}/{ym}\'">'
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
    for note in cfg["overview_notes"](org, account):
        body.append(f'<p class="mnote">{note}</p>')
    body.append('<p class="mnote"><b>🔒 Закрытый месяц</b> — себест финальный, коллектор его не '
                'пересобирает (МойСклад лишний раз не дёргается). Закрывать имеет смысл после проверки '
                'месяца; «Разморозить» открывает месяц обратно — следующий прогон соберёт его заново. '
                'Закрыть нельзя, пока в месяце есть продажи с нулевым себестом (сначала заполнить).</p>')
    if not rows:
        body = ['<p class="mnote">Данных нет. Запустите сбор: '
                f'<code>./venv/bin/python -m {cfg["collector"]}</code></p>']
    pl = cfg["platform"]
    return _shell(f"Себестоимость · {pl} · {org} · Пульт бизнеса",
                  f"Себестоимость · {pl} · {org}", "Себестоимость по отчётам (месяцам)",
                  "\n".join(body) + _overview_js(cfg, acc_key, account), tab)


# фильтр по столбцам + сортировка + пересчёт ИТОГО по видимым строкам (vanilla JS, self-contained)
_DETAIL_JS_BODY = """
function saveCost(nm){
  var inp=document.getElementById('ci_'+nm); if(!inp) return;
  var v=parseFloat(inp.value); if(isNaN(v)||v<0){alert('Введите себест ≥ 0');return;}
  fetch(API+'/manual',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({account:ACC,demand_name:nm,cogs:v,author:'сотрудник'})})
   .then(function(r){return r.json();}).then(function(d){
     if(d.ok){location.reload();}else{alert('Ошибка: '+(d.error||'не сохранено'));}});
}
function resetCost(nm){
  if(!confirm('Сбросить ручной себест и пересчитать по МС?'))return;
  fetch(API+'/reset',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({account:ACC,demand_name:nm})})
   .then(function(r){return r.json();}).then(function(){location.reload();});
}
(function(){
  var tbl=document.getElementById('dtbl'); if(!tbl) return;
  var tb=tbl.tBodies[0], rows=[].slice.call(tb.rows);
  var fst=document.getElementById('fst'), fmt=document.getElementById('fmt'),
      fc=document.getElementById('fcost'),
      fq=document.getElementById('fq'), foot=document.getElementById('dfoot'),
      cnt=document.getElementById('fcnt');
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
    foot.innerHTML='<td class="l" colspan="3">ИТОГО: '+DOCPL+' '+n+' · возвратов '+ret+'</td>'+
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


def detail_html(cfg, acc_key, ym):
    """ym в формате YYYY-MM."""
    account, org, tab = cfg["accounts"][acc_key]
    storno, loss = cfg["storno_statuses"], cfg["loss_statuses"]
    pending = cfg["pending_statuses"]     # ещё не реализовано: ни оборота, ни себеста в месяце
    pending_label = cfg["pending_label"]
    rows = db.query(f"""
        SELECT demand_name, to_char(demand_date,'YYYY-MM-DD') d, {_ST} status, status_raw,
               coalesce(our_sum,0)::float our_sum, coalesce(cogs,0)::float cogs,
               coalesce(qty,0)::float qty, method
        FROM {cfg['table']}
        WHERE account=%s AND to_char(ym,'YYYY-MM')=%s
        ORDER BY demand_date, demand_name
    """, (account, ym))
    opts = "".join(f'<option value="{v}">{lbl}</option>' for v, lbl in cfg["status_filter"])
    copts = "".join(f'<option value="{v}">{lbl}</option>' for v, lbl in _COST_FILTER)
    body = [f'<a class="backlink" href="{cfg["url"]}/{acc_key}">◀ Назад к отчётам</a>',
            '<div class="ftbar">'
            f'<label>Статус <select id="fst">{opts}</select></label>'
            '<label>Способ <select id="fmt">'
            '<option value="">все</option><option value="ms_fifo">МС (FIFO)</option>'
            '<option value="tovar_fifo">FIFO товара</option>'
            '<option value="imputed">импутация</option><option value="manual">ручной</option>'
            '</select></label>'
            f'<label>Себест <select id="fcost">{copts}</select></label>'
            f'<input id="fq" placeholder="{cfg["search_ph"]}" size="22">'
            '<span class="cnt" id="fcnt"></span></div>',
            '<div class="tblwrap"><table class="ct" id="dtbl"><thead><tr>'
            f'<th class="l srt" data-col="0" data-type="text">{cfg["doc_col"]}</th>'
            '<th class="l srt" data-col="1" data-type="text">Дата</th>'
            f'<th class="l srt" data-col="2" data-type="text">{cfg["status_col"]}</th>'
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
        emoji, label, cls = cfg["status_label"].get(st, ("•", r["status_raw"] or "—", "mut"))
        if st in cfg["ret_statuses"]:
            n_ret += 1
        # эффективные (реализованные) значения: сторно возвратов в продаваемый сток, потери = убыток
        if st in storno:
            eff_sum, eff_cogs = 0.0, 0.0           # товар вернулся в продаваемый сток → net-neutral
        elif st in loss:
            eff_sum, eff_cogs = 0.0, cogs          # потеря: оборот 0, себест остаётся убытком
        elif st in pending:
            eff_sum, eff_cogs = 0.0, 0.0           # ещё не реализовано: деньги не подтверждены
        else:
            eff_sum, eff_cogs = our_sum, cogs      # done
        eff_m = eff_sum - eff_cogs
        t_sales += eff_sum; t_cogs += eff_cogs
        flagcls = ' class="flag"' if st in cfg["flag_statuses"] else ''
        # ячейка «Сумма»: у не-продаж оборот не реализован (перечёркнут), data-v = эффективный
        sum_cell = (f'<td class="num strk" data-v="0.00">{_fmt(our_sum)}</td>' if st != "done"
                    else f'<td class="num" data-v="{our_sum:.2f}">{_fmt(our_sum)}</td>')
        # ячейка «Себест». Редактируемая для: реальных продаж с 0 себеста (нужен ввод) и ручных
        # (можно поправить/сбросить). Сторно не редактируем (net-neutral).
        need_input = (cogs == 0 and (st == "done" or st in loss))
        is_manual = (r["method"] == "manual")
        editable = (st not in storno) and (need_input or is_manual)
        if editable:
            prefill = f"{cogs:.2f}" if cogs else ""
            badge = ('<span class="stmark man">ручной</span>' if is_manual else '')
            reset = (f'<a class="stmark rst" onclick="resetCost(\'{nm}\')" title="сбросить к МС">↺</a>'
                     if is_manual else '')
            cogs_cell = (f'<td class="num" data-v="{eff_cogs:.2f}">'
                         f'<input id="ci_{nm}" class="cinp" type="number" step="0.01" min="0" '
                         f'value="{prefill}" placeholder="0.00">'
                         f'<button class="cbtn" onclick="saveCost(\'{nm}\')">💾</button>{badge}{reset}</td>')
        elif st in storno:
            cogs_cell = f'<td class="num strk" data-v="0.00">{_fmt(cogs)}<span class="stmark">сторно</span></td>'
        elif st in loss:
            cogs_cell = f'<td class="num neg" data-v="{cogs:.2f}">{_fmt(cogs)}</td>'
        elif st in pending:
            cogs_cell = f'<td class="num strk" data-v="0.00">{_fmt(cogs)}</td>'
        else:
            cogs_cell = f'<td class="num" data-v="{cogs:.2f}">{_fmt(cogs)}</td>'
        # ячейки маржи
        if need_input:                                 # себест неизвестен → маржа не считается
            m_cell = '<td class="num mut" data-v="0.00">—</td>'
            mp_cell = '<td class="num mut" data-v="0.00">нужен себест</td>'
        elif st in storno:
            m_cell = '<td class="num mut" data-v="0.00">—</td>'
            mp_cell = '<td class="num mut" data-v="0.00">сторно</td>'
        elif st in pending:
            m_cell = '<td class="num mut" data-v="0.00">—</td>'
            mp_cell = f'<td class="num mut" data-v="0.00">{pending_label}</td>'
        elif st in loss:
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
                f'<td class="l" colspan="3">ИТОГО: {cfg["doc_pl"]} {len(rows)} · возвратов {n_ret}</td>'
                f'<td class="num">{_fmt(t_sales)}</td><td class="num">{_fmt(t_cogs)}</td>'
                f'<td></td><td class="num">{_fmt(tm)}</td><td class="num">{_pct(tm, t_sales)}</td>'
                '</tr></tfoot></table></div>')
    body.append('<div class="stleg">' + "".join(f'<span>{s}</span>' for s in cfg["legend"]) + '</div>')
    body.append(f'<p class="mnote">{cfg["detail_note"]}</p>')
    if not rows:
        body.append(f'<p class="mnote">За {ym} отгрузок нет.</p>')
    ret_js = ",".join(f"{s}:1" for s in cfg["ret_statuses"])
    js = ("<script>" + _js_head(cfg, acc_key, account)
          + f"var RET={{{ret_js}}};var DOCPL='{cfg['doc_pl']}';"
          + _DETAIL_JS_BODY + "</script>")
    pl = cfg["platform"]
    return _shell(f"Себестоимость · {pl} · {org} · {ym} · Пульт бизнеса",
                  f"Себестоимость · {pl} · {org} · {ym}",
                  f"Себестоимость по {cfg['doc_dat']} — {ym}", "\n".join(body) + js, tab)
