# поток: prc
# -*- coding: utf-8 -*-
"""
Разведка модели картриджа в сети: рынок РФ, популярность, доза полной заправки, принтеры.

Зачем. Разбирая новинки, человек упирается в четыре вопроса, ответов на которые нет ни в прайсе
поставщика, ни у нас в базе:
  1. модель вообще ходит на российском рынке?
  2. популярна ли настолько, чтобы заводить карточку?
  3. сколько тонера/чернил нужно на ПОЛНУЮ заправку именно этой модели?
  4. какие принтеры она закрывает — полным списком?
На разборе тонера Kyocera TKY4 (09.08) на все четыре пришлось отвечать вручную по нашим же
карточкам: TK-3430 попал в заведение «по ресурсу — наш вывод, а не слова поставщика», TK-3490
выброшен просто потому, что его нет в нашей базе, а у TK-330 в названии стоял один FS-4000DN.
Это гадание там, где нужен факт с источником.

ПЛАТНО. Ходит серверный `web_search` Anthropic — живые деньги (правило 13 CLAUDE.md). Поэтому:
  * `estimate()` — чистая арифметика, ни одного сетевого вызова к провайдеру;
  * `ask()` без `confirm=True` поднимает `NeedsConsent` со сметой и ничего не спрашивает;
  * CLI без `--apply` печатает смету и выходит.
Потратить деньги случайно технически нельзя: гейт стоит в самой функции, а не в интерфейсе.

Кэш — по МОДЕЛИ (`prc_model_research`), а не по строке прайса: одна и та же TK-3190 приходит
в разных прайсах и разных загрузках, платить за неё второй раз незачем.

Доза и принтеры БЕЗ ссылки-источника не принимаются — пишется None / пустой список. Правило
то же, что в габаритах: синтетическая цифра дороже отсутствующей.
"""
import os
import re
import sys
import json
import pathlib
import datetime as dt
from decimal import Decimal

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.db import query, execute                                               # noqa: E402
from reports.llm_client import client_for, create_with_retry, LlmUnavailable     # noqa: E402
from prices import novelty                                                       # noqa: E402

# --- цена вопроса ---------------------------------------------------------------------------
# ЕДИНИЦА РАБОТЫ — СТРОКА ПРАЙСА, а не модель (правило Сергея 09.08: «на одну модель — один
# запрос сразу по всем пунктам»). Человек вставляет строку поставщика целиком и одним вопросом
# получает разбор всего её модельного ряда. Платим за запрос и за поиски внутри него, а не
# за перемножение «модели × вопросы».
#
# Серверный веб-поиск Anthropic: $10 за 1000 поисков. Токены с выдачей поиска на sonnet
# (вход ~25k × $3/M + выход ~2.5k × $15/M) ≈ $0.11 на запрос. При потолке в 6 поисков —
# ≈ $0.17 ≈ 14 ₽ за строку прайса, сколько бы моделей в ней ни было.
# Константы держим здесь, а не в коде интерфейса: смета и факт должны считаться одинаково.
USD_PER_SEARCH = 0.010
USD_TOKENS_PER_REQUEST = 0.11
RATE_FALLBACK = Decimal("90")          # если ЦБ недоступен — не падаем, но помечаем смету

MODEL = os.environ.get("PRC_RESEARCH_MODEL", "claude-sonnet-5")
# Потолок поисков НА ЗАПРОС (не на модель): сколько сеть реально потратит, столько и запишем
# в факт. Смета считается по потолку — худший сценарий должен быть виден заранее.
MAX_USES = int(os.environ.get("PRC_RESEARCH_MAX_USES", "6"))
MAX_TOKENS = int(os.environ.get("PRC_RESEARCH_MAX_TOKENS", "6000"))

# --- разбор модели из строки прайса ----------------------------------------------------------
# Код расходника: буквы + цифры, с дефисом или без («TK-3190», «TK3190», «CF259A», «C-EXV65»).
MODEL_RE = re.compile(r"\b([A-Z]{1,5}(?:-[A-Z]{1,5})?-?\d{2,5}[A-Z]{0,4})\b", re.I)
# Голый номер в перечислении: «TK-3060/3100/3110» — второй и третий пишутся без префикса,
# и без этого правила из строки на 24 модели вычиталась бы одна. Префикс наследуется от
# ближайшего слева полного кода.
BARE_RE = re.compile(r"\b(\d{2,5}[A-Z]{0,4})\b", re.I)
TOKEN_RE = re.compile(f"{MODEL_RE.pattern}|{BARE_RE.pattern}", re.I)
# Префиксы поставщиков: их коды — обёртка вокруг OEM-кода, сама по себе моделью не является.
VENDOR_PREFIX = ("CS", "GP", "HB", "SF", "SFR", "OEM", "SP", "BS", "RK", "CT", "CET", "EL", "PL")
# Слова, которые регулярка ловит, а моделью они не являются.
NOISE = {"A4", "A3", "MFP", "MPS", "PDF", "USB", "ISO", "RGB"}


def norm_model(code):
    """Ключ кэша: «tk-3190», «TK 3190», «TK3190» -> «TK3190».

    Дефис у одной и той же модели ставят по-разному (Kyocera пишет «TK-3190», HP свой «CF210A»
    пишет слитно, поставщик — как придётся). Ключ дефис не хранит, а показываем модель так,
    как её написали в прайсе: перерисовывать чужую запись в «правильную» — лишний источник
    расхождений.
    """
    return re.sub(r"[^A-ZА-Я0-9]", "", str(code or "").upper())


def label_model(code):
    """Модель для показа: как написано, только в верхнем регистре и без лишних пробелов."""
    return re.sub(r"\s+", "", str(code or "")).upper()


def _strip_vendor(code):
    """«CS-TK-3190» -> «TK-3190». Префикс снимаем только если после него остаётся код."""
    parts = code.upper().split("-")
    while len(parts) > 1 and parts[0] in VENDOR_PREFIX:
        parts = parts[1:]
    return "-".join(parts)


def models_of(row):
    """Модели картриджей из строки прайса. Одна строка флакона даёт список моделей.

    Где искать — зависит от типа товара, и путать их нельзя:
      * тонер и чернила — хвост «…для заправки картриджа Kyocera TK-330/TK-360/TK-3130»:
        там перечислены МОДЕЛИ, под которые идёт флакон. Артикул вычитаем — `CS-TKY4-650`
        это внутренний код флакона у поставщика, такой модели не существует;
      * картридж — наоборот: модель сидит в артикуле и в голове названия (`CS-DK7300`),
        а в хвосте после «для» перечислены ПРИНТЕРЫ, и разведывать их не надо.
    """
    name = str(row.get("name") or "")
    article = str(row.get("article") or "")
    tail = novelty.COMPAT_RE.search(name)
    refill = novelty.kind(name) in ("toner", "ink")
    if refill:
        text = tail.group(1) if tail else name
        own = {norm_model(_strip_vendor(c)) for c in MODEL_RE.findall(article)}
        own |= {norm_model(article)}
    else:
        text = f"{name[:tail.start()] if tail else name} {article}"
        own = set()
    out, seen, prefix, sep, end = [], set(), None, "", 0
    for match in TOKEN_RE.finditer(text):
        full, bare = match.group(1), match.group(2)
        # Наследовать префикс можно только внутри перечисления через «/» или «,». Иначе
        # «CF210A (131A)» дало бы несуществующий CF-131A: в скобках — второе имя того же
        # картриджа, а не следующая модель серии.
        run = re.fullmatch(r"\s*[/,]\s*", text[end:match.start()]) is not None
        end = match.end()
        if full:
            code = label_model(_strip_vendor(full))
            head = re.match(r"([A-Z]{1,5}(?:-[A-Z]{1,5})?)(-?)\d", code)
            prefix, sep = (head.group(1), head.group(2)) if head else (None, "")
        elif prefix and run:
            code = f"{prefix}{sep}{bare.upper()}"
        else:
            continue                       # голое число вне перечисления — объём, ресурс или год
        key = norm_model(code)
        if key in NOISE or key in own or key in seen or len(key) < 4:
            continue
        seen.add(key)
        out.append(code)
    return out


# --- смета ------------------------------------------------------------------------------------
class NeedsConsent(Exception):
    """Запрос платный и не подтверждён. В `.estimate` лежит готовая смета."""

    def __init__(self, estimate):
        super().__init__("нужно подтверждение: запрос платный")
        self.estimate = estimate


def cached(models):
    """Что из моделей уже разведано: {ключ кэша: строка prc_model_research}."""
    keys = [k for k in {norm_model(m) for m in models} if k]
    if not keys:
        return {}
    rows = query("select * from prc_model_research where model_key = any(%s)", (keys,))
    return {r["model_key"]: dict(r) for r in rows}


def _rate():
    """Курс доллара ЦБ на сегодня. Недоступен — берём запасной и говорим об этом честно."""
    try:
        from prices.cbr import cbr_rate
        rate, _ = cbr_rate(dt.date.today())
        return Decimal(rate), True
    except Exception:
        return RATE_FALLBACK, False


def is_refill(row):
    """Строка про сыпучий тонер / чернила (её разбирают на карточки по моделям)?"""
    return novelty.kind(str(row.get("name") or "")) in ("toner", "ink")


def estimate(rows, force=False):
    """Смета БЕЗ единого запроса к провайдеру: сколько строк спросим и сколько это стоит.

    Считаем ПО СТРОКАМ ПРАЙСА: один запрос на строку, все вопросы сразу. Строка, у которой все
    модели уже в кэше, не спрашивается вовсе. force=True — спросить заново, минуя кэш.
    """
    rows = [dict(r) for r in rows]
    have = cached([m for r in rows for m in models_of(r)])
    plan, cache_hits, seen = [], [], set()
    for row in rows:
        models = models_of(row)
        fresh = []
        for m in models:
            key = norm_model(m)
            if not force and key in have:
                cache_hits.append(m)
            elif key not in seen:
                seen.add(key)
                fresh.append(m)
        if fresh:
            plan.append({"id": row.get("id"), "article": row.get("article"),
                         "name": row.get("name"), "kind": "refill" if is_refill(row) else "single",
                         "models": fresh})
    usd = len(plan) * (MAX_USES * USD_PER_SEARCH + USD_TOKENS_PER_REQUEST)
    rate, live = _rate()
    return {
        "plan": plan, "rows": len(rows), "cached": cache_hits,
        "to_ask": [m for p in plan for m in p["models"]],
        "searches": len(plan) * MAX_USES, "usd": round(usd, 4),
        "rub": round(float(Decimal(usd) * rate), 2), "rate": float(rate), "rate_live": live,
        "llm_model": MODEL, "max_uses": MAX_USES,
        "per_row_usd": round(MAX_USES * USD_PER_SEARCH + USD_TOKENS_PER_REQUEST, 4),
    }


def estimate_text(est):
    """Смета словами — один и тот же текст в CLI и в модалке интерфейса."""
    if not est["plan"]:
        return "Все модели уже разведаны — запрос не нужен, денег не тратим."
    part = (f", {len(est['cached'])} уже в кэше — бесплатно" if est["cached"] else "")
    tail = "" if est["rate_live"] else f" (курс ЦБ недоступен, взят запасной {est['rate']:.0f})"
    return (f"Запрос платный. Строк прайса: {len(est['plan'])} "
            f"(моделей в них {len(est['to_ask'])}{part}) — один запрос на строку, все вопросы "
            f"сразу. Поисков не больше {est['searches']}, ≈ ${est['usd']:.2f} ≈ "
            f"{est['rub']:.0f} ₽{tail}. Худший сценарий — та же сумма при нулевом результате.")


# --- собственно разведка ------------------------------------------------------------------------
SYSTEM = """Ты — товаровед магазина совместимых расходников «Цифровой квадрат» (Wildberries, Ozon).
Тебе дают ОДНУ строку из прайса поставщика и список моделей, которые из неё вычитаны. По каждой
модели мы решаем, заводить ли отдельную карточку товара на маркетплейсах, и чем её заполнить.
Отвечай ОДНИМ запросом сразу по всем пунктам и по всем моделям. Верни СТРОГО один JSON
без markdown.

В списке может оказаться не картридж, а ПРИНТЕР: поставщик пишет «для ...» и туда, и туда
одинаково. Если тебе назвали принтер — найди его штатный картридж, верни код картриджа в поле
"cartridge" и отвечай про картридж. Если картридж — "cartridge" повтори как есть.

По каждой модели:
1. Ходит ли она на РОССИЙСКОМ рынке — продаётся ли на Ozon / Wildberries / Яндекс.Маркет / в
   российских магазинах расходников, поставлялся ли принтер под неё в Россию. Нероссийские
   помечай "ru": false — по ним карточку не заводим.
2. Насколько популярна, по числу предложений и отзывов на российских площадках: high — десятки
   предложений и живые отзывы; mid — предложения есть, спрос умеренный; low — единичные
   предложения; none — на российском рынке не найдена.
3. ПОЛНЫЙ перечень принтеров, в которых этот картридж используется В РОССИИ, — так, как их
   пишет производитель, со всеми модификациями серии: «ECOSYS M3550idn», «M3560idn»,
   «FS-4200DN». Не сокращай и не схлопывай в «и др.».
4. Ресурс печати в страницах (по стандарту ISO/IEC при 5% заполнении).
5. Только для тонера и чернил: сколько граммов тонера или миллилитров чернил нужно на ПОЛНУЮ
   заправку ОДНОГО картриджа этой модели. Нужна доза заправки, а НЕ вес картриджа в сборе и
   НЕ объём флакона из магазина. Для готовых картриджей оставь null.
6. Вес товара В УПАКОВКЕ, кг, и размеры УПАКОВКИ (коробки), см — длина, ширина, высота.
   Это упаковка одной штуки, а не мастер-картон и не палета.

ЖЕЛЕЗНОЕ ПРАВИЛО ИСТОЧНИКОВ: список принтеров, доза заправки, вес и размеры принимаются ТОЛЬКО
если ты нашёл их на конкретной странице и эта страница есть среди результатов поиска. Ничего
не вычисляй по аналогии, не оценивай «примерно», не достраивай список по номеру серии, не
пересчитывай короб из объёма. Не нашёл — верни null и пустой список. Отсутствие ответа стоит
дешевле выдуманного: за неверный габарит маркетплейс перемеряет коробку и штрафует.

JSON:
{"models": [
  {"model": "<модель, как её назвали в вопросе>",
   "cartridge": "<код картриджа, о котором отвечаешь>",
   "ru": true|false,
   "popularity": "high|mid|low|none",
   "resource_pages": <int|null>,
   "refill_amount": <число|null>,
   "refill_unit": "г|мл|null",
   "printers": ["<модель принтера>", ...],
   "weight_kg": <число|null>,
   "pack_cm": [<длина>, <ширина>, <высота>] | null,
   "pack_source": "<чей сайт дал вес и короб>|null",
   "verdict": "<одна строка: заводить карточку или нет и почему>",
   "why": "<1-2 предложения: на чём основан вывод>",
   "confidence": "high|mid|low"}
]}"""

PROMPT_REFILL = """СТРОКА ПОСТАВЩИКА: {name}
Артикул поставщика: {article}

Я буду делать отдельные карточки товара для маркетплейсов по этому товару поставщика — по одной
на каждую модель картриджа. Убери из списка нероссийские модели. Для оставшихся найди список
принтеров, в которых они используются в РФ, объём тонера/чернил для полной заправки, ресурс
печати, вес товара в упаковке и размеры упаковки. Нужны точные данные из проверенных источников.

Модели из этой строки: {models}"""

PROMPT_SINGLE = """МОДЕЛЬ ПОСТАВЩИКА: {models}
Строка прайса: {name} (артикул {article})

Я буду делать карточку товара для маркетплейсов. Найди полный список принтеров для России,
ресурс печати, вес товара в упаковке и размеры упаковки. Нужны точные данные из проверенных
источников."""


def _text(message):
    return "".join(b.text for b in message.content if getattr(b, "type", None) == "text").strip()


def _sources(message):
    out = []
    for block in message.content:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        for res in (getattr(block, "content", None) or []):
            url = getattr(res, "url", None)
            if url:
                out.append({"url": url, "title": (getattr(res, "title", None) or "")[:120]})
    return out[:8]


def _searches_used(message):
    usage = getattr(message, "usage", None)
    tool = getattr(usage, "server_tool_use", None)
    return int(getattr(tool, "web_search_requests", 0) or 0)


def _num(value, cast=float):
    try:
        return cast(value) if value not in (None, "", "null") else None
    except (TypeError, ValueError):
        return None


def _one(data, sources):
    """Одна модель из ответа -> запись. Всё, что без источника, отбрасывается."""
    has_source = bool(sources)
    amount = _num(data.get("refill_amount"))
    unit = data.get("refill_unit") if data.get("refill_unit") in ("г", "мл") else None
    printers = [str(p).strip() for p in (data.get("printers") or []) if str(p).strip()]
    pack = data.get("pack_cm") or []
    pack = [_num(x) for x in pack][:3] if isinstance(pack, list) and len(pack) >= 3 else []
    pack = pack if has_source and all(p for p in pack) else []
    return {
        "model": str(data.get("model") or data.get("cartridge") or "").strip()[:60],
        "cartridge": (str(data.get("cartridge")).strip()[:60] or None
                      if data.get("cartridge") else None),
        "ru": data.get("ru") if isinstance(data.get("ru"), bool) else None,
        "popularity": data.get("popularity") if data.get("popularity") in
        ("high", "mid", "low", "none") else None,
        "resource_pages": _num(data.get("resource_pages"), int),
        # без ссылки-источника цифра и список не принимаются — см. шапку модуля
        "refill_amount": amount if has_source else None,
        "refill_unit": unit if (has_source and amount is not None) else None,
        "printers": printers if has_source else [],
        "weight_kg": _num(data.get("weight_kg")) if has_source else None,
        "pack_l_cm": pack[0] if pack else None,
        "pack_w_cm": pack[1] if pack else None,
        "pack_h_cm": pack[2] if pack else None,
        "pack_source": (str(data.get("pack_source") or "")[:120] or None) if has_source else None,
        "verdict": str(data.get("verdict") or "")[:400],
        "why": str(data.get("why") or "")[:600],
        "confidence": data.get("confidence") if data.get("confidence") in
        ("high", "mid", "low") else "low",
        "sources": sources,
    }


def parse(text, sources, models=()):
    """Ответ на строку прайса -> {модель: запись}. В ответе разбор сразу всех моделей строки."""
    match = re.search(r"\{.*\}", str(text or ""), re.S)
    data = {}
    if match:
        try:
            data = json.loads(match.group(0))
        except Exception:
            data = {}
    items = data.get("models") if isinstance(data.get("models"), list) else ([data] if data else [])
    out, by_key = {}, {norm_model(m): m for m in models}
    for item in items:
        if not isinstance(item, dict):
            continue
        rec = _one(item, sources)
        # Модель возвращаем под тем именем, каким её спросили: сеть любит переписать
        # «TK-330» в «TK-330 (1T02GA0EU0)», а ключ кэша должен остаться нашим.
        key = norm_model(rec["model"]) or norm_model(rec["cartridge"])
        label = by_key.get(key) or rec["model"] or rec["cartridge"]
        if label:
            out[label] = rec
    return out


def save(model, rec):
    execute(
        """insert into prc_model_research
             (model, model_key, cartridge, ru, popularity, resource_pages, refill_amount,
              refill_unit, printers, weight_kg, pack_l_cm, pack_w_cm, pack_h_cm, pack_source,
              asked_row, verdict, why, confidence, sources, llm_model, searches,
              cost_usd, asked_at)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,
                   %s,%s,%s, now())
           on conflict (model_key) do update set
             cartridge=excluded.cartridge, ru=excluded.ru, popularity=excluded.popularity,
             resource_pages=excluded.resource_pages, refill_amount=excluded.refill_amount,
             refill_unit=excluded.refill_unit, printers=excluded.printers,
             weight_kg=excluded.weight_kg, pack_l_cm=excluded.pack_l_cm,
             pack_w_cm=excluded.pack_w_cm, pack_h_cm=excluded.pack_h_cm,
             pack_source=excluded.pack_source, asked_row=excluded.asked_row,
             verdict=excluded.verdict, why=excluded.why, confidence=excluded.confidence,
             sources=excluded.sources, llm_model=excluded.llm_model,
             searches=excluded.searches, cost_usd=excluded.cost_usd, asked_at=now()""",
        (label_model(model), norm_model(model), rec.get("cartridge"),
         rec["ru"], rec["popularity"], rec["resource_pages"], rec["refill_amount"],
         rec["refill_unit"], json.dumps(rec["printers"], ensure_ascii=False),
         rec.get("weight_kg"), rec.get("pack_l_cm"), rec.get("pack_w_cm"), rec.get("pack_h_cm"),
         rec.get("pack_source"), rec.get("asked_row"), rec["verdict"],
         rec["why"], rec["confidence"], json.dumps(rec["sources"], ensure_ascii=False),
         rec.get("llm_model"), rec.get("searches"), rec.get("cost_usd")))


def research_row(client, item):
    """ОДИН платный запрос на ОДНУ строку прайса — сразу все вопросы и все её модели.

    Вызывается только из `ask(confirm=True)`. Формулировка — та, которой Сергей пользуется
    руками: строку поставщика вставляем как есть, дальше список вопросов одним куском.
    """
    models = item["models"]
    template = PROMPT_REFILL if item.get("kind") == "refill" else PROMPT_SINGLE
    prompt = template.format(name=item.get("name") or "", article=item.get("article") or "",
                             models=", ".join(models))
    message = create_with_retry(
        client, model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_USES}],
        messages=[{"role": "user", "content": prompt}])
    used = _searches_used(message)
    recs = parse(_text(message), _sources(message), models)
    # Расход — на запрос, а не на модель: делим его по моделям строки только для журнала.
    cost = round(used * USD_PER_SEARCH + USD_TOKENS_PER_REQUEST, 4)
    for rec in recs.values():
        rec["llm_model"] = MODEL
        rec["searches"] = used
        rec["cost_usd"] = round(cost / max(len(recs), 1), 4)
        rec["asked_row"] = (item.get("name") or "")[:400]
    return recs, cost


def ask(rows, confirm=False, force=False, progress=None):
    """Разведать строки прайса. БЕЗ `confirm=True` не тратит ничего и поднимает `NeedsConsent`.

    -> {"estimate": …, "results": {модель: запись}, "spent_usd": …, "errors": {…}}
    """
    rows = [dict(r) for r in rows]
    est = estimate(rows, force=force)
    if est["plan"] and not confirm:
        raise NeedsConsent(est)
    all_models = [m for r in rows for m in models_of(r)]
    have = {} if force else cached(all_models)
    results = {m: dict(have[norm_model(m)]) for m in all_models if norm_model(m) in have}
    errors, spent = {}, 0.0
    if est["plan"]:
        try:
            client = client_for(MODEL)
        except LlmUnavailable as exc:
            return {"estimate": est, "results": results, "spent_usd": 0.0,
                    "errors": {p["article"]: f"провайдер недоступен: {exc}" for p in est["plan"]}}
        for item in est["plan"]:
            try:
                recs, cost = research_row(client, item)
            except Exception as exc:                   # запрос мог не состояться — не роняем пачку
                errors[item.get("article") or "?"] = str(exc)[:200]
                continue
            spent += cost
            for model, rec in recs.items():
                save(model, rec)
                results[model] = rec
            missed = [m for m in item["models"] if m not in recs]
            if missed:
                errors[item.get("article") or "?"] = ("сеть не ответила по моделям: "
                                                      + ", ".join(missed))
            if progress:
                progress(item, recs)
    return {"estimate": est, "results": results, "spent_usd": round(spent, 4), "errors": errors}


def rows_by_id(ids):
    """Строки новинок по id — то, чем спрашивают из интерфейса и из CLI."""
    if not ids:
        return []
    return [dict(r) for r in query(
        "select id, article, name, kind, decision from prc_novelty where id = any(%s)",
        (list(ids),))]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in argv
    force = "--force" in argv
    ids = [int(a.split("=")[1]) for a in argv if a.startswith("--id=")]
    free = [a for a in argv if not a.startswith("--")]
    rows = rows_by_id(ids)
    if free:                       # строка прайса текстом: «python -m prices.research "Тонер …"»
        rows += [{"id": None, "article": "", "name": " ".join(free)}]
    if not rows:
        print('usage: python -m prices.research --id=42 [--id=43] | "строка прайса целиком" '
              '[--force] [--apply]')
        return 2
    est = estimate(rows, force=force)
    print(estimate_text(est))
    print(f"  модель-ответчик: {est['llm_model']}, потолок поисков на строку: {MAX_USES}")
    for item in est["plan"]:
        print(f"  строка {item['article'] or '—'}: {', '.join(item['models'])}")
    if est["cached"]:
        print(f"  из кэша: {', '.join(est['cached'])}")
    if not apply:
        print("  --apply не указан: ничего не запрошено, деньги не потрачены.")
        return 0
    out = ask(rows, confirm=True, force=force)
    for model, rec in out["results"].items():
        dose = (f"{float(rec['refill_amount']):g} {rec['refill_unit']}"
                if rec.get("refill_amount") else "нет источника")
        pack = (f"{rec['pack_l_cm']}×{rec['pack_w_cm']}×{rec['pack_h_cm']} см"
                if rec.get("pack_l_cm") else "нет источника")
        print(f"  {model}: РФ={rec.get('ru')} спрос={rec.get('popularity')} заправка={dose} "
              f"ресурс={rec.get('resource_pages') or '—'} вес={rec.get('weight_kg') or '—'} "
              f"упаковка={pack} принтеров={len(rec.get('printers') or [])}")
    for where, err in out["errors"].items():
        print(f"  ! {where}: {err}")
    print(f"потрачено ≈ ${out['spent_usd']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
