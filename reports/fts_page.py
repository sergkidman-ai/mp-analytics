# поток: fin
"""reports/fts_page.py — подраздел «Отчёты ФТС» вкладки «Отчёты МП» (web/static/reports_fts.html).

Показывает за отчётный месяц: сколько отправлений уехало в страны ЕАЭС, сколько из них
выкупил маркетплейс (по ним отчитывается он), сколько осталось на нас, и даёт готовую
форму на каждую страну одной кнопкой. Статус «сдано» сотрудник ставит вручную — факт
загрузки в ЛК ФТС виден только человеку.
"""
import html
import pathlib
import sys
from datetime import date, timedelta

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from reports import fts_report, mp_tabs  # noqa: E402
from reports.ozon_mp_page import SHELL_CSS, REPORT_CSS, SIDEBAR, MPTABS, _atomic_write  # noqa: E402

OUT = BASE_DIR / "web" / "static" / "reports_fts.html"
ACC_LABEL = {"oz_acc1": "Ozon · Цифровой", "oz_acc2": "Ozon · Дисквэр"}
MONTHS_BACK = 6   # сколько месяцев показывать в переключателе периода

CSS = """
.fts-cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0 18px}
@media(max-width:760px){.fts-cards{grid-template-columns:1fr 1fr}}
.fts-kc{border:1px solid var(--line);border-radius:12px;padding:12px 15px;background:var(--card,#fff)}
.fts-kc .l{font-size:12px;color:var(--muted,#667);font-weight:600;margin-bottom:4px}
.fts-kc .v{font-size:26px;font-weight:750;font-variant-numeric:tabular-nums}
.fts-kc.due{border-top:3px solid #e6a04a} .fts-kc.done{border-top:3px solid #2f9e57}
.fts-kc.warn{border-top:3px solid #d33}
.perbar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin:2px 0 6px}
.perbar a{padding:4px 11px;border:1px solid var(--line);border-radius:20px;font-size:12px;
  text-decoration:none;color:var(--txt,#333);font-weight:600}
.perbar a.cur{background:var(--acc,#3fa7ff);border-color:transparent;color:#fff}
table.fts{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}
table.fts th,table.fts td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
table.fts th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted,#667)}
table.fts td.n{text-align:right;font-variant-numeric:tabular-nums}
table.fts td.nm{white-space:normal;max-width:280px;overflow-wrap:anywhere}
.fts-dl{display:inline-block;background:#2f6fd0;color:#fff;border-radius:8px;padding:6px 13px;
  font-weight:700;font-size:12px;text-decoration:none}
.st{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700}
.st.due{background:#faedd6;color:#8a5a12} .st.filed{background:#e2f3e9;color:#1f6d3f}
.st.buy{background:var(--card,#eee);color:var(--muted,#667)}
@media(prefers-color-scheme:dark){.st.due{background:#3a2e18;color:#f0c078}.st.filed{background:#183024;color:#7fd6a3}}
.btn-mark{background:transparent;border:1px solid var(--line);color:var(--txt,#333);border-radius:7px;
  padding:4px 10px;font-size:12px;cursor:pointer;font-weight:600}
.btn-mark:hover{background:var(--card,#f2f2f2)}
.prob{color:#b21f1f;font-size:12px;font-weight:600}
@media(prefers-color-scheme:dark){.prob{color:#ff8f8f}}
"""

SCRIPT = """
<script>
async function ftsMark(btn,period,platform,account,posting,filed){
  btn.disabled=true;
  try{
    var r=await fetch('/api/fts/status',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({period:period,platform:platform,account:account,posting_number:posting,filed:filed})});
    if(r.ok){location.reload();return;}
  }catch(e){}
  btn.disabled=false;alert('Не удалось сохранить статус');
}
</script>
"""


def _rub(x):
    return f"{float(x):,.2f}".replace(",", " ").replace(".", ",") if x is not None else "—"


def _periods(cur):
    out, d = [], date.today().replace(day=1)
    for _ in range(MONTHS_BACK):
        d = (d - timedelta(days=1)).replace(day=1)
        out.append(d)
    if cur not in out:
        out.insert(0, cur)
    return out


def _country_block(period, c):
    due = [r for r in c["rows"] if r["status"] != "filed"]
    return f"""      <tr>
        <td><b>{html.escape(c['title'])}</b> <span class="sub">({c['code']})</span></td>
        <td class="n">{len(c['rows'])}</td>
        <td class="n">{len(due)}</td>
        <td class="n">{_rub(c['amount_rub'])} ₽</td>
        <td>{html.escape(c['city'])}</td>
        <td>{'<a class="fts-dl" href="/reports/fts/form/%s/%s.xml">⬇ Скачать форму</a>' % (period.strftime('%Y-%m'), c['code']) if due else '<span class="sub">всё сдано</span>'}</td>
      </tr>"""


def _row_html(period, r):
    per = period.strftime("%Y-%m")
    if r["is_buyout"]:
        status = '<span class="st buy">выкупил МП</span>'
        act = '<span class="sub">—</span>'
    elif r["status"] == "filed":
        status = '<span class="st filed">сдано</span>'
        act = (f'<button class="btn-mark" onclick="ftsMark(this,\'{per}\',\'{r["platform"]}\','
               f'\'{r["account"]}\',\'{r["posting_number"]}\',false)">вернуть в «к сдаче»</button>')
    else:
        status = '<span class="st due">к сдаче</span>'
        act = (f'<button class="btn-mark" onclick="ftsMark(this,\'{per}\',\'{r["platform"]}\','
               f'\'{r["account"]}\',\'{r["posting_number"]}\',true)">отметить сданным</button>')
    goods = ", ".join(f"{g['ved']} / {g['gtd'] or '—'}" for g in r["goods"]) or "—"
    prob = f'<div class="prob">⚠ {html.escape(r["problems"])}</div>' if r.get("problems") else ""
    rub = _rub(r.get("amount_rub")) + " ₽" if r.get("amount_rub") is not None else "—"
    return f"""      <tr>
        <td>{html.escape(r['posting_number'])}<div class="sub">{ACC_LABEL.get(r['account'], r['account'])} · {r['schema'].upper()}</div>{prob}</td>
        <td>{r['delivered_at']:%d.%m.%Y}</td>
        <td>{r['country']} <span class="sub">{html.escape(r['cluster'] or '')}</span></td>
        <td class="n">{_rub(r['amount'])} {html.escape(r['currency'])}</td>
        <td class="n">{rub}</td>
        <td class="n">{r['weight_g']:.0f} г</td>
        <td class="nm">{html.escape(goods)}</td>
        <td>{status}</td>
        <td>{act}</td>
      </tr>"""


def html_for(period):
    data = fts_report.build(period)
    t = data["totals"]
    per = period.strftime("%Y-%m")

    perbar = "\n    ".join(
        f'<a class="cur">{p:%m.%Y}</a>' if p == period
        else f'<a href="/reports/fts?period={p:%Y-%m}">{p:%m.%Y}</a>'
        for p in _periods(period))

    countries = "\n".join(_country_block(period, c) for c in data["by_country"].values()) or \
        '      <tr><td colspan="6" class="sub">за месяц нет отправлений к сдаче</td></tr>'
    rows = "\n".join(_row_html(period, r) for r in data["rows"]) or \
        '      <tr><td colspan="9" class="sub">за месяц нет отправлений в страны ЕАЭС</td></tr>'
    # Страны вне ЕАЭС (Узбекистан и прочие) на странице не показываем совсем: статформа по ним
    # не сдаётся, и в таблице «к сдаче» они только путают.
    warn = (f'<div class="fts-kc warn"><div class="l">⚠ Не хватает данных</div><div class="v">{t["problems"]}</div></div>'
            if t["problems"] else
            f'<div class="fts-kc done"><div class="l">Сдано</div><div class="v">{t["filed"]}</div></div>')

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Отчёты ФТС · Пульт бизнеса</title>
<style>{SHELL_CSS}{REPORT_CSS}{CSS}</style>
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
    {mp_tabs.tabs_html('fts')}
  </div>
  <p class="eyebrow">Отчёты МП · Отчёты ФТС</p>
  <h1>Статформы ФТС по продажам в ЕАЭС</h1>
  <p class="sub">Отчёт сдаём в первых числах месяца за прошлый. В форму попадают отправления,
  получившие статус <b>«Доставлен» в отчётном месяце</b> (отгружены могли быть и раньше),
  и только те, где маркетплейс <b>не выкупил</b> товар: если выкупил — собственником стал он
  и в таможню отчитывается сам. Стоимость пересчитана по курсу ЦБ <b>на дату доставки</b>,
  вес нетто — с карточки Ozon, ТН ВЭД и номер ГТД — из МойСклада (ГТД по FIFO: последняя
  приёмка позиции до даты отгрузки). Одна форма — одна страна, товары схлопнуты
  по ТН ВЭД, номеру ГТД и стране происхождения.</p>
  <div class="perbar">Отчётный месяц:
    {perbar}
  </div>
  <div class="fts-cards">
    <div class="fts-kc"><div class="l">Отправлений в ЕАЭС</div><div class="v">{t['eaes']}</div></div>
    <div class="fts-kc done"><div class="l">Выкупил маркетплейс</div><div class="v">{t['buyout']}</div></div>
    <div class="fts-kc due"><div class="l">К сдаче нами</div><div class="v">{t['due']}</div></div>
    {warn}
  </div>

  <h2 style="font-size:17px;margin:18px 0 4px">Формы по странам</h2>
  <table class="fts">
    <thead><tr><th>Страна</th><th>Отправлений</th><th>К сдаче</th><th>Стоимость</th>
    <th>Город получателя</th><th>Форма для ЛК ФТС</th></tr></thead>
    <tbody>
{countries}
    </tbody>
  </table>
  <p class="sub" style="margin-top:8px">Город получателя подставляется в форму: для Казахстана —
  кластер доставки с наибольшей суммой, для остальных стран кластер Ozon совпадает со страной,
  поэтому ставится столица.</p>

  <h2 style="font-size:17px;margin:22px 0 4px">Отправления за {period:%m.%Y}</h2>
  <table class="fts">
    <thead><tr><th>Отправление</th><th>Доставлено</th><th>Страна</th><th>Оплачено</th>
    <th>В рублях</th><th>Вес нетто</th><th>ТН ВЭД / ГТД</th><th>Статус</th><th></th></tr></thead>
    <tbody>
{rows}
    </tbody>
  </table>
</main>
{SCRIPT}
</body>
</html>"""


def render(period=None):
    period = period or (date.today().replace(day=1) - timedelta(days=1)).replace(day=1)
    _atomic_write(OUT, html_for(period))
    return OUT


if __name__ == "__main__":
    p = sys.argv[1] if len(sys.argv) > 1 else None
    per = date(int(p[:4]), int(p[5:7]), 1) if p else None
    print("OK →", render(per))
