# поток: rev
"""Недельный дайджест по обращениям покупателей — контентщику и закупщику (задача 11.09.2026).

Зачем. Движок отвечает на каждое обращение по отдельности и тем самым ГАСИТ сигнал: один и тот же
вопрос, заданный шесть раз по одной карточке, шесть раз получает вежливый ответ — и никто не узнаёт,
что в карточке дырка. То же с претензиями: ответ покупателю не доходит до закупщика, а именно ему
решать, что делать с партией, где мажента мажет.

Принцип. Здесь НЕТ LLM и быть не должно: дайджест — это счёт по фактам, а не мнение. Группировка
идёт по классу обращения (`reports/request_class.py`), теме и симптому — всё регулярками, офлайн,
воспроизводимо. Цена ошибки низкая (никто не отвечает покупателю), но цена выдумки высокая: если
в списке закупщика окажется артикул, которого там быть не должно, следующий список он не откроет.

Два адресата — два файла:
  • контентщику — чего не хватает в карточке (вопросы, которые карточка обязана была снять);
  • закупщику — что ломается в товаре (претензии по симптомам, срез по цвету CMYK).

Запуск:
    ./venv/bin/python -m reports.feedback_digest --days 30            # собрать и показать
    ./venv/bin/python -m reports.feedback_digest --days 7 --send      # + отправить в Telegram
"""
import os
import re
import sys
import argparse
import datetime as dt
import pathlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import db                                       # noqa: E402
from reports import request_class as rc                   # noqa: E402

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
OUT_ROOT = BASE_DIR / "docs" / "reports"
MIN_HITS = 2            # «≥2 вопроса одного класса» и «≥2 претензии одного симптома»
COLOR_DAYS = 30         # срез по цвету считается по 30 дням всегда, даже если окно дайджеста 7
COLOR_MIN = 3


# ── темы вопросов для контентщика ───────────────────────────────────────────────────────────
# Это НЕ классы request_class: там «чип», «оригинальность» и «тип чернил» слиты в одни
# «характеристики» — для контентщика это три разных абзаца карточки, и сваливать их в кучу
# бессмысленно. Порядок значим, первое совпадение выигрывает.
TOPICS = [
    ("совместимость", r"подойд|подойт|подош[её]л|подход|совмест|встанет\s+ли|для\s+модел|"
                      r"для\s+моего|для\s+моей|аналог|вместо\s+картридж"),
    ("комплектация", r"комплект|в\s+набор|набор\w*\s+из|что\s+вход|сколько\s+(?:штук|шт\b|картридж)"),
    ("заправка", r"заправ|дозапр|перезапр|снпч|зали(?:ть|вать)|одноразов"),
    ("чип", r"\bчип\w*|прошив|dynamic\s*security|счётчик|счетчик"),
    ("оригинальность", r"оригинал|подделк|контрафакт|\bref\b|восстановлен|б/?у\b"),
    ("тип чернил", r"чернил|пигмент|водораствор|водорасвор|красител|тонер"),
]

# ── симптомы претензий для закупщика ────────────────────────────────────────────────────────
# Словарь физических отказов: закупщик разговаривает с поставщиком именно на этом языке
# («партия полосит»), а не на языке классов обращения.
SYMPTOMS = [
    ("ошибка чипа", r"ошибк\w*\s+(?:чип|картридж)|чип\w*\s+не\s+(?:работ|раб|чит)|"
                    r"не\s+распозна|не\s+опозна|не\s+определя|пишет\s+ошибк"),
    ("не видит", r"не\s+вид(?:ит|но)|не\s+обнаруж|принтер\s+не\s+прин"),
    ("пустой", r"пуст(?:ой|ая|ое|ые|ым)|без\s+тонер|без\s+чернил|мало\s+тонер|"
               r"законч(?:ил|ился|ились|илась)\s+(?:сразу|через|бы|уже)|\bнедолив"),
    ("полосит", r"полос\w*|полошит|штрих\w*\s+на\s+(?:лист|бумаг)"),
    ("мажет", r"мажет|мараеет|марает|пачкает|грязн\w*\s+(?:лист|печат|бумаг)|осыпа\w*\s+тонер"),
    ("не подошёл", r"не\s+подош|не\s+подход|не\s+встал|не\s+совпад|не\s+тот\s+(?:размер|картридж)|"
                   r"друг(?:ой|ое|ая)\s+(?:товар|картридж|цвет)|пересорт"),
    ("сбой подачи", r"замин|зажёвыв|зажевыв|не\s+захват|не\s+подаёт|не\s+подает|застрева|"
                    r"скрип|хруст|шум\w*\s+при\s+печат"),
    ("не печатает", r"не\s+печат|печат\w*\s+не\s+иде|бледн|блекл|не\s+пропечат"),
]

# Претензия «после заправки» — не наш дефект: покупатель залил в картридж своё. Такие строки
# в список закупщика не попадают вовсе (прямое указание Сергея), но и не теряются — считаются
# отдельным числом в шапке файла, чтобы было видно, сколько отсеяли и не режем ли лишнего.
REFILLED_RX = re.compile(
    r"после\s+(?:за|пере)правк|сам\w*\s+заправ|заправил|перезаправ|дозаправ|залил\s+(?:свои|чернил)",
    re.I)

# Расхождение карточки с товаром — отдельный блок контентщику: тут виновата не полиграфия,
# а фото и описание, которые обещали не то.
MISMATCH_RX = re.compile(
    r"на\s+фото\s+(?:друг|не\s+т|был)|не\s+как\s+(?:в|на)\s+(?:описан|фото|картинк)|"
    r"друг(?:ой|ое|ая|ие)\s+товар|не\s+соответств\w*\s+(?:опис|фото|карточ)|"
    r"фото\s+не\s+соответств|в\s+описании\s+(?:одно|друг)|прислали\s+не\s+т",
    re.I)

# Цвета CMYK. Ключ — канонический цвет, значение — как покупатель его называет.
COLORS = [
    ("мажента", r"мажент|маджент|magenta|пурпур|малинов|розов"),
    ("голубой", r"голуб\w*|cyan|циан|синий"),
    ("жёлтый", r"ж[её]лт\w*|yellow"),
    ("чёрный", r"ч[её]рн\w*|black|\bbk\b"),
]

_AFFIRM_RX = re.compile(r"\bда\b|подойд[её]т|подход(?:ит|ящ)|совмест|встанет|работать\s+будет", re.I)
# Модель принтера в вопросе. Кириллицу в начало НЕ пускаем: общий MODEL_RX движка её допускает,
# и «На 3103fdw подойдет?» давал кандидата «На 3103fdw», а «нужен картридж 106R01158» —
# «ртридж 106R01158». В заголовок карточки такое попасть не должно.
MODEL_RX = re.compile(r"\b[A-Za-z]{1,12}[- ]?\d{2,5}[A-Za-z0-9\-]*|\b\d{3,5}[A-Za-z][A-Za-z0-9\-]*")


def _first(rules, text):
    t = text or ""
    for name, rx in rules:
        if re.search(rx, t, re.I):
            return name
    return None


def _text(r):
    """Текст покупателя одной строкой. У отзывов смысл размазан по pros/cons — склеиваем все три."""
    return " ".join(x for x in (r.get("body"), r.get("pros"), r.get("cons")) if x).strip()


def _short(t, n=160):
    t = re.sub(r"\s+", " ", (t or "").strip())
    return t if len(t) <= n else t[:n - 1] + "…"


def _rows(days, kind=None):
    where = ["created_at > now() - make_interval(days => %s)", "coalesce(article,'') <> ''"]
    args = [days]
    if kind:
        where.append("kind = %s")
        args.append(kind)
    return db.query(f"""SELECT platform, account, kind, ext_id, item_id, article, product_name,
                               rating, body, pros, cons, created_at
                        FROM raw_feedback WHERE {' AND '.join(where)}
                        ORDER BY created_at""", tuple(args))


def _group(rows, keyfn):
    out = {}
    for r in rows:
        k = keyfn(r)
        if k:
            out.setdefault(k, []).append(r)
    return out


# ─────────────────────────────── контентщику ───────────────────────────────
def content_gaps(days):
    """Артикулы, по которым за окно пришло ≥2 вопроса одной темы. Такой вопрос — это дырка
    в карточке: покупатель не нашёл ответа там, где обязан был найти."""
    rows = [r for r in _rows(days, kind="question") if _text(r)]
    # Вопрос без темы из списка (доставка, «когда ответите», «где мой заказ») — не дырка
    # в карточке, а работа поддержки: в список контентщика такие строки не берём вовсе.
    g = _group(rows, lambda r: (r["article"], _first(TOPICS, _text(r)))
               if _first(TOPICS, _text(r)) else None)
    out = []
    for (art, topic), rs in g.items():
        if len(rs) < MIN_HITS:
            continue
        out.append({"article": art, "topic": topic, "n": len(rs),
                    "product": rs[0].get("product_name") or "",
                    "texts": [_short(_text(r)) for r in rs],
                    "advice": ADVICE.get(topic, "добавить в карточку прямой ответ на этот вопрос")})
    return sorted(out, key=lambda x: (-x["n"], x["article"]))


ADVICE = {
    "совместимость": "полный список моделей принтера в заголовке и в характеристиках",
    "комплектация": "строка «в комплекте: …» с числом картриджей и цветами",
    "заправка": "строка о заправляемости и о том, как это влияет на гарантию",
    "чип": "строка о чипе: установлен / не требуется / переставляется, и что будет со счётчиком",
    "оригинальность": "прямая строка «совместимый аналог» или «оригинал» — без эвфемизмов",
    "тип чернил": "тип расходника (пигмент / водорастворимые / тонер) в характеристиках",
}


def card_mismatch(days):
    """Отзывы про «на фото другое» и «не как в описании» — претензия к карточке, не к товару."""
    out = []
    for r in _rows(days, kind="review"):
        t = _text(r)
        if t and MISMATCH_RX.search(t):
            out.append({"article": r["article"], "product": r.get("product_name") or "",
                        "rating": r.get("rating"), "text": _short(t, 220),
                        "platform": r["platform"], "created": r["created_at"]})
    return sorted(out, key=lambda x: x["article"])


def title_candidates(days):
    """Модели принтера из вопросов о совместимости, которых НЕТ в списке карточки, а ответ был
    утвердительный и утверждён человеком. Это готовые кандидаты в заголовок: спрос подтверждён
    вопросом, совместимость подтверждена оператором, а карточка о модели молчит.

    Строгость намеренная: берём только те строки, где ответ реально ушёл после ✅ (state='sent'),
    иначе в список уехали бы черновики, которых человек не видел."""
    try:
        from reports.card_facts import CardFacts
        cf = CardFacts()
    except Exception as e:                           # карточек нет — блок пустой, дайджест живёт
        return [], f"список моделей карточки недоступен ({e})"
    rows = db.query("""SELECT f.platform, f.account, f.kind, f.ext_id, f.item_id, f.article,
                              f.product_name, f.body, m.final_text
                       FROM raw_feedback f
                       JOIN feedback_moderation m
                         ON (m.platform, m.account, m.kind, m.ext_id)
                          = (f.platform, f.account, f.kind, f.ext_id)
                       WHERE f.kind = 'question' AND m.state = 'sent'
                         AND f.created_at > now() - make_interval(days => %s)
                         AND coalesce(f.article,'') <> ''""", (days,))
    out = []
    for r in rows:
        q = r.get("body") or ""
        if rc.classify(q, kind="question") != "совместимость":
            continue
        if not _AFFIRM_RX.search(r.get("final_text") or ""):
            continue
        f = (cf.for_ozon(r["item_id"]) if r["platform"] == "ozon"
             else cf.for_wb(r["item_id"]) if r["platform"] == "wb"
             else cf.for_yandex(r["item_id"]) if r["platform"] == "yandex" else None) or {}
        card = " ".join([*(f.get("models") or []), str(f.get("code") or ""),
                         r.get("product_name") or ""]).lower()
        card = re.sub(r"[\s\-]", "", card)
        new = []
        for m in MODEL_RX.finditer(q):
            tok = m.group(0)
            if not re.search(r"\d", tok):
                continue
            if re.sub(r"[\s\-]", "", tok.lower()) in card:
                continue
            new.append(tok)
        if new:
            out.append({"article": r["article"], "product": r.get("product_name") or "",
                        "models": sorted(set(new)), "question": _short(q, 180),
                        "has_card_models": bool(f.get("models"))})
    return sorted(out, key=lambda x: x["article"]), None


# ─────────────────────────────── закупщику ───────────────────────────────
def symptom_claims(days):
    """Артикулы с ≥2 претензиями ОДНОГО симптома. Претензией считаем то же, что считает движок
    (`request_class.is_claim_text`), — иначе у закупщика и у модератора разошлись бы определения."""
    rows, skipped_refill = [], 0
    # Берём и ВОПРОСЫ тоже: претензия сплошь и рядом приходит в вопросительной форме («все
    # картриджи полные, а красный пустой — как такое возможно?»), ровно ради этого в движке
    # и живёт правило A1.1. Ограничься мы отзывами — самый громкий кейс лота Xerox C310
    # (5723M8LPHJEP, август 2026) в список закупщика не попал бы вовсе.
    for r in _rows(days):
        t = _text(r)
        if not t or not rc.is_claim_text(t, kind=r["kind"], rating=r.get("rating")):
            continue
        if REFILLED_RX.search(t):
            skipped_refill += 1
            continue
        s = _first(SYMPTOMS, t)
        if s:
            rows.append(dict(r, _symptom=s, _t=t))
    g = _group(rows, lambda r: (r["article"], r["_symptom"]))
    out = []
    for (art, sym), rs in g.items():
        if len(rs) < MIN_HITS:
            continue
        stars = [r["rating"] for r in rs if r.get("rating") is not None]
        out.append({"article": art, "symptom": sym, "n": len(rs),
                    "product": rs[0].get("product_name") or "",
                    "stars": stars,
                    "texts": [{"rating": r.get("rating"), "platform": r["platform"],
                               "kind": r["kind"], "text": _short(r["_t"], 220)} for r in rs]})
    return sorted(out, key=lambda x: (-x["n"], x["article"])), skipped_refill


def color_alert(days=COLOR_DAYS):
    """Один цвет в ≥3 претензиях по РАЗНЫМ артикулам за 30 дней — это уже не карточка и не
    единичный брак, а партия или сам цвет у поставщика. Требование разных артикулов обязательно:
    три жалобы на один артикул — это строка симптомов выше, а не системный сигнал по цвету."""
    by, base = {}, {}
    for r in _rows(days):
        t = _text(r)
        if not t:
            continue
        # Цвет ищем ТОЛЬКО в словах покупателя. Название карточки для этого не годится:
        # у набора CMYK в названии есть все четыре цвета сразу, и любая претензия к набору
        # разошлась бы по всем четырём — ровно тот мусор, из-за которого блок перестают читать.
        c = _first(COLORS, t)
        if not c:
            continue
        base[c] = base.get(c, 0) + 1          # знаменатель: сколько всего отзывов про этот цвет
        if not rc.is_claim_text(t, kind=r["kind"], rating=r.get("rating")):
            continue
        if REFILLED_RX.search(t):
            continue
        by.setdefault(c, []).append(dict(r, _t=t))
    out = []
    for c, rs in by.items():
        arts = sorted({r["article"] for r in rs})
        if len(rs) >= COLOR_MIN and len(arts) >= 2:
            out.append({"color": c, "n": len(rs), "articles": arts, "base": base.get(c, len(rs)),
                        "texts": [{"article": r["article"], "rating": r.get("rating"),
                                   "text": _short(r["_t"], 160)} for r in rs[:6]]})
    # Сортируем по ДОЛЕ, а не по числу: чёрный будет в этом блоке всегда — его просто больше
    # всего продаётся, и абсолютный счёт по нему ничего не говорит. Партию выдаёт доля.
    return sorted(out, key=lambda x: (-(x["n"] / max(x["base"], 1)), -x["n"]))


# ─────────────────────────────── рендер ───────────────────────────────
def _stars(v):
    return "—" if v is None else ("★" * int(v))


def render_content(data, days, day):
    L = [f"# Дайджест контентщику — {day}",
         "",
         f"Окно: последние **{days} дн.** Источник — `raw_feedback`, без ИИ: счёт по темам вопросов.",
         "Правило чтения: каждая строка — это вопрос, на который карточка обязана была ответить",
         "сама. Пока она молчит, за неё отвечает оператор, и так по кругу.",
         ""]
    L += ["## 1. Карточка не отвечает на вопрос (≥2 вопроса одной темы)", ""]
    if not data["gaps"]:
        L += ["_За окно таких артикулов нет._", ""]
    for g in data["gaps"]:
        L += [f"### {g['article']} — {g['topic']} ×{g['n']}",
              f"*{g['product']}*" if g["product"] else "",
              f"**Что добавить:** {g['advice']}", ""]
        L += [f"- {t}" for t in g["texts"]] + [""]
    L += ["## 2. Отзывы «на фото другое / не как в описании»", ""]
    if not data["mismatch"]:
        L += ["_Нет._", ""]
    for m in data["mismatch"]:
        L += [f"- **{m['article']}** ({m['platform']}, {_stars(m['rating'])}) "
              f"{m['product']}: {m['text']}"]
    L += [""]
    L += ["## 3. Модели принтера — кандидаты в заголовок", "",
          "Спрошено покупателем, отвечено «да», ответ утверждён человеком, а в списке моделей",
          "карточки модели нет.", ""]
    if data["titles_note"]:
        L += [f"_{data['titles_note']}_", ""]
    if not data["titles"]:
        L += ["_Нет._", ""]
    for t in data["titles"]:
        L += [f"- **{t['article']}** {t['product']}: добавить `{', '.join(t['models'])}`"
              + ("" if t["has_card_models"] else " _(у карточки вообще нет списка моделей)_"),
              f"  - вопрос: {t['question']}"]
    return "\n".join(x for x in L if x is not None) + "\n"


def render_purchase(data, days, day):
    L = [f"# Дайджест закупщику — {day}",
         "",
         f"Окно: последние **{days} дн.** Источник — `raw_feedback`, без ИИ: счёт по симптомам.",
         "В список попадают только претензии (то же определение, что у движка модерации).",
         f"Претензий «после заправки» отсеяно: **{data['skipped_refill']}** — это не наш дефект.",
         ""]
    if data["colors"]:
        L += [f"## ⚠️ Срез по цвету (за {COLOR_DAYS} дн., разные артикулы)", ""]
        for c in data["colors"]:
            share = 100.0 * c["n"] / max(c["base"], 1)
            L += [f"### {c['color']} — {c['n']} претензий на {len(c['articles'])} артикулах",
                  f"Доля: {c['n']} из {c['base']} отзывов по этому цвету (**{share:.0f} %**) — "
                  f"сравнивать надо именно доли: чёрного продаётся больше всех, и по числу "
                  f"претензий он будет первым всегда.",
                  f"Артикулы: {', '.join(c['articles'])}", ""]
            L += [f"- {t['article']} ({_stars(t['rating'])}): {t['text']}" for t in c["texts"]]
            L += [""]
    L += [f"## Артикулы с повторяющимся симптомом (≥{MIN_HITS} за окно)", ""]
    if not data["symptoms"]:
        L += ["_За окно таких артикулов нет._", ""]
    for s in data["symptoms"]:
        stars = ", ".join(_stars(v) for v in s["stars"]) or "— (пришло вопросами)"
        L += [f"### {s['article']} — {s['symptom']} ×{s['n']}",
              f"*{s['product']}*" if s["product"] else "",
              f"Оценки: {stars}", ""]
        L += [f"- ({t['platform']}, "
              + ("вопрос" if t["kind"] == "question" else _stars(t["rating"]))
              + f") {t['text']}" for t in s["texts"]]
        L += [""]
    return "\n".join(x for x in L if x is not None) + "\n"


def build(days=7, day=None):
    day = day or dt.date.today().isoformat()
    titles, note = title_candidates(days)
    symptoms, skipped = symptom_claims(days)
    data = {"gaps": content_gaps(days), "mismatch": card_mismatch(days),
            "titles": titles, "titles_note": note,
            "symptoms": symptoms, "skipped_refill": skipped, "colors": color_alert()}
    data["content_md"] = render_content(data, days, day)
    data["purchase_md"] = render_purchase(data, days, day)
    data["day"] = day
    data["days"] = days
    return data


def write_files(data):
    d = OUT_ROOT / f"digest_{data['day']}"
    d.mkdir(parents=True, exist_ok=True)
    c, p = d / "content.md", d / "purchasing.md"
    c.write_text(data["content_md"], encoding="utf-8")
    p.write_text(data["purchase_md"], encoding="utf-8")
    return c, p


def tg_text(data, files):
    c, p = files
    n_cards = len({g["article"] for g in data["gaps"]} | {m["article"] for m in data["mismatch"]}
                  | {t["article"] for t in data["titles"]})
    n_arts = len({s["article"] for s in data["symptoms"]})
    color = ("\n⚠️ Цвет: " + ", ".join(
                f"{x['color']} {x['n']}/{x['base']}" for x in data["colors"])
             if data["colors"] else "")
    return (f"🗂 <b>Недельный дайджест по обращениям</b> ({data['day']}, окно {data['days']} дн.)\n\n"
            f"Контентщику: <b>{n_cards}</b> карточек\n"
            f"Закупщику: <b>{n_arts}</b> артикулов{color}\n\n"
            f"<code>{c}</code>\n<code>{p}</code>")


def send(data, files):
    """Сообщение + оба файла вложением. Ссылкой в привычном смысле файлы на сервере не являются:
    дашборд закрыт basic-auth и живёт в чужом потоке, поэтому даём путь и сам файл."""
    from feedback_bot import tg_moderation as tm
    text = tg_text(data, files)
    sent = 0
    for cid in tm.NOTIFY_IDS:
        if tm.send(cid, text):
            sent += 1
        for f in files:
            _send_doc(tm, cid, f)
    return sent


def _send_doc(tm, chat_id, path):
    """sendDocument multipart — у tm.api() только JSON, а файл им не отправить."""
    import urllib.request
    boundary = "----mpdigest7a1f"
    body = b""
    for k, v in (("chat_id", str(chat_id)),):
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n"
                 ).encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; "
             f"filename=\"{path.parent.name}_{path.name}\"\r\n"
             f"Content-Type: text/markdown\r\n\r\n").encode()
    body += path.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{tm.API}/sendDocument", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status == 200
    except Exception as e:
        tm.log(f"дайджест: файл {path.name} не ушёл: {e}")
        return False


def main():
    ap = argparse.ArgumentParser(description="Недельный дайджест по обращениям (rev)")
    ap.add_argument("--days", type=int, default=int(os.getenv("FEEDBACK_DIGEST_DAYS", "7")))
    ap.add_argument("--send", action="store_true", help="отправить в Telegram")
    ap.add_argument("--day", help="дата в имени папки (по умолчанию сегодня)")
    a = ap.parse_args()
    data = build(days=a.days, day=a.day)
    files = write_files(data)
    print(tg_text(data, files).replace("<b>", "").replace("</b>", "")
          .replace("<code>", "").replace("</code>", ""), flush=True)
    if a.send:
        print(f"отправлено адресатам: {send(data, files)}", flush=True)


if __name__ == "__main__":
    main()
