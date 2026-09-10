# поток: rev
"""reports/brand_notes.py — знания по брендам и сериям: НАШИ входные данные для сборки ответа.

ЗАЧЕМ. Факты вида «чип 067H одноразовый, после заправки принтер считает картридж пустым» или
«гарантия на повторную заправку не распространяется» в карточке товара не лежат и лежать не будут.
Раньше движок добирал их веб-поиском — с чужого сайта, каждый раз чуть иначе и иногда неверно, — а
контролёр (llm_routing.verify) справедливо резал их как утверждения вне входных данных. Теперь это
таблица `brand_notes` (миграция 707), которую наполняет человек: то, что здесь написано, движок
считает фактом наравне с карточкой, а контролёр видит те же строки во ВХОДНЫХ ДАННЫХ.

ПРИОРИТЕТ ИСТОЧНИКОВ (правило Сергея 10.09.2026):
    CARD_DATA  >  brand_notes  >  approved_answers  >  веб-факты
Карточка конкретного лота всегда сильнее общего знания о серии: в карточке — что мы реально продаём.

ПОДБОР. Запись цепляется к обращению, если совпал бренд И (серия пустая ИЛИ хотя бы один код из
`series_pattern` встретился в CARD_DATA или в самом вопросе). Общие правила (`brand='*'`) идут
всегда: это позиция магазина, она не зависит от бренда.

ВЕБ. `covers()` отвечает на вопрос «есть ли у нас своё знание по этому бренду и этой теме» — если
есть, веб-поиск не зовём вовсе (пункт 3 задачи): платить DeepSeek за то, что уже написано у нас,
незачем, и чужой текст тут только добавляет расхождений.

    ./venv/bin/python -m reports.brand_notes --load dropbox/…_brand_notes.md   # загрузка файла
    ./venv/bin/python -m reports.brand_notes --status
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import db                                                        # noqa: E402

ANY_BRAND = "*"          # общие правила: подставляются к любому обращению
_LIMIT_BRAND = 6         # сколько записей бренда максимум уходит в промпт (длина контекста — деньги)

# Темы, по которым мы считаем себя источником: если по бренду есть запись такой темы — в веб не идём.
_TOPIC_RX = {
    "чип": re.compile(r"\bчип\w*|\bchip\b", re.I),
    "заправка": re.compile(r"заправ|перезаправ|дозаправ", re.I),
    "прошивка": re.compile(r"прошивк|firmware|dynamic\s+security", re.I),
    "серии": re.compile(r"подойд|совмест|вместо|аналог|замен[аы]|отлича|разниц", re.I),
    "ресурс": re.compile(r"ресурс|сколько\s+(?:страниц|листов)|хватит", re.I),
    "тип чернил": re.compile(r"чернил|пигмент|водораствор", re.I),
    "гарантия": re.compile(r"гаранти", re.I),
    "оригинальность": re.compile(r"оригинал", re.I),
}

# Код серии внутри series_pattern: «953XL», «W1360A», «M454», «TK-1150», «067H». Слова без цифр
# («OfficeJet», «Pro») кодами не считаем — они матчатся почти на всё и тянут чужую серию в промпт.
_CODE_RX = re.compile(r"[A-Za-zА-Яа-я]*\d[\w\-]*", re.U)
_MIN_CODE = 2


def _norm_brand(s):
    """«Konica Minolta» → konica, «Общие правила (все бренды)» → '*'."""
    s = (s or "").strip().lower()
    if not s or s.startswith("общ"):
        return ANY_BRAND
    return re.split(r"[\s/,(]+", s)[0]


def _codes(pattern):
    out = []
    for m in _CODE_RX.finditer(pattern or ""):
        c = m.group(0).strip("-.,;()").lower()
        if len(c) >= _MIN_CODE and any(ch.isdigit() for ch in c):
            out.append(c)
    return out


def topics_of(text):
    """Темы обращения — по ним решаем, есть ли у нас своё знание (и нужен ли веб)."""
    t = text or ""
    return {name for name, rx in _TOPIC_RX.items() if rx.search(t)}


# --------------------------------------------------------------------------- чтение

def all_notes(brand=None):
    if brand:
        return db.query("""SELECT brand, series_pattern, topic, text FROM brand_notes
                            WHERE brand IN (%s, %s) ORDER BY brand, topic""", (brand, ANY_BRAND))
    return db.query("SELECT brand, series_pattern, topic, text FROM brand_notes ORDER BY brand, topic")


def lookup(brand, question, card_text="", limit=_LIMIT_BRAND):
    """Записи, относящиеся к обращению → список dict. Общие правила всегда первыми.

    Серия считается совпавшей, если код из `series_pattern` встретился в CARD_DATA или в вопросе.
    Запись без серии относится ко всему бренду и берётся, если совпала тема обращения (иначе в
    промпт уехал бы весь справочник бренда).
    """
    rows = all_notes(brand)
    if not rows:
        return []
    hay = ((card_text or "") + " " + (question or "")).lower()
    tops = topics_of(question)
    common, brand_hits = [], []
    for r in rows:
        if r["brand"] == ANY_BRAND:
            common.append(r)
            continue
        codes = _codes(r["series_pattern"])
        by_series = any(c in hay for c in codes)
        by_topic = (not codes) and (r["topic"] in tops)
        if by_series or by_topic:
            brand_hits.append(dict(r, _hit="серия" if by_series else "тема"))
    return common + brand_hits[:limit]


def covers(brand, question):
    """Есть ли НАШЕ знание по бренду и теме обращения → веб-поиск не нужен (пункт 3).

    Общие правила здесь не считаются: они про позицию магазина, а не про технику конкретной серии.
    """
    if not brand:
        return False
    tops = topics_of(question)
    if not tops:
        return False
    rows = db.query("SELECT topic FROM brand_notes WHERE brand=%s", (brand,))
    return any(r["topic"] in tops for r in rows)


def block(notes):
    """Готовый блок промпта. Пусто → пустая строка (блока в промпте тогда нет вовсе)."""
    if not notes:
        return ""
    lines = []
    for n in notes:
        title = n["topic"] if n["brand"] == ANY_BRAND else f"{n['brand']} · {n['topic']}"
        ser = (n["series_pattern"] or "").strip()
        if ser and ser not in ("—", "-"):
            title += f" · {ser[:120]}"
        lines.append(f"— [{title}] {n['text'].strip()}")
    return ("ЗНАНИЯ ПО БРЕНДУ (наш справочник, источник ТАКОЙ ЖЕ достоверности, как карточка: это "
            "проверенные нами факты, а не догадки — опирайся на них и не подвергай сомнению. Если "
            "они противоречат CARD_DATA, прав CARD_DATA — карточка описывает именно наш лот):\n"
            + "\n".join(lines) + "\n\n")


def facts_for(brand, question, card_text=""):
    """(текст блока, число записей) — то, что уходит и в промпт сборки, и во входные контролёра."""
    notes = lookup(brand, question, card_text)
    return block(notes), len(notes)


# --------------------------------------------------------------------------- запись

def upsert(brand, series_pattern, topic, text, source="manual"):
    db.execute("""INSERT INTO brand_notes (brand, series_pattern, topic, text, source)
                  VALUES (%s,%s,%s,%s,%s)
                  ON CONFLICT (brand, topic, series_pattern)
                  DO UPDATE SET text=EXCLUDED.text, source=EXCLUDED.source, updated_at=now()""",
               (_norm_brand(brand) if brand != ANY_BRAND else ANY_BRAND,
                (series_pattern or "").strip(), (topic or "").strip().lower(), (text or "").strip(),
                source))


def load_file(path):
    """Загрузка выгрузки «бренд | серия | тема | текст» (разделы `## Бренд`). → (загружено, пропущено).

    Разделитель раздела определяет бренд по умолчанию, но бренд из самой строки главнее: в файле он
    и так стоит первым полем. «общее» и раздел «Общие правила» → brand='*'.
    """
    ok, skip = 0, 0
    cur = None
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if line.startswith("##"):
            cur = _norm_brand(line.lstrip("# ").strip())
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 4:
            skip += 1
            continue
        brand, series, topic = parts[0], parts[1], parts[2]
        text = "|".join(parts[3:]).strip()          # текст мог содержать «|» — склеиваем обратно
        b = _norm_brand(brand) if brand.lower() not in ("", "—", "-") else (cur or ANY_BRAND)
        if series in ("—", "-"):
            series = ""
        if not text:
            skip += 1
            continue
        upsert(b, series, topic, text)
        ok += 1
    return ok, skip


def status():
    return db.query("""SELECT brand, count(*) n, max(updated_at) upd FROM brand_notes
                        GROUP BY brand ORDER BY n DESC, brand""")


if __name__ == "__main__":
    a = sys.argv[1:]
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))
    if "--load" in a:
        n, s = load_file(a[a.index("--load") + 1])
        print(f"brand_notes: загружено/обновлено {n}, пропущено строк {s}")
    for r in status():
        print(f"  {r['brand']:<12}{r['n']:>4}  {r['upd']:%Y-%m-%d %H:%M}")
