# поток: fin
"""КУДиР: исходный регистр бухгалтера, проверка итогов и готовность своего учёта."""
from decimal import Decimal
from datetime import date
from html import escape
import json

from reports import kudir_report, kudir_tax, mp_tabs
from reports.ozon_mp_page import SHELL_CSS, REPORT_CSS, SIDEBAR, MPTABS

AUDIT_PATH = kudir_report.BASE_DIR / "docs/reports/kudir_2026-10-14/analysis.json"

CSS = """
.k-per{display:flex;gap:8px;flex-wrap:wrap;margin:18px 0}
.k-per a,.k-download{padding:9px 14px;border:1px solid var(--line);border-radius:9px;text-decoration:none;color:var(--txt);background:var(--card)}
.k-per a.cur{border-color:var(--acc);box-shadow:inset 0 -2px 0 var(--acc)}
.k-cards{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:15px 0 24px}
.k-card,.k-panel{padding:16px;background:var(--card);border:1px solid var(--line);border-radius:12px}
.k-card .label{font-size:12px;color:var(--mut);margin-bottom:7px}.k-card .value{font-size:24px;font-weight:700;font-variant-numeric:tabular-nums}
.k-note{font-size:13px;color:var(--mut);line-height:1.6}.k-alert{padding:14px 16px;border:1px solid #c98a35;background:#c98a3514;border-radius:10px;margin:15px 0;line-height:1.6}
.k-scroll{overflow:auto;max-height:650px;border:1px solid var(--line);border-radius:10px;margin:12px 0 20px}
.k-table{width:100%;border-collapse:collapse;font-size:13px}.k-table th,.k-table td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
.k-table th{position:sticky;top:0;background:var(--card);z-index:1;color:var(--mut)}
.k-table .num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}.k-table td.desc{min-width:260px;max-width:580px;white-space:pre-line;overflow-wrap:anywhere}
.k-table .doc{min-width:145px;white-space:pre-line;overflow-wrap:anywhere}.k-flag{background:#c98a3514}.k-badge{display:block;color:#b97d24;font-size:11px;margin-top:4px}
.k-filter{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:14px 0}.k-filter input{background:var(--card);color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:10px;min-width:260px;max-width:100%}
.k-panel{margin:18px 0}.k-panel summary{cursor:pointer;font-weight:600}.k-panel p,.k-panel li{line-height:1.65}.k-panel a{color:var(--acc)}
.k-source{overflow-wrap:anywhere}.k-ready td{min-width:180px}.k-ready td:first-child{min-width:145px}
.k-menu{display:none}
.k-tax-cards{grid-template-columns:repeat(2,minmax(0,1fr))}.k-tax-current{background:#338c8c14}
.k-tax-table td:first-child,.k-tax-table th:first-child{min-width:145px;max-width:180px;white-space:normal;text-align:left;line-height:1.5}
@media(min-width:1400px){.k-tax-cards{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:760px){body{display:block}header{width:100%;height:auto;position:static;flex-direction:row;flex-wrap:wrap;align-items:center;border-right:0;border-bottom:1px solid var(--line)}header .logo{flex:1;margin:0}header .lvlnav{display:none;flex-basis:100%}.k-nav-open header .lvlnav{display:flex}.k-menu{display:block;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--txt);padding:8px 12px}main{padding:16px 14px 40px;width:100%}.k-cards{grid-template-columns:1fr}.k-card .value{font-size:22px}.k-filter input{width:100%;min-width:0}.k-panel{padding:12px}}
"""


def rub(value):
    return f"{Decimal(value):,.2f}".replace(",", " ").replace(".", ",") + " ₽"


def _summary(data):
    a, c = data["accountant"], data["calculated"]
    verified = data.get("manual_verification")
    def original(key):
        if verified:
            return rub(verified[key])
        if a[key] is None:
            return '<span class="k-badge">' + escape(a.get("errors", {}).get(key) or "Нет числа") + ' в файле</span>'
        return rub(a[key])
    def difference(key):
        if verified:
            return rub(Decimal(c[key]) - Decimal(verified[key]))
        return rub(data["delta"][key]) if data["delta"][key] is not None else "Нельзя сверить"
    return f"""<div class="k-scroll"><table class="k-table" aria-label="Сверка итогов квартала">
<thead><tr><th>Показатель</th><th class="num">Итог бухгалтера{' · ручная сверка' if verified else ' в файле'}</th><th class="num">Сумма всех строк</th><th class="num">Разница</th></tr></thead>
<tbody>{''.join(f'<tr><td>{label}</td><td class="num">{original(key)}</td><td class="num">{rub(c[key])}</td><td class="num">{difference(key)}</td></tr>' for key, label in (("income", "Доходы"), ("expense", "Расходы")))}</tbody></table></div>"""


def _operations(data):
    rows = []
    excluded = set(data["income_rows_outside_formula"])
    for op in data["operations"]:
        badges = []
        if op["row"] in excluded:
            badges.append("Бухгалтер учтёт в годовом отчёте" if data.get('income_confirmation') else "Доход вне итоговой формулы")
        if op["date_outside_quarter"]:
            badges.append("Дата документа вне квартала — проверить")
        inherited = '<small class="k-note">Продолжение предыдущего документа</small>' if op["document_inherited"] else ""
        badge = ''.join(f'<span class="k-badge">{escape(s)}</span>' for s in badges)
        rows.append(f"""<tr class="{'k-flag' if badges else ''}" data-operation>
<td>{op['row']}</td><td>{'—' if data.get('manual_verification') and op.get('number_error') else escape(op['number'])}</td><td class="doc">{escape(op['document'])}<br>{inherited}</td>
<td class="desc">{escape(op['description'])}{badge}</td>
<td class="num">{rub(op['income']) if Decimal(op['income']) else '—'}</td><td class="num">{rub(op['expense']) if Decimal(op['expense']) else '—'}</td></tr>""")
    return '\n'.join(rows)


def _tax_summary(tax):
    current = tax['current']
    period = current['label'] + ' ' + str(current['year'])
    if current['is_partial']:
        through = date.fromisoformat(current['through']).strftime('%d.%m.%Y') if current['through'] else 'даты в книге'
        period = f'С начала года по {through} · предварительный расчёт'
    cards = ''.join(f'<div class="k-card"><div class="label">{label}</div><div class="value">{rub(current[key])}</div></div>'
                    for key, label in [('income', 'Доходы с начала года'), ('expense', 'Расходы с начала года'),
                                       ('tax_base', 'Налоговая база'), ('tax', 'Годовой налог' if current['is_annual'] else 'Начислено по ставке 15%')])
    rows = []
    for p in tax['periods']:
        label = 'Неполный год' if p['is_partial'] else p['label']
        if p['is_partial'] and p['through']:
            label += '<br><small>по ' + date.fromisoformat(p['through']).strftime('%d.%m.%Y') + '</small>'
        rows.append(f'<tr class="{"k-tax-current" if p["quarter"] == current["quarter"] else ""}"><td>{label}</td>'
                    + ''.join(f'<td class="num">{rub(p[k])}</td>' for k in ('income', 'expense', 'tax_base', 'tax', 'tax_change')) + '</tr>')
    confirmation = ''
    if tax['income_confirmation']:
        confirmation = ('<p class="k-note"><b>Три доходные строки Цифрового на 1 288 086,99 ₽ уже включены в этот расчёт.</b> '
                        'Вы подтвердили: бухгалтер учтёт их в годовом отчёте. Повторно сумма не добавляется.</p>')
    minimum = '<p class="k-note">Минимальный налог 1% применяется по итогам полного года. Квартальные авансы считаются по ставке 15%.</p>'
    if current['is_annual']:
        minimum = (f'<p class="k-note">Налог 15%: {rub(current["ordinary_tax"])}; минимальный годовой налог 1%: '
                   f'{rub(current["minimum_tax"])}. Годовое начисление — большая из этих сумм.</p>')
    elif current['is_partial']:
        minimum += (f'<p class="k-note">Ориентир 1% по имеющимся доходам: {rub(current["minimum_tax_reference"])}. '
                    'Годовой итог появится после загрузки полного IV квартала.</p>')
    return f'''<section class="k-panel" id="k-tax" aria-labelledby="k-tax-title">
<h2 id="k-tax-title">Расчёт УСН · доходы минус расходы · 15%</h2><p class="k-note">{period}</p>
<div class="k-cards k-tax-cards">{cards}</div>
<p class="k-note">База = доходы − расходы с начала года, но не ниже нуля. Налог округляется до целых рублей.
Расчёт по всем строкам загруженной КУДиР, без переноса убытков прошлых лет. Это начисление; уплаченные авансы не вычтены.</p>
{confirmation}{minimum}
<div class="k-scroll"><table class="k-table k-tax-table" aria-label="Расчёт налога нарастающим итогом"><thead><tr><th>Период</th>
<th class="num">Доходы</th><th class="num">Расходы</th><th class="num">База</th><th class="num">Начислено</th><th class="num">Изменение начисления</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
<p class="k-note">Все суммы — нарастающим итогом. Последний столбец — разница с предыдущим расчётным периодом;
отрицательное значение означает уменьшение начисления. <a href="{tax['fns_url']}" target="_blank" rel="noopener noreferrer">Правила расчёта ФНС</a>.</p></section>'''


def html_for(quarter=None, org="cifrovoi"):
    quarter = quarter if quarter is not None else 3
    data = dict(kudir_report.load(quarter, org))
    data['income_confirmation'] = kudir_tax.confirmed_income(data)
    tax = kudir_tax.load(org, quarter)
    available = kudir_report.quarters(org)
    summary_quarters = (1, 2, 3)
    both = [kudir_report.load(q, org) for q in summary_quarters]
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    h1 = {key: sum(Decimal(q["calculated"][key]) for q in both) for key in ("income", "expense")}
    labels = {1: "I", 2: "II", 3: "III", 4: "IV"}
    orgs = ''.join(f'<a class="{"cur" if key == org else ""}" href="/reports/kudir?org={key}">{escape(label)}</a>' for key, label in kudir_report.ORGANISATIONS.items())
    periods = ''.join(f'<a class="{"cur" if q == quarter else ""}" href="/reports/kudir?org={org}&amp;quarter={q}">{labels[q]} квартал{" · неполный" if org == "diskver" and q == 4 else ""}</a>' for q in available)
    if data.get('manual_verification'):
        alert = '<p class="k-note">Итоги сверены вручную пользователем. Доходы и расходы совпадают с суммами строк.</p>'
    elif data['status'] == 'accountant_import_excel_errors':
        alert = '<div class="k-alert" role="status"><b>В сохранённых итогах Excel — ошибка #NAME?.</b><br>Пульт пересчитал доходы и расходы по числовым значениям строк. Ошибочные итоги и номера записей оставлены как в оригинале; сравнить пересчёт с числовым итогом бухгалтера пока нельзя. Оригинал книги не исправлялся.</div>'
    elif data['income_confirmation']:
        alert = ('<p class="k-note"><b>Подтверждено вами: бухгалтер учтёт три доходные строки в годовом отчёте.</b> '
                 'Сумма — 1 288 086,99 ₽. В расчёте Пульта эти строки уже учтены; оригинальный файл бухгалтера сохранён.</p>')
    elif any(Decimal(value or 0) for value in data["delta"].values()):
        alert = f"""<div class="k-alert" role="status"><b>Итог бухгалтера расходится с суммой строк.</b><br>
Формула доходов <code>{escape(data['accountant']['income_formula'] or '—')}</code> не включает строки
{', '.join(map(str, data['income_rows_outside_formula']))}. Разница доходов — <b>{rub(data['delta']['income'])}</b>.
Оригинал сохранён; пересчёт показан для сверки с бухгалтером и не заменяет согласованную КУДиР.</div>"""
        if quarter == 2 and org == "cifrovoi":
            alert += '<p class="k-note">Все три строки относятся к отчёту Маркета от 30.06.2026. Сумма 1 284 991,43 ₽ совпадает с июньским покупательским оборотом после возвратов без НДС 5%. Назначения 2 561 ₽ и 534,56 ₽ в XLS не расшифрованы. Учёт этих сумм в расчёте налога по двум файлам установить нельзя.</p>'
    else:
        alert = '<p class="k-note">Итоги квартала совпадают с суммой строк до копейки. Проверка арифметики не подтверждает полноту налогового учёта.</p>'
    if org == 'cifrovoi' and quarter == 3:
        alert += ('<div class="k-alert" role="status"><b>Внешняя сверка III квартала не закрыта.</b> '
                  'Доходы Ozon в книге отличаются от суммы продаж и возвратов в отчёте о реализации Ozon; '
                  'особенно требуется сверить баллы за скидки, зачёты и НДС. '
                  'У WB есть расхождения за август и сентябрь. '
                  'Суммы источников имеют разную основу и сами по себе не доказывают пропуск в КУДиР. '
                  'Проверьте их с бухгалтером по отчётам агента и первичным документам.</div>')
    if data.get('is_partial'):
        end = date.fromisoformat(data['document_date_range']['to']).strftime('%d.%m.%Y')
        alert += f'<div class="k-alert"><b>IV квартал ещё не завершён.</b> В книге представлены операции по {end}. Эти суммы не включены в итог за 9 месяцев.</div>'
    date_note = ''
    if data['outside_document_dates']:
        date_note = f'<p class="k-note">Дата первичного документа вне квартала: строки {", ".join(map(str, data["outside_document_dates"]))}. Она может отличаться от даты признания расхода. Даты не исправлялись; основания включения нужно проверить по первичке.</p>'
    groups = ''.join(f'<tr><td>{escape(g["category"])}</td><td class="num">{g["count"]}</td><td class="num">{rub(g["income"])}</td><td class="num">{rub(g["expense"])}</td></tr>' for g in data['groups'])
    readiness = ''.join(f'<tr><td><b>{escape(r["area"])}</b></td><td>{escape(r["available"])}</td><td>{escape(r["needed"])}</td></tr>' for r in audit['readiness'])
    observed = ''.join(f'<li>{escape(s)}</li>' for s in audit['accountant_formation_observed'])
    legal = ''.join(f'<li><a href="{escape(s["url"], quote=True)}" target="_blank" rel="noopener noreferrer">{escape(s["title"])}</a></li>' for s in audit['legal_sources'])
    title = labels[quarter]
    summary_label = "9 месяцев"
    status_note = "Сохранённые итоги Excel требуют пересчёта" if org == "diskver" else "Итоги II квартала требуют сверки"
    if org == 'cifrovoi' and kudir_tax.confirmed_income(both[1]):
        status_note = 'Три доходные строки учтут в годовом отчёте'
    if data.get('manual_verification'):
        status_note = "Итоги сверены вручную"
    description = 'Раздел I «Доходы и расходы» из книги бухгалтера за 2026 год. Доступны четыре квартала; IV квартал заполнен частично.' if org == "diskver" else 'Раздел I «Доходы и расходы» из файлов бухгалтера за I–III кварталы.'
    download = f'/reports/kudir/file/{quarter}?org={org}'
    download_label = 'Скачать оригинал книги XLSX · 2026' if org == 'diskver' else f'Скачать оригинал XLS · {title} квартал'
    source_note = 'Юрлицо и ИНН подтверждены титульным листом. Объект — доходы, уменьшенные на величину расходов. Оригинал содержит также титул и дополнительные разделы; в Пульте показаны строки раздела I.' if org == 'diskver' else 'Юрлицо привязано по вашему поручению: в этих файлах только раздел I, без титульного листа. Налог рассчитан по заданной вами ставке 15% на основе сумм всех строк.'
    explanation = f'<p>{escape(audit["conclusion"])}</p><h3>Что видно в работе бухгалтера</h3><ul>{observed}</ul><h3>Наши данные и что ещё требуется</h3><div class="k-scroll"><table class="k-table k-ready"><thead><tr><th>Область</th><th>Есть в Пульте</th><th>Нужно для налогового регистра</th></tr></thead><tbody>{readiness}</tbody></table></div>'
    if org == "diskver":
        diskver_audit = json.loads((kudir_report.BASE_DIR / 'docs/reports/kudir_diskver_2026-10-14/analysis.json').read_text(encoding='utf-8'))
        explanation = '<p>Книга импортирована без изменения оригинала. Полное совпадение с отчётами WB и Ozon пока не установлено: 43 продажные строки не называют площадку, а с июля структура записей меняется.</p>'
        explanation += '<ul>' + ''.join(f'<li><b>{escape(item["title"])}</b>: {escape(item["details"])}</li>' for item in diskver_audit['findings']) + '</ul>'
        explanation += '<p>Для собственного налогового регистра нужны расшифровка доходов, первичные документы, даты признания и связь оплаты поставщику с реализованными товарами. Назначение банковского платежа само по себе не подтверждает налоговый расход.</p>'
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>КУДиР · Пульт бизнеса</title>
<style>{SHELL_CSS}{REPORT_CSS}{CSS}</style></head><body><header>{SIDEBAR.replace('class="lvlnav"', 'class="lvlnav" id="k-main-nav"')}<button class="k-menu" id="k-menu" aria-controls="k-main-nav" aria-expanded="false">Меню</button></header>
<main id="mpr"><nav class="mptabs">{MPTABS}</nav><div class="rtabs">{mp_tabs.tabs_html('kudir')}</div>
<p class="eyebrow">Отчёты · Налоговый учёт</p><h1>КУДиР · {escape(data['org'])} · 2026</h1>
<nav class="k-per" aria-label="Юрлицо">{orgs}</nav>
<p class="sub">{description}
Здесь доступны исходные строки, проверка итогов и оригиналы для скачивания.</p>
<div class="k-cards"><div class="k-card"><div class="label">{summary_label} · доходы по всем строкам</div><div class="value">{rub(h1['income'])}</div></div>
<div class="k-card"><div class="label">{summary_label} · расходы по всем строкам</div><div class="value">{rub(h1['expense'])}</div></div>
<div class="k-card"><div class="label">Статус регистра</div><div class="value">Импорт бухгалтера</div><p class="k-note">{status_note}</p></div></div>
<nav class="k-per" aria-label="Квартал">{periods}<a class="k-download" href="{download}">{download_label}</a></nav>
{_tax_summary(tax)}
<h2>{title} квартал · проверка итогов</h2>{alert}{_summary(data)}{date_note}
<h2>Строки бухгалтера</h2><p class="k-note">«Строка XLS» — номер строки исходного файла; «№ записи» — нумерация бухгалтера.
Для продолжений показан предыдущий документ с явной отметкой. Все суммы и даты сохранены.</p>
<div class="k-filter"><input id="k-search" type="search" placeholder="Документ, операция или сумма" aria-label="Поиск по строкам КУДиР"><span id="k-count" class="k-note">{len(data['operations'])} строк</span></div>
<div class="k-scroll"><table class="k-table" id="k-operations"><thead><tr><th>Строка XLS</th><th>№ записи</th><th>Первичный документ</th><th>Содержание операции</th><th class="num">Доходы</th><th class="num">Расходы</th></tr></thead><tbody>{_operations(data)}</tbody></table></div>
<details class="k-panel"><summary>Состав доходов и расходов квартала</summary><p class="k-note">Группировка по описаниям строк; это не решение о допустимости расхода для УСН.</p>
<div class="k-scroll"><table class="k-table"><thead><tr><th>Группа</th><th class="num">Строк</th><th class="num">Доходы</th><th class="num">Расходы</th></tr></thead><tbody>{groups}</tbody></table></div></details>
<details class="k-panel"><summary>Как сформированы файлы бухгалтера</summary>
{explanation}
<p>Автоматическое ведение КУДиР отложено по вашему решению. Здесь используем загруженные файлы бухгалтера.</p></details>
<details class="k-panel"><summary>Источник и нормативные основания</summary><p class="k-note k-source">Файл: {escape(data['filename'])}<br>SHA-256: {data['sha256']}<br>
{source_note}</p><ul>{legal}</ul></details>
</main><script>
const search=document.getElementById('k-search'), rows=Array.from(document.querySelectorAll('[data-operation]'));
const normalize=value=>value.toLocaleLowerCase('ru').replace(/[\\s,.]/g,'');
search.addEventListener('input',()=>{{const term=normalize(search.value);let count=0;for(const row of rows){{row.hidden=!normalize(row.textContent).includes(term);if(!row.hidden)count++;}}document.getElementById('k-count').textContent=count+' из '+rows.length+' строк';}});
document.getElementById('k-menu').addEventListener('click',event=>{{const expanded=document.body.classList.toggle('k-nav-open');event.currentTarget.setAttribute('aria-expanded',String(expanded));}});
</script></body></html>"""
