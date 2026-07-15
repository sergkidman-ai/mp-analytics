"""reports/feedback_llm.py — LLM-слой черновиков ответов через Message Batches API (−50%).

Шаблоны (пустые 5★) остаются в feedback_drafts. Сюда идёт всё С ТЕКСТОМ — вопросы, негатив,
позитив-с-текстом: собираем грунтовку из карточки, шлём батчем, модель отвечает JSON
{reply, route, confidence, grounded, note}. Жёсткое правило: утверждать только то, что есть в
грунтовке. Пост-guardrail: если про совместимость сказано «подходит», а модель из вопроса не
найдена в данных карточки — принудительно route=review. Режим только черновики: не постит.

Запуск:  ./venv/bin/python reports/feedback_llm.py           # весь бэклог review-категорий
         ./venv/bin/python reports/feedback_llm.py --limit 20
"""
import os
import re
import sys
import json
import time
import pathlib

from dotenv import load_dotenv

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env")
from core import db                                        # noqa: E402
from reports.feedback_drafts import _ozon_compat, _write_html, _classify, _norm, MODEL_RX  # noqa: E402
from reports.feedback_corpus import load_corpus            # noqa: E402

MODEL = "claude-sonnet-5"          # клиентские ответы — важна корректность; батч даёт −50%
MAX_TOKENS = 500

SYSTEM = """Ты — специалист поддержки интернет-магазина «Цифровой квадрат», продающего картриджи и
расходники для принтеров на маркетплейсах (Wildberries, Ozon). Пишешь ответы на отзывы и вопросы
покупателей от лица магазина.

ТОН (по нашим реальным ответам): вежливо, тепло, кратко (1–3 предложения), на «Вы». На позитив —
благодаришь за выбор товара и магазина. На негатив — сожалеешь, не споришь, зовёшь написать в чат
по QR-коду на упаковке и обещаешь разобраться (возврат/замена). На вопрос — отвечаешь по делу.

ГЛАВНОЕ ПРАВИЛО — ПРОВЕРКА ДАННЫХ. Утверждай ТОЛЬКО то, что подтверждено блоком CARD_DATA
(характеристики/совместимость карточки). Если спрашивают про модель принтера, которой НЕТ в
CARD_DATA — не говори «подходит»; попроси уточнить точную модель и предложи помощь. Никогда не
выдумывай совместимость, регион чипа, ресурс, цвет, «оригинал/совместимый». Если данных нет —
route=review. Технические проблемы («не опознан», «ошибка чипа», «печатает серым») — краткий
совет + приглашение в чат, без гарантий.

ФОРМАТ ОТВЕТА — СТРОГО один JSON-объект, без markdown:
{"reply": "<готовый к публикации текст ответа>",
 "route": "auto" | "review",
 "confidence": <число 0..1>,
 "grounded": <true если reply полностью опирается на CARD_DATA/факты, иначе false>,
 "note": "<кратко: на чём основан ответ или что перепроверить>"}
route=auto только если ответ безопасен и полностью обоснован; спорные возвраты, претензии,
неподтверждённая совместимость — route=review."""

def _fewshot(examples):
    """Динамические few-shot: похожие НАШИ прошлые ответы (из справочника)."""
    if not examples:
        return "(похожих прошлых ответов не нашлось — держи наш тон: вежливо, кратко, на «Вы».)"
    lines = ["ВОТ КАК МЫ РЕАЛЬНО ОТВЕЧАЛИ НА ПОХОЖЕЕ (образец тона и фактуры, не копируй дословно, "
             "факты бери из CARD_DATA):"]
    for e in examples:
        src = (e["src"] or f"отзыв {e['rating']}★")[:120]
        lines.append(f"— На «{src}» → «{e['answer'][:200]}»")
    return "\n".join(lines)


def _user_block(r, name, compat, examples):
    kind = "ВОПРОС" if r["kind"] == "question" else f"ОТЗЫВ {r['rating']}★"
    text = (r["body"] or "").strip()
    if r["pros"]:
        text += f"\n[достоинства]: {r['pros']}"
    if r["cons"]:
        text += f"\n[недостатки]: {r['cons']}"
    card = compat.strip() if compat else "(нет данных карточки)"
    return (f"{_fewshot(examples)}\n\n"
            f"ПЛОЩАДКА: {r['platform']}\nТОВАР: {r['product_name']}\n"
            f"ИМЯ ПОКУПАТЕЛЯ: {name}\n"
            f"CARD_DATA (характеристики/совместимость карточки, единственный источник фактов):\n"
            f"\"\"\"{card[:2500]}\"\"\"\n\n"
            f"{kind} ОТ ПОКУПАТЕЛЯ:\n\"\"\"{text[:1500]}\"\"\"\n\n"
            f"Составь ответ строго в формате JSON.")


def _asked_models(body):
    return [m.group(0) for m in MODEL_RX.finditer(body or "")
            if re.search(r"\d", m.group(0)) and len(_norm(m.group(0))) >= 3]


def _gather(limit=None):
    rows = db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,body,pros,cons,payload
        FROM raw_feedback WHERE is_answered=false AND account IN ('wb_acc1','oz_acc1')""")
    items = [r for r in rows if _classify(r) in ("question", "negative", "positive")]
    items.sort(key=lambda r: {"question": 0, "negative": 1, "positive": 2}[_classify(r)])
    return items[:limit] if limit else items


def _name(r):
    if r["platform"] == "wb":
        n = ((r["payload"] or {}).get("userName") or "").strip()
        return n.split()[0] if n else "(имя неизвестно)"
    return "(на Ozon отзывы анонимны)"


def run(limit=None):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        print("НЕТ ANTHROPIC_API_KEY в .env — добавь ключ и повтори.", flush=True)
        return
    from anthropic import Anthropic
    from anthropic.types.messages.batch_create_params import Request
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    client = Anthropic(api_key=key)

    items = _gather(limit)
    if not items:
        print("Нет элементов для LLM.", flush=True)
        return
    oz_skus = {r["item_id"] for r in items if r["platform"] == "ozon" and r["item_id"]}
    compat = _ozon_compat("oz_acc1", oz_skus) if oz_skus else {}
    corpus = load_corpus()
    print(f"Справочник наших ответов: {len(corpus.items)} шт (динамические few-shot)", flush=True)

    reqs, idx, cid_compat = [], {}, {}
    for i, r in enumerate(items):
        cid = f"i{i}"
        idx[cid] = r
        cc = compat.get(r["item_id"], "") if r["platform"] == "ozon" else ""
        cid_compat[cid] = cc
        src = (r["body"] or r["pros"] or r["cons"] or "")
        ex = corpus.retrieve(r["kind"], src, r["product_name"], k=5)
        content = _user_block(r, _name(r), cc, ex)
        reqs.append(Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
            model=MODEL, max_tokens=MAX_TOKENS, system=SYSTEM,
            messages=[{"role": "user", "content": content}])))

    batch = client.messages.batches.create(requests=reqs)
    print(f"Батч создан: {batch.id} ({len(reqs)} запросов, модель {MODEL}, −50% batch). Ждём…", flush=True)
    t0 = time.time()
    while True:
        b = client.messages.batches.retrieve(batch.id)
        if b.processing_status == "ended":
            break
        if time.time() - t0 > 3600:
            print("Таймаут ожидания батча (>1ч). Позже: --resume", batch.id, flush=True)
            return
        time.sleep(15)
    print(f"Готово за {time.time()-t0:.0f}с. Counts: {b.request_counts}", flush=True)
    _store(client, batch.id, idx, cid_compat)


def _store(client, batch_id, idx, cid_compat):
    ok = err = auto = 0
    for res in client.messages.batches.results(batch_id):
        r = idx.get(res.custom_id)
        if r is None or res.result.type != "succeeded":
            err += 1
            continue
        raw = res.result.message.content[0].text.strip()
        try:
            m = re.search(r"\{.*\}", raw, re.S)
            data = json.loads(m.group(0))
        except Exception:
            err += 1
            continue
        reply = (data.get("reply") or "").strip()
        route = "auto" if data.get("route") == "auto" else "review"
        conf = float(data.get("confidence") or 0)
        note = data.get("note") or ""
        grounded = bool(data.get("grounded"))
        # GUARDRAIL: утвердительный ответ про совместимость без подтверждения модели → review
        if r["kind"] == "question":
            asked = _asked_models(r["body"])
            aff = re.search(r"подойд|подход|совмест|да,? ", reply.lower())
            cn = _norm(cid_compat.get(res.custom_id, ""))
            if asked and aff and cn and not any(_norm(a) in cn for a in asked):
                route, grounded = "review", False
                note = "guardrail: совместимость не подтверждена карточкой; " + note
            route = "review"          # в фазе «только черновики» вопросы всегда на человека
        ground = {"llm": True, "grounded": grounded, "note": note[:300], "model": MODEL}
        db.execute("""UPDATE raw_feedback SET draft_text=%s, draft_route=%s, draft_confidence=%s,
            draft_grounding=%s, draft_at=now() WHERE platform=%s AND account=%s AND kind=%s AND ext_id=%s""",
            (reply, route, conf, _J(ground),
             r["platform"], r["account"], r["kind"], r["ext_id"]))
        ok += 1
        auto += 1 if route == "auto" else 0
    print(f"Записано LLM-черновиков: {ok} (auto {auto}, review {ok-auto}), ошибок {err}", flush=True)
    _export_html()


def _J(d):
    from psycopg2.extras import Json
    return Json(d)


def _export_html():
    """Пересобрать HTML из текущих draft_* в БД (и шаблонные, и LLM)."""
    rows = db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,body,pros,cons,
        draft_text,draft_route,draft_confidence,draft_category,draft_grounding
        FROM raw_feedback WHERE is_answered=false AND draft_at IS NOT NULL""")
    updates = [{"platform": r["platform"], "account": r["account"], "kind": r["kind"], "ext_id": r["ext_id"],
                "item_id": r["item_id"], "product_name": r["product_name"], "rating": r["rating"],
                "body": r["body"], "pros": r["pros"], "cons": r["cons"],
                "cat": r["draft_category"] or _classify(r), "draft": r["draft_text"] or "",
                "route": r["draft_route"] or "review", "conf": float(r["draft_confidence"] or 0),
                "ground": r["draft_grounding"] or {}} for r in rows]
    from datetime import datetime, timezone
    _write_html(updates, datetime.now(timezone.utc))
    print("HTML обновлён: docs/feedback_drafts.html", flush=True)


if __name__ == "__main__":
    lim = None
    if "--limit" in sys.argv:
        lim = int(sys.argv[sys.argv.index("--limit") + 1])
    run(lim)
