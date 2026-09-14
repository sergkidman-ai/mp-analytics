# поток: fin
"""reports/upd_page.py — подраздел «УПД по выкупам ВБ» вкладки «Отчёты МП».

ВБ, выкупая наш товар, выпускает «Уведомление о выкупе» (XLSX в zip, скачивается
сборщиком collectors/wb_documents.py). На каждое уведомление продавец обязан выставить
РВБ счёт-фактуру с документом об отгрузке — УПД — и отправить через ЭДО. Страница
показывает, по каким уведомлениям УПД ещё не выгружен, и отдаёт готовый XML для Диадока.

Статус «выгружен» ставится вручную: факт загрузки в Диадок виден только человеку.
"""
import html
import pathlib
import sys

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from reports import mp_tabs  # noqa: E402
from reports.upd_orgs import SELLERS  # noqa: E402
from reports.ozon_mp_page import SHELL_CSS, REPORT_CSS, SIDEBAR, MPTABS, _atomic_write  # noqa: E402
from reports.fts_page import CSS as FTS_CSS  # noqa: E402  плитки и таблица те же

OUT = BASE_DIR / "web" / "static" / "reports_upd.html"
ACC_LABEL = {"wb_acc1": "ВБ · Цифровой квадрат", "wb_acc2": "ВБ · Дисквэр"}

FILTER_CSS = """
.upd-filter{display:flex;flex-wrap:wrap;align-items:center;gap:16px;margin:18px 0 10px}
.upd-filter label{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--mut)}
.upd-filter select{font:inherit;font-size:13px;padding:5px 10px;border-radius:8px;
  border:1px solid var(--line);background:var(--card);color:var(--txt)}
.upd-count{font-size:13px;color:var(--mut);margin-left:auto}
"""

SCRIPT = """
<script>
// Фильтр клиентский: строк меньше сотни, перерисовывать страницу ради него незачем.
// Выбор храним в localStorage — после «отметить выгруженным» страница перезагружается,
// и без этого фильтр сбрасывался бы на каждом клике.
function updFilter(){
  var a=document.getElementById('fAcc').value, s=document.getElementById('fSt').value;
  try{localStorage.setItem('updFilter',a+'|'+s);}catch(e){}
  var n=0,sum=0;
  document.querySelectorAll('table.fts tbody tr[data-acc]').forEach(function(tr){
    var ok=(!a||tr.dataset.acc===a)&&(!s||tr.dataset.st===s);
    tr.hidden=!ok;
    if(ok){n++;sum+=parseFloat(tr.dataset.sum)||0;}
  });
  document.getElementById('fCount').textContent=
    n+' из '+document.querySelectorAll('table.fts tbody tr[data-acc]').length+
    ' · '+sum.toLocaleString('ru-RU',{minimumFractionDigits:2,maximumFractionDigits:2})+' \u20bd';
}
window.addEventListener('DOMContentLoaded',function(){
  try{
    var v=(localStorage.getItem('updFilter')||'').split('|');
    if(v[0])document.getElementById('fAcc').value=v[0];
    if(v[1])document.getElementById('fSt').value=v[1];
  }catch(e){}
  updFilter();
});
async function updMark(btn,account,number,made){
  btn.disabled=true;
  try{
    var r=await fetch('/api/upd/status',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({account:account,doc_number:number,made:made})});
    if(r.ok){location.reload();return;}
  }catch(e){}
  btn.disabled=false;alert('Не удалось сохранить статус');
}
</script>
"""


def _rub(x):
    return f"{float(x):,.2f}".replace(",", " ").replace(".", ",")


def _rows():
    return db.query("""SELECT account, doc_number, doc_date, created_at, upd_status,
                              total_wo_vat, total_vat, total_with_vat,
                              jsonb_array_length(positions) AS pos
                         FROM wb_redeem_notice
                     ORDER BY doc_date DESC, account""")


def _row_html(r):
    acc, num = r["account"], r["doc_number"]
    org = SELLERS.get(acc)
    ready = bool(org and org.get("edi_id"))
    if r["upd_status"] == "made":
        st = '<span class="st filed">УПД выгружен</span>'
        act = (f'<button class="btn-mark" onclick="updMark(this,\'{acc}\',\'{num}\',false)">'
               f'вернуть в «к выгрузке»</button>')
    else:
        st = '<span class="st due">к выгрузке</span>'
        act = (f'<button class="btn-mark" onclick="updMark(this,\'{acc}\',\'{num}\',true)">'
               f'отметить выгруженным</button>')
    dl = (f'<a class="fts-dl" href="/reports/upd/file/{acc}/{num}.xml">⬇ Скачать УПД</a>'
          if ready else '<span class="prob">⚠ нет идентификатора ЭДО</span>'
          if org else '<span class="prob">⚠ нет реквизитов продавца</span>')
    return f"""      <tr data-acc="{acc}" data-st="{r['upd_status']}" data-sum="{float(r['total_with_vat']):.2f}">
        <td><b>{html.escape(num)}</b><div class="sub">{ACC_LABEL.get(acc, acc)}</div></td>
        <td>{r['doc_date']:%d.%m.%Y}</td>
        <td class="n">{r['pos']}</td>
        <td class="n">{_rub(r['total_wo_vat'])} ₽</td>
        <td class="n">{_rub(r['total_vat'])} ₽</td>
        <td class="n">{_rub(r['total_with_vat'])} ₽</td>
        <td>{st}</td>
        <td>{dl}</td>
        <td>{act}</td>
      </tr>"""


def html_for():
    rows = _rows()
    due = [r for r in rows if r["upd_status"] != "made"]
    made = len(rows) - len(due)
    amount = sum(float(r["total_with_vat"]) for r in due)
    # Без идентификатора ЭДО УПД не собрать — такой аккаунт считаем незаполненным.
    no_req = sorted({r["account"] for r in rows
                     if not (SELLERS.get(r["account"]) or {}).get("edi_id")})
    warn = ('<div class="fts-kc warn"><div class="l">⚠ Нет идентификатора ЭДО</div>'
            f'<div class="v">{", ".join(ACC_LABEL.get(a, a) for a in no_req)}</div></div>'
            if no_req else
            f'<div class="fts-kc"><div class="l">Сумма к выгрузке</div>'
            f'<div class="v">{_rub(amount)} ₽</div></div>')
    acc_opts = "\n".join(
        f'        <option value="{a}">{ACC_LABEL.get(a, a)}</option>'
        for a in sorted({r["account"] for r in rows}))
    body = "\n".join(_row_html(r) for r in rows) or \
        '      <tr><td colspan="9" class="sub">уведомлений о выкупе пока нет</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>УПД по выкупам ВБ · Пульт бизнеса</title>
<style>{SHELL_CSS}{REPORT_CSS}{FTS_CSS}{FILTER_CSS}</style>
</head>
<body>
{SIDEBAR}
<main class="wrap">
  <div class="rtabs">
    {MPTABS}
    {mp_tabs.tabs_html('upd')}
  </div>
  <p class="eyebrow">Отчёты МП · УПД по выкупам ВБ</p>
  <h1>Уведомления о выкупе Wildberries → УПД</h1>
  <p class="sub">Выкупая наш товар, ВБ выпускает <b>уведомление о выкупе</b>; на каждое такое
  уведомление продавец выставляет РВБ <b>УПД</b> (счёт-фактура + документ об отгрузке, КНД
  1115131, функция СЧФДОП) и отправляет его через ЭДО. Кнопка отдаёт готовый XML — его
  остаётся загрузить в Диадок и подписать. Суммы берутся из уведомления как есть: стоимость
  с НДС и сумма НДС — из документа ВБ, цена без НДС и за единицу считаются из них.
  Генератор сверен с уже принятым УПД № 764559005 — файл совпадает побайтово.</p>
  <div class="fts-cards">
    <div class="fts-kc"><div class="l">Уведомлений</div><div class="v">{len(rows)}</div></div>
    <div class="fts-kc due"><div class="l">УПД к выгрузке</div><div class="v">{len(due)}</div></div>
    <div class="fts-kc done"><div class="l">Выгружено</div><div class="v">{made}</div></div>
    {warn}
  </div>

  <div class="upd-filter">
    <label>Организация
      <select id="fAcc" onchange="updFilter()">
        <option value="">все</option>
{acc_opts}
      </select>
    </label>
    <label>Статус
      <select id="fSt" onchange="updFilter()">
        <option value="">все</option>
        <option value="due">к выгрузке</option>
        <option value="made">выгружен</option>
      </select>
    </label>
    <span class="upd-count" id="fCount"></span>
  </div>

  <table class="fts">
    <thead><tr><th>Уведомление</th><th>Дата</th><th>Позиций</th><th>Без НДС</th><th>НДС</th>
    <th>С НДС</th><th>Статус</th><th>Документ</th><th></th></tr></thead>
    <tbody>
{body}
    </tbody>
  </table>
  <p class="sub" style="margin-top:8px">Дата УПД равна дате уведомления (не дате его выкладки
  в кабинете ВБ). КИЗ не выводим — в уведомлениях он пустой.</p>
</main>
{SCRIPT}
</body>
</html>"""


def render():
    _atomic_write(OUT, html_for())
    return OUT


if __name__ == "__main__":
    print("OK →", render())
