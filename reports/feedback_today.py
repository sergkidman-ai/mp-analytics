"""reports/feedback_today.py — ПРОГОН ЧЕРНОВИКОВ на свежих необработанных отзывах и вопросах.

Забирает необработанное (is_answered=false) за последние дни и генерит черновики ВСЕМ функционалом:
  ВОПРОСЫ + отзывы С ТЕКСТОМ → ИИ-слой (Claude через relay): факты card_facts v2 (WB-модели из
    описания, WB-чип из Ozon-двойника) + каталог наших листингов + few-shot из корпуса наших ответов.
  ПУСТЫЕ отзывы (5★ без текста) → детерминированный шаблон (без токенов).
Сохраняет черновики в raw_feedback.draft_* и собирает один артефакт docs/feedback_today_artifact.html.
НИЧЕГО НЕ ПОСТИТ на площадках — только черновики.

Запуск:  ./venv/bin/python reports/feedback_today.py [--since YYYY-MM-DD]
"""
import os
import re
import sys
import json
import html
import pathlib
import warnings
from collections import Counter

warnings.filterwarnings("ignore")
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from dotenv import load_dotenv
load_dotenv(BASE_DIR / ".env")
from core import db                                                              # noqa: E402
from reports.feedback_llm import (_card_data, _user_block, _name, SYSTEM, MODEL,  # noqa: E402
                                  _text_of, _asked_models)
from reports.feedback_drafts import _norm                                        # noqa: E402
from reports.card_facts import CardFacts                                         # noqa: E402
from reports.feedback_corpus import load_corpus, intent                         # noqa: E402
from reports.feedback_draft_run import (draft_review, _first_name, _short, DEFECT_RX,  # noqa: E402
                                        review_flagged)
from reports import card_rating
from reports import neg_templates                                                    # noqa: E402
from reports.feedback_web import web_compat                                      # noqa: E402
from reports.llm_client import create_with_retry as _create, LlmUnavailable      # noqa: E402,F401
from reports import answer_cache as ac                                          # noqa: E402
from reports import request_class as rc                                         # noqa: E402
from reports.compat_cache import get as cc_get, put as cc_put                    # noqa: E402
from reports.catalog import _BRANDS                                              # noqa: E402
from reports import llm_batch                                                    # noqa: E402
from reports import brand_notes as bn
from reports import llm_routing                                                  # noqa: E402
from reports import sku_relations                                                # noqa: E402
from reports.llm_client import client_for                                        # noqa: E402

# сигнал, что товар УЖЕ куплен/используется (тогда уместен QR на упаковке/чеке); иначе — пред-продажа.
# Широко: покупка + любой признак использования/поломки (печатает бело/пусто, «что делать», выдаёт ошибку).
_OWNED_RX = re.compile(
    r"куп(и|л|ил|лен)|приобре|заказал|пришёл|пришел|получил|установил|поставил|вставил|"
    r"распечат|не\s+печат|не\s+вид|не\s+опозна|ошибк|брак|верну|возврат|замен|сломал|течёт|течет|"
    r"мажет|полос|что\s+делать|выдаёт|выдает|пишет\b|горит|мига|перестал|не\s+работа|"
    r"(?:бел|пуст|чист|сер)\w*\s+лист|печатает\s+(?:пусто|бел|плохо|бледно|сер)", re.I)
_QR_RX = re.compile(r"(?:по\s*)?QR[-\s]?код\w*"
                    r"(?:\s*(?:внутри|товарн\w*|чек\w*|упаковк\w*|коробк\w*|стикер\w*|или|на|и|в|,))+", re.I)

ART = BASE_DIR / "docs" / "feedback_today_artifact.html"

# ВОПРОСЫ — финальная сборка ответа на Sonnet 4.6. Решение Сергея от 10.09.2026: Opus в ответах
# покупателю не используется вовсе — задача (собрать ответ из готовых фактов карточки и справочников)
# Sonnet'у по силам, а разница в цене пятикратная. Веб-поиск остаётся на WEB_MODEL/DeepSeek.
QUESTION_MODEL = os.environ.get("FEEDBACK_QUESTION_MODEL", "claude-sonnet-4-6")

# Проверяющий проход по готовому черновику (reports/llm_routing.verify) — короткий промпт,
# синхронно и на той же модели: ставить его в батч нельзя, иначе черновик ждёт два такта батча.
VERIFY_MODEL = os.environ.get("FEEDBACK_VERIFY_MODEL", "claude-sonnet-4-6")
VERIFY_ON = os.environ.get("FEEDBACK_VERIFY", "1") == "1"

# A3 (бриф 07.09.2026). Отвечать на отзывы Ozon по API нельзя: /v1/review/comment/create отдаёт 403
# без подписки Premium Plus, а подписки не будет ни у одного юрлица (решение Сергея 24.08.2026).
# Черновиков при этом накопилось 1218 при 71 опубликованном — то есть 1147 вызовов модели ушли
# в никуда и продолжали бы уходить каждый цикл. Поэтому отзывы Ozon в драфт не берём вовсе;
# строка raw_feedback остаётся на месте (её ждёт ручной канал ЛК, docs/reports/
# ozon_lk_manual_reviews_2026-08-24.md). ВОПРОСЫ Ozon правило не трогает — их API открыт.
# Вернуть генерацию: OZON_REVIEW_DRAFTS=1 в .env (появится подписка или работа через ЛК).
OZON_REVIEW_DRAFTS = os.environ.get("OZON_REVIEW_DRAFTS", "0") == "1"

# Сводка последнего run(): {'drafts', 'fails': [{platform,kind,ext_id,err}], 'llm_calls', 'web_calls'}.
# Читает feedback_bot.health.report_cycle, чтобы сообщить, что именно не ушло и почему.
LAST_RUN = {"drafts": 0, "fails": [], "llm_calls": 0, "web_calls": 0}

# Прайс переехал в reports/llm_pricing.py: ту же цену считает сборщик батча, а из feedback_today
# он её взять не мог — кольцевой импорт.
from reports import llm_pricing                                                  # noqa: E402


class _CostTracker:
    """Счётчик токенов/стоимости по вызовам _llm() (синхронная сборка ответа)."""

    def __init__(self):
        self.calls = {}

    def add(self, model, usage):
        it = getattr(usage, "input_tokens", 0) or 0
        ot = getattr(usage, "output_tokens", 0) or 0
        c = self.calls.setdefault(model, {"in": 0, "out": 0, "n": 0})
        c["in"] += it
        c["out"] += ot
        c["n"] += 1

    def summary(self):
        if not self.calls:
            return "  (вызовов не было)"
        lines, total = [], 0.0
        for model, c in self.calls.items():
            cost = llm_pricing.cost(model, c["in"], c["out"])
            total += cost
            avg = f", ≈${cost / c['n']:.4f}/ответ" if c["n"] and cost else ""
            lines.append(f"  {model}: {c['n']} вызовов · {c['in']}+{c['out']} ток. · ≈${cost:.4f}{avg}")
        lines.append(f"  ИТОГО ≈${total:.4f}")
        return "\n".join(lines)

    def persist(self):
        """Upsert в feedback_llm_cost_log (текущие сутки) — суточная сводка агрегирует по дню,
        т.к. _CostTracker живёт только в памяти одного процесса, а циклов в сутках ~12."""
        for model, c in self.calls.items():
            cost = llm_pricing.cost(model, c["in"], c["out"])
            db.execute("""INSERT INTO feedback_llm_cost_log (day, model, calls, tokens_in, tokens_out, cost_usd)
                VALUES (current_date, %s, %s, %s, %s, %s)
                ON CONFLICT (day, model) DO UPDATE SET
                    calls = feedback_llm_cost_log.calls + EXCLUDED.calls,
                    tokens_in = feedback_llm_cost_log.tokens_in + EXCLUDED.tokens_in,
                    tokens_out = feedback_llm_cost_log.tokens_out + EXCLUDED.tokens_out,
                    cost_usd = feedback_llm_cost_log.cost_usd + EXCLUDED.cost_usd""",
                (model, c["n"], c["in"], c["out"], cost))


_COST = _CostTracker()


def _base_model(m):
    """База модели: обрезаем короткий буквенный суффикс варианта серии (CX17NF→CX17, C1750N→C1750)."""
    s = re.sub(r"[^a-z0-9]", "", (m or "").lower())
    return re.sub(r"([0-9])[a-z]{1,4}$", r"\1", s)


# вопрос про подбор/наличие картриджа под КОНКРЕТНУЮ модель принтера (даже если intent≠«совместимость»):
# «есть ли для X», «имеются ли для X», «подойдёт для X», «какой нужен для X» — гоним через компат/веб/каталог
_COMPAT_Q_RX = re.compile(r"есть\s+ли|имеют?ся?\s+ли|подойд|подход|совмест|"
                          r"как(?:ой|ая|ое|ие)\b|нужен|нужна|чем\s+замен", re.I)


def _is_compat_q(body):
    return bool(_asked_models(body or "")) and bool(_COMPAT_Q_RX.search(body or ""))


# ФАКТ-ВЕБ: объективные ТТХ, которых нет в карточке, легко ищутся в вебе — не отфутболивать «напишите нам».
# Габариты/размеры (длина ленты, ширина/высота, «50×57», «9.5 см», мм/диаметр) — тоже объективный факт.
_SPEC_Q_RX = re.compile(r"грамм|вес\s+тонер|тонер.{0,12}грамм|пигментн|водораствор|тип\s+чернил|"
                        r"состав\s+чернил|скольк\w*\s+мл\b|объ[её]м\s+мл|"
                        r"размер|габарит|длин|ширин|высот|диаметр|\bмм\b|\bсм\b|\d+\s*[x×хХ]\s*\d+", re.I)
# сомнение покупателя в характеристике: «ресурс 150 — это нормально?», «так мало?»
_DOUBT_RX = re.compile(r"это\s+норм|нормальн|так\s+мало|почему\s+так\s+мало|маловат|это\s+мало|правильн\w*\s+ли", re.I)
# ответ модели-«отказ»: значения нет / отправляет уточнять — сигнал, что стоит проверить веб
_DEFER_RX = re.compile(r"не\s+указан|нет\s+информац|напишите\s+нам|уточните|обратитесь|не\s+могу\s+сказать|"
                       r"не\s+заявл|заявленн\w+\s+характеристик", re.I)
_INCOMPAT_RX = re.compile(r"не\s+подход|не\s+подойд|не\s+совмест", re.I)
# процедурный/техвопрос по эксплуатации: чип / прошивка / сброс счётчика / «не видит картридж» /
# «просит оригинал» — объективный ответ есть в вебе, даже если совместимость уже подтверждена
_PROC_Q_RX = re.compile(r"чип|прошивк|перепрош|firmware|сброс\w*\s+сч[еёо]тчик|обнул\w+|"
                        r"проверк\w*\s+чип|отключить\s+проверк|не\s+вид\w+\s+картридж|не\s+распозна\w*|"
                        r"требует\s+оригинал|просит\s+оригинал|неоригинальн\w+", re.I)
# та же тема уже раскрыта в ответе → повторно веб не нужен
_PROC_A_RX = re.compile(r"чип|прошивк|перепрош|firmware|сброс|обнул|сч[еёо]тчик", re.I)
# ложный отказ по НАЛИЧИЮ: модель говорит «в каталоге нет / нет в наличии / уточните наличие /
# подходящего варианта нет» — если листинг под модель реально есть, это ошибка, чиним детерминированно
_DENY_AVAIL_RX = re.compile(
    r"в\s+(?:нашем\s+)?каталоге\s+[^.!?]*нет|нет\s+в\s+(?:нашем\s+)?каталоге|нет\s+в\s+наличии|"
    r"уточнит[еь]\s+наличие|не\s+могу\s+подтвердить\s+наличие|в\s+ассортименте\s+(?:пока\s+)?нет|"
    r"подходящ\w+\s+[^.!?]*?(?:сейчас\s+)?нет\b", re.I)
# ответ уводит покупателя «на сторону», не предложив наш артикул (нашли серию по вебу — а нас не назвали)
_REDIRECT_RX = re.compile(
    r"поищите|поискать|в\s+поиске\s+(?:магазин|нашем)|найд[её]те\s+в\s+магазин|под\s+заказ|"
    r"рекомендуем\s+поиск|в\s+ассортименте[^.!?]*нет|в\s+нашем\s+магазине[^.!?]*нет|"
    r"у\s+нас\s+(?:пока\s+)?нет\b|уточнит[еь][^.!?]*налич", re.I)
# уже назван НАШ площадочный артикул → добор не нужен
_HAS_OUR_ART_RX = re.compile(r"артикул\s+ВБ|Ozon\s+SKU|wildberries\.ru/catalog|ozon\.ru/product|"
                             r"market\.yandex\.ru|Яндекс\.Маркете", re.I)
# вопрос о ПРОИЗВОДИТЕЛЕ/СТРАНЕ: клиент требует прямой ответ «Китай», а не уклончивое «не указываем»
_PRODUCER_Q_RX = re.compile(r"производител|кто\s+производ|чей\s+бренд|какой\s+бренд|как(?:ая|ой)\s+фирм|"
                            r"стран[аеы]?\s+производ|где\s+(?:производ|сдела|изготов)|"
                            r"это\s+оригинал\b|оригинал\s+или|чьё\s+производ|чье\s+производ", re.I)
# ответ уже назвал происхождение (Китай/совместимый аналог) → подставлять не нужно
_HAS_ORIGIN_RX = re.compile(r"кита|совместим\w+\s+аналог", re.I)
# уклончивая формулировка про производителя, которую вырезаем перед подстановкой прямого ответа
_EVASIVE_PROD_RX = re.compile(r"(?:производител|бренд|фирм|стран)\w*[^.!?]*"
                              r"(?:не\s+указ|не\s+раскрыва|не\s+сообща|не\s+приво|не\s+могу\s+сказать|"
                              r"напишите\s+нам|уточните)", re.I)


def _needs_fact_web(question, reply):
    """Стоит ли достроить ответ внешним веб-поиском факта/подбора (когда карточка/модель не дали ответа)."""
    q, a = question or "", reply or ""
    # 1) спец-вопрос про ТТХ + модель ушла в отказ ИЛИ вернула пусто/обрывок → веб знает граммы/тип чернил
    if _SPEC_Q_RX.search(q) and (_DEFER_RX.search(a) or len(a.strip()) < 12):
        return True
    # 2) сомнение в характеристике (ресурс/граммы) → веб-сверка с типичным значением
    if _DOUBT_RX.search(q) and re.search(r"ресурс|стран|грамм|\bмл\b|тонер|чернил", q, re.I):
        return True
    # 3) несовместимо, но модель принтера НАЗВАНА, а альтернатива НЕ предложена → веб-подбор картриджа
    if _asked_models(q) and _INCOMPAT_RX.search(a) and not re.search(r"артикул\s+ВБ|Ozon\s+SKU", a):
        return True
    # 4) процедурный вопрос (чип/прошивка/сброс): тема не раскрыта ЛИБО модель отфутболила
    #    («напишите нам»/«в карточке нет») — веб знает ответ по конкретной модели
    if _PROC_Q_RX.search(q) and (not _PROC_A_RX.search(a) or _DEFER_RX.search(a)):
        return True
    return False


# вопрос НЕ только про совместимость — есть ещё тема (заправка/ресурс/чип/комплектация/гарантия):
# серия-shortcut в этом случае НЕЛЬЗЯ отдавать как весь ответ, он покрывает только совместимость.
_EXTRA_TOPIC_RX = re.compile(r"заправ|дозаправ|ресурс|\bчип\w*|компл[ек]т|гаранти", re.I)


# п.7 пакета 21.09.2026: вопрос назвал СЕРИЮ, а не модель («MF650», «M6700», «C5x90»). Раньше это
# давало 'unknown' → LLM/веб/переспрос. Верный ответ — перечислить модели этой серии, которые ЕСТЬ
# в списке совместимости карточки: «Если у вас MF651Cw, MF655Cdw или MF657Cdw — да». Утверждения
# сверх карточки тут нет: называем только её модели и только условно.
_SERIES_X_RX = re.compile(r"\b([A-Za-z]{1,6})[- ]?(\d[\dxX]{1,4})\b")


def _series_pattern(prefix, core):
    """«mf»+«650» → mf65\\d ; «c»+«5x90» → c5\\d90 ; не серия (нет x и не кончается нулём) → None."""
    core = core.lower()
    if "x" in core:
        pat = core.replace("x", r"\d")
    elif core.endswith("0") and len(core) >= 3:
        k = len(core) - len(core.rstrip("0"))
        pat = core.rstrip("0") + r"\d" * k
    else:
        return None
    return re.compile(re.escape(prefix.lower()) + pat + r"[a-z]{0,5}(?:ii)?$")


def _series_members(question, card_models, limit=6):
    """→ модели карточки из названной в вопросе серии (оригинальное написание), [] если серии нет."""
    if not card_models:
        return []
    out = []
    for pre, core in _SERIES_X_RX.findall(question or ""):
        rx = _series_pattern(pre, core)
        if not rx:
            continue
        exact = _norm(pre + core)
        for m in card_models:
            nm = _norm(m)
            if nm.endswith(exact):
                return []                       # точная модель в карточке — это не серия, это 'yes'
            if rx.search(nm) and m not in out:
                out.append(m)
    return out[:limit]


def _or_list(xs):
    xs = list(xs)
    return xs[0] if len(xs) == 1 else ", ".join(xs[:-1]) + " или " + xs[-1]


# п.11 пакета 21.09.2026: источник в трейсе — по ФАКТУ происхождения. До правки любой ответ модели
# писался как «карточка»; кейс 0160: модель сама написала в note «не из CARD_DATA», а источник стоял
# «карточка» — и гейт считал ответ карточным. Теперь модель называет источник сама (поле source),
# а её собственная оговорка в note главнее: признание «не из карточки» не перекрывается ничем.
_LLM_SRC = {"card": "карточка", "brand_notes": "справочник бренда", "knowledge": "модель",
            "mixed": "модель+карточка"}
_NOT_CARD_NOTE_RX = re.compile(r"не\s+(?:из|в|по)\s+CARD_DATA|нет\s+в\s+CARD_DATA|вне\s+CARD_DATA|"
                               r"знани\w*\s+(?:серии|модели|бренда)|по\s+(?:своим|общим)\s+знани|"
                               r"из\s+(?:своих|общих)\s+знаний|общ\w+\s+знани", re.I)


def _llm_source(d):
    src = _LLM_SRC.get(str(d.get("source") or "").strip().lower())
    if _NOT_CARD_NOTE_RX.search(d.get("note") or ""):
        return "модель" if src in (None, "карточка") else src
    if src:
        return src
    return "карточка" if d.get("grounded") else "модель"


# п.10 пакета 21.09.2026: склейка вопросов одного покупателя по товару за 48 часов. Кейс 0160
# (Ozon, T0921): «жёлтый картридж меньше других?» и через 4 минуты «дело в том, что жёлтый короче
# остальных трёх…» — во втором ответе бот забыл первый. Имя покупателя опорой служить не может:
# в том же кейсе первый вопрос подписан «Пользователь предпочёл скрыть свои данные», второй —
# «Валерий К.», а у WB-вопросов имени нет вовсе. Поэтому ключ — (площадка, кабинет, товар, 48 ч),
# а явно ЧУЖИЕ имена (оба указаны и различаются) склейку отменяют.
_HIDDEN_NAME_RX = re.compile(r"скры|аноним|^покупатель$|^—?$", re.I)


def _author(p):
    p = p or {}
    a = p.get("author_name") or (p.get("author") or {}).get("name") or p.get("userName") or ""
    return str(a).strip()


def _thread(r, hours=48, limit=3):
    if r.get("kind") != "question" or not r.get("item_id"):
        return []
    try:
        rows = db.query("""SELECT f.body, f.payload, f.created_at, m.final_text,
                                  to_char(f.created_at AT TIME ZONE 'Europe/Moscow','DD.MM HH24:MI') AS t
                             FROM raw_feedback f
                             LEFT JOIN feedback_moderation m
                               ON (m.platform,m.account,m.kind,m.ext_id)=(f.platform,f.account,f.kind,f.ext_id)
                            WHERE f.platform=%s AND f.account=%s AND f.kind='question'
                              AND f.item_id::text=%s AND f.ext_id<>%s
                              AND f.created_at < %s AND f.created_at >= %s - make_interval(hours => %s)
                            ORDER BY f.created_at DESC LIMIT %s""",
                         (r["platform"], r["account"], str(r["item_id"]), r["ext_id"],
                          r["created_at"], r["created_at"], hours, limit))
    except Exception:
        return []
    me = _author(r.get("payload"))
    out = []
    for p in reversed(rows):
        them = _author(p["payload"])
        if me and them and not _HIDDEN_NAME_RX.search(me) and not _HIDDEN_NAME_RX.search(them) \
                and me.lower() != them.lower():
            continue                                  # оба назвались, и это разные люди
        out.append({"t": p["t"], "body": p["body"], "answer": p["final_text"]})
    return out


def _positive_clean(r):
    """п.1 пакета 21.09.2026: отзыв ≥4★ с текстом без DEFECT_RX и без маркеров претензии. Такой отзыв
    публикуется БЕЗ человека после контролёра — независимо от длины. За неделю 7 длинных позитивов
    прошли вычитку без единой правки. На вычитке остаются только ≤3★, дефект в тексте и FIX контролёра."""
    if r.get("kind") != "review" or (r.get("rating") or 0) < 4:
        return False
    t = " ".join(filter(None, [r.get("body"), r.get("pros"), r.get("cons")])).strip()
    if not t:
        return False
    return not DEFECT_RX.search(t) and not rc.is_claim_text(t, "review", r.get("rating"))


def _fam_status(question, card_models):
    """Совместимость с учётом ВАРИАНТОВ серии. → ('yes',matched)|('unknown',asked)|('no_data'|'no_ask',[])."""
    asked = _asked_models(question)
    if not card_models:
        return ("no_data", [])
    if not asked:
        return ("no_ask", [])
    cn = [_norm(m) for m in card_models]
    cb = [_base_model(m) for m in card_models]
    matched = []
    for a in asked:
        na, ba = _norm(a), _base_model(a)
        if any(na in c or c in na for c in cn) or (len(ba) >= 3 and ba in cb):
            matched.append(a)
    return ("yes", matched) if matched else ("unknown", asked)


def _gather(since):
    # необработанные за окно (последний месяц) — и вопросы, и отзывы по дате created_at.
    # posted_at IS NULL — не пере-драфтить/не пере-класть в очередь то, что этот же цикл уже реально
    # отправил (auto-send/модерация), а сборщик ещё не подтвердил is_answered. skipped_old=false —
    # вопросы старше 30 дней помечены отдельным циклом (feedback_cycle.py) и сюда не попадают.
    # КЭШ ПО СОДЕРЖИМОМУ: draft_src_hash = md5(body+pros+cons) на момент драфта (см. _store). Если
    # хэш совпадает — содержимое не менялось с прошлого драфта, генерацию (в т.ч. вызов Opus на
    # вопросы) пропускаем. Без этого условия цикл каждые 2ч перегенерил ВЕСЬ неотвеченный бэклог
    # заново (было: 3 цикла за день = 3× одинаковых 15 ИИ-вызовов на те же 16 вопросов).
    q = db.query("""SELECT platform,account,kind,ext_id,item_id,article,product_name,rating,body,pros,cons,
        payload, created_at FROM raw_feedback WHERE is_answered=false
        AND account IN ('wb_acc1','wb_acc2','oz_acc1','oz_acc2','ya_acc1')
        AND posted_at IS NULL AND NOT skipped_old
        AND (draft_src_hash IS NULL
             OR draft_src_hash IS DISTINCT FROM md5(coalesce(body,'')||coalesce(pros,'')||coalesce(cons,'')))
        AND (%s OR NOT (platform='ozon' AND kind='review'))
        AND created_at >= %s ORDER BY (kind='question') DESC, created_at DESC NULLS LAST""",
        (OZON_REVIEW_DRAFTS, since))
    return q


def _has_text(r):
    return len((r["body"] or "") + (r["pros"] or "") + (r["cons"] or "")) > 3


def _client():
    from reports.llm_client import client_for
    return client_for(MODEL)


def _brand_of(r):
    """Бренд обращения — для подбора утверждённых ответов той же семьи (пункт 4)."""
    low = ((r.get("product_name") or "") + " " + (r.get("body") or "")).lower()
    return next((b for b in _BRANDS if b in low), None)


def _llm(client, r, cf, corpus, hint=None, web_facts=None):
    """ИИ-черновик: карточка+каталог+few-shot → JSON {reply,route,confidence,grounded,note}.
    ВОПРОСЫ — финальная сборка на QUESTION_MODEL, ОТЗЫВЫ — на MODEL; обе с 10.09.2026 — Sonnet 4.6.
    hint — подсказка по совместимости (напр. серия-shortcut), когда вопрос ЕЩЁ про что-то помимо
    совместимости: LLM собирает полный ответ, а не только компат.
    web_facts — ответ web_fact(): уходит в промпт ОТДЕЛЬНЫМ помеченным блоком, а не подмешивается
    в CARD_DATA (пункт 4: карточка и чужой сайт — источники разного веса).

    При включённом батче (llm_batch.enabled()) вызова API здесь нет вовсе: готовый ответ берётся
    из feedback_llm_result, а промах поднимает LlmPending — запись остаётся без черновика до
    следующего цикла. Это ровно то состояние, в котором она оказывается при сбое модели, и
    пайплайн его уже умеет."""
    cc = _card_data(r, cf)
    ex = corpus.retrieve(r["kind"], r["body"] or r["pros"] or r["cons"] or "", r["product_name"], k=5)
    _cls = rc.classify(" ".join(filter(None, [r.get("body"), r.get("pros"), r.get("cons")]))
                       if r["kind"] == "review" else r.get("body"),
                       kind=r["kind"], rating=r.get("rating"))
    approved = ac.recent_for(_cls, _brand_of(r), limit=5,
                             exclude_article=ac.internal_article(r["platform"], None, r.get("item_id")))
    # Справочник бренда (brand_notes, миграция 707) — наши факты о серии: чип, заправка, прошивка.
    # Идут в промпт отдельным блоком сразу за CARD_DATA и попадают во ВХОДНЫЕ ДАННЫЕ контролёра,
    # иначе он честно режет их как утверждения «вне входных данных» — так и было до 10.09.2026.
    bn_block, bn_n = bn.facts_for(_brand_of(r), r.get("body") or r.get("cons") or r.get("pros"), cc)
    content = _user_block(r, _name(r), cc, ex, hint=hint, approved=approved, web_facts=web_facts,
                          brand_notes=bn_block, thread=_thread(r))
    # thinking-модели (DeepSeek-v4 pro/flash) тратят output-токены на размышления до JSON —
    # держим запас (env FEEDBACK_MAX_TOKENS). Кэш SYSTEM снимаем на не-Anthropic (DeepSeek его игнорит).
    max_tok = int(os.environ.get("FEEDBACK_MAX_TOKENS", "3000"))
    model = QUESTION_MODEL if r["kind"] == "question" else MODEL
    if llm_batch.enabled():
        raw = llm_batch.ask(model, SYSTEM, content, max_tok, r)      # или LlmPending
    else:
        if model != MODEL:
            from reports.llm_client import client_for
            client = client_for(model)
        sysparam = ([{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}]
                    if not model.lower().startswith("deepseek") else SYSTEM)
        m = _create(client, model=model, max_tokens=max_tok, system=sysparam,
                    messages=[{"role": "user", "content": content}])
        _COST.add(model, getattr(m, "usage", None))
        raw = _text_of(m)
    d = None
    mm = re.search(r"\{.*\}", raw, re.S)
    if mm:
        try:
            d = json.loads(mm.group(0))
        except Exception:
            d = None
    if d is None:                                    # JSON битый (обрезка/мусор) — НЕ салважим текст в
        # ответ покупателю (утекала служебная разметка). Маркер + на человека, оператор ответит вручную.
        return ({"reply": "⚠️ Ошибка парсинга ответа модели — нужен ручной ответ оператора.",
                 "route": "human", "confidence": 0, "grounded": False,
                 "note": "ошибка парсинга JSON модели"}, cc, model)
    # guardrail совместимости: утвердительное «да, подойдёт» без модели в карточке → review
    if r["kind"] == "question":
        asked = _asked_models(r["body"])
        aff = re.search(r"подойд|подход|совмест|да,? ", (d.get("reply") or "").lower())
        cn = _norm(cc or "")
        if asked and aff and cn and not any(_norm(a) in cn for a in asked):
            d["route"], d["grounded"] = "review", False
            d["note"] = "guardrail: совместимость не подтверждена карточкой; " + (d.get("note") or "")
        d["route"] = "review"                      # фаза «только черновики»: вопросы всегда на вычитку
    return d, cc, model


# п.9 пакета 21.09.2026: автопубликация вопросов карточных классов. Ответ целиком из карточки
# (или из утверждённого кэша по тому же артикулу), контролёр сказал PASS, гейт чист — кнопка не нужна.
# Совместимость — только при ТОЧНОМ совпадении модели со списком карточки. Стоп-лист A1.2 (обещания)
# и претензии — через тот же publish_gate.verdict_full, что держит и кнопку ✅.
AUTO_Q_CLASSES = ("характеристики", "производитель", "комплектация", "габарит", "совместимость")
AUTO_Q_ON = os.environ.get("FEEDBACK_AUTO_CARD_Q", "1") == "1"


def _auto_card_q(r, reply, route, ground):
    """→ (route, ground). Меняет только review → auto и только для вопросов, прошедших всё сразу."""
    if not AUTO_Q_ON or r.get("kind") != "question" or route != "review" or not (reply or "").strip():
        return route, ground
    from reports import publish_gate
    cls = ground.get("request_class") or rc.classify(r.get("body"), kind="question", rating=r.get("rating"))
    if cls not in AUTO_Q_CLASSES or ground.get("verify") != "PASS":
        return route, ground
    src = str(ground.get("source") or "")
    cache = ground.get("cache") if isinstance(ground.get("cache"), dict) else {}
    if src.startswith("кэш"):
        # первые подстановки («подтвердите»), устаревший кэш и попадание по семейству кода — глазами
        if cache.get("confirm") or cache.get("stale") or cache.get("family") or "веб" in src:
            return route, ground
    elif src != "карточка":
        return route, ground
    g = dict(ground, request_class=cls)
    if cls == "совместимость" and not (g.get("compat") or {}).get("exact"):
        return route, ground
    row = dict(r, draft_text=reply, draft_route="auto", draft_grounding=g)
    if not publish_gate.eased_class(row, g, cls):
        return route, ground
    allow, why, _trace = publish_gate.verdict_full(row, reply)
    if not allow or why:
        return route, ground
    return "auto", dict(g, auto_policy=f"вопрос «{cls}» из карточки, контролёр PASS",
                        note="п.9: автопубликация карточного класса; " + (g.get("note") or "")[:200])


def _store(r, reply, route, conf, ground):
    from psycopg2.extras import Json
    from reports import request_class as rc
    # A2 (07.09.2026): шаблон и класс обращения — колонками, а не только внутри текста черновика.
    # grounding у auto-строк не должен быть пустым (было 117 таких): без него о решении бота не
    # известно ничего, кроме самого текста.
    tpl = ground.get("template_id") or ("llm" if ground.get("llm") else None)
    cls = rc.classify(" ".join(filter(None, [r.get("body"), r.get("pros"), r.get("cons")]))
                      if r["kind"] == "review" else r.get("body"),
                      kind=r["kind"], rating=r.get("rating"))
    ground = dict(ground, template_id=tpl, request_class=cls)
    if not ground.get("note"):
        ground["note"] = f"без пометки: {tpl or 'источник не задан'}"
    # draft_src_hash считаем от ЖИВЫХ колонок body/pros/cons в БД (не от объекта r), чтобы хэш всегда
    # был согласован с тем, что видит фильтр в _gather() — тот же md5(...) над теми же полями.
    db.execute("""UPDATE raw_feedback SET draft_text=%s, draft_route=%s, draft_confidence=%s,
        draft_category=%s, draft_grounding=%s, draft_at=now(),
        template_id=%s, request_class=%s,
        draft_src_hash=md5(coalesce(body,'')||coalesce(pros,'')||coalesce(cons,''))
        WHERE platform=%s AND account=%s AND kind=%s AND ext_id=%s""",
        (reply, route, conf, ("question" if r["kind"] == "question" else "review"),
         Json(ground), tpl, cls, r["platform"], r["account"], r["kind"], r["ext_id"]))


def _enqueue_moderation(r, reply):
    """Боевой режим: поставить в очередь модерации (feedback_moderation) ВОПРОСЫ и ОТЗЫВЫ-С-ТЕКСТОМ.
    Черновик уже в raw_feedback.draft_text — бот-модератор возьмёт его оттуда. Гейт FEEDBACK_MODERATION=1,
    иначе прогон остаётся draft-only. Пустые оценки-звёзды (без текста) НЕ ставим в очередь — их ~2900,
    там шаблон. Повтор не плодит дублей (UNIQUE-ключ)."""
    if os.environ.get("FEEDBACK_MODERATION", "0") != "1":
        return
    if not (reply or "").strip():
        return
    kind = r["kind"]
    if kind == "question":
        pass
    elif kind == "review" and (r.get("body") or r.get("pros") or r.get("cons") or "").strip():
        pass
    else:
        return
    db.execute("""INSERT INTO feedback_moderation (platform, account, kind, ext_id, state)
        VALUES (%s, %s, %s, %s, 'queued')
        ON CONFLICT (platform, account, kind, ext_id) DO NOTHING""",
        (r["platform"], r["account"], kind, r["ext_id"]))


# код расходника/картриджа в тексте ответа: TK-435, LC-421, CF210A, CB540A, 106R03623, C-EXV65,
# TN-241, 07232 (6-значные наши артикулы), S050614 и т.п. — то, что покупатель может «заказать по коду».
_CODE_IN_REPLY = re.compile(
    r"\b(?:TK|LC|CF|CE|CB|CLT|MLT|TN|DR|CRG|CLI|PGI|GPR|MK)-?\d{2,4}[A-Za-z]*\b|"
    r"\bC-EXV\s?\d{1,2}\b|\b\d{3}R\d{4,5}\b|\bC13S\d{5}\b|\bS0?50\d{3}\b|\b\d{6}\b", re.I)


def _codes(text):
    return {re.sub(r"[\s-]", "", m.group(0)).upper() for m in _CODE_IN_REPLY.finditer(text or "")}


def _code_guard(reply, allowed_text, note):
    """Детерминированная страховка от ВЫДУМАННЫХ кодов (DeepSeek смелее Opus). Любой код-расходника в
    ответе, которого НЕТ в CARD_DATA/КАТАЛОГ (allowed_text) — вырезаем предложение с ним. Возвращает
    (очищенный reply, флаг сработал). Коды покупателя из его же вопроса допустимы (передаются в allowed)."""
    allowed = _codes(allowed_text)
    bad = _codes(reply) - allowed
    if not bad:
        return reply, False
    kept = []
    for sent in re.split(r"(?<=[.!?])\s+", reply):
        if _codes(sent) & bad:
            continue                       # предложение содержит выдуманный код — выкидываем
        kept.append(sent)
    out = " ".join(kept).strip()
    if not out or len(out) < 20:           # если вырезали почти всё — безопасный фолбэк
        out = ("Здравствуйте! Уточните, пожалуйста, точную модель вашего принтера — и мы подберём "
               "подходящий картридж и подскажем артикул.")
    return re.sub(r"\s{2,}", " ", out), True


def _repair_denial(reply, offer):
    """Убирает предложения с ложным «наличия нет» / уводом «на сторону» и дописывает реальный листинг."""
    base = _strip_denial(reply)
    return (base.rstrip(" .") + f". Для вашего принтера у нас есть подходящий картридж — "
            f"{offer['ref']}, ссылка {offer['url']}")


def _strip_denial(reply):
    kept = [s for s in re.split(r"(?<=[.!?])\s+", reply or "")
            if not _DENY_AVAIL_RX.search(s) and not _REDIRECT_RX.search(s)]
    base = " ".join(kept).strip()
    return base if len(base) >= 15 else "Здравствуйте!"


# как называется чат площадки — покупателю говорим на языке ЕГО канала
_CHAT_NAME = {"wb": "в чат на Wildberries", "ozon": "в чат на Ozon", "yandex": "в чат на Яндекс.Маркете"}


def _ask_in_chat(reply, platform):
    """Своего листинга в канале покупателя нет. Ссылку на ЧУЖУЮ площадку не даём (там он не купит),
    но и молча отказывать нельзя — зовём уточнить у нас в чате, где подберём вручную."""
    base = _strip_denial(reply).rstrip(" .")
    return (base + f". Подходящий вариант подберём вручную: напишите нам, пожалуйста, "
            f"{_CHAT_NAME.get(platform, 'в чат')} и укажите точную модель принтера.")


# URL до первого символа вне безопасного набора; служебный текст/кириллица/U+FFFC внутрь не попадают
_URL_RX = re.compile(r"https?://[^\s<>\"'«»]+")
_URL_SAFE_RX = re.compile(r"[^A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]")


def _scrub_urls(text):
    """Ссылка не должна слипаться с пунктуацией и служебным текстом.

    Инцидент 01.08.2026: покупателю ушла ссылка вида «…/detail.aspx.￼источник» — точка предложения
    и пометка источника оказались ВНУТРИ URL, автолинк захватил их, ссылка не открывалась.
    Режем URL по первому символу вне безопасного набора (кириллица, U+FFFC и пр.) и снимаем
    хвостовую пунктуацию; после ссылки гарантируем пробел."""
    def fix(m):
        u, tail = m.group(0), ""
        cut = _URL_SAFE_RX.search(u)
        if cut:
            u, tail = u[:cut.start()], u[cut.start():]     # хвост не теряем — отделяем пробелом
        return u.rstrip(".,;:!?)»") + (" " + tail.lstrip("￼ \t") if tail.strip("￼ ") else "")
    out = _URL_RX.sub(fix, text or "")
    return re.sub(r"\s{2,}", " ", out).strip()


def _our_offer(reply, question, product_name, models, platform, card_code=None, item_id=None,
               account=None):
    """Каталог-после-веба: ищем НАШ листинг по кодам НУЖНОГО картриджа из веб-ответа, а если по коду не
    нашли — по модели принтера из вопроса. Уважает цвет из вопроса. Коды берём ТОЛЬКО из «положительных»
    фраз (не из «X не подходит» — там наш текущий несовместимый) и исключаем код/листинг текущей карточки."""
    from reports.catalog import catalog_by_code, catalog_offer, _detect_color
    color = _detect_color(question or "")
    pos = " ".join(s for s in re.split(r"(?<=[.!?])\s+", reply or "")
                   if not re.search(r"не\s+подход|не\s+подойд|не\s+совмест|не\s+взаимозамен|"
                                    r"разн\w+\s+(?:сери|поколен|устройств)", s, re.I))
    off = catalog_by_code(pos, platform=platform, color=color, account=account,
                          exclude=[card_code] if card_code else None, exclude_id=item_id,
                          context=(question or "") + " " + (product_name or ""))
    if not off:
        off = catalog_offer(question or "", product_name or "", models, platform, account=account)
    return off


def _fix_producer(reply, question):
    """Вопрос о производителе/стране: DeepSeek уклоняется («производителя не указываем»), а клиент требует
    прямой ответ. Если ответ не назвал происхождение — вырезаем уклончивую фразу и подставляем «Китай».
    В контексте вопроса о производителе ЛЮБОЙ уклон (не сообщаем/напишите нам) — это и есть уклонение,
    вырезаем его, чтобы не получить противоречие «не сообщаем… Производство Китай»."""
    if not _PRODUCER_Q_RX.search(question or "") or _HAS_ORIGIN_RX.search(reply or ""):
        return reply
    kept = [s for s in re.split(r"(?<=[.!?])\s+", reply or "")
            if not (_EVASIVE_PROD_RX.search(s) or _DEFER_RX.search(s)
                    or re.search(r"не\s+сообща|не\s+раскрыва|эту\s+информац", s, re.I))]
    base = " ".join(kept).strip().rstrip(" .!?")
    if len(base) < 12:
        base = "Здравствуйте"
    return (base + ". Производство — Китай: это качественный совместимый аналог "
            "(не оригинал), полностью готовый к печати.")


def _presale_scrub(reply, r):
    """Пред-продажный вопрос: у покупателя нет коробки/чека — убираем ссылку на QR на упаковке/в чеке."""
    if r["kind"] != "question":
        return reply
    body = r["body"] or ""
    if _OWNED_RX.search(body) or DEFECT_RX.search(body):   # уже купил/использует, есть проблема — QR уместен
        return reply
    if _QR_RX.search(reply):
        reply = _QR_RX.sub("в нашем чате", reply)
        reply = re.sub(r"в\s+чат\w*\s+в\s+нашем\s+чате", "в нашем чате", reply, flags=re.I)
        reply = re.sub(r"\s{2,}", " ", reply).replace(" ,", ",").replace(" .", ".").strip()
    return reply


# ВНУТРЕННИЕ ОГОВОРКИ. «В карточке характеристика не указана», «в описании нет данных» — это наша
# кухня: покупателю она ничего не даёт и читается как отписка. Правило Сергея (28.07): в ответе либо
# ФАКТ, либо предложение уточнить у нас в чате. Промпт это запрещает (см. feedback_llm.SYSTEM), но
# модель срывается — поэтому детерминированная зачистка ПОСЛЕ генерации.
#   • КОРОТКУЮ клаузу-оговорку («по модели C2504 данных нет», «у нас в карточке нет») вырезаем;
#     длинную содержательную клаузу не трогаем — в ней обычно факт, а не отписка (там только чистим
#     ссылку на источник), иначе зачистка съедает половину ответа;
#   • ссылку на источник («в карточке», «в характеристиках») вырезаем даже у ПОЛОЖИТЕЛЬНОГО факта
#     («в карточке подтверждена совместимость» → «подтверждена совместимость»);
#   • подлежащее вырезанной оговорки («Точных дат, указанных на упаковке партии, …нет») уносим вместе
#     с ней: без своего сказуемого голова предложения превращается в обрубок;
#   • если что-то вырезали и приглашения написать нам в ответе не осталось — дописываем его.
# Источник — только про НАШУ кухню: карточка, характеристики, «описание товара», наши данные/база.
# «в описании комплекта» и прочие описания, которые видит сам покупатель, не трогаем.
_SRC_REF_RX = re.compile(r"\s*(?:у\s+нас\s+)?(?:в|по)\s+(?:наш\w+\s+)?"
                         r"(?:карточк\w+|характеристик\w+|описани\w+\s+товара|данных|базе)\s*", re.I)
_NODATA_RX = re.compile(r"не\s+указан\w*|не\s+уточн[яё]\w*|не\s+прописан\w*|"
                        r"нет\s+(?:данных|информац\w*|сведений)|данн\w*\s+нет|информац\w*\s+нет|"
                        r"отсутству\w*\s+(?:данн|информац|сведени)", re.I)
_PREDICATE_RX = re.compile(r"\b\w{2,}(?:ем|ешь|ет|ете|ут|ют|ит|ят|им|ите|ла|ло|ли|на|но|ны|ся|сь)\b", re.I)
_HAS_INVITE_RX = re.compile(r"напиш\w+|уточните|обратитесь|свяжитесь|в\s+чат", re.I)
_INVITE = "Напишите нам в чат — уточним и подскажем."
_CAVEAT_MAX_WORDS = 9                                      # длиннее — это уже содержательная фраза


def _is_caveat(clause):
    """Клауза-оговорка: явное «нет данных / не указано» или ссылка на источник с отрицанием
    («у нас в карточке нет» — отрицание голое, шаблоном _NODATA_RX не ловится). Только короткие:
    в длинной клаузе рядом с оговоркой обычно стоит факт, ради которого ответ и написан."""
    if len(clause.split()) > _CAVEAT_MAX_WORDS:
        return False
    return bool(_NODATA_RX.search(clause)
                or (_SRC_REF_RX.search(clause) and re.search(r"\bнет\b|\bне\s", clause)))


def _has_own_predicate(text):
    """Кусок предложения читается самостоятельно: есть глагольная форма, число или тире-сказуемое
    («ресурс 1500 страниц», «производство — Китай»). Без этого — обрубок вроде «Точных дат»."""
    return bool(_PREDICATE_RX.search(text) or re.search(r"\d|\s[—–]\s", text))


def _no_internal_caveats(reply):
    """Вырезать внутренние оговорки про источник данных. → очищенный текст.

    Предложение, которое НЕ трогали, остаётся как есть (иначе тест на обрубок съедал бы
    «Здравствуйте!»)."""
    if not reply or reply.lstrip().startswith("⚠️"):       # маркер «на человека» — служебный, не текст
        return reply
    if not (_NODATA_RX.search(reply) or _SRC_REF_RX.search(reply)):
        return reply
    kept_sents, cut = [], False
    for sent in re.split(r"(?<=[.!?])\s+", reply.strip()):
        toks = re.split(r"(\s*[—–]\s*|,\s+)", sent)       # чётные — клаузы, нечётные — разделители
        clauses, seps = toks[0::2], toks[1::2]
        bad = [i for i, cl in enumerate(clauses) if _is_caveat(cl)]
        keep = [i for i in range(len(clauses)) if i not in bad]
        if bad:
            # голова предложения до первой оговорки без своего сказуемого = её подлежащее, уносим тоже
            head = [i for i in keep if i < bad[0]]
            if head and not _has_own_predicate(" ".join(clauses[i] for i in head)):
                keep = [i for i in keep if i not in head]
        parts = []
        for i in keep:
            parts.append(("" if not parts else (seps[i - 1] if i - 1 < len(seps) else ", ")) + clauses[i])
        rest = "".join(parts).strip(" ,;—–")
        touched = bool(bad)
        if _SRC_REF_RX.search(rest):
            rest = _SRC_REF_RX.sub(" ", rest).strip(" ,;—–")
            touched = True
        if not touched:
            kept_sents.append(sent)
            continue
        cut = True
        rest = re.sub(r"^(?:но|однако|а|и|при\s+этом|хотя)\s+", "", rest.strip(), flags=re.I)
        rest = re.sub(r"\s{2,}", " ", rest).replace(" ,", ",").replace(" .", ".").strip(" ,;—–")
        if not rest or (bad and not _has_own_predicate(rest)):
            continue                                       # обрубок — сносим предложение целиком
        if not re.search(r"[.!?]$", rest):
            rest += sent[-1] if sent[-1] in "!?" else "."
        kept_sents.append(rest[0].upper() + rest[1:])
    out = " ".join(kept_sents).strip()
    if cut and out and not _HAS_INVITE_RX.search(out):
        out = out.rstrip() + " " + _INVITE
    return out


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# КАЧЕСТВО ОТВЕТА НА ВОПРОС — правила Сергея 12.08.2026. Три класса нарушений, которые модель
# допускает регулярно, а покупатель читает как ложь или воду:
#
#  1) ПРИПИСКА КАРТОЧКЕ. «в карточке указано», «производитель указывает», цитата «…» как факт нашей
#     карточки — разрешены ТОЛЬКО если факт реально есть в CARD_DATA. Инцидент 12.08 (вопрос Яндекса
#     про фотобарабан 44844408, OKI C822): покупателю ушло «товара действительно есть предупреждение
#     "Не идёт в аппарат C822"» плюс выдуманное «по нашему опыту … может работать некорректно».
#     Предупреждение — текст ВНУТРЕННЕГО названия МС («*ВНИМАНИЕ* … Не идёт в аппарат C822»), которое
#     попадает в промпт через product_name; в CARD_DATA (наша карточка) его нет, а все четыре
#     веб-источника из grounding говорили обратное. Веб-факт карточке не приписываем никогда:
#     не подтверждается CARD_DATA — предложение вырезаем целиком.
#  2) ВЫДУМАННЫЙ ОПЫТ. «по нашему опыту», «мы сталкивались», «по отзывам покупателей» — у движка
#     опыта нет, это выдумка от первого лица магазина.
#  3) ВОДА. Рассуждательные связки («Уточним важный момент», «Что касается…»), не-прямой ответ на
#     вопрос «да/нет» и длина: жёсткий лимит 500 знаков (URL не считаем — их читатель не читает),
#     ориентир ~300.
# ═══════════════════════════════════════════════════════════════════════════════════════════════
_ATTRIB_RX = re.compile(
    r"(?:в|на|по|согласно)\s+(?:наш\w+\s+)?(?:карточк\w+|описани\w+|характеристик\w+|аннотац\w+)"
    r"(?:\s+товара)?\s*(?:указан\w*|прописан\w*|заявлен\w*|сказан\w*|отмечен\w*|стоит|есть)?|"
    r"(?:указан\w*|прописан\w*|заявлен\w*|написан\w*|отмечен\w*)\s+(?:в|на)\s+"
    r"(?:наш\w+\s+)?(?:карточк|описани|характеристик)\w+|"
    r"производител\w*\s+(?:указыва|заявля|отмеча|пиш|сообща|предупрежда|не\s+рекоменд)\w*|"
    r"по\s+данным\s+производител\w+", re.I)
# остаток приписки после _no_internal_caveats: «(в карточке) товара есть предупреждение «…»»
_CARD_CLAIM_RX = re.compile(r"товара\s+(?:действительно\s+)?(?:есть|указан\w*|содержится|стоит)|"
                            r"предупрежден\w+|пометк\w+\s+«", re.I)
_QUOTE_RX = re.compile(r"[«\"]([^»\"]{4,160})[»\"]")
_TOKEN_RX = re.compile(r"\b[A-Za-z]{0,4}-?\d{2,5}[A-Za-z]{0,3}\b")
_OUR_EXP_RX = re.compile(
    r"по\s+наш\w+\s+опыту|мы\s+сталкива\w+|на\s+наш\w+\s+практик\w+|наш\s+опыт\s+показыва\w*|"
    r"как\s+показывает\s+(?:наша|наш)\s+\w+|мы\s+(?:замеча|наблюда|отмеча|счита|полага)\w+|"
    r"нам\s+известн\w+\s+случа\w+|по\s+отзывам\s+(?:наших\s+)?покупател\w+|"
    r"по\s+наш\w+\s+(?:данным|наблюдени\w+|мнени\w+)", re.I)
_FILLER_RX = re.compile(
    r"^\s*(?:уточним\s+важный\s+момент|важный\s+момент|уточним|поясним|отметим|стоит\s+отметить|"
    r"хотим\s+отметить|хотелось\s+бы\s+отметить|важно\s+понимать|важно\s+отметить|"
    r"обратите\s+внимание|что\s+касается\s+[^,—–:.!?]{1,45})\s*[,:—–-]*\s*(?:что\s+)?", re.I)
_YESNO_Q_RX = re.compile(r"\bли\b|подойд[её]т|подход\w+|совмест\w+|можно\s+л|встан\w+\s+л|"
                         r"\bгодит\w+|\bесть\s+л", re.I)
_DIRECT_RX = re.compile(r"^\s*(?:да|нет|зависит)\b", re.I)
_NEG_ANS_RX = re.compile(r"\bне\s+(?:подойд|подход|совмест|встан|получ|стои|рекоменд)\w*|"
                         r"\bнельзя\b|не\s+смож\w+|отсутству\w+\s+в\s+списк", re.I)
# «совместимость» (существительное) положительным ответом НЕ считается: «информацией о совместимости
# не располагаем» — это отказ, а не «да» (инцидент на вопросе про насадки Rowenta)
_POS_ANS_RX = re.compile(r"\b(?:подойд[её]т|подходит|подойдут|совместим(?!ост)\w*|встанет|"
                         r"можно\s+(?:использ|ставить|устанавл)|да,)\b", re.I)
_GREET_RX = re.compile(r"^\s*(?:здравствуйте|добрый\s+день|доброе\s+утро|добрый\s+вечер|"
                       r"приветству\w+)[!,.\s]*$", re.I)
_LEN_HARD = 500                                    # жёсткий потолок видимого текста (без URL)


def _nrm(s):
    return re.sub(r"[^a-zа-яё0-9]", "", (s or "").lower())


def _sents(text):
    return [s for s in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if s.strip()]


def _visible_len(text):
    """Длина без URL: ссылка на наш листинг — служебная нагрузка, покупатель её не читает."""
    return len(_URL_RX.sub("", text or "").strip())


def _card_attrib_guard(reply, card_text):
    """Приписка факта нашей карточке/производителю. → (reply, cut).

    Предложение со ссылкой на карточку/производителя или с цитатой из неё остаётся ТОЛЬКО если всё,
    что в нём названо (коды, модели, числа, сама цитата), есть в CARD_DATA. Подтверждённый факт
    оставляем, но саму ссылку на источник вырезаем — покупателю наша кухня не нужна.
    Не подтверждено — предложение уходит целиком: недоказанный веб-факт в ответ не попадает."""
    if not reply or reply.lstrip().startswith("⚠️"):
        return reply, False
    cn = _nrm(card_text)
    kept, cut = [], False
    for sent in _sents(reply):
        attrib = bool(_ATTRIB_RX.search(sent))
        quotes = _QUOTE_RX.findall(sent) if _CARD_CLAIM_RX.search(sent) else []
        if not attrib and not quotes:
            kept.append(sent)
            continue
        toks = {_nrm(t) for t in _TOKEN_RX.findall(sent)}
        toks = {t for t in toks if len(t) >= 3}
        ok = bool(cn) and all(t in cn for t in toks) and all(_nrm(q) in cn for q in quotes)
        if not ok:
            cut = True
            continue
        rest = _ATTRIB_RX.sub(" ", sent)
        rest = re.sub(r"^\s*[,:;—–-]+\s*", "", re.sub(r"\s{2,}", " ", rest)).strip(" ,;")
        rest = re.sub(r"^(?:что|и|а|но)\s+", "", rest, flags=re.I).strip()
        if len(rest) < 12:
            cut = True
            continue
        if not re.search(r"[.!?]$", rest):
            rest += "."
        kept.append(rest[0].upper() + rest[1:])
        cut = cut or rest != sent
    out = re.sub(r"\s{2,}", " ", " ".join(kept)).strip()
    return out, cut


def _quality_guard(reply, question):
    """Правила качества ответа на вопрос. → (reply, [нарушения]).

    Порядок: выдуманный опыт → связки → прямой ответ первой фразой → длина."""
    if not reply or reply.lstrip().startswith("⚠️"):
        return reply, []
    viol, sents = [], _sents(reply)
    keep = [s for s in sents if not _OUR_EXP_RX.search(s)]
    if len(keep) != len(sents):
        viol.append("выдуманный опыт")
    out = []
    for s in keep:
        s2 = _FILLER_RX.sub("", s).strip()
        if s2 != s.strip():
            viol.append("связка")
            if s2:
                s2 = s2[0].upper() + s2[1:]
        if s2:
            out.append(s2)
    # ПРЯМОЙ ОТВЕТ. Вопрос «да/нет» → первая содержательная фраза начинается с Да/Нет/Зависит.
    # Полярность берём из самой фразы: додумывать вердикт за модель нельзя, поэтому если фраза
    # не заявляет ни «подойдёт», ни «не подойдёт» — только помечаем нарушение для модератора.
    if out and _YESNO_Q_RX.search(question or ""):
        i = 1 if (_GREET_RX.match(out[0]) and len(out) > 1) else 0
        first = out[i]
        if not _DIRECT_RX.match(first):
            # ТЕКСТ НЕ ТРОГАЕМ (10.09.2026). До этой даты guard сам вписывал в ответ «Да,»/«Нет,»,
            # и на замере причин блоков это оказалось источником собственного брака: «Нет,
            # технически заправить можно…», «Нет, по самой модели — да…» — машина ставила
            # полярность впереди фразы, которая говорила обратное. Полярность ответа знает только
            # автор ответа; наше дело — сказать модератору, что первой фразой прямого «да/нет» нет,
            # и подсказать, на что это похоже. Решение принимает человек.
            scope = " ".join(out[i:i + 2])
            hedged = bool(re.search(r"\bтолько\b|\bлишь\b|не\s+заявлен|уточните|не\s+гарант|"
                                    r"не\s+можем|если\s+это|к\s+сожалению|не\s+распола|"
                                    r"не\s+уверен|не\s+знаем", scope, re.I))
            head = ("Нет" if _NEG_ANS_RX.search(scope) else
                    "Да" if (_POS_ANS_RX.search(first) and not hedged) else None)
            viol.append("не прямой ответ (текст не изменён"
                        + (f", похоже на «{head}»" if head else ", полярность не определена") + ")")
    # ДЛИНА. Лимит держит промпт на генерации; здесь — страховка: снимаем ТОЛЬКО хвостовые
    # предложения и только если в них нет ссылки/артикула (это конверсия, её резать нельзя).
    # Резать середину нельзя тем более: там обычно и лежит ответ на вопрос (инцидент mid=19 —
    # обрезка выкинула абзац про возврат, ради которого вопрос и задан). Не влезли — не калечим
    # текст, а помечаем нарушение: длину поправит оператор или следующая генерация.
    def _txt(parts):
        return re.sub(r"\s{2,}", " ", " ".join(parts)).strip()
    payload = re.compile(r"https?://|артикул|\bSKU\b", re.I)
    while (_visible_len(_txt(out)) > _LEN_HARD and len(out) > 2
           and not payload.search(out[-1])):
        out.pop()
        if "длина" not in viol:
            viol.append("длина")
    res = _txt(out)
    if _visible_len(res) > _LEN_HARD and "длина" not in viol:
        viol.append(f"длина {_visible_len(res)} знаков — править вручную")
    return res, viol


_QA_FALLBACK = ("Здравствуйте! Уточните, пожалуйста, точную модель вашего принтера — и мы подберём "
                "подходящий картридж и подскажем артикул.")


def _qa_guards(reply, card_text, question):
    """Единая точка правил качества для ВОПРОСОВ: приписка карточке + качество текста.
    Зовётся последней, после всех обогащений (в т.ч. веб-добора без карточки). → (reply, [нарушения])."""
    viol = []
    reply, cut = _card_attrib_guard(reply, card_text)
    if cut:
        viol.append("приписка карточке")
    reply, qv = _quality_guard(reply, question)
    viol += qv
    if len((reply or "").strip()) < 20:
        reply = _QA_FALLBACK
        viol.append("фолбэк после чистки")
    return reply, viol


# ДОМЕН-ФИЛЬТР. Наш профиль — картриджи/расходники печати.
#   • НЕ расходник → жёстко на человека, всегда (ответ по общему знанию запрещён — это не наша тема).
#   • Расходник БЕЗ данных карточки → раньше тоже уходил на человека, и это была ложная отбраковка:
#     реальный кейс CF540A-CF543A (вопрос про M254nw, wb_acc2) — товар профильный, а CARD_DATA пуст
#     только потому, что контент карточек Дисквэра мы не собирали. Теперь такой вопрос идёт обычной
#     цепочкой каталог → веб (DeepSeek) → сборка Opus, с пометкой «без данных карточки, проверьте
#     внимательнее» в карточке модератора; на человека — только если цепочка ничего не дала.
_CONSUMABLE_RX = re.compile(
    r"картридж|тонер|чернил|фото[-\s]?барабан|\bбарабан\b|драм|\bdrum\b|туб[аы]|фьюзер|"
    r"термопл[её]нк|термопленк|девелопер|снпч|заправк|риббон|печат\w*\s*головк|ракель|"
    r"\bролик\b|блок\s+проявк|узел\s+закреп|скребок|\bчип\b|cartridge|toner", re.I)


def _is_consumable(product_name):
    return bool(_CONSUMABLE_RX.search(product_name or ""))


NO_CARD_NOTE = "без данных карточки, проверьте внимательнее"


def _card_facts(cf, r):
    """Карточка товара под площадку строки (у Яндекса item_id = offerId, а не nmID)."""
    try:
        return (cf.for_ozon(r["item_id"]) if r["platform"] == "ozon" else
                cf.for_yandex(r["item_id"]) if r["platform"] == "yandex" else
                cf.for_wb(r["item_id"]))
    except Exception:
        return None


def _cache_answer(r, cf, cls, cc="", client=None):
    """Блок G: готовый утверждённый ответ по нашему артикулу вместо новой генерации.
    → (outd, reply, route, conf, ground, False, False) либо None.

    Маршрут всегда review: автопубликации вопросов в движке нет вовсе (аудит 25.08.2026), и кэш
    её не вводит — он убирает повторную генерацию, а не человека. «Первые две подстановки с
    пометкой, дальше ALLOW» из брифа — про машинный вердикт publish_gate, а не про кнопку ✅."""
    hit = ac.try_hit(r, _card_facts(cf, r), cls=cls)
    if not hit:
        return None
    reply, ground = hit
    # Правила качества прогоняем и по тексту из кэша (замечание №4 ревью 08.09.2026): ранний
    # возврат не имеет права быть дырой в обход _qa_guards/_scrub_urls. Утверждён ответ был под
    # прежний вопрос — под новым тем же ключом приписка карточке может уже не соответствовать.
    reply, qviol = _qa_guards(reply, cc, r["body"])
    if qviol:
        ground["qa_guard"] = qviol
        ground["note"] = "qa-guard: " + ", ".join(qviol) + "; " + (ground.get("note") or "")[:180]
    reply = _scrub_urls(reply)
    # п.9 (21.09.2026): кэш — законный источник автопубликации карточного класса, но «контролёр
    # остаётся». Подтверждаемые подстановки (первые две, семейство кода, устаревшие) и так идут
    # человеку — на них вызов контролёра не тратим.
    _c = ground.get("cache") or {}
    if client is not None and not (_c.get("confirm") or _c.get("stale") or _c.get("family")):
        reply, ground = _verified(client, r, reply, cc, ground)
    conf = 0.9
    outd = dict(r, cat="question", reply=reply, route="review", conf=conf, card=cc,
                note=ground.get("note"), grounded=True, catalog=False, source=ground["source"],
                web=False, sources=[], intent=intent(r["body"]))
    return outd, reply, "review", conf, ground, False, False


def _early_human(r, cc, reply, note, used_llm):
    """Ранний возврат «на человека» с маркером-черновиком (домен-фильтр / ошибка парсинга).
    Маркер попадёт в очередь модерации, но отправку кнопкой ✅ бот для route=human блокирует."""
    ground = {"llm": used_llm, "grounded": False, "source": "—", "route": "human", "note": note}
    outd = dict(r, cat="question", reply=reply, route="human", conf=0, card=cc,
                note=note, grounded=False, catalog=False, source="—", web=False, sources=[],
                intent=intent(r["body"]) if r["kind"] == "question" else "")
    return outd, reply, "human", 0, ground, used_llm, False


def _polish_with_web(client, r, cf, corpus, wf, base_reply, ground):
    """Пересобрать ответ на Sonnet, отдав веб-факты ОТДЕЛЬНЫМ помеченным блоком (пункт 4).

    До этого веб-ответ подставлялся покупателю как есть — чужим текстом и чужим тоном, а карточка
    в нём не участвовала. Теперь веб идёт во входные данные модели с пометкой «источник вторичный».

    Проход СИНХРОННЫЙ даже при включённом батче, и это осознанно: веб-запрос уже оплачен, а
    отложить сборку значит на следующем цикле сходить в веб заново (и получить другой текст —
    другой ключ очереди, то есть запись, которая ждёт вечно). Сбой прохода не рушит ответ:
    остаётся тот вариант, что был.
    """
    try:
        with llm_batch.synchronous():
            d2, cc2, model2 = _llm(client, r, cf, corpus, web_facts=wf)
    except Exception as e:
        ground["note"] = f"веб→модель не отработал ({type(e).__name__}); " + (ground.get("note") or "")[:200]
        return base_reply, ground
    txt = (d2.get("reply") or "").strip()
    if not txt or d2.get("route") == "human":
        return base_reply, ground
    ground.update({"model": model2, "web_into_llm": True,
                   "grounded": bool(d2.get("grounded")) or ground.get("grounded", False)})
    return txt, ground


def _verified(client, r, reply, card, ground):
    """Проверяющий проход по ГОТОВОМУ черновику (пункт 3). → (текст, ground).

    Текст не правит: вердикт FIX только помечает черновик и уводит на человека. Править ответ
    второй моделью — это ещё одна генерация без источника фактов, ровно то, что запрещено
    правилом об источнике факта; здесь нужен контролёр, а не соавтор.
    Сбой контролёра черновик не рушит: молча остаёмся с тем, что собрал движок."""
    if not VERIFY_ON or not (reply or "").strip():
        return reply, ground
    q = r.get("body") or r.get("cons") or r.get("pros")
    try:
        # ВХОДНЫЕ ДАННЫЕ контролёра = карточка + справочник бренда. Без второго слагаемого контролёр
        # резал как выдумку ровно то, что мы про серию знаем достоверно («чип одноразовый»,
        # «гарантия на заправку не распространяется») — см. прогон oz_acc2 от 10.09.2026.
        bn_block, _ = bn.facts_for(_brand_of(r), q, card)
        inputs = (card or "") + ("\n\n" + bn_block if bn_block else "")
        verdict, note, usage = llm_routing.verify(
            lambda **kw: _create(client_for(VERIFY_MODEL), **kw), VERIFY_MODEL,
            r["kind"], q, reply, inputs)
        _COST.add(VERIFY_MODEL, usage)
    except Exception as e:
        ground = dict(ground, verify="skip", note=(ground.get("note") or "")[:220]
                      + f"; контролёр не отработал ({type(e).__name__})")
        return reply, ground
    ground = dict(ground, verify=verdict)
    if verdict == "FIX":
        ground["route"] = "human"
        ground["note"] = f"контролёр: {note or 'ответ не прошёл проверку'}; " + (ground.get("note") or "")[:200]
    return reply, ground


def _answer(client, r, cf, corpus):
    """Полный движок ответа на ОДИН элемент. → (out_dict, reply, route, conf, ground, used_llm, used_web)."""
    used_web = False
    no_card = False
    review_len_route = False       # отзыв попал на модель по длине текста → маршрут только человеку
    # Домен-фильтр (только вопросы). НЕ расходник → на человека сразу, БЕЗ вызова модели.
    # Расходник без CARD_DATA → пропускаем в цепочку, но помечаем (no_card) для карточки модератора.
    if r["kind"] == "question":
        cc0 = _card_data(r, cf)
        if not r.get("product_name") and r["platform"] == "yandex":
            # имя оффера на витрине Маркета (его видит покупатель) → карточка-двойник ВБ. Пустое имя
            # отправило бы вопрос на человека домен-фильтром. МС здесь не спрашиваем: external_code
            # не уникален и отдаёт произвольный бренд из восьми позиций (инцидент 6806, 13.08.2026).
            rows = db.query("""SELECT COALESCE(NULLIF(payload->'mapping'->>'marketSkuName', ''),
                                               NULLIF(payload->'offer'->>'name', '')) AS n
                               FROM raw_yandex_offer WHERE offer_id=%s LIMIT 1""", (str(r["item_id"]),))
            r["product_name"] = ((rows[0]["n"] if rows else None)
                                 or (cf.for_yandex(r["item_id"]) or {}).get("name") or None)
        if not _is_consumable(r.get("product_name")):
            marker = ("⚠️ Вне профиля (товар не расходник печати) — ответ по общему знанию запрещён, "
                      "нужен ручной ответ оператора.")
            return _early_human(r, cc0, marker, "домен-фильтр: не расходник", used_llm=False)
        no_card = not (cc0 and cc0.strip())
        # G (08.09.2026): кэш утверждённых ответов — ДО генерации, сразу после классификации.
        # Попадание = ответ, который человек уже утвердил по этому же артикулу и тому же вопросу;
        # модель не зовём вовсе. Домен-фильтр отработал выше: не расходник в кэш не попадёт.
        _cls0 = rc.classify(r.get("body"), kind="question", rating=r.get("rating"))
        _cached = _cache_answer(r, cf, _cls0, cc0, client=client)
        if _cached:
            return _cached
    if r["kind"] == "question":
        # Пункт 3 (10.09.2026): модель зовём только там, где ответ надо СОБРАТЬ. Простой вопрос,
        # на который карточка отвечает буквально (чип / ресурс / состав набора), собирается
        # детерминированно — как и было до появления LLM в этой ветке. Нет нужного факта в
        # карточке — не выдумываем: вопрос идёт дальше по общей цепочке (каталог, справочник, веб).
        _need, _why = llm_routing.needs_llm("question", r.get("body"), _cls0, r.get("rating"))
        if not _need:
            _f0 = (cf.for_ozon(r["item_id"]) if r["platform"] == "ozon" else
                   cf.for_yandex(r["item_id"]) if r["platform"] == "yandex" else
                   cf.for_wb(r["item_id"])) or {}
            _chip_ln = sku_relations.chip_line(r["platform"], r.get("account"), r.get("item_id"),
                                               r.get("body"), _f0.get("chip"))
            _txt0, _mark = llm_routing.card_answer(_cls0, r.get("body"), _f0, chip_line=_chip_ln)
            if _txt0:
                _g0 = {"llm": False, "grounded": True, "source": "карточка", "template": True,
                       "template_id": "card_" + (_cls0 or "simple"), "route": "review",
                       "note": f"без модели ({_why}); {_mark}"}
                _txt0, _g0 = _verified(client, r, _txt0, cc0, _g0)
                _route0 = _g0.get("route", "review")
                _out0 = dict(r, cat="question", reply=_txt0, route=_route0, conf=0.8, card=cc0,
                             note=_g0["note"], grounded=True, catalog=False, source="карточка",
                             web=False, sources=[], intent=intent(r["body"]))
                return _out0, _txt0, _route0, 0.8, _g0, False, False
        used_llm = True
    else:
        _txt = (r["body"] or "") + " " + (r["pros"] or "") + " " + (r["cons"] or "")
        _neg = (r["rating"] or 5) <= 3
        # LLM — только для положительных отзывов с вопросом/проблемой по сути; обычный позитив и
        # негатив → шаблоны (позитив: разнообразная ротация 16 вариантов; негатив: хендофф по QR)
        used_llm = _has_text(r) and (not _neg) and (bool(DEFECT_RX.search(_txt)) or "?" in _txt)
        # Пункт 3 (10.09.2026): содержательный отзыв (текст > 50 символов) тоже собирает модель —
        # раньше на него шла ротация шаблонов, то есть покупатель получал вежливую фразу мимо того,
        # что написал. Отдельный флаг: такие черновики НЕ уходят автопубликацией — отзывы, в отличие
        # от вопросов, публикуются без человека, и первый раз новый текст обязан увидеть оператор.
        _long_review, _why_r = llm_routing.needs_llm("review", _txt, None, r.get("rating"))
        if _long_review and _has_text(r) and not used_llm:
            used_llm, review_len_route = True, True
    if used_llm:
        # БЕЗ except: сбой вызова (LlmUnavailable после повторов или любое другое исключение)
        # поднимается в run() и запись остаётся без черновика. Подстановка текста ошибки в reply
        # запрещена — см. LlmUnavailable.
        d, cc, used_model = _llm(client, r, cf, corpus)
        # Битый JSON модели → _llm вернул route=human с маркером: на человека, БЕЗ обогащения/утечек.
        if r["kind"] == "question" and d.get("route") == "human":
            return _early_human(r, cc, (d.get("reply") or "").strip(),
                                d.get("note") or "ошибка парсинга", used_llm=True)
        reply = (d.get("reply") or "").strip()
        route = "auto" if d.get("route") == "auto" else "review"
        if review_len_route:
            route = "review"
        conf = float(d.get("confidence") or 0)
        ground = {"llm": True, "grounded": bool(d.get("grounded")), "note": (d.get("note") or "")[:300],
                  "model": used_model, "catalog": "КАТАЛОГ" in (cc or ""), "source": _llm_source(d)}
        if review_len_route:
            ground["note"] = f"{_why_r} — на вычитку оператору; " + (ground.get("note") or "")[:220]
        cat = "question" if r["kind"] == "question" else "review-text"
        # A1.3 (07.09.2026): след совместимости — какие модели спросили и какие из них подтверждает
        # карточка. Пишем ДО веток обогащения, чтобы след был у любого вопроса с моделью, а не только
        # у тех, что ушли в компат-ветку; ниже компат-ветка перезапишет его точным _fam_status().
        if r["kind"] == "question":
            _asked0 = _asked_models(r["body"] or "")
            if _asked0:
                _cn0 = _norm(cc or "")
                _mm0 = [a for a in _asked0 if _cn0 and _norm(a) in _cn0]
                ground["compat"] = {"asked": _asked0, "matched": _mm0,
                                    "status": "yes" if _mm0 else ("no_data" if not _cn0 else "unknown")}
        # СОВМЕСТИМОСТЬ: карточка-семья (вариант серии) → прямой ответ; регуляторный код или модель
        # вне карточки → веб (источник №3, объяснит напр. L662B = европейское обозначение CX17NF)
        if r["kind"] == "question" and (intent(r["body"]) == "совместимость модели"
                                        or _is_compat_q(r["body"])):
            fct = (cf.for_ozon(r["item_id"]) if r["platform"] == "ozon" else
                   cf.for_yandex(r["item_id"]) if r["platform"] == "yandex" else
                   cf.for_wb(r["item_id"]))     # у Яндекса item_id = offerId, а не nmID
            code = (fct or {}).get("code")
            st, mm = _fam_status(r["body"], (fct or {}).get("models") or [])
            asked_m = _asked_models(r["body"])
            # D2 (блок D, 08.09.2026): справочник compat_ref — ВТОРОЙ и последний законный источник
            # «да» после карточки. Он детерминированный: строки заводятся одобренными ответами,
            # командой /compat_add или импортом OEM, веб в него не пишет никогда. Модуль работает
            # офлайн: любая ошибка БД = пустой ответ = подтверждения нет.
            series, ref_ok, ref_src, ref_rows = None, [], "", []
            variant_mismatch, region_needed = None, []
            try:
                from reports import compat_ref as _cr
                series = code or _cr.series_of(fct or {})
                if asked_m:
                    ref_ok, ref_src, ref_rows = _cr.confirms(asked_m, series)
                    variant_mismatch = _cr.variant_conflict(asked_m, series)   # D4
                    regs = _cr.regions_for(series)                             # D5
                    if len(regs) > 1 and not _cr.region_in(r["body"] or ""):
                        region_needed = regs
            except Exception:
                series, ref_ok, ref_src, ref_rows = (code or None), [], "", []
            ref_ok = ref_ok or []
            # п.9 (21.09.2026): ТОЧНОЕ совпадение — каждая спрошенная модель равна модели из списка
            # карточки после нормализации. Подстрока/база серии (_fam_status) для автопубликации мало.
            _cmn = {_norm(m) for m in ((fct or {}).get("models") or [])}
            exact_m = bool(asked_m) and all(_norm(a) in _cmn for a in asked_m)
            if asked_m:                                # точный след: матчер с базовой моделью серии
                # ВНИМАНИЕ: _fam_status при st != 'yes' возвращает вторым значением СПРОШЕННЫЕ модели,
                # а не совпавшие. Записать их как matched = отменить всё правило A1.3/D1.
                ground["compat"] = {"asked": asked_m, "matched": mm if st == "yes" else [], "exact": exact_m,
                                    "status": st, "series": series, "ref_matched": ref_ok,
                                    "ref_source": ref_src, "variant_mismatch": variant_mismatch,
                                    "region_needed": region_needed}
            defect = re.search(r"вернуть|возврат|не\s+счита|не\s+вид|ошибк", (r["body"] or "").lower())
            reg = [x for x in re.findall(r"\b[A-Za-z]\d{3,4}[A-Za-z]\b", r["body"] or "")
                   if _norm(x) not in _norm(cc or "")]
            # ЦВЕТ. Серия-shortcut называет ОДИН generic-код карточки, игнорируя цвет. Если в вопросе указан
            # цвет / задан вопрос о цвете — shortcut небезопасен (CLX-3185 «жёлтый»→не давать чёрный CLT-K407S;
            # Xerox 6700 «на самом деле синий?»). Тогда пропускаем shortcut → LLM+каталог подберут по цвету.
            from reports.catalog import _detect_color as _dc
            color_q = bool(_dc(r["body"] or "")) or bool(re.search(
                r"как\w*\s+цвет|каком\s+цвете|на\s+самом\s+деле|это\s+(?:чёрн|черн|цветн)", r["body"] or "", re.I))
            # п.4 пакета 21.09.2026: хвост «— это вариант серии из списка совместимости карточки» уходил
            # покупателю дословно — внутренняя кухня (класс D3 эталона); за неделю три блока на этой фразе.
            fam_reply = (f"Здравствуйте! Да, подойдёт для {', '.join(mm)}."
                         + (f" Наш картридж — {code}." if code else "")) if mm else ""
            series_mm = _series_members(r["body"], (fct or {}).get("models") or []) if st != "yes" else []
            # shortcut = ТОЛЬКО совместимость. Если в вопросе есть ещё тема (заправка/ресурс/чип/
            # комплектация/гарантия), детерминированный шаблон её проигнорирует — вместо него полный
            # ответ собирает Opus по CARD_DATA/каталогу, а fam_reply идёт ему подсказкой по совместимости.
            extra_topic = bool(_EXTRA_TOPIC_RX.search(r["body"] or ""))
            # D4: ресурсная версия. Справочник знает эту модель под ДРУГИМ вариантом той же серии
            # (OKI/Kyocera/HP LaserJet вниз не совместимы) — отвечаем отказом с нужной версией.
            if variant_mismatch and not defect:
                _th = "/".join(map(str, variant_mismatch.get("theirs") or [])) or "другая"
                reply = (f"Здравствуйте! Нет, для {variant_mismatch.get('model')} нужна версия "
                         f"{_th}, а этот картридж — версия {variant_mismatch.get('ours') or 'другого ресурса'}. "
                         f"Ресурсные версии этой серии между собой не взаимозаменяемы.")
                ground.update({"grounded": True, "source": "compat_ref:" + (ref_src.split(":")[-1] or "manual"),
                               "note": f"D4: несовпадение ресурсной версии по справочнику ({_th})"})
            # D5: серия существует в региональных версиях — без региона в вопросе однозначного
            # ответа не бывает (Epson WF-C5x90: EU T11C/D/E против ASIA T11F/G).
            elif region_needed and not defect:
                reply = ("Здравствуйте! Уточните, пожалуйста, региональную версию вашего аппарата — "
                         "для этой серии выпускаются разные версии картриджей ("
                         + "/".join(map(str, region_needed)) + "), и подходит только своя.")
                ground.update({"grounded": True, "source": "compat_ref:" + (ref_src.split(":")[-1] or "manual"),
                               "note": "D5: нужен регион, серия есть в версиях "
                                       + "/".join(map(str, region_needed))})
            elif st == "yes" and not defect and not color_q and fam_reply and not extra_topic:
                # карточка-серия, вопрос целиком про совместимость: детерминированно и бесплатно — веб не нужен
                reply = fam_reply
                ground.update({"grounded": True, "source": "карточка-серия",
                               "note": f"вариант серии, совпало по базе: {', '.join(mm)}"})
            elif st == "yes" and not defect and not color_q and fam_reply and extra_topic:
                # совместимость подтверждена + доп. тема — полный ответ через Opus с подсказкой по совместимости
                try:
                    d2, _cc2, model2 = _llm(client, r, cf, corpus, hint=fam_reply)
                except Exception as e:
                    d2, model2 = {"reply": ""}, used_model
                reply2 = (d2.get("reply") or "").strip()
                if reply2:
                    reply = reply2
                    ground.update({"grounded": True, "source": "карточка-серия+llm", "model": model2,
                                   "note": f"серия подтверждена ({', '.join(mm)}) + доп. тема через LLM"})
                else:
                    # п.5 пакета 21.09.2026: раньше здесь уходил shortcut по совместимости — «лучше
                    # частичный ответ, чем ничего». Кейс FS-1120D: три вопроса, ответ на нулевой из
                    # них, оператор его почти отправил. Вопрос многосоставный по определению ветки
                    # (совместимость + доп. тема), поэтому без модели — пустой черновик человеку.
                    return _early_human(
                        r, cc, "⚠️ Модель не ответила, а вопрос многосоставный (совместимость + "
                               "ещё тема) — ответ на одну часть здесь хуже, чем никакого. "
                               "Нужен ручной ответ оператора.",
                        f"LLM без ответа на многосоставный вопрос; совместимость по карточке: "
                        f"{', '.join(mm)}", used_llm=True)
                route = "review"
            elif (not defect and asked_m and ref_ok
                  and all(a in ref_ok for a in asked_m) and not extra_topic and not color_q):
                # D2: все спрошенные модели подтверждены справочником — детерминированный ответ,
                # веб не нужен. Именно так «подойдёт к LBP633», одобренное однажды оператором,
                # со второго раза закрывается бесплатно и одинаково.
                reply = (f"Здравствуйте! Да, подойдёт для {', '.join(ref_ok)}"
                         + (f" — наш картридж {code}." if code else ".")
                         + " Совместимость подтверждена нашим справочником.")
                ground.update({"grounded": True, "source": ref_src,
                               "note": "подтверждено справочником совместимости: " + ", ".join(ref_ok)})
            elif series_mm and not defect and not color_q and not extra_topic:
                # п.7: названа серия без модели — перечисляем модели серии из карточки, не блокируем
                # и не переспрашиваем. Источник — список совместимости карточки, как у fam_reply.
                reply = (f"Здравствуйте! Если у вас {_or_list(series_mm)} — да, подойдёт."
                         + (f" Наш картридж — {code}." if code else ""))
                ground.update({"grounded": True, "source": "карточка-серия",
                               "note": "серия без модели, перечислены модели серии из карточки: "
                                       + ", ".join(series_mm)})
                ground["compat"] = dict(ground.get("compat") or {}, series_members=series_mm)
            elif not defect and bool(asked_m):
                # MODEL-FIRST: ответ _llm по знанию модели + карточка/каталог уже готов (reply).
                # Веб зовём РЕДКО — только если модель сама не уверена (need_web), низкая уверенность
                # или тёмный регуляторный код (L662B-подобный). Это главный рычаг экономии.
                need_web = bool(d.get("need_web")) or (0 < conf < 0.6) or bool(reg)
                # КЭШ: до веба смотрим, отвечали ли уже по этой паре (товар × модель принтера).
                # Хит → берём вердикт из БД, веб НЕ зовём (главная экономия). Первая asked-модель — ключ.
                cached = None
                if need_web and asked_m:
                    cached = cc_get(r["platform"], r["item_id"], asked_m[0])
                if cached:
                    reply = (cached.get("reply") or reply).strip()
                    ground.update({"web": False, "source": "кэш(" + (cached.get("source") or "веб") + ")",
                                   "grounded": True, "verdict": cached.get("verdict"),
                                   "sources": cached.get("sources") or [],
                                   "note": "из кэша совместимости: " + (cached.get("note") or "")[:200]})
                elif need_web and bn.covers(_brand_of(r), r.get("body")):
                    # своё знание по бренду+теме уже есть в brand_notes — чужой сайт не нужен
                    ground.update({"source": "справочник бренда", "note": "веб не звали: есть "
                                   "запись brand_notes по теме; " + (ground.get("note") or "")[:180]})
                elif need_web:
                    from reports.feedback_web import WEB_MODEL
                    from reports.llm_client import client_for
                    wa = web_compat(client_for(WEB_MODEL), r["body"], r["product_name"], cc)
                    used_web = True
                    if wa and wa.get("verdict") in ("yes", "no") and (wa.get("reply") or "").strip():
                        reply = wa["reply"].strip()
                        ground.update({"web": True, "source": "веб (BLOCK)", "grounded": True,
                                       "verdict": wa["verdict"], "sources": wa.get("sources", []),
                                       "note": "веб: " + (wa.get("note") or "")[:220]})
                        # сохраняем вердикт по КАЖДОЙ спрошенной модели → впредь бесплатно из кэша
                        for am in asked_m:
                            cc_put(r["platform"], r["item_id"], am, wa["verdict"], wa["reply"].strip(),
                                   "веб", wa.get("sources", []), (wa.get("note") or "")[:200])
                    else:
                        ground.update({"source": "модель", "note": "модель неуверена, веб без вердикта; "
                                       + (ground.get("note") or "")[:200]})
                else:
                    ground.update({"source": "модель", "note": "по знанию модели (совм. вне карточки); "
                                   + (ground.get("note") or "")[:200]})
            route = "review"
        # ФАКТ-ВЕБ: карточка/модель не дали ответа на объективный вопрос (ТТХ / подбор по модели) —
        # достраиваем внешним поиском вместо «напишите нам». Веб → source=веб-факт, всегда на ревью.
        if (r["kind"] == "question" and not used_web and _needs_fact_web(r["body"], reply)
                and not bn.covers(_brand_of(r), r.get("body"))):
            from reports.feedback_web import web_fact, WEB_MODEL
            from reports.llm_client import client_for
            base = (reply or "").strip()
            # процедурный добор (чип/прошивка): к уже готовому ответу про совместимость дописываем веб-факт,
            # НО выкидываем отфутболивающие предложения («напишите нам»/«в карточке нет») — их заменит веб.
            proc_gap = bool(_PROC_Q_RX.search(r["body"] or ""))
            keep_base = ""
            if proc_gap:
                kept = [s for s in re.split(r"(?<=[.!?])\s+", base) if not _DEFER_RX.search(s)]
                keep_base = " ".join(kept).strip()
            append = proc_gap and len(keep_base) >= 15
            wf = web_fact(client_for(WEB_MODEL), r["body"], r["product_name"], cc,
                          draft=keep_base if append else "")
            used_web = True
            if wf and (wf.get("answer") or "").strip():
                ans = wf["answer"].strip()
                reply = (keep_base + " " + ans) if append else ans
                route = "review"
                ground.update({"web": True, "source": "веб-факт", "grounded": True,
                               "sources": wf.get("sources", []),
                               "note": ("веб-факт(добор): " if append else "веб-факт: ")
                               + (wf.get("note") or "")[:220]})
                reply, ground = _polish_with_web(client, r, cf, corpus, wf, reply, ground)
            else:
                ground.update({"note": "веб-факт без ответа; " + (ground.get("note") or "")[:200]})
    else:
        name = _first_name(r["payload"]) if r["platform"] == "wb" else None
        _c, reply, route, conf, _tpl = draft_review(r, name, _short(r["product_name"]))
        # для карточки модератора видно, какой именно шаблон подставлен (тип жалобы или общий)
        _nt = neg_templates.classify(" ".join(filter(None, [r.get("body"), r.get("pros"), r.get("cons")]))) \
            if _c == "negative" else None
        _note = (f"шаблон негатива: {neg_templates.LABELS[_nt]}" if _nt else
                 "шаблон негатива: общий (тип не определён)" if _c == "negative" else
                 "шаблон отзыва (ротация вариантов)")
        if _c == "negative":                               # видно, по какому рейтингу принято решение
            _note += "; " + card_rating.note(card_rating.verdict(r["platform"], r.get("item_id")))
        # template_id (A2) — не для красоты: без него нельзя ни померить работу конкретного шаблона,
        # ни восстановить историю после правки списка вариантов ротации.
        cc, ground = "", {"llm": False, "note": _note, "source": "шаблон", "template": True,
                          "template_id": _tpl}
        cat = "review-empty"
    # A0 (07.09.2026). Гейт СТОИТ ПОСЛЕ ОБЕИХ ВЕТОК, потому что дыра была в обеих: 7 отзывов с
    # маркером проблемы закрыла позитивная ротация, ещё 4 — «одобренный» самой моделью ответ
    # (route='auto' приходит из её JSON). Замер: rev_auto_gap_examples_2026-09-07.md.
    # Исключение одно — «мёртвая» карточка: там уходит не благодарность, а сухой хендофф, и правило
    # Сергея от 24.08.2026 (карточку под убой не спасают ответом) A0 не отменяет.
    # п.1 (21.09.2026): маркер теперь только дефект или претензия. «?» и слова сожаления на позитиве
    # вычитку больше не требуют — вопрос в отзыве отвечает модель, её текст проверяет контролёр.
    if (r["kind"] == "review" and route == "auto" and ground.get("template_id") != "dead_card"
            and _has_text(r) and not _positive_clean(r)):
        route = "review"
        ground["review_flag"] = True
        ground["note"] = "позитив с маркером дефекта; " + (ground.get("note") or "")[:200]
    elif r["kind"] == "review" and _positive_clean(r) and (reply or "").strip() and route != "auto":
        route = "auto"
        ground["auto_policy"] = "позитив ≥4★ без дефекта и претензии"
        ground["note"] = "п.1: чистый позитив — автопубликация после контролёра; " + \
            (ground.get("note") or "").replace("на вычитку оператору; ", "")[:200]
    # КАТАЛОГ-ПОСЛЕ-ВЕБА + страховка от ложного «нет»: ответ отрицает наличие ИЛИ (после веба) уводит
    # покупателя «на сторону»/говорит «не подходит» БЕЗ нашего артикула — а по коду картриджа из веб-ответа
    # или по модели принтера у нас реально есть листинг → подставляем наш площадочный артикул.
    if (r["kind"] == "question" and reply and not _HAS_OUR_ART_RX.search(reply)
            and (_DENY_AVAIL_RX.search(reply)
                 or (used_web and (_INCOMPAT_RX.search(reply) or _REDIRECT_RX.search(reply))))):
        _fct = (cf.for_ozon(r["item_id"]) if r["platform"] == "ozon" else
                cf.for_yandex(r["item_id"]) if r["platform"] == "yandex" else
                cf.for_wb(r["item_id"]))
        off = _our_offer(reply, r["body"], r["product_name"], (_fct or {}).get("models"),
                         r["platform"], (_fct or {}).get("code"), r["item_id"],
                         account=r.get("account"))
        if off:
            if _DENY_AVAIL_RX.search(reply) or _REDIRECT_RX.search(reply):
                reply = _repair_denial(reply, off)             # вырезаем отказ/увод, дописываем наш листинг
            else:
                reply = reply.rstrip(" .") + (f". В нашем магазине есть подходящий — "
                                              f"{off['ref']}, ссылка {off['url']}")
            route = "review"
            ground.update({"catalog": True, "grounded": True,
                           "source": (ground.get("source") or "модель") + "+каталог-после-веба",
                           "note": "каталог-после-веба: подставлен наш листинг; "
                           + (ground.get("note") or "")[:200]})
        elif _DENY_AVAIL_RX.search(reply) or _REDIRECT_RX.search(reply):
            # В КАНАЛЕ ПОКУПАТЕЛЯ подходящего листинга нет. Уводить на другую площадку запрещено
            # (инцидент 01.08.2026: яндексовцу дали ссылку на WB) — честно зовём уточнить в чат.
            reply = _ask_in_chat(reply, r["platform"])
            route = "review"
            ground.update({"note": f"нет листинга в канале {r['platform']} → без ссылки, "
                           f"предложено уточнить в чате; " + (ground.get("note") or "")[:180]})
    # производитель/страна → прямой ответ «Китай» (детерминированно, т.к. DeepSeek уклоняется)
    if r["kind"] == "question" and reply:
        fixed = _fix_producer(reply, r["body"])
        if fixed != reply:
            reply = fixed
            ground["note"] = "guard: производитель→Китай; " + (ground.get("note") or "")[:220]
    # guardrail от выдуманных кодов расходников: любой код в ответе, которого нет в CARD_DATA/КАТАЛОГ
    # (и не из вопроса покупателя), — вырезаем предложение с ним. Только для вопросов (у отзывов cc пуст).
    if r["kind"] == "question" and cc:
        reply, guarded = _code_guard(reply, (cc or "") + " " + (r["body"] or ""), ground.get("note", ""))
        if guarded:
            ground["note"] = "code-guard: убран непроверенный код; " + (ground.get("note") or "")[:220]
            ground["code_guard"] = True
    reply = _presale_scrub(reply, r)
    scrubbed = _no_internal_caveats(reply)               # внутренние оговорки покупателю не показываем
    if scrubbed != reply:
        reply = scrubbed
        ground["note"] = "scrub: убрана оговорка про карточку/данные; " + (ground.get("note") or "")[:220]
    # финальная страховка: пустой/обрезанный ответ на вопрос (thinking съел max_tokens, JSON битый) →
    # безопасный фолбэк вместо пустоты; всегда review (в фазе черновиков и так review)
    if r["kind"] == "question" and len((reply or "").strip()) < 12:
        reply = ("Здравствуйте! Уточните, пожалуйста, точную модель вашего принтера — и мы подберём "
                 "подходящий картридж и подскажем артикул.")
        route = "review"
        ground["note"] = "фолбэк: пустой ответ модели; " + (ground.get("note") or "")[:220]
    # ПРОФИЛЬНЫЙ ТОВАР БЕЗ CARD_DATA. Опереться не на что, поэтому веб-добор зовём БЕЗУСЛОВНО (обычный
    # `_needs_fact_web` рассчитан на случай, когда карточка есть и просто молчит по теме). Порядок тот
    # же: каталог/модель уже отработали выше → веб (DeepSeek) → что получилось, то и показываем.
    # Оператору отдаём только если не осталось содержательного ответа (пусто или дежурный фолбэк).
    if r["kind"] == "question" and no_card:
        ground["no_card"] = True
        if not used_web and not (ground.get("grounded") or ground.get("catalog")):
            from reports.feedback_web import web_fact, WEB_MODEL
            from reports.llm_client import client_for
            wf = web_fact(client_for(WEB_MODEL), r["body"], r["product_name"], cc)
            used_web = True
            if wf and (wf.get("answer") or "").strip():
                reply = wf["answer"].strip()
                ground.update({"web": True, "source": "веб-факт (без карточки)", "grounded": True,
                               "sources": wf.get("sources", []),
                               "note": "веб-факт: " + (wf.get("note") or "")[:200]})
                reply, ground = _polish_with_web(client, r, cf, corpus, wf, reply, ground)
            else:
                ground["note"] = "веб-факт без ответа; " + (ground.get("note") or "")[:200]
        fallback = str(ground.get("note") or "").startswith("фолбэк")
        if not (reply or "").strip() or fallback:
            marker = ("⚠️ Нет данных карточки, и цепочка каталог → веб → ИИ не дала ответа — "
                      "нужен ручной ответ оператора.")
            return _early_human(r, cc, marker, f"{NO_CARD_NOTE}: цепочка без результата", used_llm)
        route = "review"                                   # без карточки — никогда не auto
        src = ground.get("source") or "—"
        ground["source"] = src if "без карточки" in src else src + " (без карточки)"
        ground["note"] = NO_CARD_NOTE + "; " + (ground.get("note") or "")[:200]
    # ПРАВИЛА КАЧЕСТВА (вопросы) — последними, после всех обогащений, включая веб-добор без карточки
    if r["kind"] == "question":
        reply, qviol = _qa_guards(reply, cc, r["body"])
        if qviol:
            ground["qa_guard"] = qviol
            ground["note"] = "qa-guard: " + ", ".join(qviol) + "; " + (ground.get("note") or "")[:180]
    reply = _scrub_urls(reply)          # ссылка не должна слипаться с пунктуацией/служебным текстом
    # Контролёр — на ЛЮБОМ черновике, включая шаблонный (пункт 3). Стоит последним: проверять надо
    # ровно тот текст, который увидит покупатель, а не промежуточный до guard'ов и веб-добора.
    reply, ground = _verified(client, r, reply, cc, ground)
    route = ground.get("route", route) if ground.get("verify") == "FIX" else route
    # п.1: автопубликация позитива — ПОСЛЕ контролёра. Контролёр не отработал (сбой, выключен) —
    # новое правило не действует, черновик на вычитку.
    if ground.get("auto_policy") and route == "auto" and ground.get("verify") != "PASS":
        route = "review"
        ground["note"] = "контролёр не подтвердил — автопубликация отменена; " + (ground.get("note") or "")[:200]
    outd = dict(r, cat=cat, reply=reply, route=route, conf=conf, card=cc,
                note=ground.get("note", ""), grounded=ground.get("grounded", False),
                catalog=ground.get("catalog", False), source=ground.get("source", ""),
                web=ground.get("web", False), sources=ground.get("sources", []),
                intent=intent(r["body"]) if r["kind"] == "question" else "")
    return outd, reply, route, conf, ground, used_llm, used_web


def run(since="2026-06-17"):
    rows = _gather(since)
    cf, corpus = CardFacts(), load_corpus()
    client = _client()
    print(f"Свежий необработанный поток с {since}: {len(rows)} (вопросов "
          f"{sum(r['kind']=='question' for r in rows)}, отзывов {sum(r['kind']=='review' for r in rows)}). "
          f"Корпус few-shot: {len(corpus.items)}.", flush=True)

    out, nllm, nweb, nfail, npend = [], 0, 0, 0, 0
    fails = []
    for i, r in enumerate(rows, 1):
        try:
            outd, reply, route, conf, ground, ul, uw = _answer(client, r, cf, corpus)
        except llm_batch.LlmPending:
            # Запрос ушёл в батч, ответа ещё нет — это НЕ сбой: запись остаётся без черновика и
            # без draft_src_hash, следующий цикл возьмёт её снова и заберёт готовый ответ из
            # feedback_llm_result. В fails такое не пишем, иначе health поднимет ложную тревогу.
            npend += 1
            continue
        except Exception as e:
            # Сбой генерации — запись остаётся БЕЗ черновика и БЕЗ карточки модерации: пусть лучше
            # покупатель ждёт следующего цикла, чем получит служебный текст. draft_src_hash не
            # проставлен → _gather() возьмёт эту запись снова через 2 часа.
            nfail += 1
            # причину копим для health.report_cycle: молчаливый провал недопустим — 13.08.2026
            # четыре дня вопросы падали на пустом балансе Anthropic, а цикл рапортовал OK
            fails.append({"platform": r["platform"], "kind": r["kind"], "ext_id": r["ext_id"],
                          "err": f"{type(e).__name__}: {str(e)[:200]}"})
            print(f"[{i}/{len(rows)}] ПРОПУСК без черновика {r['platform']}/{r['kind']} "
                  f"{r['ext_id']}: {type(e).__name__}: {str(e)[:120]}", flush=True)
            continue
        route, ground = _auto_card_q(r, reply, route, ground)
        _store(r, reply, route, conf, ground)
        _enqueue_moderation(r, reply)
        out.append(outd)
        nllm += 1 if ul else 0
        nweb += 1 if uw else 0
        if ul:
            tag = "ВОПРОС" if r["kind"] == "question" else f"ОТЗЫВ {r['rating']}★"
            print(f"[{i}/{len(rows)}] {r['platform']} · {tag} · {outd['intent']}", flush=True)
            print("   Q:", (r["body"] or r["pros"] or "")[:120].replace("\n", " "), flush=True)
            print("   →:", reply[:200].replace("\n", " "), f"[{route}]", flush=True)

    _html(out, since)
    c = Counter(o["cat"] for o in out)
    print(f"\nИТОГ: {len(out)} черновиков · ИИ-вызовов {nllm} · веб-проверок {nweb} · вопросов {c['question']} · "
          f"отзывов-с-текстом {c['review-text']} · пустых-шаблоном {c['review-empty']}"
          + (f" · ПРОПУЩЕНО без черновика (сбой модели) {nfail}" if nfail else "")
          + (f" · ЖДУТ БАТЧА {npend}" if npend else ""), flush=True)
    print("Токены/стоимость (сборка ответа _llm):", flush=True)
    print(_COST.summary(), flush=True)
    _COST.persist()
    print(f"Артефакт-файл: {ART}", flush=True)
    # сводка прогона для feedback_bot.health.report_cycle — возврат run() не трогаем, его читают
    # другие вызывающие (ручные прогоны, backfill)
    global LAST_RUN
    LAST_RUN = {"drafts": len(out), "fails": fails, "llm_calls": nllm, "web_calls": nweb,
                "pending": npend}
    return out


def _e(s):
    return html.escape(str(s or ""))


def _html(out, since):
    qs = [o for o in out if o["cat"] == "question"]
    rt = [o for o in out if o["cat"] == "review-text"]
    re_ = [o for o in out if o["cat"] == "review-empty"]

    def q_row(o):
        card = _e(o["card"])[:900].replace("\n", "<br>") if o["card"] else '<span class="muted">карточка не сшита</span>'
        src = o.get("source") or ("карточка" if o["card"] else "")
        scls = {"веб": "s-web", "карточка-серия": "s-fam"}.get(src, "s-card")
        src_chip = f'<span class="chip {scls}">источник: {_e(o["catalog"] and "каталог+" or "")}{_e(src or "—")}</span>'
        links = ""
        if o.get("sources"):
            items = "".join(f'<li><a href="{_e(s.get("url"))}" target="_blank" rel="noopener">{_e(s.get("title") or s.get("url"))[:80]}</a></li>'
                            for s in o["sources"][:5])
            links = f'<div class="links"><span class="lbl">веб-источники</span><ul>{items}</ul></div>'
        return f"""<div class="item">
 <div class="ihead"><span class="plat">{_e(o['platform'])}</span>
   <span class="tag">{_e(o['intent'])}</span>{src_chip}
   <span class="badge b-review">на вычитку</span></div>
 <div class="q">{_e(o['body'])}</div>
 <div class="prod muted">{_e(o['product_name'])[:70]}</div>
 <div class="reply"><span class="lbl">черновик ответа</span>{_e(o['reply'])}</div>
 {links}
 <details class="src"><summary>факты, на которых построен ответ</summary>
   <div class="facts">{card}</div>
   <div class="note muted">grounded={str(o['grounded']).lower()} · {_e(o['note'])[:220]}</div></details>
</div>"""

    def r_row(o):
        return f"""<div class="item">
 <div class="ihead"><span class="plat">{_e(o['platform'])}</span>
   <span class="tag">отзыв {_e(o['rating'])}★</span>
   <span class="badge {'b-auto' if o['route']=='auto' else 'b-review'}">{'авто' if o['route']=='auto' else 'на вычитку'}</span></div>
 <div class="q">{_e((o['body'] or '') + (' · ' + o['pros'] if o['pros'] else '') + (' · ' + o['cons'] if o['cons'] else '')) or '(без текста)'}</div>
 <div class="prod muted">{_e(o['product_name'])[:70]}</div>
 <div class="reply"><span class="lbl">черновик ответа</span>{_e(o['reply'])}</div></div>"""

    style = """:root{--bg:#eef1f4;--surface:#fff;--surface2:#f7f9fb;--ink:#141a20;--muted:#5f6b78;--border:#e0e5ea;--accent:#0f6e8c;--auto:#127c47;--auto-bg:#e5f4ec;--review:#8a6a00;--review-bg:#fbf1d8;--chip:#e2eef2;--chipink:#0f6e8c;--dash:#dbe1e7}
@media(prefers-color-scheme:dark){:root{--bg:#0d1116;--surface:#161d24;--surface2:#111820;--ink:#e6ecf1;--muted:#93a0ad;--border:#26303a;--accent:#4bb8d6;--auto:#54cc8b;--auto-bg:#122a1e;--review:#e2b64a;--review-bg:#2c2410;--chip:#123039;--chipink:#7fd3e8;--dash:#2b333d}}
:root[data-theme="dark"]{--bg:#0d1116;--surface:#161d24;--surface2:#111820;--ink:#e6ecf1;--muted:#93a0ad;--border:#26303a;--accent:#4bb8d6;--auto:#54cc8b;--auto-bg:#122a1e;--review:#e2b64a;--review-bg:#2c2410;--chip:#123039;--chipink:#7fd3e8;--dash:#2b333d}
:root[data-theme="light"]{--bg:#eef1f4;--surface:#fff;--surface2:#f7f9fb;--ink:#141a20;--muted:#5f6b78;--border:#e0e5ea;--accent:#0f6e8c;--auto:#127c47;--auto-bg:#e5f4ec;--review:#8a6a00;--review-bg:#fbf1d8;--chip:#e2eef2;--chipink:#0f6e8c;--dash:#dbe1e7}
*{box-sizing:border-box}body{font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;margin:0;background:var(--bg);color:var(--ink)}
.wrap{max-width:900px;margin:0 auto;padding:26px 20px 48px}.eyebrow{font-size:12px;letter-spacing:.09em;text-transform:uppercase;color:var(--accent);font-weight:700;margin:0 0 6px}
h1{font-size:24px;margin:0 0 6px;letter-spacing:-.01em}.sub{color:var(--muted);margin:0 0 18px;max-width:74ch}
.tiles{display:flex;gap:12px;margin:16px 0;flex-wrap:wrap}.tile{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:12px 16px;flex:1;min-width:120px}
.tile .n{font-size:26px;font-weight:750;font-variant-numeric:tabular-nums}.tile .k{font-size:12px;color:var(--muted);margin-top:4px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.06em;color:var(--accent);margin:30px 0 10px;padding-bottom:6px;border-bottom:1px solid var(--border)}
.item{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:14px 16px;margin:12px 0}
.ihead{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}.plat{font-weight:650;text-transform:capitalize}
.tag{font-size:11.5px;color:var(--muted)}.chip{font-size:11px;font-weight:650;color:var(--chipink);background:var(--chip);padding:2px 8px;border-radius:20px}
.chip.s-web{color:var(--auto);background:var(--auto-bg)}.chip.s-fam{color:var(--review);background:var(--review-bg)}
.links{margin-top:8px}.links ul{margin:4px 0 0;padding-left:18px}.links li{font-size:12px;margin:2px 0}.links a{color:var(--accent)}
.badge{margin-left:auto;display:inline-block;padding:3px 10px;border-radius:20px;font-size:11.5px;font-weight:750;white-space:nowrap}.b-auto{color:var(--auto);background:var(--auto-bg)}.b-review{color:var(--review);background:var(--review-bg)}
.q{font-weight:550;margin:2px 0}.prod{font-size:12px;margin:2px 0 10px}
.reply{background:var(--surface2);border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:8px;padding:10px 12px;font-size:14px}
.lbl{display:block;font-size:10.5px;font-weight:750;text-transform:uppercase;letter-spacing:.05em;color:var(--accent);margin-bottom:4px}
.src{margin-top:9px}.src summary{cursor:pointer;font-size:12px;color:var(--muted)}.facts{font-size:12px;background:var(--surface2);border-radius:8px;padding:9px 11px;margin-top:7px;line-height:1.5}
.note{font-size:11.5px;margin-top:6px}.muted{color:var(--muted)}
.foot{color:var(--muted);font-size:13px;margin-top:24px;max-width:78ch;border-top:1px dashed var(--dash);padding-top:14px}"""
    body = f"""<div class="wrap"><p class="eyebrow">Цифровой квадрат · черновики на свежий поток</p>
<h1>Ответы-черновики: необработанные отзывы и вопросы</h1>
<p class="sub">Свежий необработанный поток с {_e(since)}. Вопросы — ИИ-слой ({_e(QUESTION_MODEL)}) на
фактах карточки (card_facts v2: WB-модели из описания, чип из Ozon-двойника) + каталог наших листингов
+ few-shot из наших прошлых ответов; отзывы-с-текстом — та же схема на {_e(MODEL)}. Совместимость: сначала карточка
с учётом <b>вариантов серии</b> (CX17→CX17NF), затем <b>веб-поиск</b> для моделей, которых в карточке
нет. Пустые 5★ — шаблон. <b>Это черновики — на площадках ничего не опубликовано.</b></p>
<div class="tiles">
 <div class="tile"><div class="n">{len(qs)}</div><div class="k">вопросов (ИИ)</div></div>
 <div class="tile"><div class="n">{len(rt)}</div><div class="k">отзывов с текстом (ИИ)</div></div>
 <div class="tile"><div class="n">{len(re_)}</div><div class="k">пустых 5★ (шаблон)</div></div>
 <div class="tile"><div class="n">{len(out)}</div><div class="k">всего черновиков</div></div></div>
<h2>Вопросы — {len(qs)}</h2>{''.join(q_row(o) for o in qs)}
<h2>Отзывы с текстом — {len(rt)}</h2>{''.join(r_row(o) for o in rt) or '<p class="muted">нет</p>'}
<h2>Пустые отзывы (5★) — {len(re_)} · шаблон</h2>{''.join(r_row(o) for o in re_[:12])}
{'<p class="muted">…и ещё ' + str(len(re_)-12) + ' по тому же шаблону (ротация вариантов).</p>' if len(re_)>12 else ''}
<p class="foot">Вопросы в фазе «только черновики» помечены «на вычитку» — публикацию решает оператор.
Совместимость решается по слоям: <b>карточка-серия</b> (вариант линейки, напр. CX17NF при CX17 в списке —
бесплатно), затем <b>веб-поиск</b> (Claude web_search: определяет, входит ли принтер покупателя в серию,
которую наш картридж покрывает, со ссылками на источники); если ни один слой не подтверждает —
модель честно просит уточнить и не выдумывает (ложное «да, подойдёт» = возврат). Раскройте «факты,
на которых построен ответ», чтобы видеть grounding.</p></div>"""
    ART.write_text(f'<title>Черновики ответов на свежий поток — Цифровой квадрат</title>\n<style>{style}</style>\n{body}',
                   encoding="utf-8")


if __name__ == "__main__":
    since = "2026-06-17"
    if "--since" in sys.argv:
        since = sys.argv[sys.argv.index("--since") + 1]
    run(since)
