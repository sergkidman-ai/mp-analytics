"""reports/questions_prototype_html.py — рендер HTML-прототипа движка вопросов.

Читает доказательную базу (proto_evidence.json) и МОИ (сессия=мозги) решения по каждому
вопросу: класс, маршрут, ответ, источник, уверенность. Показывает рядом реальный
исторический ответ для сверки. Вывод — docs/questions_prototype.html (PII → gitignore).

Это ДЕМО, не продакшн: в проде «мозги» = Claude API (batch) с теми же источниками.
"""
import sys
import json
import html
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
EVID = "/tmp/claude-0/-opt-mp-analytics/cbfef8e3-231d-43df-95de-edbb2cff5f9a/scratchpad/proto_evidence.json"
OUT = BASE_DIR / "docs" / "questions_prototype.html"

# МОИ решения: (sku, подстрока вопроса) -> decision. Маршрут: auto | review | human.
# Источник: карточка | прошлые Q&A | веб | нет данных.
D = [
 # === ФАКТ ИЗ КАРТОЧКИ — уверенный автоответ ===
 ("209665462", "с чипом", dict(cls="Факт: чип", route="auto", conf="высокая",
   src="карточка (chip=installed)",
   ans="Здравствуйте! Да, картридж E260A21E для Lexmark идёт с чипом — установлен, докупать ничего не нужно.")),
 ("549947933", "Катюша м130", dict(cls="Факт: совместимость", route="auto", conf="высокая",
   src="карточка (модель в списке)",
   ans="Здравствуйте, Ольга! Да, фотобарабан + картридж PCM130+THM130 подойдёт для МФУ Катюша M130.")),
 ("1153395894", "Это Оригинал", dict(cls="Факт: оригинал/аналог", route="auto", conf="высокая",
   src="карточка (kind=совместимый)",
   ans="Здравствуйте, Наталья! Это совместимый фотобарабан C-EXV14 DU — аналог оригинала высокого качества.")),
 ("257945736", "оригинал", dict(cls="Факт: оригинал/аналог", route="auto", conf="высокая",
   src="карточка (kind=совместимый)",
   ans="Здравствуйте, Юлия! Картридж W1540A — новый, совместимый (аналог оригинала), с чипом и гарантией год.")),
 ("199333284", "не заправленными", dict(cls="Факт: заправка", route="auto", conf="высокая",
   src="карточка + прошлые Q&A",
   ans="Здравствуйте! Картридж CF310A поставляется заправленным и готовым к работе.")),

 # === УВЕРЕННЫЙ ОТКАЗ по совместимости (модели нет в списке) ===
 ("1155649394", "Altalink", dict(cls="Факт: совместимость (нет)", route="auto", conf="высокая",
   src="карточка (модели нет в списке)",
   ans="Здравствуйте! Нет, на Xerox AltaLink C8030 этот фотобарабан не подойдёт.")),

 # === ЗАМЕНА УКЛОНЧИВОГО ответа прямым фактом (то, что клиент назвал «бесячим») ===
 ("192634315", "перезаправляемые", dict(cls="Факт: заправка (замена «в теории»)", route="review", conf="средняя",
   src="карточка + политика из прошлых Q&A",
   ans="Здравствуйте, Ольга! Специальных многоразовых картриджей (с отверстием для заправки и многоразовым чипом) у нас нет. Наши 920XL заправить можно после окончания чернил, но потребуется замена чипа. Заправку рекомендуем в сервисном центре; гарантия на заправку не распространяется.")),

 # === ВЕБ (источник №3) — карточка не покрывает ===
 ("1692352035", "M2000DNWs", dict(cls="Совместимость → ВЕБ (подтв.)", route="review", conf="высокая",
   src="карточка (M2000DW) + веб-подтверждение семейства",
   ans="Здравствуйте! Да, картридж T2 подойдёт к Deli M2000DNWs — это то же семейство M2000, что и указанные на карточке. Чип уже установлен, отдельная прошивка не требуется. Важно: вскрытый установленный картридж возврату не подлежит (кроме брака).")),
 ("1611110080", "c2504", dict(cls="Совместимость → ВЕБ (спорно)", route="human", conf="низкая",
   src="карточка (C2500/C2000) + веб (модель неоднозначна)",
   ans="Здравствуйте! Уточните, пожалуйста, точную модель принтера. Наши картриджи 842451–842454 предназначены для Ricoh IM/M C2000, C2500. Для модели Ricoh MP C2504 они не подходят — там другая серия картриджей.")),

 # === КАТАЛОГ/АРТИКУЛ — нужен поиск по номенклатуре → человек ===
 ("199334449", "Штучно", dict(cls="Наличие/артикул → каталог", route="human", conf="низкая",
   src="нет в карточке (нужен поиск SKU)",
   ans="[черновик] Да, чёрный можно купить отдельно — подставить артикул из номенклатуры. (реальный ответ был: артикул 199334171)")),
 ("906874689", "T027 цветной", dict(cls="Наличие другого товара → каталог", route="human", conf="низкая",
   src="нет в карточке (вопрос про ДРУГОЙ артикул)",
   ans="[черновик] Проверить наличие T027 цветного в номенклатуре и дать артикул/статус.")),

 # === ЛОГИСТИКА — типовой канонический ответ (авто) ===
 ("192228592", "товарный чек", dict(cls="Логистика: чек", route="auto", conf="высокая",
   src="прошлые Q&A (канон)",
   ans="Здравствуйте! Товарный чек мы вкладываем в каждый заказ; также закрывающие документы доступны в вашем личном кабинете.")),
 ("216341479", "задерживается", dict(cls="Логистика: доставка", route="auto", conf="высокая",
   src="прошлые Q&A (канон)",
   ans="Здравствуйте! К сожалению, сроки доставки мы не контролируем — доставку выполняет Wildberries. Просим обратиться в их службу поддержки.")),
 ("199335024", "деньги назад", dict(cls="Логистика: возврат денег", route="human", conf="средняя",
   src="прошлые Q&A (канон) — но деньги → человек",
   ans="Здравствуйте! Мы не занимаемся доставкой и расчётами. Вы можете отказаться от заказа при получении или оформить возврат; по возврату средств обратитесь в поддержку Wildberries.")),

 # === ДЕФЕКТ/ПРОБЛЕМА — хендофф в поддержку по QR (как негатив) ===
 ("866443528", "не видит", dict(cls="Проблема → хендофф", route="auto", conf="высокая",
   src="классификатор (дефект)",
   ans="Здравствуйте! Напишите, пожалуйста, нам в чат по QR-коду на упаковке о проблеме с картриджем 70 для HP — обязательно разберёмся и поможем.")),
 ("873312854", "серый фон", dict(cls="Проблема → хендофф", route="auto", conf="высокая",
   src="классификатор (дефект)",
   ans="Здравствуйте! Напишите, пожалуйста, нам в чат по QR-коду на упаковке о проблеме с печатью серого фона на картриджах W2030A-W2033A — поможем разобраться.")),
 ("199569932", "белые листы", dict(cls="Проблема → хендофф", route="auto", conf="высокая",
   src="классификатор (дефект)",
   ans="Здравствуйте, Александра! Напишите, пожалуйста, в поддержку по QR-коду на коробке и в товарном чеке — поможем решить.")),
 ("2203349842", "не опознан", dict(cls="Проблема → хендофф", route="auto", conf="высокая",
   src="классификатор (дефект)",
   ans="Здравствуйте! Напишите, пожалуйста, нам в чат по QR-коду на упаковке о проблеме «тонер не опознан» с картриджем для Toshiba e-Studio 18 — обязательно поможем.")),
 ("651939639", "захватывать бумагу", dict(cls="Проблема → хендофф", route="auto", conf="высокая",
   src="классификатор (дефект)",
   ans="Здравствуйте! Напишите, пожалуйста, нам в чат по QR-коду на упаковке — специалисты помогут с проблемой захвата бумаги на вашем Canon Selphy.")),

 # === НЕТ ДАННЫХ В КАРТОЧКЕ (не картридж / нет признака) → человек, НЕ выдумываем ===
 ("199572507", "с чипом или нет", dict(cls="Факт: чип — НЕТ в WB-карточке", route="human", conf="низкая",
   src="карточка не дала признак чипа (зона улучшения)",
   ans="[на человека] WB-карточка 737 не содержит явного признака чипа → не гадаем. Улучшение: взять чип из Ozon-двойника этого товара (там chip явно задан).")),
 ("1179635796", "оригинал", dict(cls="Не картридж → нет данных", route="human", conf="низкая",
   src="нет карточки-фактов (утюг)",
   ans="[на человека] Товар — утюг Braun, факт-слой картриджей неприменим. Ответ должен дать продавец бытовой техники.")),
 ("1175251000", "насадки от модели", dict(cls="Не картридж → ВЕБ/человек", route="human", conf="низкая",
   src="нет карточки-фактов (эпилятор)",
   ans="[на человека/веб] Совместимость насадок эпилятора Rowenta EP8430↔EP5660 — вне факт-слоя картриджей; проверить в вебе или у поставщика.")),
 ("3735014796", "пигментная", dict(cls="Факт вне карточки → ВЕБ/человек", route="human", conf="низкая",
   src="в карточке нет признака пигмент/водные",
   ans="[черновик] По GI-46: чёрный — пигмент, цветные — водные (нет в карточке, подтвердить). Реальный ответ был именно таким.")),
]


def facts_line(f):
    if not f:
        return '<span class="muted">— нет фактов карточки —</span>'
    parts = []
    if f.get("chip"):
        m = {"installed": "с чипом (установлен)", "not_required": "чип не требуется докупать",
             "none": "без чипа"}.get(f["chip"], f["chip"])
        parts.append(f"чип: <b>{html.escape(m)}</b>")
    if f.get("resource"):
        parts.append(f"ресурс: {html.escape(str(f['resource']))}")
    if f.get("kind"):
        parts.append(html.escape(f["kind"]))
    if f.get("refillable"):
        parts.append("заправляемый")
    if f.get("models"):
        parts.append("модели: " + html.escape(", ".join(f["models"][:6])) + ("…" if len(f["models"]) > 6 else ""))
    return " · ".join(parts)


ROUTE_BADGE = {"auto": ("АВТО", "auto"),
               "review": ("РЕВЬЮ", "review"),
               "human": ("ЧЕЛОВЕК", "human")}


def main():
    evid = json.loads(pathlib.Path(EVID).read_text(encoding="utf-8"))
    rows = []
    n_auto = n_review = n_human = 0
    for sku, sub, dec in D:
        rec = next((e for e in evid if e["sku"] == sku and sub.lower() in (e["question"] or "").lower()), None)
        if not rec:
            rows.append(f'<tr><td colspan="6" style="color:#a50e0e">НЕ НАЙДЕН: {sku} / {sub}</td></tr>')
            continue
        rb, rcls = ROUTE_BADGE[dec["route"]]
        if dec["route"] == "auto":
            n_auto += 1
        elif dec["route"] == "review":
            n_review += 1
        else:
            n_human += 1
        real = rec.get("real_answer") or ""
        real_html = (f'<div class="real"><span class="lbl">Наш реальный ответ:</span> {html.escape(real)}</div>'
                     if real else '<div class="real muted">(из неотвеченного backlog — реального ответа ещё нет)</div>')
        rows.append(f"""<tr>
 <td class="plat">{rec['platform']}<br><span class="muted">{html.escape(rec['sku'])}</span></td>
 <td class="q">{html.escape(rec['question'])}
   <div class="prod muted">{html.escape(rec.get('product') or '')}</div></td>
 <td><span class="cls">{html.escape(dec['cls'])}</span></td>
 <td class="facts">{facts_line(rec.get('card_facts'))}</td>
 <td class="ans"><div class="myans">{html.escape(dec['ans'])}</div>{real_html}
   <div class="src muted">источник: {html.escape(dec['src'])} · уверенность: {dec['conf']}</div></td>
 <td><span class="badge b-{rcls}">{rb}</span></td>
</tr>""")

    total = n_auto + n_review + n_human
    style = """
 :root{
   --bg:#eef1f4; --surface:#ffffff; --surface2:#f7f9fb; --ink:#141a20; --muted:#5f6b78;
   --border:#e0e5ea; --accent:#0f6e8c; --accent-soft:#e2eef2;
   --auto:#127c47; --auto-bg:#e5f4ec; --review:#8a6a00; --review-bg:#fbf1d8;
   --human:#b23020; --human-bg:#fbe7e3; --dash:#dbe1e7;
 }
 @media (prefers-color-scheme:dark){
   :root{
     --bg:#0d1116; --surface:#161d24; --surface2:#111820; --ink:#e6ecf1; --muted:#93a0ad;
     --border:#26303a; --accent:#4bb8d6; --accent-soft:#123039;
     --auto:#54cc8b; --auto-bg:#122a1e; --review:#e2b64a; --review-bg:#2c2410;
     --human:#f0897b; --human-bg:#2e1a17; --dash:#2b333d;
   }
 }
 :root[data-theme="dark"]{
   --bg:#0d1116; --surface:#161d24; --surface2:#111820; --ink:#e6ecf1; --muted:#93a0ad;
   --border:#26303a; --accent:#4bb8d6; --accent-soft:#123039;
   --auto:#54cc8b; --auto-bg:#122a1e; --review:#e2b64a; --review-bg:#2c2410;
   --human:#f0897b; --human-bg:#2e1a17; --dash:#2b333d;
 }
 :root[data-theme="light"]{
   --bg:#eef1f4; --surface:#ffffff; --surface2:#f7f9fb; --ink:#141a20; --muted:#5f6b78;
   --border:#e0e5ea; --accent:#0f6e8c; --accent-soft:#e2eef2;
   --auto:#127c47; --auto-bg:#e5f4ec; --review:#8a6a00; --review-bg:#fbf1d8;
   --human:#b23020; --human-bg:#fbe7e3; --dash:#dbe1e7;
 }
 *{box-sizing:border-box}
 body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
   margin:0;background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
 .wrap{max-width:1180px;margin:0 auto;padding:28px 20px 48px}
 .eyebrow{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--accent);font-weight:700;margin:0 0 6px}
 h1{font-size:25px;line-height:1.2;margin:0 0 6px;text-wrap:balance;letter-spacing:-.01em}
 .sub{color:var(--muted);margin:0 0 22px;max-width:70ch}
 .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px}
 .how b{color:var(--ink)}
 .tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}
 .tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
 .tile .n{font-size:30px;font-weight:750;font-variant-numeric:tabular-nums;line-height:1}
 .tile .k{font-size:12.5px;color:var(--muted);margin-top:6px}
 .tile.auto{border-top:3px solid var(--auto)} .tile.auto .n{color:var(--auto)}
 .tile.review{border-top:3px solid var(--review)} .tile.review .n{color:var(--review)}
 .tile.human{border-top:3px solid var(--human)} .tile.human .n{color:var(--human)}
 .tile.tot .n{color:var(--ink)}
 .note{background:var(--accent-soft);border:1px solid var(--border);border-radius:12px;padding:15px 18px;margin:20px 0;font-size:14px}
 .note b{color:var(--ink)}
 .scroll{overflow-x:auto;border:1px solid var(--border);border-radius:12px;background:var(--surface)}
 table{border-collapse:collapse;width:100%;min-width:900px}
 th,td{text-align:left;padding:12px 13px;border-bottom:1px solid var(--border);vertical-align:top}
 tr:last-child td{border-bottom:none}
 th{background:var(--surface2);font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;
   color:var(--muted);font-weight:700;position:sticky;top:0}
 tbody tr:hover{background:var(--surface2)}
 .plat{font-weight:650;text-transform:capitalize;white-space:nowrap}
 .q{max-width:260px;font-weight:500} .prod{font-size:12px;margin-top:4px;font-weight:400}
 .facts{font-size:13px;max-width:220px;color:var(--ink)}
 .ans{max-width:400px} .myans{font-weight:500}
 .real{margin-top:8px;padding-top:8px;border-top:1px dashed var(--dash);font-size:13px}
 .real .lbl{color:var(--auto);font-weight:650}
 .src{margin-top:7px;font-size:12px}
 .cls{font-size:12.5px;font-weight:650;color:var(--accent)}
 .muted{color:var(--muted)}
 .badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11.5px;font-weight:750;
   white-space:nowrap;letter-spacing:.02em}
 .b-auto{color:var(--auto);background:var(--auto-bg)}
 .b-review{color:var(--review);background:var(--review-bg)}
 .b-human{color:var(--human);background:var(--human-bg)}
 .foot{color:var(--muted);font-size:13px;margin-top:18px;max-width:75ch}
 @media(max-width:720px){.tiles{grid-template-columns:repeat(2,1fr)}}
"""
    body = f"""<div class="wrap">
<p class="eyebrow">Цифровой квадрат · движок обращений</p>
<h1>Прототип автоответчика на вопросы</h1>
<p class="sub">{total} реальных вопросов WB&nbsp;+&nbsp;Ozon. «Мозги» в этом прогоне — сессия Claude Code;
в проде та&nbsp;же логика на Claude&nbsp;API. Отзывы (позитив/негатив) движок не трогает — там шаблоны.</p>

<div class="card how">
 <b>Как это работает.</b> Каждый вопрос проходит роутер: <b>классификатор</b> определяет тип обращения, затем
 ответ ищется в трёх источниках&nbsp;— <b>①&nbsp;наши прошлые Q&amp;A</b> (2932&nbsp;пары), <b>②&nbsp;факты карточки</b>
 (структурный экстрактор: чип&nbsp;в&nbsp;3&nbsp;состояниях, модели, ресурс, тип), <b>③&nbsp;веб</b>&nbsp;— когда карточка
 не покрывает. Факт твёрдый&nbsp;→ прямой ответ; данных нет&nbsp;→ честная эскалация человеку, без выдумок.
 Дефекты и логистика уходят по своим полосам.
</div>

<div class="tiles">
 <div class="tile auto"><div class="n">{n_auto}</div><div class="k">АВТО — можно постить</div></div>
 <div class="tile review"><div class="n">{n_review}</div><div class="k">РЕВЬЮ — беглый взгляд человека</div></div>
 <div class="tile human"><div class="n">{n_human}</div><div class="k">ЧЕЛОВЕК — нет данных / деньги / каталог</div></div>
 <div class="tile tot"><div class="n">{total}</div><div class="k">всего вопросов в прогоне</div></div>
</div>

<div class="note">
 <b>Главное, что показывает прототип:</b> экстрактор карточки&nbsp;v2 даёт <b>прямой ответ про чип</b>
 (3&nbsp;состояния) — это заменяет уклончивые формулировки, которые вы назвали «бесячими»
 («если на оригинале есть чип, то и у&nbsp;нас», «в&nbsp;теории заправить можно»). Веб закрывает совместимость,
 которой нет в карточке: Deli&nbsp;M2000DNWs&nbsp;→ уверенное «да», Ricoh&nbsp;c2504&nbsp;→ честное «уточните модель».
 Где данных нет (не&nbsp;картридж, признак не задан, нужен артикул из каталога) — движок не гадает.
</div>

<div class="scroll"><table>
<thead><tr><th>Площадка</th><th>Вопрос</th><th>Класс</th><th>Факты карточки&nbsp;②</th>
<th>Ответ движка / сверка</th><th>Маршрут</th></tr></thead>
<tbody>
{''.join(rows)}
</tbody></table></div>
<p class="foot">Прототип, не продакшн. В проде «мозги» = Claude&nbsp;API (Message&nbsp;Batches, −50%) с теми&nbsp;же
тремя источниками; нужен только ключ&nbsp;API. Автопостинг — по вашему решению после оценки качества.</p>
</div>"""
    full = ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Прототип движка вопросов — Цифровой квадрат</title>'
            f'<style>{style}</style></head><body>{body}</body></html>')
    OUT.write_text(full, encoding="utf-8")
    # артефакт-версия: контент напрямую (skeleton добавит doctype/head/body)
    art = f'<title>Прототип движка вопросов — Цифровой квадрат</title>\n<style>{style}</style>\n{body}'
    (BASE_DIR / "docs" / "questions_prototype_artifact.html").write_text(art, encoding="utf-8")
    print(f"OK → {OUT}  (auto {n_auto} / review {n_review} / human {n_human})")
    print(f"артефакт → {BASE_DIR/'docs'/'questions_prototype_artifact.html'}")


if __name__ == "__main__":
    main()
