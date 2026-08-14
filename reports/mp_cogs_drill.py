# поток: fin
"""reports/mp_cogs_drill.py — провал в строку «Себестоимость» отчётов МП.

Клик по ячейке месяца в строке COGS раскрывает список отгрузок, которые этот отчёт оплатил:
что за отгрузка, дата, сколько штук отчёт провёл продажей и сколько сторнировал, себест и способ
его получения, статус из раздела «Себестоимость».

Принцип (решение Сергея 2026-08-13): себестоимость в отчёте МП — только по товару, реально
реализованному покупателю. Критерий — факт начисления выручки В САМОМ ОТЧЁТЕ, а не статус
отгрузки в МойСклад. Поэтому список строится ОТ ОТЧЁТА (строки финотчёта ВБ / транзакции Озона /
заказы Маркета за месяц), а себест и статус подтягиваются к нему из проверенных таблиц раздела
«Себестоимость» (`wb_/oz_/ya_cogs_demand`).

Почему сумма списка не обязана совпасть со строкой COGS отчёта:
  * ВБ: FBO-продажи (~11–12%) документа «Отгрузка» в МС не создают → в `wb_cogs_demand` их нет,
    в отчёте они покрыты импутацией;
  * Озон/Маркет: отправления без матча в МС закрываются импутацией/группой/ручным слоем.
Разрыв показываем строкой «покрыто списком», а не прячем.
"""
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402

def _yp():
    """Хелперы страницы себестоимости — ЛЕНИВО. На импорте модуля нельзя: цикл
    ozon_mp_page → mp_cogs_drill → ya_cogs_page → ozon_mp_page роняет любой вход,
    начинающийся с ozon_mp_page (CLI reports.ozon_mp_freeze, cron границы месяца)."""
    from reports import ya_cogs_page as Y
    return Y


def _fmt(v):
    return _yp()._fmt(v)



MAX_ROWS = 300          # длиннее в раскрывающийся блок не тянем — есть полный раздел «Себестоимость»

STATUS_LABEL = {
    "done": ("✅", "Реализован"),
    "return_stock": ("↩️", "Возврат → наш склад"),
    "return_defect": ("♻️", "Возврат → брак"),
    "return_ozon": ("🏬", "Возврат на склад Озон"),
    "return_wb": ("↩️", "ВБ сторнировал продажу"),
    "unredeemed": ("🔙", "Невыкуп / отмена"),
    "unreported": ("⚠️", "Нет в отчёте (старше лага)"),
    "other": ("•", "В пути / в обработке"),
    "no_revenue": ("🚫", "Выручка не начислена (отмена/возврат)"),
}
# Статус — судьба ТОВАРА по данным МойСклад. На себестоимость отчёта он не влияет: решает факт
# начисления (и сторно) выручки самой площадкой. Поэтому «возврат → брак» с плюсовым себестом —
# не ошибка: площадка выручку не сторнировала, потери брака живут в разделе «Себестоимость».
NOTE_STATUS = ("Статус — судьба товара по данным МойСклад; себестоимость отчёта он не определяет: "
               "решает факт начисления или сторно выручки площадкой.")
METHOD_LABEL = {"ms_fifo": "МС (FIFO)", "tovar_fifo": "FIFO товара", "nabor_fifo": "FIFO набора",
                "analog_fifo": "FIFO аналога", "imputed": "импутация", "manual": "ручной"}

CSS = """<style>
.drill{background:#0f1720;padding:14px 16px;border-radius:10px;margin:6px 0 10px}
.drill h4{margin:0 0 8px;font-size:14px;font-weight:600}
.drill .dsum{font-size:12px;color:#8fa3b8;margin:0 0 10px;line-height:1.5}
.drill table{width:100%;border-collapse:collapse;font-size:12px}
.drill th{text-align:left;color:#8fa3b8;font-weight:500;padding:4px 8px;border-bottom:1px solid #24303d}
.drill td{padding:3px 8px;border-bottom:1px solid #18222c}
.drill td.n,.drill th.n{text-align:right;font-variant-numeric:tabular-nums}
.drill .mut{color:#7b8ea3}
.drill .wrap{max-height:420px;overflow:auto}
.drill .cls{float:right;cursor:pointer;color:#8fa3b8}
</style>"""

# ── выборки по площадкам: список отгрузок, которые ОТЧЁТ месяца ym провёл ────────────────────
# Все три возвращают одинаковые поля: doc (документ), dd (дата отгрузки), sold_q / ret_q (штук
# продано/сторнировано ИМЕННО В ЭТОМ отчёте), our_sum (наша цена), cogs, qty (штук в отгрузке),
# method, status.

SQL_WB = """
WITH rep AS (
  SELECT payload->>'assembly_id' doc,
         sum(CASE WHEN payload->>'supplier_oper_name'='Продажа'
                  THEN coalesce((payload->>'quantity')::numeric,0) ELSE 0 END) sold_q,
         sum(CASE WHEN payload->>'supplier_oper_name'='Возврат'
                  THEN coalesce((payload->>'quantity')::numeric,0) ELSE 0 END) ret_q,
         sum(CASE WHEN payload->>'supplier_oper_name'='Продажа'
                  THEN coalesce((payload->>'retail_price_withdisc_rub')::numeric,0) ELSE 0 END) our_sum
  FROM raw_wb_report
  WHERE account=%(acc)s AND to_char((payload->>'create_dt')::date,'YYYY-MM')=%(ym)s
    AND coalesce(payload->>'assembly_id','0') <> '0'
  GROUP BY 1)
SELECT r.doc, to_char(d.demand_date,'YYYY-MM-DD') dd, r.sold_q::float, r.ret_q::float,
       r.our_sum::float, coalesce(d.cogs,0)::float cogs, coalesce(d.qty,0)::float qty,
       coalesce(d.method,'') method, coalesce(%(st)s,'') status
FROM rep r LEFT JOIN wb_cogs_demand d ON d.account=%(acc)s AND d.demand_name=r.doc
WHERE r.sold_q <> 0 OR r.ret_q <> 0
"""

SQL_OZ = """
WITH t AS (
  SELECT payload->'posting'->>'posting_number' doc,
         CASE WHEN coalesce((payload->>'accruals_for_sale')::numeric,0) > 0 THEN 1 ELSE 0 END sold,
         CASE WHEN coalesce((payload->>'accruals_for_sale')::numeric,0) < 0
                OR payload->>'operation_type'='OperationAgentStornoDeliveredToCustomer'
              THEN 1 ELSE 0 END ret,
         coalesce((payload->>'accruals_for_sale')::numeric,0) accr
  FROM raw_ozon_transaction, jsonb_array_elements(payload->'items') it
  WHERE account=%(acc)s AND substr(payload->>'operation_date',1,7)=%(ym)s
    AND payload->'posting'->>'posting_number' IS NOT NULL AND it->>'sku' IS NOT NULL),
rep AS (SELECT doc, sum(sold) sold_q, sum(ret) ret_q,
               sum(CASE WHEN accr > 0 THEN accr ELSE 0 END) our_sum
        FROM t GROUP BY 1)
SELECT r.doc, to_char(d.demand_date,'YYYY-MM-DD') dd, r.sold_q::float, r.ret_q::float,
       r.our_sum::float, coalesce(d.cogs,0)::float cogs, coalesce(d.qty,0)::float qty,
       coalesce(d.method,'') method, coalesce(%(st)s,'') status
FROM rep r LEFT JOIN oz_cogs_demand d ON d.account=%(acc)s AND d.demand_name=r.doc
WHERE r.sold_q <> 0 OR r.ret_q <> 0
"""

SQL_YA = """
WITH rep AS (
  SELECT DISTINCT ON (payload->>'id') payload->>'id' doc, payload->>'status' st,
         (SELECT coalesce(sum((i->>'count')::numeric),0)
            FROM jsonb_array_elements(payload->'items') i) q
  FROM raw_yandex_stats_order
  WHERE account=%(acc)s AND substr(payload->>'creationDate',1,7)=%(ym)s
  ORDER BY payload->>'id', loaded_at DESC)
SELECT r.doc, to_char(d.demand_date,'YYYY-MM-DD') dd,
       CASE WHEN r.st LIKE 'CANCELLED%%' OR r.st IN ('RETURNED','PARTIALLY_RETURNED')
            THEN 0 ELSE r.q END::float sold_q,
       CASE WHEN r.st LIKE 'CANCELLED%%' OR r.st IN ('RETURNED','PARTIALLY_RETURNED')
            THEN r.q ELSE 0 END::float ret_q,
       coalesce(d.our_sum,0)::float our_sum, coalesce(d.cogs,0)::float cogs,
       coalesce(d.qty,0)::float qty, coalesce(d.method,'') method, coalesce(%(st)s,'') status
FROM rep r LEFT JOIN ya_cogs_demand d ON d.account=%(acc)s AND d.demand_name=r.doc
"""

PLATFORMS = {
    "wb":     {"sql": SQL_WB, "doc": "Сборочное задание", "url": "/reports/cost/wb",
               "neg": True, "note":
               "FBO-продажи ВБ (~11–12% оборота) документа «Отгрузка» в МойСклад не создают — "
               "в списке их нет, в строке COGS они покрыты импутацией."},
    "ozon":   {"sql": SQL_OZ, "doc": "Отправление", "url": "/reports/cost/ozon",
               "neg": True, "note":
               "Отправления без матча в МойСклад закрыты импутацией/группой/ручным слоем — "
               "в списке у них себест 0, в строке COGS они учтены."},
    "yandex": {"sql": SQL_YA, "doc": "Заказ", "url": "/reports/cost", "neg": False, "note":
               "Заказы, по которым Маркет выручку не начислил (отмена/возврат), себестоимость "
               "не формируют — показаны отдельной строкой сводки."},
}
ACC_TAB = {"wb_acc1": "acc1", "wb_acc2": "acc2", "oz_acc1": "acc1", "oz_acc2": "acc2"}


def _report_cogs(platform, account, ym):
    """COGS строки отчёта за месяц — то, что видно на странице (для честного сравнения)."""
    if platform == "yandex":
        r = db.query("SELECT cogs FROM yandex_finance_monthly WHERE account=%s AND month=%s",
                     (account, ym + "-01"))
        return float(r[0]["cogs"] or 0) if r else None
    r = db.query("""SELECT sum(cogs) c FROM margin_by_sku
                    WHERE platform=%s AND account=%s AND period_from=%s""",
                 (platform, account, ym + "-01"))
    return float(r[0]["c"] or 0) if r and r[0]["c"] is not None else None


def fragment_html(platform, account, ym):
    cfg = PLATFORMS.get(platform)
    if not cfg:
        return '<div class="drill">Неизвестная площадка</div>'
    # _ST — доводка статуса 'other' старше лага до 'unreported' (см. reports/ya_cogs_page)
    rows = db.query(cfg["sql"].replace("%(st)s", _yp()._ST), {"acc": account, "ym": ym})

    tot_cogs = tot_sold = tot_ret = 0.0
    n_zero = 0
    per_status = {}
    view = []
    for r in rows:
        # Себест считаем ДОКУМЕНТОМ, а не делением на штуки: в отгрузке МС может лежать набор
        # (одна строка отчёта = 1 шт, а в документе 4 картриджа) — деление занизило бы себест.
        # Доля отчёта: продал целиком → +cogs, сторнировал часть штук → минус эта доля,
        # чистое сторно (продажа была в прошлом отчёте) → −cogs.
        if r["sold_q"] > 0:
            eff = r["cogs"] * (r["sold_q"] - r["ret_q"]) / r["sold_q"]
        elif cfg["neg"]:
            # ВБ/Озон сторнируют выручку в ОТЧЁТЕ более позднего месяца — там же минусуем себест
            eff = -r["cogs"] if r["ret_q"] > 0 else 0.0
        else:
            # Маркет выручку по отменённому/возвращённому заказу не начисляет вовсе — не расход
            eff = 0.0
            if r["ret_q"] > 0:
                r["status"] = "no_revenue"
        tot_cogs += eff
        tot_sold += r["sold_q"]
        tot_ret += r["ret_q"]
        st = r["status"] or "—"
        s = per_status.setdefault(st, [0, 0.0])
        s[0] += 1
        s[1] += eff
        if r["sold_q"] > 0 and eff == 0:
            n_zero += 1
        view.append((r, eff))
    view.sort(key=lambda x: -abs(x[1]))

    rep_cogs = _report_cogs(platform, account, ym)
    cov = f"{tot_cogs / rep_cogs * 100:.0f}%" if rep_cogs else "—"
    tab = ACC_TAB.get(account)
    full = f'{cfg["url"]}/{tab}' if tab else cfg["url"]

    stat_line = " · ".join(
        f'{STATUS_LABEL.get(k, ("•", k))[0]} {STATUS_LABEL.get(k, ("•", k))[1]}: {v[0]} '
        f'({_fmt(v[1])})' for k, v in sorted(per_status.items(), key=lambda kv: -kv[1][1]))

    h = [CSS, '<div class="drill">',
         f'<span class="cls" onclick="mpDrillClose(this)">✕</span>',
         f'<h4>Себестоимость отчёта {ym} — отгрузки, по которым площадка начислила выручку</h4>',
         '<p class="dsum">'
         f'В отчёте: {len(view)} документов, продано {tot_sold:.0f} шт, сторнировано {tot_ret:.0f} шт. '
         f'Себест по списку: <b>{_fmt(tot_cogs)}</b>'
         + (f' — это {cov} от строки COGS отчёта ({_fmt(rep_cogs)}).' if rep_cogs else '.')
         + f'<br>{stat_line}'
         + (f'<br>Без себеста в списке: {n_zero} документов.' if n_zero else '')
         + f'<br>{NOTE_STATUS}'
         + f'<br>{cfg["note"]}'
         f' Полный проверенный список месяца — <a href="{full}">раздел «Себестоимость»</a>.'
         + (f' Показаны первые {MAX_ROWS} по величине себеста.' if len(view) > MAX_ROWS else '')
         + '</p>',
         '<div class="wrap"><table><thead><tr>'
         f'<th>{cfg["doc"]}</th><th>Дата отгрузки</th><th class="n">Продано, шт</th>'
         '<th class="n">Сторно, шт</th><th class="n">Наша цена</th><th class="n">Себест.</th>'
         '<th>Способ</th><th>Статус</th></tr></thead><tbody>']
    for r, eff in view[:MAX_ROWS]:
        em, lbl = STATUS_LABEL.get(r["status"], ("•", r["status"] or "—"))
        h.append(f'<tr><td>{r["doc"]}</td><td class="mut">{r["dd"] or "—"}</td>'
                 f'<td class="n">{r["sold_q"]:.0f}</td>'
                 f'<td class="n mut">{r["ret_q"]:.0f}</td>'
                 f'<td class="n">{_fmt(r["our_sum"])}</td><td class="n">{_fmt(eff)}</td>'
                 f'<td class="mut">{METHOD_LABEL.get(r["method"], r["method"] or "—")}</td>'
                 f'<td class="mut">{em} {lbl}</td></tr>')
    h.append('</tbody></table></div></div>')
    return "".join(h)


def page_js(platform):
    """Раскрытие ячейки месяца в строке «Себестоимость» на странице отчётов МП.
    Аккаунт берётся из data-acc секции организации, месяц — из data-ym ячейки."""
    return """<style>
#mpr td.drill{cursor:pointer;text-decoration:underline dotted rgba(255,255,255,.35);text-underline-offset:3px}
#mpr td.drill:hover{background:rgba(255,255,255,.05)}
#mpr tr.drillrow>td{padding:0 8px}
</style>
<script>
(function(){
  var P='%s';
  window.mpDrillClose=function(el){var r=el.closest('tr.drillrow'); if(r) r.remove();};
  document.addEventListener('click', function(e){
    if(!e.target||!e.target.closest) return;
    var td=e.target.closest('td.drill[data-ym]'); if(!td) return;
    var tr=td.closest('tr'); if(!tr||tr.getAttribute('data-k')!=='cogs') return;
    var sec=tr.closest('section.org'), acc=sec?sec.getAttribute('data-acc'):'';
    var nx=tr.nextElementSibling;
    if(nx&&nx.classList.contains('drillrow')){
      var same=nx.getAttribute('data-ym')===td.getAttribute('data-ym');
      nx.remove(); if(same) return;                 // повторный клик по тому же месяцу — свернуть
    }
    var row=document.createElement('tr'); row.className='drillrow';
    row.setAttribute('data-ym', td.getAttribute('data-ym'));
    var cell=document.createElement('td'); cell.colSpan=tr.children.length;
    cell.innerHTML='<div style="padding:10px;color:#8fa3b8">Загружаю отгрузки…</div>';
    row.appendChild(cell); tr.parentNode.insertBefore(row, tr.nextSibling);
    fetch('/api/mp-cogs/detail?platform='+P+'&account='+encodeURIComponent(acc)+
          '&ym='+encodeURIComponent(td.getAttribute('data-ym')))
      .then(function(r){return r.text();}).then(function(t){cell.innerHTML=t;})
      .catch(function(){cell.innerHTML='<div style="padding:10px;color:#e2726e">Не удалось загрузить</div>';});
  });
})();
</script>""" % platform
