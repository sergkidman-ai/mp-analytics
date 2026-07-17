"""reports/feedback_draft_run.py — ПРОГОН черновиков ответов на реальные вопросы И отзывы.

Детерминированный движок (без API-ключа): архитектура из переоценки —
  ОТЗЫВЫ  = шаблоны (позитив: ротация вариантов; негатив: хендофф в поддержку по QR).
  ВОПРОСЫ = роутер: сначала детектор проблемы/дефекта (→ хендофф), затем интент →
            факт из карточки (reports.card_facts) → прямой ответ; логистика → канон;
            нет данных → человек (без выдумок). Совместимость сверяется со списком моделей.

Прогон:
  1) весь неотвеченный backlog (отзывы + вопросы) → черновики + маршрут/уверенность;
  2) замер покрытия на выборке ОТВЕЧЕННЫХ вопросов (черновик vs наш реальный ответ).
Вывод: docs/feedback_run.html (полный) + docs/feedback_run_artifact.html (для артефакта).
Ничего не постит. PII → в git не коммитить.

Запуск:  ./venv/bin/python reports/feedback_draft_run.py
"""
import re
import sys
import html
import pathlib
from collections import Counter

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from core import db                                       # noqa: E402
from reports.card_facts import CardFacts                  # noqa: E402
from reports.feedback_corpus import intent, load_corpus   # noqa: E402

FULL = BASE_DIR / "docs" / "feedback_run.html"
ART = BASE_DIR / "docs" / "feedback_run_artifact.html"

# ─────────────────────── шаблоны отзывов ───────────────────────
POS_WB = [
    "{name}, благодарим, что выбрали {product} в нашем магазине «Цифровой квадрат»! Хорошего настроения 🌟",
    "{name}, спасибо за высокую оценку! Рады, что всё подошло. Лёгкой печати и приятных покупок 🛍",
    "{name}, благодарим за отзыв и 5 звёзд! Будем рады видеть вас снова 🌟",
    "{name}, спасибо, что выбираете нас! Приятно, что товар оправдал ожидания 🖨️🌟",
    "{name}, спасибо за доверие! Рады, что {product} вам подошёл. Хорошей и лёгкой печати 🖨️",
    "{name}, благодарим за тёплый отзыв! Нам очень приятно. Заходите к нам ещё 😊",
    "{name}, спасибо за 5 звёзд и за выбор нашего магазина! Всегда рады помочь с расходниками 🌟",
    "{name}, рады, что всё прошло гладко и товар порадовал! Спасибо, что были с «Цифровым квадратом» 🛍",
    "{name}, благодарим за оценку! Приятно знать, что картридж работает как надо. Хорошего дня ☀️",
    "{name}, спасибо, что нашли время оставить отзыв! Ждём вас за новыми покупками 🖨️🌟",
    "{name}, здорово, что {product} оправдал ожидания! Благодарим за выбор и доверие 💙",
    "{name}, спасибо за отзыв! Рады каждому довольному покупателю — обращайтесь ещё 🙌",
    "{name}, благодарим за поддержку и высокую оценку! Лёгкой печати без хлопот 🖨️",
    "{name}, приятно, что всё понравилось! Спасибо, что выбираете «Цифровой квадрат» 🌟",
    "{name}, спасибо за 5 звёзд! Будем и дальше стараться держать планку. Хорошего настроения 😊",
    "{name}, благодарим за заказ и добрые слова! Всегда рады видеть вас снова 🛍🌟",
]
POS_OZ = [
    "Благодарим за заказ и высокую оценку! Рады, что товар подошёл. Хорошего настроения 🌟",
    "Спасибо за 5 звёзд! Приятно, что оправдали ожидания — будем рады видеть вас снова 🛍",
    "Благодарим за отзыв! Лёгкой печати и хорошего настроения 🖨️🌟",
    "Спасибо, что выбрали наш магазин! Рады, что всё понравилось 🌟",
    "Благодарим за доверие и оценку! Рады, что картридж вам подошёл. Хорошей печати 🖨️",
    "Спасибо за тёплые слова! Нам очень приятно — заходите к нам ещё 😊",
    "Благодарим, что нашли время на отзыв! Всегда рады помочь с расходниками для печати 🙌",
    "Спасибо за 5 звёзд и за выбор нашего магазина! Обращайтесь ещё 🌟",
    "Рады, что всё прошло гладко и товар порадовал! Благодарим за покупку 🛍",
    "Спасибо за оценку! Приятно знать, что картридж работает как надо. Хорошего дня ☀️",
    "Благодарим за отзыв! Будем и дальше стараться. Лёгкой печати без хлопот 🖨️",
    "Спасибо, что выбираете нас! Рады каждому довольному покупателю 💙",
    "Благодарим за высокую оценку и доверие! Ждём вас за новыми покупками 🌟",
    "Спасибо за добрые слова! Всегда рады видеть вас снова в нашем магазине 😊",
    "Рады, что товар оправдал ожидания! Благодарим за выбор и хороший отзыв 🙌",
    "Спасибо за 5 звёзд! Хорошего настроения и приятных покупок 🛍🌟",
]
NEG_WB = ("Здравствуйте, {name}! Напишите нам, пожалуйста, в чат по QR-коду на упаковке или в товарном чеке внутри коробки о проблеме "
          "с {product} — обязательно разберёмся и поможем.")
NEG_OZ = ("Здравствуйте! Сожалеем, что товар вызвал нарекания. Напишите нам в чат по QR-коду на упаковке или в товарном чеке внутри коробки — "
          "обязательно разберёмся и поможем с решением.")

# ─────────────────────── помощники ───────────────────────
DEFECT_RX = re.compile(
    r"не\s+вид|не\s+печат|не\s+работ|не\s+опозна|не\s+распозна|не\s+захват|ошибк|замените\s+картридж|"
    r"мига|брак|бракован|полос|бледн|пуст[оа]|течёт|течет|подтек|вмятин|сломал|дефект|неисправ|"
    r"пятн|серый\s+фон|грязн|не\s+заряж", re.I)
MODEL_RX = re.compile(r"[A-Za-zА-Яа-я]{0,8}[- ]?\d{2,5}[A-Za-z0-9\-]*")


def _norm(s):
    return re.sub(r"[\s\-_/]", "", str(s).lower())


def _first_name(payload):
    n = ((payload or {}).get("userName") or "").strip()
    return n.split()[0] if n else None


def _short(name):
    name = re.sub(r"^(Картридж(и)?|Фотобарабан|Чернила|Набор\s+картриджей|Заправочный\s+комплект)\s+", "",
                  name or "", flags=re.I).strip()
    return (name[:55] + "…") if len(name) > 56 else (name or "ваш товар")


def _pick(variants, ext_id):
    return variants[sum(map(ord, str(ext_id))) % len(variants)]


def _asked_models(body):
    out = []
    for m in MODEL_RX.finditer(body or ""):
        t = m.group(0).strip(" -")
        if re.search(r"\d", t) and len(_norm(t)) >= 3:
            out.append(t)
    return out


def _compat_check(body, models):
    """Возвращает ('yes', matched) | ('no_data',[]) | ('unknown', asked) по списку моделей карточки."""
    asked = _asked_models(body)
    if not models:
        return ("no_data", [])
    if not asked:
        return ("no_ask", [])
    cn = [_norm(m) for m in models]
    matched = [a for a in asked if any(_norm(a) in c or c in _norm(a) for c in cn)]
    if matched:
        return ("yes", matched)
    return ("unknown", asked)


CHIP_TXT = {
    "installed": "Да, картридж {art} идёт с чипом — он уже установлен, дополнительно докупать ничего не нужно.",
    "not_required": "Здравствуйте! Чип на картридже {art} уже установлен, дополнительно докупать и ставить чип не требуется.",
    "none": "Здравствуйте! Картридж {art} поставляется без чипа — чип можно переставить с вашего прежнего "
            "картриджа, а в настройках принтера отключить слежение за расходными материалами.",
}


# ─────────────────────── композитор ВОПРОСА ───────────────────────
def draft_question(body, product_name, facts):
    """→ (draft, route, conf, source). route: auto|review|human. Пусто+human = на человека."""
    b = (body or "").lower()
    it = intent(body)
    art = (facts or {}).get("article") or ""
    art = re.sub(r"\s*DS$", "", art).strip()

    # 0) проблема/дефект → хендофф в поддержку (как негатив)
    if DEFECT_RX.search(b):
        return ("Здравствуйте! Напишите нам, пожалуйста, в чат по QR-коду на упаковке или в товарном чеке внутри коробки — специалисты "
                "разберутся в вашей ситуации и помогут.", "auto", 0.9, "классификатор: проблема→хендофф")

    # 1) чип
    if it == "чип/не читается" or re.search(r"\bчип", b):
        chip = (facts or {}).get("chip")
        if chip in CHIP_TXT:
            return (CHIP_TXT[chip].format(art=art).replace("  ", " "), "auto", 0.85, "карточка: чип")
        return ("", "human", 0.2, "чип не задан в карточке → человек")

    # 2) заправка / тонер
    if it == "заправка/тонер":
        if re.search(r"пигмент|водн|какой\s+тонер|производител", b):
            return ("", "human", 0.2, "состав/производитель — нет в карточке → человек")
        if re.search(r"перезаправ|заправлять|заправк|многоразов|дозаправ", b):
            return ("Здравствуйте! После окончания тонера картридж можно заправить — рекомендуем обратиться "
                    "в сервисный центр. Обратите внимание: гарантия на заправку не распространяется.",
                    "review", 0.6, "политика заправки")
        return ("Здравствуйте! Да, картридж поставляется заправленным, с тонером — полностью готов к работе.",
                "auto", 0.7, "стандарт: заправлен")

    # 3) оригинал / совместимый
    if it == "оригинал/совместимый" or re.search(r"оригинал", b):
        if (facts or {}).get("kind") and "совмест" in facts["kind"]:
            return ("Здравствуйте! Это совместимый картридж (аналог оригинала) высокого качества — не оригинал.",
                    "auto", 0.8, "карточка: kind=совместимый")
        return ("", "human", 0.2, "нет признака оригинал/совместимый → человек")

    # 4) совместимость с моделью
    if it == "совместимость модели" or re.search(r"подойд|подход|совмест|для .*(принтер|мфу)", b):
        status, models = _compat_check(body, (facts or {}).get("models") or [])
        if status == "yes":
            return (f"Здравствуйте! Да, подойдёт для {', '.join(models)} — эта модель есть в списке "
                    f"совместимости карточки.", "auto", 0.85, "карточка: модель в списке")
        if status == "unknown":
            return (f"Здравствуйте! Уточните, пожалуйста, точную модель принтера: в списке совместимости "
                    f"этой позиции модели {', '.join(models)} нет. Поможем подобрать верный вариант.",
                    "human", 0.4, "модель не в списке → уточнить/веб")
        return ("", "human", 0.25, "нет списка моделей / не распознали модель → человек")

    # 5) ресурс / хранение
    if it == "ресурс/объём":
        if re.search(r"хран|годност|срок", b):
            return ("Здравствуйте! В заводской упаковке, при комнатной температуре в сухом месте без прямых "
                    "солнечных лучей картриджи хранятся несколько лет. Гарантия — 1 год.", "auto", 0.65, "канон: хранение")
        res = (facts or {}).get("resource")
        if res and re.search(r"ресурс|страниц|хват|сколько", b):
            return (f"Здравствуйте! Ресурс картриджа {art} — около {res} страниц при 5% заполнении листа.",
                    "auto", 0.75, "карточка: ресурс")
        return ("", "human", 0.2, "ресурс/объём без данных → человек")

    # 6) логистика
    if it == "доставка/упаковка":
        return ("Здравствуйте! Сроки доставки, к сожалению, мы не контролируем — доставку выполняет площадка. "
                "По статусу заказа лучше обратиться в её службу поддержки.", "review", 0.55, "канон: логистика")

    # 7) возврат/брак → хендофф
    if it == "возврат/брак":
        return ("Здравствуйте! Напишите нам, пожалуйста, в чат по QR-коду на упаковке или в товарном чеке внутри коробки — поможем с возвратом "
                "или заменой и разберёмся в ситуации.", "review", 0.55, "возврат→хендофф")

    # 8) прочее / цвет-наличие → человек (нужен каталог)
    return ("", "human", 0.15, f"{it}: нужен каталог/контекст → человек")


# ─────────────────────── композитор ОТЗЫВА ───────────────────────
def draft_review(r, name, prod):
    rating = r["rating"] or 0
    empty = not (r["body"] or "").strip() and not (r["pros"] or "").strip() and not (r["cons"] or "").strip()
    if rating <= 3:
        cat = "negative"
        draft = (NEG_WB.format(name=name or "Здравствуйте", product=prod) if r["platform"] == "wb" else NEG_OZ)
        return cat, draft, "review", 0.5
    cat = "empty5" if empty else "positive"
    if r["platform"] == "wb":
        draft = _pick(POS_WB, r["ext_id"]).format(name=name or "Здравствуйте", product=prod)
    else:
        draft = _pick(POS_OZ, r["ext_id"])
    return cat, draft, "auto", (0.95 if cat == "empty5" else 0.8)


# ─────────────────────── прогон ───────────────────────
def run():
    cf = CardFacts()

    def facts_for(r):
        return cf.for_ozon(r["item_id"]) if r["platform"] == "ozon" else cf.for_wb(r["item_id"])

    # 1) неотвеченный backlog (свежие первыми)
    back = db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,body,pros,cons,payload,
        created_at FROM raw_feedback WHERE is_answered=false AND account IN ('wb_acc1','oz_acc1')
        ORDER BY created_at DESC NULLS LAST""")
    b_items = []
    for r in back:
        name = _first_name(r["payload"]) if r["platform"] == "wb" else None
        prod = _short(r["product_name"])
        if r["kind"] == "question":
            f = facts_for(r)
            draft, route, conf, src = draft_question(r["body"], r["product_name"], f)
            b_items.append(dict(r, cat="question", draft=draft, route=route, conf=conf, src=src,
                                facts=f, intent=intent(r["body"]), answer_text=None))
        else:
            cat, draft, route, conf = draft_review(r, name, prod)
            b_items.append(dict(r, cat=cat, draft=draft, route=route, conf=conf, src="шаблон отзыва"))

    # 2) замер покрытия на ОТВЕЧЕННЫХ вопросах (черновик vs реальный ответ)
    ans = db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,body,answer_text,payload
        FROM raw_feedback WHERE kind='question' AND is_answered AND coalesce(trim(answer_text),'')<>''
        AND length(trim(coalesce(body,'')))>12 AND account IN ('wb_acc1','oz_acc1')
        ORDER BY ext_id DESC LIMIT 400""")
    q_items = []
    for r in ans:
        f = facts_for(r)
        draft, route, conf, src = draft_question(r["body"], r["product_name"], f)
        q_items.append(dict(r, draft=draft, route=route, conf=conf, src=src, facts=f, intent=intent(r["body"])))

    return b_items, q_items


# ─────────────────────── рендер ───────────────────────
def _e(s):
    return html.escape(str(s or ""))


CAT_RU = {"question": "Вопрос", "negative": "Негатив", "positive": "Позитив+текст", "empty5": "Пустой 5★"}
BADGE = {"auto": ("АВТО", "b-auto"), "review": ("РЕВЬЮ", "b-review"), "human": ("ЧЕЛОВЕК", "b-human")}


def _route_counts(items):
    c = Counter(i["route"] for i in items)
    return c["auto"], c["review"], c["human"]


def _rows_reviews(items):
    out = []
    for i in items:
        rb, rc = BADGE[i["route"]]
        orig = (i["body"] or i["pros"] or i["cons"] or "").strip() or "(без текста)"
        rat = f"{i['rating']}★ " if i["rating"] else ""
        out.append(f"""<tr>
 <td class="plat">{i['platform']}</td>
 <td><span class="cls">{rat}{CAT_RU[i['cat']]}</span><div class="prod muted">{_e(i['product_name'])[:70]}</div></td>
 <td class="q">{_e(orig)[:160]}</td>
 <td class="ans"><div class="myans">{_e(i['draft'])}</div></td>
 <td><span class="badge {rc}">{rb}</span></td></tr>""")
    return "".join(out)


def _facts_line(f):
    if not f:
        return '<span class="muted">— нет фактов —</span>'
    p = []
    if f.get("chip"):
        m = {"installed": "с чипом", "not_required": "чип не требуется докупать", "none": "без чипа"}.get(f["chip"], f["chip"])
        p.append(f"чип: <b>{_e(m)}</b>")
    if f.get("resource"):
        p.append("ресурс " + _e(f["resource"]))
    if f.get("models"):
        p.append("модели: " + _e(", ".join(f["models"][:4])) + ("…" if len(f["models"]) > 4 else ""))
    return " · ".join(p) or '<span class="muted">—</span>'


def _rows_questions(items, limit=20):
    out = []
    for i in items[:limit]:
        rb, rc = BADGE[i["route"]]
        draft = _e(i["draft"]) if i["draft"] else '<span class="muted">— на человека —</span>'
        real = _e(i["answer_text"])[:260] if i.get("answer_text") else ""
        real_html = f'<div class="real"><span class="lbl">реальный ответ:</span> {real}</div>' if real else ""
        out.append(f"""<tr>
 <td class="plat">{i['platform']}<div class="muted" style="font-size:11px">{_e(i['intent'])}</div></td>
 <td class="q">{_e(i['body'])[:170]}<div class="prod muted">{_e(i['product_name'])[:60]}</div></td>
 <td class="facts">{_facts_line(i.get('facts'))}</td>
 <td class="ans"><div class="myans">{draft}</div>{real_html}<div class="src muted">{_e(i['src'])}</div></td>
 <td><span class="badge {rc}">{rb}</span></td></tr>""")
    return "".join(out)


STYLE = """
 :root{--bg:#eef1f4;--surface:#fff;--surface2:#f7f9fb;--ink:#141a20;--muted:#5f6b78;--border:#e0e5ea;
  --accent:#0f6e8c;--accent-soft:#e2eef2;--auto:#127c47;--auto-bg:#e5f4ec;--review:#8a6a00;--review-bg:#fbf1d8;
  --human:#b23020;--human-bg:#fbe7e3;--dash:#dbe1e7}
 @media(prefers-color-scheme:dark){:root{--bg:#0d1116;--surface:#161d24;--surface2:#111820;--ink:#e6ecf1;
  --muted:#93a0ad;--border:#26303a;--accent:#4bb8d6;--accent-soft:#123039;--auto:#54cc8b;--auto-bg:#122a1e;
  --review:#e2b64a;--review-bg:#2c2410;--human:#f0897b;--human-bg:#2e1a17;--dash:#2b333d}}
 :root[data-theme="dark"]{--bg:#0d1116;--surface:#161d24;--surface2:#111820;--ink:#e6ecf1;--muted:#93a0ad;
  --border:#26303a;--accent:#4bb8d6;--accent-soft:#123039;--auto:#54cc8b;--auto-bg:#122a1e;--review:#e2b64a;
  --review-bg:#2c2410;--human:#f0897b;--human-bg:#2e1a17;--dash:#2b333d}
 :root[data-theme="light"]{--bg:#eef1f4;--surface:#fff;--surface2:#f7f9fb;--ink:#141a20;--muted:#5f6b78;
  --border:#e0e5ea;--accent:#0f6e8c;--accent-soft:#e2eef2;--auto:#127c47;--auto-bg:#e5f4ec;--review:#8a6a00;
  --review-bg:#fbf1d8;--human:#b23020;--human-bg:#fbe7e3;--dash:#dbe1e7}
 *{box-sizing:border-box}
 body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;margin:0;
  background:var(--bg);color:var(--ink);-webkit-font-smoothing:antialiased}
 .wrap{max-width:1180px;margin:0 auto;padding:28px 20px 48px}
 .eyebrow{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--accent);font-weight:700;margin:0 0 6px}
 h1{font-size:25px;line-height:1.2;margin:0 0 6px;text-wrap:balance;letter-spacing:-.01em}
 h2{font-size:16px;margin:30px 0 10px;letter-spacing:-.01em}
 .sub{color:var(--muted);margin:0 0 20px;max-width:72ch}
 .card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:15px 18px}
 .card b{color:var(--ink)}
 .tiles{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:18px 0}
 .tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
 .tile .n{font-size:29px;font-weight:750;font-variant-numeric:tabular-nums;line-height:1}
 .tile .k{font-size:12.5px;color:var(--muted);margin-top:6px}
 .tile.auto{border-top:3px solid var(--auto)}.tile.auto .n{color:var(--auto)}
 .tile.review{border-top:3px solid var(--review)}.tile.review .n{color:var(--review)}
 .tile.human{border-top:3px solid var(--human)}.tile.human .n{color:var(--human)}
 .note{background:var(--accent-soft);border:1px solid var(--border);border-radius:12px;padding:14px 18px;margin:18px 0;font-size:14px}
 .note b{color:var(--ink)}
 .scroll{overflow-x:auto;border:1px solid var(--border);border-radius:12px;background:var(--surface)}
 table{border-collapse:collapse;width:100%;min-width:880px}
 th,td{text-align:left;padding:11px 13px;border-bottom:1px solid var(--border);vertical-align:top}
 tr:last-child td{border-bottom:none}
 th{background:var(--surface2);font-size:11.5px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);font-weight:700}
 tbody tr:hover{background:var(--surface2)}
 .plat{font-weight:650;text-transform:capitalize;white-space:nowrap}
 .q{max-width:280px} .prod{font-size:11.5px;margin-top:3px}
 .facts{font-size:12.5px;max-width:190px}
 .ans{max-width:400px}.myans{font-weight:500}
 .real{margin-top:7px;padding-top:7px;border-top:1px dashed var(--dash);font-size:12.5px}
 .real .lbl{color:var(--auto);font-weight:650}
 .src{margin-top:6px;font-size:11.5px}
 .cls{font-size:12.5px;font-weight:650;color:var(--accent)}
 .muted{color:var(--muted)}
 .badge{display:inline-block;padding:3px 10px;border-radius:20px;font-size:11.5px;font-weight:750;white-space:nowrap}
 .b-auto{color:var(--auto);background:var(--auto-bg)}.b-review{color:var(--review);background:var(--review-bg)}
 .b-human{color:var(--human);background:var(--human-bg)}
 .foot{color:var(--muted);font-size:13px;margin-top:20px;max-width:78ch}
 @media(max-width:720px){.tiles{grid-template-columns:repeat(2,1fr)}}
"""


def render(b_items, q_items):
    reviews = [i for i in b_items if i["cat"] != "question"]
    bq = [i for i in b_items if i["cat"] == "question"]
    r_auto, r_rev, r_hum = _route_counts(reviews)
    # покрытие вопросов на отвеченной выборке
    q_auto, q_rev, q_hum = _route_counts(q_items)
    q_total = len(q_items)
    cov = round(100 * (q_auto) / q_total) if q_total else 0
    cov_soft = round(100 * (q_auto + q_rev) / q_total) if q_total else 0
    rev_cnt = Counter(i["cat"] for i in reviews)

    body = f"""<div class="wrap">
<p class="eyebrow">Цифровой квадрат · прогон черновиков</p>
<h1>Черновики ответов на реальные вопросы и отзывы</h1>
<p class="sub">Детерминированный движок без ИИ-ключа: отзывы — по шаблонам, вопросы — факт из карточки +
классификатор. Ничего не опубликовано. Внизу — замер качества вопросов на {q_total} отвеченных обращениях
(черновик против нашего реального ответа).</p>

<h2>① Отзывы — весь неотвеченный backlog ({len(reviews)})</h2>
<div class="tiles">
 <div class="tile"><div class="n">{rev_cnt['empty5']}</div><div class="k">пустые 5★ → шаблон «спасибо»</div></div>
 <div class="tile"><div class="n">{rev_cnt['positive']}</div><div class="k">позитив с текстом → шаблон</div></div>
 <div class="tile"><div class="n">{rev_cnt['negative']}</div><div class="k">негатив → хендофф по QR</div></div>
 <div class="tile auto"><div class="n">{r_auto}</div><div class="k">из них АВТО (безопасно постить)</div></div>
</div>
<div class="scroll"><table>
<thead><tr><th>Пл.</th><th>Тип / товар</th><th>Отзыв</th><th>Черновик ответа</th><th>Маршрут</th></tr></thead>
<tbody>{_rows_reviews(reviews[:20])}</tbody></table></div>
<p class="foot">Показаны 20 самых свежих неотвеченных отзывов. Позитив/негатив безопасны к автопостингу;
текст позитива ротируется, чтобы не было идентичных ответов подряд.</p>

<h2>② Вопросы — движок на реальных обращениях</h2>
<div class="tiles">
 <div class="tile auto"><div class="n">{cov}%</div><div class="k">уверенный автоответ (АВТО)</div></div>
 <div class="tile review"><div class="n">{cov_soft-cov}%</div><div class="k">черновик на ревью</div></div>
 <div class="tile human"><div class="n">{100-cov_soft}%</div><div class="k">на человека (нет данных)</div></div>
 <div class="tile"><div class="n">{q_total}</div><div class="k">вопросов в замере (+{len(bq)} из backlog)</div></div>
</div>
<div class="note"><b>Как читать:</b> для каждого вопроса — факты, поднятые из карточки, черновик движка и
<b>наш реальный исторический ответ</b> для сверки. Где движок отвечает фактом (чип, совместимость, ресурс,
оригинал) — это АВТО; дефект/возврат → хендофф; где данных нет (состав тонера, наличие другого артикула) →
честно ЧЕЛОВЕК.</div>
<div class="scroll"><table>
<thead><tr><th>Пл. / интент</th><th>Вопрос</th><th>Факты карточки</th><th>Черновик / сверка</th><th>Маршрут</th></tr></thead>
<tbody>{_rows_questions(bq + q_items, limit=20)}</tbody></table></div>

<p class="foot">Прогон детерминированный (без LLM). С ключом Claude API «мозги» усилят пограничные случаи
(нестандартные формулировки, кросс-совместимость через веб), но каркас «шаблоны + факт-таблица» уже
закрывает основную массу. Автопостинг — по вашему решению.</p>
</div>"""

    full = ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Прогон черновиков — Цифровой квадрат</title>'
            f'<style>{STYLE}</style></head><body>{body}</body></html>')
    FULL.write_text(full, encoding="utf-8")
    ART.write_text(f'<title>Прогон черновиков — Цифровой квадрат</title>\n<style>{STYLE}</style>\n{body}',
                   encoding="utf-8")
    print(f"Отзывы backlog: {len(reviews)} (auto {r_auto}/rev {r_rev}/hum {r_hum}) | {dict(rev_cnt)}")
    print(f"Вопросы замер: {q_total} → авто {q_auto} ({cov}%) / ревью {q_rev} / человек {q_hum}; backlog-вопросов {len(bq)}")
    print(f"→ {FULL}\n→ {ART}")


if __name__ == "__main__":
    b_items, q_items = run()
    render(b_items, q_items)
