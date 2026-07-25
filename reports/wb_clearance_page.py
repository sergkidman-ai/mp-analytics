# поток: rev
"""reports/wb_clearance_page.py — вкладка «Распродажа остатков ВБ» (подраздел «Склада»).

Два блока: Цифровой квадрат (wb_acc1) и Дисквэр (wb_acc2). В каждом — таблица распродажи
(wb_clearance) × живой остаток на складе WB (последний снимок wb_stocks). Сигнал: остаток WB
стал 0 → «Поднять цену». Сотрудник сам закрывает отработанные позиции галочкой (пишутся в
wb_clearance_dismissed, поблочно), их можно вернуть. Страница статическая, перегенерируется в
run_daily и в API dismiss/restore.
"""
import sys
import html
import pathlib
from datetime import datetime

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db  # noqa: E402
from reports.ozon_mp_page import SHELL_CSS, REPORT_CSS, SIDEBAR, _atomic_write  # noqa: E402

OUT = BASE_DIR / "web" / "static" / "reports_wb_clearance.html"
ACC_LABEL = {"wb_acc1": "ЦК", "wb_acc2": "Дисквэр"}
ACC_FULL = {"wb_acc1": "Цифровой квадрат (ЦК)", "wb_acc2": "Дисквэр"}
ACCOUNTS = ["wb_acc1", "wb_acc2"]     # порядок блоков сверху вниз
LOW_THRESHOLD = 1  # ≤ этого — «заканчивается» (остался 1 — следующая продажа обнулит)

# распродажа — подраздел «Склада»: в левом меню активен «Склад» (не «Отчёты МП»),
# сам переход — через горизонтальный бар внутри раздела «Склад» (см. render()).
SIDEBAR_CLR = (SIDEBAR
               .replace('<a href="/reports" class="cur">', '<a href="/reports">')
               .replace('<a href="/warehouse">', '<a href="/warehouse" class="cur">'))

CSS = """
.clr-block{margin:24px 0 8px;padding-top:8px;border-top:2px solid var(--line)}
.clr-block:first-of-type{border-top:0}
.clr-block h2.blk-h{font-size:17px;margin:0 0 8px;display:flex;align-items:baseline;gap:10px}
.clr-block h2.blk-h .cnt{font-size:12px;color:var(--muted,#667);font-weight:600}
.clr-cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:8px 0 14px}
@media(max-width:760px){.clr-cards{grid-template-columns:1fr 1fr}}
.clr-kc{border:1px solid var(--line);border-radius:12px;padding:12px 15px;background:var(--card,#fff)}
.clr-kc .l{font-size:12px;color:var(--muted,#667);font-weight:600;margin-bottom:4px}
.clr-kc .v{font-size:24px;font-weight:750;font-variant-numeric:tabular-nums}
.clr-kc.red{border-top:3px solid #d33} .clr-kc.amber{border-top:3px solid #e6a04a} .clr-kc.green{border-top:3px solid #2f9e57}
table.clr{border-collapse:collapse;width:100%;font-size:13px;margin-top:6px}
table.clr th,table.clr td{padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap;text-align:left}
table.clr th{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted,#667);position:sticky;top:0;background:var(--card,#fff)}
table.clr td.n{text-align:right;font-variant-numeric:tabular-nums}
table.clr td.nm{white-space:normal;max-width:300px;overflow-wrap:anywhere}
table.clr th.chk,table.clr td.chk{text-align:center;width:34px}
table.clr input[type=checkbox]{width:16px;height:16px;cursor:pointer}
.sig{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;font-weight:700}
.sig.red{background:#fbe4e4;color:#b21f1f} .sig.amber{background:#faedd6;color:#8a5a12} .sig.green{background:#e2f3e9;color:#1f6d3f}
@media(prefers-color-scheme:dark){.sig.red{background:#3a1c1c;color:#ff8f8f}.sig.amber{background:#3a2e18;color:#f0c078}.sig.green{background:#183024;color:#7fd6a3}}
.clr-scroll{max-height:560px;overflow:auto;border:1px solid var(--line);border-radius:12px}
.clr-actions{display:flex;align-items:center;gap:10px;margin:6px 0 10px;min-height:36px}
.btn-close{background:#d33;color:#fff;border:0;border-radius:8px;padding:8px 15px;font-weight:700;font-size:13px;cursor:pointer}
.btn-close[disabled]{opacity:.4;cursor:default}
.btn-restore{background:transparent;border:1px solid var(--line);color:var(--txt,#333);border-radius:7px;padding:4px 10px;font-size:12px;cursor:pointer}
.btn-restore:hover{background:var(--card,#f2f2f2)}
.closed-toggle{color:var(--acc,#3fa7ff);text-decoration:none;font-weight:600;margin-left:4px}
.closed-box{margin:8px 0 10px}
.closed-box table{border-collapse:collapse;width:100%;font-size:12px;max-width:720px}
.closed-box td{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left}
.closed-box td.n{text-align:right;font-variant-numeric:tabular-nums}
"""

SCRIPT = """
<script>
function clrBlk(el){return el.closest('.clr-block');}
function clrSelIn(sec){return Array.prototype.slice.call(sec.querySelectorAll('.rowchk:checked')).map(function(c){return {account:c.dataset.acc, nm_id:parseInt(c.dataset.nm,10)};});}
function clrUpd(sec){var s=clrSelIn(sec);var b=sec.querySelector('.btn-close');b.textContent='🗑 Закрыть выбранные ('+s.length+')';b.disabled=s.length===0;}
document.addEventListener('change',function(e){
  var t=e.target;if(!t.classList)return;
  if(t.classList.contains('chkall')){var sec=clrBlk(t);var on=t.checked;sec.querySelectorAll('.rowchk').forEach(function(c){c.checked=on;});clrUpd(sec);}
  else if(t.classList.contains('rowchk')){clrUpd(clrBlk(t));}
});
async function clrPost(url,items){try{var r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({items:items})});return r.ok;}catch(err){return false;}}
async function closeSelected(btn){var sec=clrBlk(btn);var s=clrSelIn(sec);if(!s.length)return;if(!confirm('Закрыть '+s.length+' позиц.? Они исчезнут из таблицы — вернуть можно ниже («Закрыто → показать»).'))return;var ok=await clrPost('/api/wb/clearance/dismiss',s);if(ok){location.reload();}else{alert('Не удалось сохранить, попробуйте ещё раз');}}
async function clrRestore(acc,nm){var ok=await clrPost('/api/wb/clearance/restore',[{account:acc,nm_id:parseInt(nm,10)}]);if(ok){location.reload();}else{alert('Не удалось вернуть');}}
function toggleClosed(a){var sec=clrBlk(a);var b=sec.querySelector('.closed-box');var show=(b.style.display==='none');b.style.display=show?'block':'none';a.textContent=show?'— скрыть':'— показать';return false;}
</script>
"""


def _rub(x):
    return f"{int(round(x)):,}".replace(",", " ") if x not in (None, "") else "—"


# бренды принтеров (многословные — первыми, чтобы «Konica Minolta» матчился раньше «Konica»)
_PRN_BRANDS = ["Konica Minolta", "Hewlett Packard", "Canon", "HP", "Xerox", "Kyocera",
               "Samsung", "Epson", "Brother", "Ricoh", "Sharp", "Pantum", "OKI",
               "Lexmark", "Toshiba", "Dell", "Develop", "Olivetti", "Panasonic",
               "Katun", "Kodak", "Konica", "Minolta", "Utax", "Riso"]


def _shorten_name(name):
    """«Картридж + модель + для + бренд принтера»: режем секцию после первой запятой, затем
    оставляем только бренд принтера после «для» (серию/модель принтера и цвет убираем), чтобы
    название было коротким и не налезало на столбец «Артикул ВБ». Неизвестный бренд → первое
    слово после «для». Если «для» нет — обрезаем по длине."""
    if not name:
        return ""
    s = str(name).split(",")[0].strip()
    k = s.lower().find(" для ")
    if k == -1:
        return s[:46]
    head = s[:k + 5]              # «… для » (len ' для ' = 5)
    rest = s[k + 5:].strip()
    lr = rest.lower()
    for b in _PRN_BRANDS:
        bl = b.lower()
        if lr == bl or lr.startswith(bl + " "):
            return (head + rest[:len(b)]).strip()
    first = rest.split(" ")[0] if rest else ""
    return (head + first).strip()


def _name_of(r):
    return r["title"] or f'{r["brand"] or ""} {r["category"] or ""}'.strip() or (r["vendor_code"] or "")


def _rows():
    q = """
    WITH liv AS (
      SELECT s.account, s.nm_id, sum(s.quantity) q
      FROM wb_stocks s
      JOIN (SELECT account, max(captured_at) mx FROM wb_stocks GROUP BY account) m
        ON m.account=s.account AND m.mx=s.captured_at
      GROUP BY s.account, s.nm_id
    )
    SELECT c.account, c.nm_id, c.vendor_code, c.brand, c.category,
           c.orig_price, c.discount_pct, c.clearance_price,
           c.uploaded_wb_stock, c.seller_stock,
           COALESCE(l.q, 0) AS live_wb, t.title
    FROM wb_clearance c
    LEFT JOIN liv l ON l.account=c.account AND l.nm_id=c.nm_id
    LEFT JOIN wb_cards t ON t.account=c.account AND t.nm_id=c.nm_id
    LEFT JOIN wb_clearance_dismissed d ON d.account=c.account AND d.nm_id=c.nm_id
    WHERE d.nm_id IS NULL  -- закрытые позиции (остаток 0, цену подняли) не показываем
    """
    out = []
    for r in db.query(q):
        live = float(r["live_wb"] or 0)
        if live <= 0:
            sig, rank = ("red", "🔴 Поднять цену"), 0
        elif live <= LOW_THRESHOLD:
            sig, rank = ("amber", "🟡 Заканчивается"), 1
        else:
            sig, rank = ("green", "🟢 Распродажа"), 2
        up = float(r["uploaded_wb_stock"] or 0)
        sold = max(0, up - live)
        out.append({**r, "live": live, "sold": sold, "sig_cls": sig[0], "sig_txt": sig[1], "rank": rank})
    out.sort(key=lambda x: (x["rank"], -(x["clearance_price"] or 0)))
    return out


def _closed_rows():
    q = """
    SELECT d.account, d.nm_id, c.vendor_code, c.brand, c.category, c.clearance_price, t.title
    FROM wb_clearance_dismissed d
    LEFT JOIN wb_clearance c ON c.account=d.account AND c.nm_id=d.nm_id
    LEFT JOIN wb_cards t ON t.account=d.account AND t.nm_id=d.nm_id
    ORDER BY d.dismissed_at DESC
    """
    return db.query(q)


def _block(acc, rows, closed):
    esc = html.escape
    n = len(rows)
    red = sum(1 for r in rows if r["rank"] == 0)
    amber = sum(1 for r in rows if r["rank"] == 1)
    green = sum(1 for r in rows if r["rank"] == 2)

    trs = []
    for r in rows:
        raw = _name_of(r)
        name = _shorten_name(raw)
        trs.append(
            f'<tr><td class="chk"><input type="checkbox" class="rowchk" data-acc="{r["account"]}" data-nm="{r["nm_id"]}"></td>'
            f'<td class="nm" title="{esc(str(raw))}">{esc(name)}</td>'
            f'<td class="n">{r["nm_id"]}</td>'
            f'<td class="n">{_rub(r["orig_price"])}</td>'
            f'<td class="n">−{_rub(r["discount_pct"])}%</td>'
            f'<td class="n"><b>{_rub(r["clearance_price"])}</b></td>'
            f'<td class="n">{int(r["live"])}</td>'
            f'<td class="n">{_rub(r["uploaded_wb_stock"])}</td>'
            f'<td class="n">{int(r["sold"])}</td>'
            f'<td class="n">{_rub(r["seller_stock"])}</td>'
            f'<td><span class="sig {r["sig_cls"]}">{r["sig_txt"]}</span></td></tr>'
        )
    body = "\n".join(trs) or '<tr><td colspan="11" style="text-align:center;padding:24px;color:#889">Список пуст — загрузите файл через dropbox_bot</td></tr>'

    if closed:
        crs = []
        for cr in closed:
            crs.append(
                f'<tr><td>{esc(_shorten_name(_name_of(cr)))}</td><td class="n">{cr["nm_id"]}</td>'
                f'<td class="n">{_rub(cr["clearance_price"])}</td>'
                f'<td><button class="btn-restore" onclick="clrRestore(\'{cr["account"]}\',{cr["nm_id"]})">↩ Вернуть</button></td></tr>'
            )
        closed_html = "<table>" + "\n".join(crs) + "</table>"
    else:
        closed_html = '<p class="sub">Закрытых позиций нет.</p>'

    return f"""<section class="clr-block" data-acc="{acc}">
  <h2 class="blk-h">🏷️ {ACC_FULL.get(acc, acc)} <span class="cnt">{n} позиций в распродаже</span></h2>
  <div class="clr-cards">
    <div class="clr-kc red"><div class="l">🔴 Поднять цену (WB = 0)</div><div class="v">{red}</div></div>
    <div class="clr-kc amber"><div class="l">🟡 Заканчивается (≤{LOW_THRESHOLD})</div><div class="v">{amber}</div></div>
    <div class="clr-kc green"><div class="l">🟢 Ещё продаётся</div><div class="v">{green}</div></div>
    <div class="clr-kc"><div class="l">Закрыто (цену подняли)</div><div class="v">{len(closed)}</div></div>
  </div>
  <div class="clr-actions">
    <button class="btn-close" disabled onclick="closeSelected(this)">🗑 Закрыть выбранные (0)</button>
    <span class="sub">Отметьте отработанные позиции и нажмите — уйдут из таблицы (вернуть можно ниже).</span>
  </div>
  <div class="clr-scroll"><table class="clr">
    <thead><tr><th class="chk"><input type="checkbox" class="chkall" title="Выбрать все"></th>
    <th>Товар</th><th>Артикул WB</th><th>Цена</th><th>Скидка</th><th>Цена распр.</th>
    <th>Остаток WB</th><th>Было WB</th><th>Продано</th><th>Наш склад</th><th>Сигнал</th></tr></thead>
    <tbody>
{body}
    </tbody>
  </table></div>
  <p class="sub" style="margin-top:10px">Закрыто позиций (остаток 0, цену подняли): <b>{len(closed)}</b>
  <a href="#" class="closed-toggle" onclick="return toggleClosed(this)">— показать</a></p>
  <div class="closed-box" style="display:none">{closed_html}</div>
</section>"""


def render():
    all_rows = _rows()
    all_closed = _closed_rows()
    blocks = []
    for acc in ACCOUNTS:
        rws = [r for r in all_rows if r["account"] == acc]
        cls = [c for c in all_closed if c["account"] == acc]
        if not rws and not cls:
            continue
        blocks.append(_block(acc, rws, cls))
    blocks_html = "\n".join(blocks) or '<p class="sub">Список распродажи пуст — загрузите файлы через dropbox_bot.</p>'
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")

    html_doc = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Распродажа остатков ВБ · Пульт бизнеса</title>
<style>{SHELL_CSS}{REPORT_CSS}{CSS}</style>
</head>
<body>
<header>
{SIDEBAR_CLR}
</header>
<main id="mpr">
  <nav class="rtabs">
    <a class="rtab" href="/warehouse">🏭 Остатки складов</a>
    <a class="rtab cur" href="/reports/wb-clearance">🏷️ Распродажа остатков ВБ</a>
  </nav>
  <p class="eyebrow">Склад · Распродажа остатков ВБ</p>
  <h1>Распродажа остатков Wildberries</h1>
  <p class="sub">Следим за живым остатком на складе WB по двум юрлицам. Как только остаток SKU стал <b>0</b> —
  сигнал <span class="sig red">🔴 Поднять цену</span>, чтобы не продавать товар с нашего склада по скидке.
  Отработанные позиции отмечайте галочкой и закрывайте — за ними перестанем следить.
  Остатки WB — снимок {stamp} (обновляется ежедневно).</p>
{blocks_html}
</main>
{SCRIPT}
</body>
</html>"""
    _atomic_write(OUT, html_doc)
    return OUT


if __name__ == "__main__":
    print("OK →", render())
