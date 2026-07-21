"""reports/ozon_removal_candidates.py — отбор товара к вывозу со склада Ozon FBO.

Правила (OR), решено с пользователем (4 критерия):
  C1   «капает хранение» → застой ≥90д   — прокси платного хранения для ЛЮБОГО товара. Per-SKU платное
                                          хранение/возраст Ozon API не отдаёт (проверено: нет поля, теги
                                          = FBS_RETURN/возвраты, excess=0). Ловит и чёрные части наборов.
  C2   цветные части наборов, застой>30д  — offer_id ∈ обратный индекс set_cost.components (наборы из
                                          thecartridge) + цвет 9602 ≠ black + days_without_sales > 30.
  C3   товары НЕ из набора, застой>60д     — offer_id ∉ обратный индекс наборов + days_without_sales > 60.
  W    инвентаризация Озона → проверить   — карточка из архива (is_archived) с остатком ИЛИ появление на
       (watch, не к вывозу)                 стоке партией ≥3 шт сразу (обычные возвраты 1–2 шт исключены).
                                            Точного канала «что поставил Ozon» в API нет (supply-order=404).

Обязательные поля для заявки в ЛК Ozon: точное название склада, артикул (offer_id), количество.
Источник склада и количества — ozon_fbo_stock (последний снимок). Сигналы — ozon_stock_signals.
Создания заявки в Seller API нет (только UI) → выход полуавтомат: список кандидатов, человек
оформляет вывоз в ЛК (FBO → Вывоз и утилизация → «Со стока»).

Запуск:  ./venv/bin/python reports/ozon_removal_candidates.py [--build] [--report]
"""
import sys
import datetime
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
import re  # noqa: E402
from core import db  # noqa: E402

ACCOUNTS = ["oz_acc1", "oz_acc2"]
ACC_LABEL = {"oz_acc1": "Цифровой", "oz_acc2": "Дисквэр"}
C1_DWS = 90            # C1 «капает хранение» — прокси: любой товар с застоем ≥ 90д (per-SKU
                       #     платного хранения Ozon API не отдаёт — проверено; ловит и чёрные части наборов)
C2_DWS_MIN = 30        # C2 — цветной из набора: вывозим при застое > 30 дней
C3_DWS_MIN = 60        # C3 — НЕ из набора: вывозим при застое > 60 дней
WATCH_DAYS = 14        # W инвентаризация Озона: «новое на стоке» не позже чем WATCH_DAYS назад
WATCH_MIN_QTY = 3      # W: партия ≥3 шт сразу (обычные возвраты 1–2 шт — не считаем инвентаризацией)
COLOR_9602 = 9602      # атрибут Ozon «Цвет тонера»

# бренды/стоп-слова для укорачивания наименования до «Тип + модель»
_NAME_STOP = re.compile(
    r"\b(для\s+принтеров|для|Canon|HP|Epson|Samsung|Xerox|Brother|Kyocera|OKI|Konica|Sharp|"
    r"Lexmark|Ricoh|Pantum|Panasonic|Katun|голубой|пурпурн\w*|жёлт\w*|желт\w*|чёрн\w*|черн\w*|"
    r"матов\w*|совместим\w*)", re.I)


def _short_name(name):
    """Наименование → «Тип + модель», напр. «Картридж 054HC для принтеров Canon…» → «Картридж 054HC»."""
    s = (name or "").strip()
    m = _NAME_STOP.search(s)
    if m:
        s = s[:m.start()].strip()
    return s or (name or "")[:40]


def _color_of(payload):
    """Значение атрибута 9602 из raw_ozon_attributes.payload → строка цвета или ''."""
    attrs = payload.get("attributes") if isinstance(payload, dict) else payload
    if not isinstance(attrs, list):
        return ""
    for a in attrs:
        if isinstance(a, dict) and (a.get("attribute_id") == COLOR_9602 or a.get("id") == COLOR_9602):
            vals = a.get("values") or []
            return ",".join(str(v.get("value", "")) for v in vals if isinstance(v, dict)).strip()
    return ""


def _is_black(color):
    c = (color or "").lower()
    return "черн" in c or "чёрн" in c or "black" in c


def build(account, run_date):
    """Пересобрать кандидатов на вывоз для аккаунта на дату run_date, записать в таблицу."""
    # 1. база: последний снимок стока (склад + артикул + количество)
    base = db.query("""
        SELECT warehouse, item_code AS offer_id, sku, item_name AS name,
               sum(free_to_sell)::int AS qty
        FROM ozon_fbo_stock
        WHERE account=%s
          AND captured_at=(SELECT max(captured_at) FROM ozon_fbo_stock WHERE account=%s)
          AND free_to_sell > 0 AND item_code IS NOT NULL
        GROUP BY warehouse, item_code, sku, item_name
    """, (account, account))

    # 2. сигналы days_without_sales (последний снимок сигналов) — по sku
    dws = {r["sku"]: r["days_without_sales"] for r in db.query("""
        SELECT sku, days_without_sales FROM ozon_stock_signals
        WHERE account=%s
          AND captured_at=(SELECT max(captured_at) FROM ozon_stock_signals WHERE account=%s)
    """, (account, account))}

    # 3. цвет (атрибут 9602) по offer_id
    color = {r["offer_id"]: _color_of(r["payload"]) for r in db.query(
        "SELECT offer_id, payload FROM raw_ozon_attributes WHERE account=%s", (account,))}

    # 4. обратный индекс компонент → наборы (C3)
    rev = {}
    for row in db.query("SELECT external_code, components FROM set_cost WHERE components IS NOT NULL"):
        for comp in (row["components"] or []):
            rev.setdefault(str(comp), set()).add(str(row["external_code"]))

    # 5. first_seen на стоке + границы истории (C4)
    first_seen = {r["offer_id"]: r["fs"] for r in db.query("""
        SELECT item_code AS offer_id, min(captured_at) AS fs
        FROM ozon_fbo_stock WHERE account=%s AND free_to_sell>0 AND item_code IS NOT NULL
        GROUP BY item_code
    """, (account,))}
    hist = db.query("SELECT min(captured_at) a, max(captured_at) b FROM ozon_fbo_stock WHERE account=%s",
                    (account,))[0]
    hist_start, hist_end = hist["a"], hist["b"]
    watch_from = hist_end - datetime.timedelta(days=WATCH_DAYS)

    # 6. архивные карточки
    archived = {r["offer_id"] for r in db.query(
        "SELECT offer_id FROM ozon_product WHERE account=%s AND is_archived=true", (account,))}

    # 7. антиповтор: позиции с уже оформленной заявкой (не предлагаем повторно). Автоочистка —
    #    удаляем из реестра то, чего больше нет в текущем снимке стока (Ozon вывез).
    stock_keys = {(b["offer_id"], b["warehouse"]) for b in base}
    submitted_rows = db.query(
        "SELECT offer_id, warehouse FROM ozon_removal_submitted WHERE account=%s", (account,))
    stale = [(r["offer_id"], r["warehouse"]) for r in submitted_rows
             if (r["offer_id"], r["warehouse"]) not in stock_keys]
    for off_, wh_ in stale:
        db.execute("DELETE FROM ozon_removal_submitted WHERE account=%s AND offer_id=%s AND warehouse=%s",
                   (account, off_, wh_))
    submitted = {(r["offer_id"], r["warehouse"]) for r in submitted_rows} - set(stale)

    recs = []
    for b in base:
        off, sku = b["offer_id"], b["sku"]
        col = color.get(off, "")
        sets = rev.get(str(off), set())
        d = dws.get(sku)
        fs = first_seen.get(off)
        arch = off in archived
        in_set = bool(sets)
        rules = []
        if d is not None and d >= C1_DWS:                                   # C1 — капает хранение (застой ≥90д, любой товар)
            rules.append("C1")
        if in_set and col and not _is_black(col) and d is not None and d > C2_DWS_MIN:  # C2 — цветной из набора >30д
            rules.append("C2")
        if (not in_set) and d is not None and d > C3_DWS_MIN:              # C3 — не из набора >60д
            rules.append("C3")
        # W — «проверить: инвентаризация Озона». Только достоверные признаки, БЕЗ обычных возвратов
        # (1–2 шт): карточка из архива с остатком ИЛИ появление на стоке партией ≥3 шт сразу.
        new_on_stock = fs is not None and fs > hist_start and fs >= watch_from
        if not rules and (arch or (new_on_stock and b["qty"] >= WATCH_MIN_QTY)):
            rules.append("W")
        if not rules:
            continue
        # антиповтор: пропускаем removal-позиции с уже оформленной заявкой (watch-строки не трогаем)
        if rules != ["W"] and (off, b["warehouse"]) in submitted:
            continue
        recs.append({
            "run_date": run_date, "account": account, "warehouse": b["warehouse"],
            "offer_id": off, "sku": sku, "name": (b["name"] or "")[:200], "qty": b["qty"],
            "color": col or None, "days_without_sales": d,
            "in_sets": ",".join(sorted(sets)) if sets else None,
            "first_seen": fs, "is_archived": arch, "rules": ",".join(rules),
        })

    # перезаписать срез этого прогона
    db.execute("DELETE FROM ozon_removal_candidates WHERE run_date=%s AND account=%s", (run_date, account))
    if recs:
        db.upsert("ozon_removal_candidates", recs,
                  conflict_cols=["run_date", "account", "warehouse", "offer_id"],
                  update_cols=["sku", "name", "qty", "color", "days_without_sales",
                               "in_sets", "first_seen", "is_archived", "rules"])
    return recs


def mark_submitted(run_date, codes=None):
    """Пометить «заявка оформлена» по removal-позициям последнего списка (codes — фильтр артикулов,
    None = все). Такие позиции движок больше не предлагает, пока они не уйдут со стока. → список помеченных."""
    codeset = {str(c).strip() for c in codes} if codes else None
    rows = db.query("""SELECT account, offer_id, warehouse, sku, qty, name FROM ozon_removal_candidates
                       WHERE run_date=%s AND rules<>'W'""", (run_date,))
    marked = [dict(r, submitted_at=run_date) for r in rows
              if codeset is None or r["offer_id"] in codeset]
    if marked:
        db.upsert("ozon_removal_submitted", marked,
                  conflict_cols=["account", "offer_id", "warehouse"],
                  update_cols=["sku", "qty", "name", "submitted_at"])
    return marked


def unmark_submitted(codes):
    """Снять пометку «оформлено» по артикулам (вернуть в предложения). → сколько снято."""
    codeset = [str(c).strip() for c in codes]
    n = 0
    for r in db.query("SELECT account, offer_id, warehouse FROM ozon_removal_submitted WHERE offer_id = ANY(%s)", (codeset,)):
        db.execute("DELETE FROM ozon_removal_submitted WHERE account=%s AND offer_id=%s AND warehouse=%s",
                   (r["account"], r["offer_id"], r["warehouse"]))
        n += 1
    return n


RULE_TXT = {"C1": "хранение ≥90д", "C2": "цветной из набора >30д", "C3": "не из набора >60д"}
WATCH_CAP = 30         # максимум строк в watch-секции, чтобы отчёт оставался компактным


def format_report(run_date):
    """Текстовый отчёт: removal-кандидаты (по аккаунту→складу) + watch «проверить»."""
    rows = db.query("""SELECT * FROM ozon_removal_candidates WHERE run_date=%s
                       ORDER BY account, warehouse, rules, offer_id""", (run_date,))
    removal = [r for r in rows if r["rules"] != "W"]
    watch = [r for r in rows if r["rules"] == "W"]
    if not removal and not watch:
        return f"🟢 Вывоз со склада Ozon — на {run_date} кандидатов нет."

    out = [f"📦 Вывоз со склада Ozon — на {run_date}",
           "Оформить: ЛК → FBO → Вывоз и утилизация → «Со стока».", ""]
    by_acc = {}
    for r in removal:
        by_acc.setdefault(r["account"], []).append(r)
    for acc, arows in by_acc.items():
        total = sum(r["qty"] for r in arows)
        out.append(f"━━━ {ACC_LABEL.get(acc, acc)} · к вывозу {len(arows)} поз., {total} шт ━━━")
        by_wh = {}
        for r in arows:
            by_wh.setdefault(r["warehouse"], []).append(r)
        for wh, wrows in by_wh.items():
            out.append(f"🏬 {wh}")
            for r in wrows:
                tags = " ".join(f"[{RULE_TXT.get(t, t)}]" for t in r["rules"].split(","))
                extra = []
                if r["days_without_sales"] is not None:
                    extra.append(f"{r['days_without_sales']}д без продаж")
                if r["color"]:
                    extra.append(r["color"])
                ex = " · ".join(extra)
                nm = _short_name(r["name"])
                out.append(f"  • {r['offer_id']} ×{r['qty']} — {tags}"
                           + (f"\n     {nm}" if nm else "")
                           + (f"\n     {ex}" if ex else ""))
            out.append("")
    if not removal:
        out.append("К вывозу по правилам C1/C2/C3 — пусто.\n")

    if watch:
        out.append(f"🔎 Проверить: инвентаризация Озона "
                   f"({len(watch)} поз. — из архива или партия ≥{WATCH_MIN_QTY} шт сразу)")
        for r in watch[:WATCH_CAP]:
            mark = "из архива" if r["is_archived"] else f"партия ×{r['qty']}"
            out.append(f"  • {r['offer_id']} ×{r['qty']} · {ACC_LABEL.get(r['account'], r['account'])}"
                       f" · {r['warehouse']} · {mark}")
        if len(watch) > WATCH_CAP:
            out.append(f"  … ещё {len(watch) - WATCH_CAP} — полный список в БД ozon_removal_candidates (rules='W').")
        out.append("")

    sub = db.query("SELECT count(*) n, coalesce(sum(qty),0) q FROM ozon_removal_submitted")[0]
    if sub["n"]:
        out.append(f"🚫 Скрыто (заявка уже оформлена): {sub['n']} поз., {sub['q']} шт — "
                   f"не предлагаю повторно, пока не уйдут со стока. Вернуть: /vyvoz_reset <артикул>.")
    out.append("Правила: C1 хранение (застой ≥90д) · C2 цветной из набора >30д · "
               "C3 не из набора >60д · W инвентаризация Озона: из архива/партия ≥3 (проверить, не к вывозу).")
    out.append("Оформили заявку по позициям — отметьте /oformleno (все) или /oformleno 5698 4526 (выборочно).")
    return "\n".join(out)


def _esc(s):
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_html(run_date):
    """Печатный pick-лист для оформления заявки на вывоз (self-contained HTML)."""
    rows = db.query("SELECT * FROM ozon_removal_candidates WHERE run_date=%s "
                    "ORDER BY account, warehouse, rules, offer_id", (run_date,))
    removal = [r for r in rows if r["rules"] != "W"]
    watch = [r for r in rows if r["rules"] == "W"]
    parts = []
    by_acc = {}
    for r in removal:
        by_acc.setdefault(r["account"], []).append(r)
    for acc, arows in by_acc.items():
        total = sum(r["qty"] for r in arows)
        parts.append(f'<h2>{_esc(ACC_LABEL.get(acc, acc))} <span class="muted">· к вывозу '
                     f'{len(arows)} поз., {total} шт</span></h2>')
        by_wh = {}
        for r in arows:
            by_wh.setdefault(r["warehouse"], []).append(r)
        for wh, wrows in by_wh.items():
            wtot = sum(r["qty"] for r in wrows)
            parts.append(f'<h3>{_esc(wh)} <span class="muted">· {wtot} шт</span></h3>')
            parts.append('<table><thead><tr><th>Артикул</th><th>Кол-во</th><th>Наименование</th>'
                         '<th>Причина</th><th class="num">Без продаж</th><th>Цвет</th>'
                         '</tr></thead><tbody>')
            for r in wrows:
                tags = ", ".join(RULE_TXT.get(t, t) for t in r["rules"].split(","))
                dws = f'{r["days_without_sales"]}д' if r["days_without_sales"] is not None else "—"
                parts.append(
                    f'<tr><td class="art">{_esc(r["offer_id"])}</td><td class="num qty">{r["qty"]}</td>'
                    f'<td>{_esc(_short_name(r["name"]))}</td><td>{_esc(tags)}</td><td class="num">{dws}</td>'
                    f'<td>{_esc(r["color"] or "—")}</td></tr>')
            parts.append('</tbody></table>')
    if not removal:
        parts.append('<p class="empty">К вывозу по правилам C1/C2/C3 — пусто.</p>')
    if watch:
        parts.append(f'<h2 class="watch">🔎 Проверить · инвентаризация Озона '
                     f'<span class="muted">({len(watch)} поз. — из архива или партия ≥{WATCH_MIN_QTY} шт сразу)</span></h2>')
        parts.append('<table><thead><tr><th>Артикул</th><th>Кол-во</th><th>Аккаунт</th>'
                     '<th>Склад</th><th>Признак</th><th>Наименование</th></tr></thead><tbody>')
        for r in watch:
            mark = "из архива" if r["is_archived"] else f"партия ×{r['qty']}"
            parts.append(
                f'<tr><td class="art">{_esc(r["offer_id"])}</td><td class="num qty">{r["qty"]}</td>'
                f'<td>{_esc(ACC_LABEL.get(r["account"], r["account"]))}</td><td>{_esc(r["warehouse"])}</td>'
                f'<td>{_esc(mark)}</td><td>{_esc(_short_name(r["name"]))}</td></tr>')
        parts.append('</tbody></table>')
    body = "\n".join(parts) or '<p class="empty">Кандидатов нет.</p>'
    tot_pos = len(removal)
    tot_qty = sum(r["qty"] for r in removal)
    return _HTML_TMPL.replace("{{DATE}}", str(run_date)).replace("{{BODY}}", body) \
        .replace("{{POS}}", str(tot_pos)).replace("{{QTY}}", str(tot_qty))


_HTML_TMPL = """<title>Вывоз со склада Ozon · {{DATE}}</title>
<style>
  :root{--bg:#f7f7f5;--card:#fff;--ink:#1a1a1a;--muted:#8a8a82;--line:#e6e6e0;
        --accent:#b02a2a;--watch:#a06a00;--head:#f0efe9;}
  @media (prefers-color-scheme:dark){:root{--bg:#16171a;--card:#1e2024;--ink:#e9e9e6;
        --muted:#9a9a92;--line:#2c2f34;--accent:#e06666;--watch:#d9a441;--head:#24262b;}}
  :root[data-theme=light]{--bg:#f7f7f5;--card:#fff;--ink:#1a1a1a;--muted:#8a8a82;--line:#e6e6e0;
        --accent:#b02a2a;--watch:#a06a00;--head:#f0efe9;}
  :root[data-theme=dark]{--bg:#16171a;--card:#1e2024;--ink:#e9e9e6;--muted:#9a9a92;--line:#2c2f34;
        --accent:#e06666;--watch:#d9a441;--head:#24262b;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
  .wrap{max-width:1000px;margin:0 auto;padding:32px 20px 64px}
  header{border-bottom:2px solid var(--accent);padding-bottom:14px;margin-bottom:8px}
  h1{font-size:22px;margin:0 0 4px}
  .sub{color:var(--muted);font-size:13px}
  .totals{display:flex;gap:20px;margin:14px 0 8px;flex-wrap:wrap}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 16px}
  .kpi b{font-size:20px;display:block;font-variant-numeric:tabular-nums}
  .kpi span{color:var(--muted);font-size:12px}
  h2{font-size:17px;margin:26px 0 6px;padding-top:8px}
  h2.watch{color:var(--watch)}
  h3{font-size:14px;margin:14px 0 4px;color:var(--accent);font-weight:600}
  .muted{color:var(--muted);font-weight:400;font-size:13px}
  .tblwrap{overflow-x:auto}
  table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);
        border-radius:8px;overflow:hidden;margin:2px 0 6px;font-size:13.5px}
  th{background:var(--head);text-align:left;padding:7px 10px;font-weight:600;font-size:12px;
     text-transform:uppercase;letter-spacing:.03em;color:var(--muted)}
  td{padding:7px 10px;border-top:1px solid var(--line);vertical-align:top}
  td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
  td.art{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-weight:600;white-space:nowrap}
  td.qty{font-weight:700}
  .empty{color:var(--muted);padding:20px 0}
  .note{color:var(--muted);font-size:12.5px;margin-top:28px;border-top:1px solid var(--line);padding-top:12px}
  @media print{body{background:#fff}.kpi,table{border-color:#ccc}}
</style>
<div class="wrap">
  <header>
    <h1>Вывоз со склада Ozon FBO</h1>
    <div class="sub">Кандидаты на {{DATE}} · оформить: ЛК → FBO → Вывоз и утилизация → «Со стока»</div>
  </header>
  <div class="totals">
    <div class="kpi"><b>{{POS}}</b><span>позиций к вывозу</span></div>
    <div class="kpi"><b>{{QTY}}</b><span>штук всего</span></div>
  </div>
  {{BODY}}
  <p class="note">Правила отбора: <b>C1</b> — «капает хранение»: застой ≥90 дней без продаж (прокси —
  per-SKU платного хранения Ozon API не отдаёт) · <b>C2</b> — цветная часть набора, НЕ чёрная, без продаж
  &gt;30 дней (цвет из атрибута Ozon «Цвет тонера») · <b>C3</b> — товар не из набора, без продаж &gt;60 дней ·
  <b>W</b> — инвентаризация Озона: карточка из архива или партия ≥3 шт сразу (проверить, не к вывозу).
  Создания заявки в Ozon Seller API нет — список полуавтомат, заявку оформляет человек в ЛК.</p>
</div>
"""


def main():
    args = set(sys.argv[1:]) or {"--build", "--report"}
    run_date = datetime.date.today()
    if "--build" in args:
        for acc in ACCOUNTS:
            recs = build(acc, run_date)
            byrule = {}
            for r in recs:
                byrule[r["rules"]] = byrule.get(r["rules"], 0) + 1
            print(f"{ACC_LABEL.get(acc, acc)}: кандидатов {len(recs)} | по правилам {byrule}", flush=True)
    if "--report" in args:
        print("\n" + format_report(run_date))
    for a in args:
        if a.startswith("--html="):
            path = a.split("=", 1)[1]
            with open(path, "w", encoding="utf-8") as f:
                f.write(build_html(run_date))
            print(f"HTML → {path}", flush=True)


if __name__ == "__main__":
    main()
