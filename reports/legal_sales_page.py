# поток: fin
"""Раздел «Отчёты продаж юрлицам» из проверенного снимка МойСклада."""
import html
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from reports import mp_tabs
from reports.ozon_mp_page import SHELL_CSS, REPORT_CSS, SIDEBAR, MPTABS

SOURCE = Path(__file__).resolve().parent / "data/ms_legal_sales/report.json"


def _rub(value):
    if value is None:
        return "—"
    return f"{Decimal(str(value)):,.2f}".replace(",", " ").replace(".", ",")


def _totals(rows):
    paid = [r["paid"] for r in rows if r["paid"] is not None]
    return {"count": len(rows), "sale": sum((Decimal(r["sale"]) for r in rows), Decimal(0)),
            "paid": sum((Decimal(x) for x in paid), Decimal(0)) if len(paid) == len(rows) else None,
            "cogs": sum((Decimal(r["cogs"]) for r in rows), Decimal(0))}


def _summary(label, totals, cls=""):
    return (f'<div class="ls-grid {cls}"><span>{html.escape(label)}</span>'
            f'<span>{totals["count"]}</span><span>{_rub(totals["sale"])} ₽</span>'
            f'<span>{_rub(totals["paid"])} ₽</span><span>{_rub(totals["cogs"])} ₽</span></div>')


def render():
    if SOURCE.exists():
        data = json.loads(SOURCE.read_text(encoding="utf-8"))
        rows = data.get("rows", [])
    else:
        data, rows = {}, []
    quarters = defaultdict(list)
    for row in rows:
        year, month = map(int, row["month"].split("-"))
        quarters[(year, (month - 1) // 3 + 1)].append(row)
    parts = []
    for (year, quarter), qrows in sorted(quarters.items(), reverse=True):
        parts.append(f'<section class="ls-quarter"><h2>{quarter} квартал {year}</h2>')
        parts.append(_summary("Итого за квартал", _totals(qrows), "ls-total"))
        month_groups = defaultdict(list)
        for row in qrows:
            month_groups[row["month"]].append(row)
        for month, mrows in sorted(month_groups.items()):
            parts.append('<details class="ls-month"><summary>')
            parts.append(_summary(f'{month[5:]}.{month[:4]}', _totals(mrows)))
            parts.append('</summary><div class="ls-scroll"><table><thead><tr>'
                         '<th>Дата отгрузки</th><th>Отгрузка</th><th>Контрагент</th>'
                         '<th>Проект</th><th>Продажа</th><th>Оплата</th><th>Себестоимость</th>'
                         '</tr></thead><tbody>')
            for row in mrows:
                esc = html.escape
                link = ("https://online.moysklad.ru/app/#demand/edit?id=" + row["demand_id"])
                demand_label = row["demand_name"] or row["order_name"]
                exchange_label = " · обмен" if row.get("order_status") == "Претензия решена обмен" else ""
                completion = row.get("completion_demand_name")
                completion_cell = ""
                if completion and row.get("completion_demand_id"):
                    completion_link = ("https://online.moysklad.ru/app/#demand/edit?id="
                                       + row["completion_demand_id"])
                    completion_date = row.get("completion_date") or ""
                    completion_label = (completion + " (" + completion_date[8:10] + "."
                                        + completion_date[5:7] + ")") if completion_date else completion
                    completion_cell = (f' · досылка <a href="{esc(completion_link)}" '
                                       f'target="_blank" rel="noopener">{esc(completion_label)}</a>')
                no_link = row["paid"] is not None and Decimal(str(row["paid"])) == 0 and Decimal(str(row["sale"])) > 0
                paid_cell = (_rub(row["paid"]) + (' <span class="ls-warn" title="Связанный входящий платёж в МС не найден">⚠</span>'
                                                  if no_link else ''))
                agent_cell = esc(row["agent"] or "")
                if row.get("demand_agent_discrepancy"):
                    agent_cell += (' <span class="ls-warn" title="В отгрузке указан другой контрагент: '
                                   + esc(row["demand_agent_discrepancy"], quote=True) + '">⚠</span>')
                parts.append(f'<tr><td>{esc(row["date"][8:10] + "." + row["date"][5:7] + "." + row["date"][:4])}</td>'
                             f'<td><a href="{esc(link)}" target="_blank" rel="noopener">{esc(demand_label)}</a>{exchange_label}{completion_cell}</td>'
                             f'<td>{agent_cell}</td><td>{esc(row["project"])}</td>'
                             f'<td class="num">{_rub(row["sale"])}</td>'
                             f'<td class="num">{paid_cell}</td>'
                             f'<td class="num">{_rub(row["cogs"])}</td></tr>')
            parts.append('</tbody></table></div></details>')
        parts.append('</section>')
    if not parts:
        parts.append('<p class="ls-empty">Данные ещё собираются.</p>')
    updated = html.escape(data.get("updated_at", "—"))
    year_total = _summary("С начала года", _totals(rows), "ls-total") if rows else ""
    return f'''<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Отчёты продаж юрлицам · Пульт бизнеса</title>
<style>{SHELL_CSS}{REPORT_CSS}
.ls-note{{color:#667788;font-size:13px;line-height:1.55;margin:8px 0 18px}}
.ls-quarter{{margin:20px 0 26px;border:1px solid var(--line);border-radius:12px;padding:16px}}
.ls-quarter h2{{margin:0 0 12px;font-size:20px}}
.ls-head,.ls-grid{{display:grid;grid-template-columns:minmax(130px,1.3fr) 90px repeat(3,minmax(110px,1fr));gap:8px;align-items:center}}
.ls-head{{font-size:11px;color:var(--muted,#667);font-weight:700;text-transform:uppercase;padding:0 12px 6px}}
.ls-grid{{padding:11px 12px;font-size:14px;font-variant-numeric:tabular-nums}}
.ls-grid span:not(:first-child){{text-align:right}}
.ls-total{{background:var(--card,#f6f8fa);border-radius:8px;font-weight:750}}
.ls-month{{border-bottom:1px solid var(--line)}}
.ls-month summary{{cursor:pointer;list-style:none}}
.ls-month summary::-webkit-details-marker{{display:none}}
.ls-month summary .ls-grid span:first-child::before{{content:'▸ ';color:var(--muted,#667)}}
.ls-month[open] summary .ls-grid span:first-child::before{{content:'▾ '}}
.ls-month summary:hover{{background:var(--card,#f6f8fa)}}
.ls-scroll{{overflow-x:auto;padding:4px 0 12px}}
.ls-scroll table{{width:100%;border-collapse:collapse;font-size:13px}}
.ls-scroll th,.ls-scroll td{{padding:7px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}}
.ls-scroll th{{color:var(--muted,#667);font-size:11px}}
.ls-scroll td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.ls-scroll td:nth-child(3){{white-space:normal;min-width:190px}}
.ls-warn{{color:#d68a20;cursor:help}}
@media(prefers-color-scheme:dark){{.ls-note{{color:#9aa9bc}}}}
@media(max-width:700px){{.ls-head,.ls-grid{{grid-template-columns:minmax(80px,1fr) 35px repeat(3,minmax(85px,1fr));gap:3px;font-size:11px;padding-left:3px;padding-right:3px}}.ls-quarter{{padding:10px}}}}
</style></head><body><header>{SIDEBAR}</header><main id="mpr">
<nav class="mptabs">{MPTABS}</nav><div class="rtabs">{mp_tabs.tabs_html('legal')}</div>
<p class="eyebrow">Отчёты · Продажи юрлицам</p><h1>Отчёты продаж юрлицам · Цифровой квадрат</h1>
<p class="ls-note">Данные МойСклада с 01.01.2026. Дата периода — дата отгрузки; продажи и фактическая FIFO-себестоимость — по отгрузке, оплата — по связанным входящим платежам. Контрагенты типов «Юрлицо» и «ИП»; проекты Digitalsquare, Kartridge.org, Вручную, Заправка; статус заказа «Доставлен» или «Претензия решена обмен» при полной оплате исходной отгрузки. Замены по браку с окончаниями .0 и .1 исключены из оборота. ⚠ у оплаты означает отсутствие связанного платежа, у контрагента — расхождение карточек заказа и отгрузки в МС; «—» означает, что платёж по нескольким отгрузкам нельзя точно распределить. Обновлено: {updated}.</p>
<div class="ls-head"><span>Период</span><span>Отгрузок</span><span>Продажа</span><span>Оплата</span><span>Себестоимость</span></div>
{year_total}
{''.join(parts)}</main></body></html>'''
