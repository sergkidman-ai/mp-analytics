# поток: rev
"""Справочник совместимости «принтер → серия картриджа» (блок D брифа 08.09.2026).

Зачем. До блока D положительный ответ о совместимости мог опираться на веб или на знание модели.
Ровно так родились четыре разобранных провала: Epson C5890 (веб принял ПЗК-корпус T945x
за картридж), Canon LBP646 (выдуманная серия 075H в допродаже), HP 4303 («спецификация HP 212A»),
Epson 680 (T026 вместо T017). Веб больше не источник «да» (D1) — источников ровно два: список
совместимости КАРТОЧКИ и эта таблица.

Кто пишет (D2): утверждённый человеком ответ класса «совместимость» (`source=approved_answer`),
команда `/compat_add` (`manual`), импорт OEM-списков (`oem`). Веб-парсинг НЕ пишет сюда никогда —
иначе правило D1 обходится через собственную же таблицу за один цикл.

Модуль обязан работать и без БД (юнит-тесты гейта офлайн): любая ошибка запроса = «справочник
молчит», а молчание справочника — это запрет публикации, а не разрешение.
"""
import re

VARIANTS = ("std", "high", "xl")
REGIONS = ("EU", "ASIA", "US")
SOURCES = ("oem", "approved_answer", "manual")

# Регион в вопросе покупателя. DWF — европейское обозначение корпуса Epson WF-C5x90, покупатели
# пишут именно его; «азиатская версия» и «китайская» — один и тот же рынок для нашей номенклатуры.
REGION_RX = [
    ("EU", r"европ|\beu\b|\bdwf\b|евро[- ]?верс"),
    ("ASIA", r"азиатск|\basia\b|китайск|\bcn\b"),
    ("US", r"америк|\bus\b|\busa\b"),
]


def norm(s):
    """Нормализация модели: только буквы и цифры в нижнем регистре. «LBP-633 Cdw» → «lbp633cdw»."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def region_in(text):
    """Регион, названный в тексте вопроса, либо None."""
    t = (text or "").lower()
    for name, rx in REGION_RX:
        if re.search(rx, t):
            return name
    return None


def _rows(sql, args):
    try:
        from core import db
        return db.query(sql, args)
    except Exception:
        return []                       # нет БД — справочник молчит, а молчание = запрет (D1)


def for_models(models):
    """→ {нормализованная модель: [строки справочника]} для перечисленных моделей принтера."""
    keys = sorted({norm(m) for m in (models or []) if norm(m)})
    if not keys:
        return {}
    out = {}
    for r in _rows("""SELECT printer_model, printer_raw, cartridge_series, oem_sku, yield_variant,
                             region, source, approved_by FROM compat_ref
                      WHERE printer_model = ANY(%s)""", (keys,)):
        out.setdefault(r["printer_model"], []).append(r)
    return out


def confirms(models, series=None):
    """→ (подтверждённые модели, источник, строки). Подтверждение — это строка справочника
    на пару «спрошенная модель + наша серия». Без серии сверяем только по модели: карточка
    товара всё равно одна, а вопрос задан в ней."""
    found = for_models(models)
    if not found:
        return [], None, []
    s = norm(series) if series else None
    ok, rows = [], []
    for m in models or []:
        hit = [r for r in found.get(norm(m), [])
               if not s or norm(r["cartridge_series"]) == s or norm(r.get("oem_sku")) == s]
        if hit:
            ok.append(m)
            rows.extend(hit)
    src = sorted({r["source"] for r in rows})
    return ok, ("compat_ref:" + "+".join(src) if src else None), rows


def variant_conflict(models, series):
    """D4. Модель есть в справочнике для ДРУГОЙ ресурсной версии той же серии, но не для нашей.
    → {'model', 'ours', 'theirs'} либо None. «Ours» — вариант нашей карточки, «theirs» — те,
    под которые этот принтер в справочнике заведён."""
    s = norm(series) if series else None
    if not s:
        return None
    found = for_models(models)
    for m in models or []:
        rows = found.get(norm(m), [])
        same = [r for r in rows if norm(r["cartridge_series"]) == s]
        if same:
            return None                                  # наша серия подтверждена — конфликта нет
        fam = [r for r in rows if r["yield_variant"] and _same_family(r["cartridge_series"], s)]
        if fam:
            return {"model": m, "ours": _variant_of(series, fam),
                    "theirs": sorted({r["yield_variant"] for r in fam})}
    return None


def _same_family(a, b):
    """Одна серия с точностью до ресурсной версии: общий буквенно-цифровой корень ≥4 символов."""
    a, b = norm(a), norm(b)
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n >= 4


def _variant_of(series, rows):
    """Ресурсная версия НАШЕЙ серии по строкам справочника той же семьи (для текста ответа)."""
    s = norm(series)
    for r in rows:
        if norm(r["cartridge_series"]) == s:
            return r["yield_variant"]
    return None


def regions_for(series):
    """D5. Регионы, под которые серия заведена в справочнике. Больше одного = ответ обязан
    спросить регион либо назвать обе версии."""
    s = norm(series) if series else None
    if not s:
        return []
    return sorted({r["region"] for r in _rows(
        "SELECT DISTINCT region FROM compat_ref WHERE cartridge_series = %s AND region IS NOT NULL",
        (series,))} | {r["region"] for r in _rows(
        "SELECT DISTINCT region FROM compat_ref WHERE oem_sku = %s AND region IS NOT NULL",
        (series,))})


def series_of(facts):
    """Наша серия по фактам карточки: код расходника, иначе первый код-подобный токен названия."""
    if not facts:
        return None
    code = (facts.get("code") or "").strip()
    if code:
        return code
    m = re.search(r"\b[A-Z]{1,4}[- ]?\d{2,5}[A-Z]{0,3}\b", facts.get("name") or "")
    return m.group(0) if m else None


def add(printer_model, cartridge_series, oem_sku=None, yield_variant=None, region=None,
        source="manual", approved_by=None, note=None):
    """Записать строку. → (записали?, пояснение). Повтор той же четвёрки ключа — не ошибка:
    обновляем источник только «вверх» (approved_answer не затирает oem)."""
    from core import db
    pm, cs = norm(printer_model), (cartridge_series or "").strip()
    if not pm or not cs:
        return False, "нужны и модель принтера, и серия картриджа"
    if source not in SOURCES:
        return False, f"источник «{source}» не из списка {SOURCES}"
    if yield_variant and yield_variant not in VARIANTS:
        return False, f"ресурсная версия «{yield_variant}» не из списка {VARIANTS}"
    if region and region not in REGIONS:
        return False, f"регион «{region}» не из списка {REGIONS}"
    db.execute("""INSERT INTO compat_ref (printer_model, printer_raw, cartridge_series, oem_sku,
                                          yield_variant, region, source, approved_by, note)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                  ON CONFLICT (printer_model, cartridge_series, coalesce(yield_variant,''),
                               coalesce(region,'')) DO UPDATE
                    SET oem_sku = coalesce(EXCLUDED.oem_sku, compat_ref.oem_sku),
                        note = coalesce(EXCLUDED.note, compat_ref.note)""",
               (pm, (printer_model or "").strip(), cs, oem_sku, yield_variant, region, source,
                approved_by, note))
    return True, f"{printer_model} → {cs}" + (f" ({yield_variant})" if yield_variant else "")


def size():
    r = _rows("SELECT count(*) n, count(DISTINCT printer_model) m FROM compat_ref", ())
    return (r[0]["n"], r[0]["m"]) if r else (0, 0)
