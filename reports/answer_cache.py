# поток: rev
"""reports/answer_cache.py — кэш утверждённых человеком ответов по нашему артикулу (блок G).

ЗАЧЕМ. 01.09.2026 один и тот же вопрос «каким тонером их заправлять?» пришёл по артикулу 5422
на oz_acc1, oz_acc2 и wb_acc2 — и получил три разных ответа: каждый черновик собирался заново,
на своей выборке few-shot и своём вызове модели. Кэш хранит ОДИН утверждённый оператором текст
на ключ (наш артикул, класс обращения, ключ вопроса) и подставляет его вместо новой генерации.

ЧТО СЮДА ПОПАДАЕТ (запись — только из модерации, см. feedback_bot/tg_moderation.py):
  ✅ без правки            → approved_by='send'
  ✏️ Править → отправлено  → approved_by='edit', перезаписывает прежний текст ключа
  backfill отправленного   → approved_by='backfill'
НЕ ПОПАДАЕТ: шаблонные авто-ответы на отзывы (человек их не утверждал), запрещённое гейтом,
классы «претензия» и «прочее», а также всё, для чего не построен ключ вопроса.

ЧТЕНИЕ (reports/feedback_today.py) — до генерации, сразу после классификации: есть живая запись →
подставляем её текст, LLM не зовём. Первые две подстановки ключа идут оператору с пометкой
«из кэша, подтвердите»; дальше запись считается проверенной. Стоп-лист A1.2 и остальной
publish_gate прогоняются по подставленному тексту ВСЕГДА — кэш не отменяет ни одной проверки.

ИНВАЛИДАЦИЯ. Ручная — /cache_drop <артикул> [класс] в боте модерации. Автоматическая — по
отпечатку карточки (card_sig): изменилась совместимость или атрибут, из которого собран ключ, →
запись помечается stale, черновик уходит оператору с запретом публикации и пометкой «кэш устарел».
Карточка НЕ прочиталась (нет данных) — это не повод считать кэш устаревшим: сравнение пропускаем.
"""
import hashlib
import re

from core import db

# Классы обращения, для которых ключ вообще строится. «претензия» и «прочее» исключены брифом:
# первая всегда ручная, вторая — мусорная корзина классификатора, одинаковых вопросов в ней нет.
KEYABLE = ("совместимость", "характеристики", "ресурс", "заправка", "инструкция",
           "производитель", "габарит", "комплектация")

# Классы, у которых ответ по артикулу один на все формулировки: ключ — сама константа класса.
# «Кто производитель?» и «какой бренд?» — один и тот же ответ по карточке, разбивать по атрибутам
# нечего; то же у комплектации, заправки и инструкции. Габарит из списка исключён
# (ревью Codex 08.09.2026): «какая длина» и «какой диаметр» — разные ответы из карточки,
# один ключ на них склеил бы их в одну запись кэша.
CONST_KEY = ("заправка", "инструкция", "производитель", "комплектация")

# Имя атрибута для классов «характеристики» и «ресурс». Порядок значим: «оригинальный ли чип»
# — вопрос про чип, а не про оригинальность.
ATTR_RX = [
    ("гарантия", r"гаранти"),
    ("чип", r"чип\b|чипом|чипа\b|chip"),
    ("ресурс", r"ресурс|хват|стран|объ[её]м|сколько\s+(?:печат|мл|грамм)"),
    ("тип чернил", r"чернил|тонер|пигмент|водн\w*\s+основ|краск"),
    ("оригинальность", r"оригинал|подлинн|неоригинал|аналог\b|драм\b|\bref\b|восстановл"),
    ("комплектация", r"комплект|в\s+набор|сколько\s+(?:штук|картридж)|что\s+в\s+коробк"),
]

# Габарит: ключ — спрошенное измерение, а не класс. «Размер» общий и стоит последним.
DIM_RX = [
    ("длина", r"длин[аоуы]"),
    ("ширина", r"ширин"),
    ("высота", r"высот"),
    ("диаметр", r"диаметр"),
    ("размер", r"габарит|разм[ев]р|сколько\s+см|помест"),
]

# Поля карточки, из которых собран ответ на ключ этого типа: их изменение делает запись stale.
SIG_FIELDS = {
    "чип": ("chip",),
    "ресурс": ("resource",),
    "тип чернил": ("ptype", "kind"),
    "оригинальность": ("kind",),
    "комплектация": ("set_info", "name"),
    "производитель": ("name", "kind"),
    "гарантия": ("kind", "name"),
    "габарит": ("weight_pkg", "name"),
    "длина": ("weight_pkg", "name"),
    "ширина": ("weight_pkg", "name"),
    "высота": ("weight_pkg", "name"),
    "диаметр": ("weight_pkg", "name"),
    "размер": ("weight_pkg", "name"),
    "заправка": ("refillable", "kind"),
    "инструкция": ("kind", "code"),
}

CONFIRM_HITS = 2          # первые N подстановок ключа идут с пометкой «из кэша, подтвердите»
_PLAT_SUFFIX_RX = re.compile(r"^(\d{3,6})[A-Za-z0-9]{6,10}$")


def _norm(s):
    return re.sub(r"[\s\-_/]", "", str(s).lower())


def internal_article(platform, article, item_id, strict=False):
    """Наш внутренний артикул. WB vendorCode и offerId Яндекса — это он и есть, иногда с
    площадочным случайным хвостом ('5422YHSC5BIR' → '5422'); у Ozon в raw_feedback артикула нет
    вовсе, берём offer_id по sku. Срез хвоста неоднозначен по длине — кандидаты сверяем с
    ms_product и выбираем самый длинный известный; не нашли — отдаём как есть.

    Канон живёт здесь: по этому же артикулу ключуется кэш, и расхождение с карточкой оператора
    (feedback_bot/tg_moderation.py) означало бы, что человек утверждает ответ одному артикулу,
    а кэш кладёт его другому.

    strict=True (замечание №3 ревью 08.09.2026) — вернуть артикул ТОЛЬКО если он подтверждён
    `ms_product`. Срез площадочного хвоста — эвристика по длине, и для показа карточки оператору
    догадка допустима, а для ключа кэша нет: два разных неизвестных кода с общим префиксом
    склеились бы в одну запись и отдали покупателю чужой ответ."""
    raw = str(article or "").strip()
    if not raw and platform == "ozon" and item_id:
        r = db.query("SELECT offer_id FROM ozon_product WHERE sku::text=%s LIMIT 1", (str(item_id),))
        raw = str(r[0]["offer_id"]).strip() if r else ""
    if not raw:
        return None
    cands = [raw]
    m = _PLAT_SUFFIX_RX.match(raw)
    if m:
        d = m.group(1)
        cands += [d[:k] for k in range(len(d), 2, -1)]
    try:
        known = {x["external_code"] for x in
                 db.query("SELECT DISTINCT external_code FROM ms_product WHERE external_code = ANY(%s)",
                          (cands,))}
    except Exception:
        known = set()
    for c in cands:
        if c in known:
            return c
    if strict:
        return None
    return cands[1] if len(cands) > 1 else raw


def question_key(text, cls):
    """→ (ключ, пояснение). Ключ None — вопрос не кэшируется, пояснение идёт в трейс черновика.

    Совместимость ключуется НАБОРОМ спрошенных моделей через текущий матчер: он грубый (работа №8
    брифа — токенайзер), но ошибается в сторону «не нашёл» — тогда ключ просто не строится и кэш
    молчит. Характеристика — именем атрибута: «есть ли чип» и «с чипом или без» это один ключ."""
    t = (text or "").strip()
    if cls not in KEYABLE:
        return None, f"класс «{cls}» не кэшируется"
    if not t:
        return None, "пустой текст вопроса"
    if cls in CONST_KEY:
        return cls, f"константа класса «{cls}»"
    if cls == "совместимость":
        from reports.feedback_draft_run import _asked_models
        models = sorted({_norm(m) for m in _asked_models(t)})
        if not models:
            return None, "совместимость без распознанной модели"
        return "модель:" + "+".join(models), f"модели: {', '.join(models)}"
    if cls == "габарит":
        for name, rx in DIM_RX:
            if re.search(rx, t, re.I):
                return "габарит:" + name, f"габарит: {name}"
        return None, "габарит без распознанного измерения"
    for name, rx in ATTR_RX:                       # характеристики / ресурс
        if re.search(rx, t, re.I):
            return "атрибут:" + name, f"атрибут «{name}»"
    return None, f"класс «{cls}» без распознанного атрибута"


def card_signature(facts, question_key_str):
    """Отпечаток тех полей карточки, из которых собран ответ на этот ключ. None — карточки нет,
    сравнивать не с чем (молчание карточки не делает кэш устаревшим)."""
    if not facts:
        return None
    if str(question_key_str or "").startswith("модель:"):
        parts = ["models=" + "|".join(sorted(_norm(m) for m in (facts.get("models") or [])))]
    else:
        attr = str(question_key_str or "").split(":", 1)[-1]
        fields = SIG_FIELDS.get(attr) or SIG_FIELDS.get(str(question_key_str), ())
        if not fields:
            return None
        parts = [f"{n}={facts.get(n)!r}" for n in fields]
    return hashlib.sha1("§".join(parts).encode("utf-8")).hexdigest()[:16]


def _key3(article, cls, key):
    """Тройка ключа в каноническом виде. Хвостовые пробелы в артикуле (правил человек в
    /cache_drop) не должны заводить вторую запись того же товара — замечание №6 ревью."""
    return (str(article or "").strip(), str(cls or "").strip(), str(key or "").strip())


def lookup(article, cls, key):
    article, cls, key = _key3(article, cls, key)
    if not (article and cls and key):
        return None
    r = db.query("""SELECT * FROM approved_answers
                    WHERE article=%s AND request_class=%s AND question_key=%s""", (article, cls, key))
    return r[0] if r else None


def remember(*, article, cls, key, text, approved_by, platform=None, account=None, ext_id=None,
             card_sig=None):
    """Upsert утверждённого ответа. → (записали?, пояснение).

    Правка перезаписывает текст и сбрасывает hit_count: доверие «подтверждено дважды» относилось
    к прежнему тексту, новый должен пройти те же две подстановки с пометкой заново."""
    text = (text or "").strip()
    article, cls, key = _key3(article, cls, key)
    if not (article and cls and key and text):
        return False, "нет артикула, класса, ключа или текста"
    if cls not in KEYABLE:
        return False, f"класс «{cls}» не кэшируется"
    db.execute("""INSERT INTO approved_answers
            (article, request_class, question_key, answer_text, approved_by, approved_at,
             source_platform, source_account, source_ext_id, card_sig)
        VALUES (%s,%s,%s,%s,%s,now(),%s,%s,%s,%s)
        ON CONFLICT ON CONSTRAINT approved_answers_key DO UPDATE SET
            answer_text = EXCLUDED.answer_text,
            approved_by = EXCLUDED.approved_by,
            approved_at = EXCLUDED.approved_at,
            source_platform = EXCLUDED.source_platform,
            source_account = EXCLUDED.source_account,
            source_ext_id = EXCLUDED.source_ext_id,
            card_sig = COALESCE(EXCLUDED.card_sig, approved_answers.card_sig),
            stale = false, stale_reason = NULL, stale_at = NULL,
            hit_count = CASE WHEN approved_answers.answer_text = EXCLUDED.answer_text
                             THEN approved_answers.hit_count ELSE 0 END""",
        (article, cls, key, text, approved_by, platform, account, ext_id, card_sig))
    return True, f"{article}/{cls}/{key}"


def facts_for(row):
    """Карточка товара под площадку строки. Ошибку глотаем: без карточки кэш работает (у oz_acc2
    атрибутов нет вовсе — ключ по артикулу от них и не зависит), только отпечаток будет пустым."""
    try:
        from reports.card_facts import CardFacts
        cf = CardFacts()
        p = row.get("platform")
        return (cf.for_ozon(row["item_id"]) if p == "ozon" else
                cf.for_yandex(row["item_id"]) if p == "yandex" else cf.for_wb(row["item_id"]))
    except Exception:
        return None


def remember_sent(row, text, approved_by, facts=None, check_gate=True):
    """Запомнить ответ, который человек утвердил (✅ или правка) либо который реально ушёл
    покупателю (backfill). → (записали?, пояснение — оно идёт в лог и в отчёт backfill).

    Гейт прогоняем по ФИНАЛЬНОМУ тексту: запрещённое к публикации в кэш не кладём — иначе один
    неудачный ответ размножился бы по всем площадкам этого артикула."""
    if row.get("kind") != "question":
        return False, "кэшируем только вопросы"
    from reports import request_class as rc
    g = row.get("draft_grounding") if isinstance(row.get("draft_grounding"), dict) else {}
    cls = (row.get("request_class") or g.get("request_class")
           or rc.classify(row.get("body"), kind="question", rating=row.get("rating")))
    key, why = question_key(row.get("body"), cls)
    if not key:
        return False, why
    art = internal_article(row.get("platform"), row.get("article"), row.get("item_id"), strict=True)
    if not art:
        return False, "артикул не подтверждён МойСкладом"
    if check_gate:
        from reports import publish_gate
        allow, reasons = publish_gate.verdict(dict(row), text)
        if not allow:
            return False, "гейт запретил: " + "; ".join(reasons)[:120]
    if facts is None:
        facts = facts_for(row)
    return remember(article=art, cls=cls, key=key, text=text, approved_by=approved_by,
                    platform=row.get("platform"), account=row.get("account"),
                    ext_id=row.get("ext_id"), card_sig=card_signature(facts, key))


def bump(row_id):
    db.execute("UPDATE approved_answers SET hit_count=hit_count+1, last_hit_at=now() WHERE id=%s",
               (row_id,))


def set_sig(row_id, sig):
    """Проставить отпечаток записи, заведённой при непрочитанной карточке (замечание №2 ревью):
    без него авто-инвалидация для такой записи не включится никогда. Пишем только в пустое —
    подменять отпечаток, по которому идёт сравнение, нельзя."""
    db.execute("UPDATE approved_answers SET card_sig=%s WHERE id=%s AND card_sig IS NULL",
               (sig, row_id))


def mark_stale(row_id, reason):
    db.execute("""UPDATE approved_answers SET stale=true, stale_reason=%s, stale_at=now()
                  WHERE id=%s AND NOT stale""", (reason[:200], row_id))


def drop(article, cls=None):
    """Ручная инвалидация (/cache_drop). → сколько записей удалено. Артикул и класс оператор
    печатает руками, поэтому сравниваем без регистра и краевых пробелов."""
    article = str(article or "").strip()
    if not article:
        return 0
    if cls:
        rows = db.query("""DELETE FROM approved_answers
                           WHERE lower(article)=lower(%s) AND lower(request_class)=lower(%s)
                           RETURNING id""", (article, str(cls).strip()))
    else:
        rows = db.query("DELETE FROM approved_answers WHERE lower(article)=lower(%s) RETURNING id",
                        (article,))
    return len(rows or [])


def try_hit(row, facts=None, cls=None):
    """Подстановка ответа из кэша ДО генерации. → (текст, ground) либо None.

    ground пишется в raw_feedback.draft_grounding и определяет поведение гейта:
      cache.stale=True    → publish_gate запрещает публикацию («кэш устарел»);
      cache.confirm=True  → первые две подстановки, оператор подтверждает текст глазами."""
    if row.get("kind") != "question":
        return None
    from reports import request_class as rc
    cls = cls or rc.classify(row.get("body"), kind="question", rating=row.get("rating"))
    key, why = question_key(row.get("body"), cls)
    if not key:
        return None
    art = internal_article(row.get("platform"), row.get("article"), row.get("item_id"), strict=True)
    if not art:
        return None
    hit = lookup(art, cls, key)
    if not hit:
        return None
    # Отпечаток карточки сравниваем ТОЛЬКО в пределах площадки, на которой ответ утверждали
    # (замечание №8 ревью 08.09.2026): артикул один на все площадки, а карточки у них разные —
    # у oz_acc2 атрибутов нет вовсе. Межплощадочное сравнение давало бы вечный ложный stale
    # и запирало кэш ровно там, ради чего он заведён.
    same_platform = bool(hit["source_platform"]) and hit["source_platform"] == row.get("platform")
    sig_now = card_signature(facts, key) if same_platform else None
    stale = bool(hit["stale"])
    if not stale and sig_now and hit["card_sig"] and sig_now != hit["card_sig"]:
        mark_stale(hit["id"], f"карточка изменилась по ключу {key}")
        stale = True
    elif sig_now and not hit["card_sig"]:
        set_sig(hit["id"], sig_now)              # карточка молчала при утверждении — усыновляем
    bump(hit["id"])
    hits = int(hit["hit_count"] or 0) + 1
    day = hit["approved_at"].strftime("%d.%m.%Y") if hit["approved_at"] else "—"
    src = f"кэш: утверждено {day} на {hit['source_platform'] or '—'}"
    ground = {
        "llm": False, "grounded": True, "source": src, "template_id": "cache",
        "request_class": cls,
        "note": (f"кэш устарел: {hit['stale_reason'] or 'карточка изменилась'}" if stale else
                 ("из кэша, подтвердите" if hits <= CONFIRM_HITS else f"из кэша, подстановка №{hits}")),
        "cache": {"id": hit["id"], "article": art, "key": key, "hits": hits,
                  "stale": stale, "confirm": hits <= CONFIRM_HITS, "why_key": why},
    }
    return (hit["answer_text"] or ""), ground


def recent_for(cls, brand=None, limit=5, exclude_article=None):
    """Свежие утверждённые ответы того же класса и бренда — во входные данные модели (пункт 4).

    Точное попадание по ключу отдаёт `try_hit` и генерации не требует; сюда мы приходим, когда
    попадания нет, но человек уже утверждал ответы на такой же вопрос по соседним карточкам того же
    бренда. Это не факт о товаре, а образец РЕШЕНИЯ: как мы формулируем ответ этого класса —
    поэтому подписью «одобрено оператором» блок и уходит в промпт.

    Бренд определяется по названию товара исходного обращения (approved_answers своего имени товара
    не хранит), поэтому join к raw_feedback; без бренда — просто свежие по классу.
    """
    if not cls:
        return []
    where = ["a.stale = false", "a.request_class = %s", "coalesce(a.answer_text,'') <> ''"]
    args = [cls]
    if brand:
        where.append("lower(coalesce(f.product_name,'')) LIKE %s")
        args.append(f"%{str(brand).lower()}%")
    if exclude_article:
        where.append("lower(a.article) <> lower(%s)")
        args.append(str(exclude_article))
    args.append(int(limit))
    rows = db.query(f"""SELECT a.answer_text, a.request_class, a.article
                          FROM approved_answers a
                          LEFT JOIN raw_feedback f
                            ON f.platform = a.source_platform AND f.account = a.source_account
                           AND f.ext_id = a.source_ext_id
                         WHERE {' AND '.join(where)}
                         ORDER BY a.approved_at DESC NULLS LAST
                         LIMIT %s""", tuple(args))
    return [{"text": r["answer_text"], "cls": r["request_class"], "article": r["article"]}
            for r in (rows or [])]
