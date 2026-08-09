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
# Серверный веб-поиск Anthropic: $10 за 1000 поисков. Токены с выдачей поиска на sonnet
# (вход ~14k × $3/M + выход ~700 × $15/M) ≈ $0.047 на модель. Итог ≈ $0.08 ≈ 7 ₽ за модель.
# Константы держим здесь, а не в коде интерфейса: смета и факт должны считаться одинаково.
USD_PER_SEARCH = 0.010
USD_TOKENS_PER_MODEL = 0.047
RATE_FALLBACK = Decimal("90")          # если ЦБ недоступен — не падаем, но помечаем смету

MODEL = os.environ.get("PRC_RESEARCH_MODEL", "claude-sonnet-5")
MAX_USES = int(os.environ.get("PRC_RESEARCH_MAX_USES", "3"))
MAX_TOKENS = int(os.environ.get("PRC_RESEARCH_MAX_TOKENS", "2000"))

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


def estimate(models, force=False):
    """Смета БЕЗ единого запроса к провайдеру: сколько спросим, сколько это стоит.

    force=True — спрашивать заново даже то, что в кэше (кэш не бесплатен только по времени).
    """
    seen, models = set(), [label_model(m) for m in models if norm_model(m)]
    models = [m for m in models if not (norm_model(m) in seen or seen.add(norm_model(m)))]
    have = cached(models)
    to_ask = models if force else [m for m in models if norm_model(m) not in have]
    searches = len(to_ask) * MAX_USES
    usd = len(to_ask) * (MAX_USES * USD_PER_SEARCH + USD_TOKENS_PER_MODEL)
    rate, live = _rate()
    return {
        "models": models, "cached": [m for m in models if norm_model(m) in have], "to_ask": to_ask,
        "searches": searches, "usd": round(usd, 4), "rub": round(float(Decimal(usd) * rate), 2),
        "rate": float(rate), "rate_live": live, "llm_model": MODEL, "per_model_usd":
        round(MAX_USES * USD_PER_SEARCH + USD_TOKENS_PER_MODEL, 4),
    }


def estimate_text(est):
    """Смета словами — один и тот же текст в CLI и в модалке интерфейса."""
    if not est["to_ask"]:
        return "Все модели уже разведаны — запрос не нужен, денег не тратим."
    part = (f" ({len(est['cached'])} уже в кэше — бесплатно)" if est["cached"] else "")
    tail = "" if est["rate_live"] else f" (курс ЦБ недоступен, взят запасной {est['rate']:.0f})"
    return (f"Запрос платный. Моделей: {len(est['to_ask'])}{part}, "
            f"поисков: {est['searches']}, ≈ ${est['usd']:.2f} ≈ {est['rub']:.0f} ₽{tail}. "
            f"Худший сценарий — та же сумма при нулевом результате.")


# --- собственно разведка ------------------------------------------------------------------------
SYSTEM = """Ты — товаровед магазина совместимых расходников «Цифровой квадрат» (Wildberries, Ozon).
Тебе называют МОДЕЛЬ. Это может быть модель картриджа (TK-3190, CF259A) ИЛИ модель принтера
(ECOSYS P6030cdn): поставщик в прайсе пишет и то, и другое одинаково — «для ... ». Сначала
определи, что тебе назвали. Если это ПРИНТЕР — найди его штатный картридж, верни его код в поле
"cartridge" и дальше отвечай про ЭТОТ картридж. Если это картридж — "cartridge" повтори как есть.
Верни СТРОГО один JSON без markdown.

Что нужно:
1. Ходит ли модель на РОССИЙСКОМ рынке: продаётся ли она на Ozon / Wildberries / Яндекс.Маркет /
   в российских магазинах расходников, поставлялся ли принтер под неё в Россию.
2. Насколько популярна — по числу предложений и отзывов на российских площадках:
   high — десятки предложений и живые отзывы; mid — предложения есть, спрос умеренный;
   low — единичные предложения; none — на российском рынке не найдена.
3. Сколько тонера (в граммах) или чернил (в миллилитрах) нужно на ПОЛНУЮ заправку ОДНОГО
   картриджа этой модели. Нужна доза заправки, а НЕ вес картриджа в сборе и НЕ объём флакона
   из магазина. Если нашёл только ресурс в страницах — верни ресурс, дозу оставь null.
4. ПОЛНЫЙ перечень принтеров под этот картридж — так, как их пишет производитель, со всеми
   модификациями серии: «ECOSYS M3550idn», «M3560idn», «FS-4200DN». Не сокращай список и не
   схлопывай модификации в «и др.».

ЖЕЛЕЗНОЕ ПРАВИЛО ИСТОЧНИКОВ: доза заправки и список принтеров принимаются ТОЛЬКО если ты
нашёл их на конкретной странице и эта страница есть среди результатов поиска. Ничего не
вычисляй по аналогии, не оценивай «примерно», не достраивай список по номеру серии.
Не нашёл — верни null и пустой список. Отсутствие ответа стоит дешевле выдуманного.

JSON:
{"cartridge": "<код картриджа, о котором отвечаешь>",
 "ru": true|false,
 "popularity": "high|mid|low|none",
 "resource_pages": <int|null>,
 "refill_amount": <число|null>,
 "refill_unit": "г|мл|null",
 "printers": ["<модель принтера>", ...],
 "verdict": "<одна строка: заводить карточку или нет и почему>",
 "why": "<1-2 предложения: на чём основан вывод>",
 "confidence": "high|mid|low"}"""


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


def parse(text, sources):
    """Ответ модели -> запись. Доза и принтеры без источника отбрасываются."""
    match = re.search(r"\{.*\}", str(text or ""), re.S)
    data = {}
    if match:
        try:
            data = json.loads(match.group(0))
        except Exception:
            data = {}
    if not data:
        return {"cartridge": None, "ru": None, "popularity": None, "resource_pages": None,
                "refill_amount": None, "refill_unit": None, "printers": [], "verdict": "",
                "why": "ответ не разобран", "confidence": "low", "sources": sources}
    has_source = bool(sources)
    amount = data.get("refill_amount")
    try:
        amount = float(amount) if amount not in (None, "", "null") else None
    except (TypeError, ValueError):
        amount = None
    printers = [str(p).strip() for p in (data.get("printers") or []) if str(p).strip()]
    unit = data.get("refill_unit")
    unit = unit if unit in ("г", "мл") else None
    pages = data.get("resource_pages")
    try:
        pages = int(pages) if pages not in (None, "", "null") else None
    except (TypeError, ValueError):
        pages = None
    return {
        "cartridge": (str(data.get("cartridge")).strip()[:60] or None
                      if data.get("cartridge") else None),
        "ru": data.get("ru") if isinstance(data.get("ru"), bool) else None,
        "popularity": data.get("popularity") if data.get("popularity") in
        ("high", "mid", "low", "none") else None,
        "resource_pages": pages,
        # без ссылки-источника цифра и список не принимаются — см. шапку модуля
        "refill_amount": amount if has_source else None,
        "refill_unit": unit if (has_source and amount is not None) else None,
        "printers": printers if has_source else [],
        "verdict": str(data.get("verdict") or "")[:400],
        "why": str(data.get("why") or "")[:600],
        "confidence": data.get("confidence") if data.get("confidence") in
        ("high", "mid", "low") else "low",
        "sources": sources,
    }


def save(model, rec):
    execute(
        """insert into prc_model_research
             (model, model_key, cartridge, ru, popularity, resource_pages, refill_amount,
              refill_unit, printers, verdict, why, confidence, sources, llm_model, searches,
              cost_usd, asked_at)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,%s,%s,%s, now())
           on conflict (model_key) do update set
             cartridge=excluded.cartridge, ru=excluded.ru, popularity=excluded.popularity,
             resource_pages=excluded.resource_pages, refill_amount=excluded.refill_amount,
             refill_unit=excluded.refill_unit, printers=excluded.printers,
             verdict=excluded.verdict, why=excluded.why, confidence=excluded.confidence,
             sources=excluded.sources, llm_model=excluded.llm_model,
             searches=excluded.searches, cost_usd=excluded.cost_usd, asked_at=now()""",
        (label_model(model), norm_model(model), rec.get("cartridge"),
         rec["ru"], rec["popularity"], rec["resource_pages"], rec["refill_amount"],
         rec["refill_unit"], json.dumps(rec["printers"], ensure_ascii=False), rec["verdict"],
         rec["why"], rec["confidence"], json.dumps(rec["sources"], ensure_ascii=False),
         rec.get("llm_model"), rec.get("searches"), rec.get("cost_usd")))


def research_one(client, model):
    """Один платный запрос по одной модели. Вызывается только из `ask(confirm=True)`."""
    prompt = (f"МОДЕЛЬ КАРТРИДЖА: {model}\n\n"
              f"Найди по ней факты и верни JSON по схеме из инструкции. "
              f"Особое внимание — полному списку принтеров и дозе полной заправки.")
    message = create_with_retry(
        client, model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
        tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": MAX_USES}],
        messages=[{"role": "user", "content": prompt}])
    used = _searches_used(message)
    rec = parse(_text(message), _sources(message))
    rec["llm_model"] = MODEL
    rec["searches"] = used
    rec["cost_usd"] = round(used * USD_PER_SEARCH + USD_TOKENS_PER_MODEL, 4)
    return rec


def ask(models, confirm=False, force=False, progress=None):
    """Разведать модели. БЕЗ `confirm=True` не тратит ничего и поднимает `NeedsConsent`.

    -> {"estimate": …, "results": {модель: запись}, "spent_usd": …, "errors": {…}}
    """
    est = estimate(models, force=force)
    if est["to_ask"] and not confirm:
        raise NeedsConsent(est)
    have = {} if force else cached(est["models"])
    results = {m: dict(have[norm_model(m)]) for m in est["models"] if norm_model(m) in have}
    errors, spent = {}, 0.0
    if est["to_ask"]:
        try:
            client = client_for(MODEL)
        except LlmUnavailable as exc:
            return {"estimate": est, "results": results, "spent_usd": 0.0,
                    "errors": {m: f"провайдер недоступен: {exc}" for m in est["to_ask"]}}
        for model in est["to_ask"]:
            try:
                rec = research_one(client, model)
            except Exception as exc:                       # запрос мог не состояться — не роняем пачку
                errors[model] = str(exc)[:200]
                continue
            save(model, rec)
            results[model] = rec
            spent += rec["cost_usd"]
            if progress:
                progress(model, rec)
    return {"estimate": est, "results": results, "spent_usd": round(spent, 4), "errors": errors}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    apply = "--apply" in argv
    force = "--force" in argv
    models = [a for a in argv if not a.startswith("--")]
    if not models:
        print("usage: python -m prices.research TK-3190 TK-3200 [--force] [--apply]")
        return 2
    est = estimate(models, force=force)
    print(estimate_text(est))
    print(f"  модель-ответчик: {est['llm_model']}, поисков на модель: {MAX_USES}")
    if est["cached"]:
        print(f"  из кэша: {', '.join(est['cached'])}")
    if not apply:
        print("  --apply не указан: ничего не запрошено, деньги не потрачены.")
        return 0
    out = ask(models, confirm=True, force=force)
    for model, rec in out["results"].items():
        printers = ", ".join(rec.get("printers") or []) or "—"
        dose = (f"{rec['refill_amount']:g} {rec['refill_unit']}"
                if rec.get("refill_amount") else "нет источника")
        print(f"  {model}: РФ={rec.get('ru')} спрос={rec.get('popularity')} "
              f"заправка={dose} ресурс={rec.get('resource_pages') or '—'}")
        print(f"      принтеры: {printers[:200]}")
    for model, err in out["errors"].items():
        print(f"  ! {model}: {err}")
    print(f"потрачено ≈ ${out['spent_usd']:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
