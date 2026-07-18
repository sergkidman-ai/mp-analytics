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
from reports.feedback_drafts import _write_html, _classify, _norm, MODEL_RX  # noqa: E402
from reports.feedback_corpus import load_corpus            # noqa: E402
from reports.card_facts import CardFacts                   # noqa: E402
from reports.catalog import catalog_block                  # noqa: E402

_CHIP_RU = {"installed": "с чипом (уже установлен, докупать не нужно)",
            "not_required": "чип уже установлен, дополнительно докупать/ставить не требуется",
            "none": "без чипа (чип переставляется с прежнего оригинального картриджа)"}


def _card_data(r, cf):
    """CARD_DATA для промпта: чистые факты карточки (card_facts v2, offline из raw_*_card_content /
    raw_ozon_attributes). Чип — 3 корректных состояния, модели — чистый список. '' если карточки нет."""
    f = (cf.for_ozon(r["item_id"]) if r["platform"] == "ozon"
         else cf.for_wb(r["item_id"]) if r["platform"] == "wb" else None)
    # каталог наших листингов — для вопросов о наличии/цвете/артикуле (источник №3); модель принтера
    # берём из карточки, если в вопросе её нет («а цветной есть?» — модель уже известна)
    cat = (catalog_block(r.get("body") or "", r.get("product_name") or "", (f or {}).get("models"))
           if r["kind"] == "question" else "")
    if not f:
        return cat  # карточки нет, но каталог по наличию может быть
    lines = []
    if f.get("code"):       lines.append(f"код картриджа: {f['code']} (можно назвать покупателю)")
    if f.get("kind"):       lines.append(f"тип: {f['kind']}" + (f" / {f['ptype']}" if f.get("ptype") else ""))
    if f.get("chip"):       lines.append("чип: " + _CHIP_RU.get(f["chip"], f["chip"]))
    else:                   lines.append("чип: в карточке НЕ указан (не утверждать)")
    lines.append("заправляемость: " + (f["refillable"] if f.get("refillable") else "в описании НЕ заявлена"))
    if f.get("resource"):   lines.append(f"ресурс, стр: {f['resource']}")
    if f.get("set_info"):   lines.append(f"КОМПЛЕКТАЦИЯ НАБОРА: {f['set_info']}")
    if f.get("color"):      lines.append(f"цвет: {f['color']}")
    if f.get("models"):
        lines.append("СОВМЕСТИМЫЕ МОДЕЛИ (полный список карточки): " + ", ".join(f["models"]))
    else:
        lines.append("совместимые модели: в карточке НЕ перечислены (совместимость не утверждать)")
    if f.get("annot"):
        lines.append("ОПИСАНИЕ КАРТОЧКИ (свободный текст — бери отсюда состав/комплектацию набора, модель "
                     "картриджа, число листов; факты в полях выше приоритетнее): " + f["annot"][:600])
    head = f"Товар: {f.get('name') or r['product_name']} (артикул {f.get('article') or '—'})"
    block = head + "\nФАКТЫ КАРТОЧКИ:\n- " + "\n- ".join(lines)
    if f.get("chip_src"):
        block += f"\n(чип уточнён по Ozon-двойнику того же артикула)"
    return block + ("\n\n" + cat if cat else "")

# Модель для клиентских ответов — важна корректность рассуждения (совместимость, серии, чипы).
# Дефолт — Opus 4.8 (лучшее качество). Переопределяется env FEEDBACK_MODEL (напр. claude-sonnet-5 для дешёвого батча).
MODEL = os.environ.get("FEEDBACK_MODEL", "claude-opus-4-8")
MAX_TOKENS = 500

SYSTEM = """Ты — специалист поддержки интернет-магазина «Цифровой квадрат», продающего картриджи и
расходники для принтеров на маркетплейсах (Wildberries, Ozon). Пишешь ответы на отзывы и вопросы
покупателей от лица магазина.

ТОН (по нашим реальным ответам): вежливо, тепло, кратко (1–3 предложения), на «Вы». На позитив —
благодаришь за выбор товара и магазина. На негатив — сожалеешь, не споришь, зовёшь написать в чат
и обещаешь разобраться (возврат/замена). На вопрос — отвечаешь по делу.

КУДА ЗВАТЬ ЗА ПОМОЩЬЮ — ЗАВИСИТ ОТ ТОГО, КУПЛЕН ЛИ ТОВАР:
• Покупатель УЖЕ купил и пишет о проблеме (отзыв; вопрос вида «купил/пришёл/установил, не печатает,
  ошибка, брак, возврат») — у него есть коробка: зови «в чат по QR-коду на упаковке или в товарном
  чеке внутри коробки».
• Это ВОПРОС ещё НЕ купившего (уточнить совместимость/цвет/наличие/характеристику) — у него НЕТ ни
  коробки, ни товарного чека, ни стикера. НЕЛЬЗЯ отправлять его «по QR-коду на упаковке/в чеке». Либо
  ответь по делу, либо попроси уточнить деталь прямо здесь, в вопросах к товару («уточните, пожалуйста,
  точную модель — подскажем, подойдёт ли»). Не упоминай коробку, упаковку, чек и QR-код.

ГЛАВНОЕ ПРАВИЛО — ФАКТЫ И ЧЕСТНОСТЬ. ХАРАКТЕРИСТИКИ нашего товара (чип, ресурс, цвет, заправляемость,
комплектация набора) бери ТОЛЬКО из CARD_DATA — их не выдумывай.
СОВМЕСТИМОСТЬ с моделью принтера:
• Модель есть в CARD_DATA, или это суффикс-вариант той же серии (CX17 и CX17NF/CX17WF; C1750 и C1750N/W;
  M2000 и M2000DN/DW — общий картридж) → отвечай прямо да/нет.
• Модели покупателя НЕТ в CARD_DATA, но ты ДОСТОВЕРНО ЗНАЕШЬ (частый массовый принтер, ясная серия),
  входит ли он в линейку, которую покрывает наш картридж → можешь ответить да/нет ПО СВОИМ ЗНАНИЯМ
  (пометь grounded=false, поставь честную confidence). Мы продаём СОВМЕСТИМЫЕ картриджи — знание серий
  принтеров это часть работы, отвечай как эксперт, а не отправляй «уточните» там, где ответ очевиден.
• Принтер РЕДКИЙ, тёмный регион-код (напр. L662B), данные противоречат, или ты НЕ уверен → НЕ угадывай:
  верни need_web=true (мы перепроверим внешним поиском) и confidence ≤0.5, в reply мягко попроси уточнить
  или пообещай проверить. Ложное «да, подойдёт» = возврат, это дороже уточнения.
Регион чипа, ресурс, цвет, «оригинал/совместимый» — не выдумывай, только из CARD_DATA. Технические
проблемы («не опознан», «ошибка чипа», «печатает серым») — краткий совет + приглашение в чат, без гарантий.

⛔ НИКОГДА НЕ ВЫДУМЫВАЙ КОДЫ И ЦИФРЫ. Артикул нашего товара, код картриджа (TK-435, LC-421, CF210,
CB540A, 106R036xx и т.п.), номер ресурса в страницах, номер прошивки — называй ТОЛЬКО если он есть в
CARD_DATA или в блоке КАТАЛОГ. Запрещено «подсказывать» покупателю код-замену от знания модели (напр.
«вам нужен TK-170» или «например, LC-421»), если этого кода нет в наших данных — это приводит к неверному
заказу. Если точного артикула/кода под его принтер у нас в данных НЕТ: скажи, что подберём, и попроси
уточнить модель — БЕЗ конкретного кода. Можно назвать модель принтера и факт «подойдёт/не подойдёт»,
но не выдуманный код расходника.

НАЛИЧИЕ/ЦВЕТ. Не утверждай, что какого-то цвета или варианта товара у нас НЕТ в ассортименте, если
это не следует из блока КАТАЛОГ. То, что в ЭТОЙ карточке только один цвет, НЕ значит, что другого нет
в магазине. Если в КАТАЛОГ есть подходящий вариант — назови его (можно артикул); если каталога нет или
он пуст — не отрицай наличие, а предложи покупателю уточнить нужный цвет прямо здесь, поможем подобрать.

ПОДБОР НАШЕГО АРТИКУЛА (важно — это конверсия в продажу). Если ЭТОТ картридж покупателю НЕ подходит
(его принтера нет в списке совместимости и это не вариант серии), но в блоке КАТАЛОГ есть НАШ листинг
именно под его модель принтера — предложи его: «для вашего <модель> подойдёт наш картридж, артикул
<id/арт из КАТАЛОГ>». Бери артикул ТОЛЬКО из блока КАТАЛОГ (это реальные наши листинги), не выдумывай.
Выбирай в КАТАЛОГ строку-КАРТРИДЖ под нужную модель (не фотобарабан, если спрашивали картридж). Если
подходящего листинга в КАТАЛОГ нет — не выдумывай артикул, попроси уточнить модель, поможем подобрать.
МОДЕЛЬ ПРИНТЕРА НЕ ПЕРЕСПРАШИВАЙ, если она уже есть в названии товара / CARD_DATA — используй её.
Пример: товар «Картридж для HP DeskJet 3745» + вопрос «а цветной есть?» → модель уже известна (HP
DeskJet 3745), смотри КАТАЛОГ и отвечай «да, есть цветной, артикул …» или предложи проверить наличие —
но НЕ проси покупателя назвать модель принтера.

ФОРМАТ ОТВЕТА — СТРОГО один JSON-объект, без markdown:
{"reply": "<готовый к публикации текст ответа>",
 "route": "auto" | "review",
 "confidence": <число 0..1>,
 "grounded": <true если reply полностью опирается на CARD_DATA/факты, иначе false>,
 "need_web": <true если для проверки совместимости нужен внешний веб-поиск (редкий/тёмный код), иначе false>,
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


def _text_of(message):
    """Текст ответа модели без thinking-блоков (relay/модель могут возвращать ThinkingBlock первым)."""
    return "".join(getattr(b, "text", "") for b in message.content
                   if getattr(b, "type", None) == "text").strip()


def _gather(limit=None):
    # весь неотвеченный текстовый backlog (~49 шт), свежие первыми; вопросы в приоритет
    rows = db.query("""SELECT platform,account,kind,ext_id,item_id,product_name,rating,body,pros,cons,payload
        FROM raw_feedback WHERE is_answered=false AND account IN ('wb_acc1','oz_acc1')
        ORDER BY created_at DESC NULLS LAST""")
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
    # relay-эндпоинт (ANTHROPIC_BASE_URL, self-signed по IP) → verify=False, как в Соколе
    base_url = os.environ.get("ANTHROPIC_BASE_URL")
    if base_url:
        import httpx
        client = Anthropic(api_key=key, base_url=base_url, http_client=httpx.Client(verify=False))
    else:
        client = Anthropic(api_key=key)

    items = _gather(limit)
    if not items:
        print("Нет элементов для LLM.", flush=True)
        return
    cf = CardFacts()
    corpus = load_corpus()
    print(f"Справочник наших ответов: {len(corpus.items)} шт (динамические few-shot)", flush=True)

    reqs, idx, cid_compat = [], {}, {}
    for i, r in enumerate(items):
        cid = f"i{i}"
        idx[cid] = r
        cc = _card_data(r, cf)
        cid_compat[cid] = cc
        src = (r["body"] or r["pros"] or r["cons"] or "")
        ex = corpus.retrieve(r["kind"], src, r["product_name"], k=5)
        content = _user_block(r, _name(r), cc, ex)
        reqs.append(Request(custom_id=cid, params=MessageCreateParamsNonStreaming(
            model=MODEL, max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": SYSTEM, "cache_control": {"type": "ephemeral"}}],
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
        raw = _text_of(res.result.message)
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


def dry_run(limit=None):
    """Предпросмотр БЕЗ API: собирает ровно те промпты, что ушли бы в батч (грунтовка + few-shot
    + обращение), и пишет docs/feedback_dry.html. Проверить начинку до траты токенов."""
    items = _gather(limit)
    if not items:
        print("Нет элементов.", flush=True)
        return
    cf = CardFacts()
    corpus = load_corpus()
    print(f"Dry-run: {len(items)} обращений, справочник {len(corpus.items)} ответов. API НЕ вызывается.",
          flush=True)
    import html as _h
    cat_badge = {"question": ("вопрос", "#084298", "#cfe2ff"),
                 "negative": ("негатив", "#842029", "#f8d7da"),
                 "positive": ("позитив", "#0f5132", "#d1e7dd")}
    parts = ["<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#f4f5f7;"
             "color:#1a1a1a;margin:0}@media(prefers-color-scheme:dark){body{background:#161719;"
             "color:#e6e6e6}.card{background:#1f2124;border-color:#2c2f33}.blk{background:#26282b}}"
             "header{padding:18px 26px;background:#5a3fa0;color:#fff}h1{margin:0;font-size:18px}"
             ".wrap{max-width:920px;margin:0 auto;padding:16px}.card{background:#fff;border:1px solid "
             "#e3e5e8;border-radius:11px;padding:14px 16px;margin:14px 0}.meta{font-size:12px;color:#888}"
             ".prod{font-size:12px;color:#5a3fa0;font-weight:600;margin:3px 0 8px}.badge{display:inline-block;"
             "font-size:11px;font-weight:700;padding:2px 8px;border-radius:20px;margin-left:6px}"
             ".blk{background:#eef0f2;border-radius:8px;padding:9px 12px;margin:8px 0;font-size:12.5px;"
             "white-space:pre-wrap;font-family:ui-monospace,Menlo,monospace}.lbl{font-size:11px;font-weight:700;"
             "text-transform:uppercase;color:#5a3fa0;letter-spacing:.4px;display:block;margin:10px 0 3px}"
             "</style><header><h1>Dry-run: что уйдёт в модель (API не вызывается)</h1></header><div class='wrap'>"
             f"<p class='meta'>{len(items)} обращений за 30 дней. Ниже per-item: факты карточки (grounding), "
             "похожие наши прошлые ответы (few-shot) и итоговый USER-блок промпта.</p>"
             f"<div class='blk'><b>SYSTEM (един на все):</b>\n{_h.escape(SYSTEM)}</div>"]
    for r in items:
        cat = _classify(r)
        lab, fg, bg = cat_badge.get(cat, (cat, "#555", "#ddd"))
        cc = _card_data(r, cf)
        src = (r["body"] or r["pros"] or r["cons"] or "")
        ex = corpus.retrieve(r["kind"], src, r["product_name"], k=5)
        ub = _user_block(r, _name(r), cc, ex)
        shown = (r["body"] or "").strip()
        if r["pros"]:
            shown += f"\n[достоинства]: {r['pros']}"
        if r["cons"]:
            shown += f"\n[недостатки]: {r['cons']}"
        parts.append(
            f"<div class='card'><div class='meta'>{_h.escape(r['platform'])} · {_h.escape(r['account'])}"
            f"<span class='badge' style='color:{fg};background:{bg}'>{lab}</span></div>"
            f"<div class='prod'>{_h.escape(r['product_name'] or '')}</div>"
            f"<div class='blk'>{_h.escape(shown.strip() or '(без текста)')}</div>"
            f"<span class='lbl'>Факты карточки (grounding)</span>"
            f"<div class='blk'>{_h.escape(cc or '(карточка не сшита — фактов нет)')}</div>"
            f"<span class='lbl'>USER-блок промпта (few-shot + карточка + обращение)</span>"
            f"<div class='blk'>{_h.escape(ub)}</div></div>")
    parts.append("</div>")
    out = BASE_DIR / "docs" / "feedback_dry.html"
    out.write_text("".join(parts), encoding="utf-8")
    print(f"Готово → {out}", flush=True)


if __name__ == "__main__":
    lim = None
    if "--limit" in sys.argv:
        lim = int(sys.argv[sys.argv.index("--limit") + 1])
    if "--dry" in sys.argv:
        dry_run(lim)
    else:
        run(lim)
