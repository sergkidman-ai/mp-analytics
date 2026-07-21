"""reports/feedback_web.py — ИСТОЧНИК №3: внешний веб-поиск для вопросов совместимости.

Когда карточка НЕ подтверждает модель (её нет в списке и это не вариант серии), а покупатель
спрашивает «подойдёт ли для <модель>» — ищем в вебе серверным инструментом Claude (web_search
через relay) и определяем, относится ли принтер покупателя к серии, которую наш картридж
покрывает. Это ровно кейс Epson CX17NF (L662B) — вариант серии CX17, картридж C13S050614 подходит.

Возвращает {verdict: yes|no|unclear, reply, sources[], note} или None (веб недоступен/пусто).
Ответ помечается source=web и в фазе «только черновики» всегда идёт на вычитку (веб-факт человек
подтверждает перед публикацией).
"""
import os
import re
import json
import sys
import pathlib

BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

WEB_SYSTEM = """Ты — специалист поддержки магазина картриджей «Цифровой квадрат» (Wildberries, Ozon).
Покупатель спрашивает, подойдёт ли наш картридж к его принтеру. В КАРТОЧКЕ этой модели нет — но
модель может быть вариантом той же серии (суффиксы -N/-NF/-WF/-DN/-DW/-DNW и служебные коды вроде
L662B — это варианты одной линейки, картридж у них общий). Найди в вебе, к какой серии принтеров
относится модель покупателя и какие картриджи для неё штатные, и сверь с нашим товаром.

Отвечай в НАШЕМ тоне: вежливо, кратко, на «Вы», 1–3 предложения, без ссылок и артикулов
производителя в тексте ответа (просто «да, подойдёт»/«нет, для этой модели нужен другой картридж»).

ЕСЛИ НАШ ТЕКУЩИЙ КАРТРИДЖ НЕ ПОДХОДИТ, но в присланных ФАКТАХ НАШЕЙ КАРТОЧКИ есть блок «КАТАЛОГ»
с нашим листингом именно под модель принтера покупателя — предложи его: «нет, этот не подойдёт, но для
вашего <модель> у нас есть подходящий — артикул <id/арт из КАТАЛОГ>». Артикул бери ТОЛЬКО из блока
КАТАЛОГ, не выдумывай; выбирай строку-картридж (не фотобарабан, если спрашивали картридж).
Это вопрос ДО покупки — у покупателя НЕТ коробки, упаковки и товарного чека: НЕ отправляй его «в чат
по QR-коду на упаковке/в чеке». Если совместимость неясна — попроси уточнить точную модель прямо здесь.
Утверждай «подойдёт», ТОЛЬКО если веб-источники ясно показывают, что принтер покупателя входит в ту
же серию/список совместимости, что и наш картридж. Если данные противоречивы — verdict=unclear.

Верни СТРОГО один JSON без markdown:
{"verdict":"yes|no|unclear","reply":"<текст ответа покупателю>","note":"<на чём основан вывод: серия/источник>"}"""


def _text_blocks(message):
    return [b for b in message.content if getattr(b, "type", None) == "text"]


def _sources(message):
    out = []
    for b in message.content:
        if getattr(b, "type", None) == "web_search_tool_result":
            for res in (getattr(b, "content", None) or []):
                u, t = getattr(res, "url", None), getattr(res, "title", None)
                if u:
                    out.append({"url": u, "title": (t or "")[:120]})
    return out[:6]


def _snippets(message):
    """Как _sources, но тянет и тело сниппета выдачи, если провайдер его отдаёт открытым текстом
    (у web_search тело часто лежит в encrypted_content — тогда останется только title/url).
    → [{title,url,text}] для передачи анализатору без повторного веб-поиска."""
    out = []
    for b in message.content:
        if getattr(b, "type", None) == "web_search_tool_result":
            for res in (getattr(b, "content", None) or []):
                u = getattr(res, "url", None)
                if not u:
                    continue
                txt = getattr(res, "text", None) or getattr(res, "snippet", None) or ""
                out.append({"url": u, "title": (getattr(res, "title", None) or "")[:120],
                            "text": (txt or "")[:500]})
    return out[:6]


# Веб — редкий фолбэк, читает страницы: держим ДЁШЕВО. Модель по env (дефолт sonnet, не Opus),
# один поиск (max_uses=1) вместо трёх — именно агентный многораундовый цикл раздувал стоимость.
WEB_MODEL = os.environ.get("FEEDBACK_WEB_MODEL", "claude-sonnet-5")
WEB_MAX_USES = int(os.environ.get("FEEDBACK_WEB_MAX_USES", "1"))

# Разделение веб-кейса совместимости: ПОИСК (чтение страниц = основная стоимость) остаётся на
# дешёвой модели WEB_MODEL, а РАССУЖДЕНИЕ по найденному отдаём более сильной модели (в A/B она
# лучше ловит неоднозначные модели принтеров). Только для web_compat; web_fact целиком на WEB_MODEL.
WEB_ANALYSIS_MODEL = os.environ.get("FEEDBACK_WEB_ANALYSIS_MODEL", "claude-sonnet-5")
WEB_SPLIT = os.environ.get("FEEDBACK_WEB_SPLIT", "0") == "1"
_ANALYSIS_CLIENT = None


def _analysis_client():
    """Свой клиент под WEB_ANALYSIS_MODEL: клиент из web_compat — дипсиковый, им Sonnet не позвать."""
    global _ANALYSIS_CLIENT
    if _ANALYSIS_CLIENT is None:
        from reports.llm_client import client_for
        _ANALYSIS_CLIENT = client_for(WEB_ANALYSIS_MODEL)
    return _ANALYSIS_CLIENT


REANALYZE_SYSTEM = """Ты — старший специалист поддержки магазина совместимых картриджей «Цифровой
квадрат». Младший специалист уже нашёл в вебе информацию и вынес ЧЕРНОВОЙ вердикт «подойдёт ли наш
картридж принтеру покупателя». Твоя задача — вынести БЕЗОПАСНЫЙ финальный вердикт по фактам карточки,
вопросу, найденным источникам и черновику.

ГЛАВНОЕ ПРАВИЛО — неоднозначная модель. Если номер модели покупателя встречается в РАЗНЫХ линейках
(например «7510» есть и у HP Photosmart, и у HP OfficeJet — это РАЗНЫЕ картриджи), НЕ подтверждай
совместимость вслепую: verdict=unclear и вежливо попроси уточнить точную модель/серию прямо здесь.
Подтверждай «yes» ТОЛЬКО если источники ясно показывают, что принтер покупателя входит в ту же
серию/список совместимости, что и наш картридж. Варианты одной линейки (суффиксы -N/-NF/-WF/-DN/-DW
и служебные коды) — это «yes». Если наш не подходит, а в фактах карточки есть блок «КАТАЛОГ» с нашим
листингом под эту модель — предложи его. Данные противоречивы → unclear.

Тон: вежливо, на «Вы», 1–3 предложения, без ссылок и артикулов производителя в тексте. Это вопрос ДО
покупки — НЕ отправляй «в чат по QR-коду на упаковке/в чеке». Верни СТРОГО один JSON без markdown:
{"verdict":"yes|no|unclear","reply":"<текст ответа покупателю>","note":"<на чём основан вывод>"}"""


def _reanalyze_compat(question, product_name, card_summary, snippets, draft):
    """Второй проход по web_compat более сильной моделью БЕЗ инструментов (без повторного поиска).
    → {verdict,reply,note} | None (тогда web_compat оставит дипсиковый черновик)."""
    src = "\n".join(f"- {s.get('title') or s['url']}: {(s.get('text') or '')[:300]}"
                    for s in (snippets or [])) or "(источники без открытого текста — опирайся на черновик)"
    prompt = (f"НАШ ТОВАР: {product_name}\n"
              f"ФАКТЫ НАШЕЙ КАРТОЧКИ:\n{(card_summary or '(нет)')[:1200]}\n\n"
              f"ВОПРОС ПОКУПАТЕЛЯ:\n\"\"\"{(question or '')[:600]}\"\"\"\n\n"
              f"НАЙДЕНО В ВЕБЕ:\n{src[:1600]}\n\n"
              f"ЧЕРНОВОЙ ВЫВОД МЛАДШЕГО СПЕЦИАЛИСТА:\n"
              f"verdict={draft.get('verdict')}; {(draft.get('reply') or '')[:400]}\n"
              f"обоснование: {(draft.get('note') or '')[:200]}\n\n"
              f"Вынеси безопасный финальный вердикт. Верни JSON.")
    m = _analysis_client().messages.create(
        model=WEB_ANALYSIS_MODEL, max_tokens=1200,
        system=[{"type": "text", "text": REANALYZE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": prompt}])
    txt = "".join(b.text for b in _text_blocks(m)).strip()
    mm = re.search(r"\{.*\}", txt, re.S)
    if not mm:
        return None
    try:
        return json.loads(mm.group(0))
    except Exception:
        return None


def web_compat(client, question, product_name, card_summary, model=None):
    """Веб-проверка совместимости. client — тот же Anthropic(relay). None если недоступно/пусто."""
    model = model or WEB_MODEL
    prompt = (f"НАШ ТОВАР: {product_name}\n"
              f"ФАКТЫ НАШЕЙ КАРТОЧКИ (для сверки серии/ресурса):\n{(card_summary or '(нет)')[:1200]}\n\n"
              f"ВОПРОС ПОКУПАТЕЛЯ:\n\"\"\"{(question or '')[:600]}\"\"\"\n\n"
              f"Определи по вебу, подойдёт ли наш картридж этому принтеру. Верни JSON.")
    try:
        sysparam = ([{"type": "text", "text": WEB_SYSTEM, "cache_control": {"type": "ephemeral"}}]
                    if not model.lower().startswith("deepseek") else WEB_SYSTEM)
        m = client.messages.create(
            model=model, max_tokens=int(os.environ.get("FEEDBACK_WEB_MAX_TOKENS", "2500")), system=sysparam,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_MAX_USES}],
            messages=[{"role": "user", "content": prompt}])
    except Exception as e:
        return {"verdict": "unclear", "reply": "", "sources": [], "note": f"web-error: {str(e)[:120]}", "error": True}
    txt = "".join(b.text for b in _text_blocks(m)).strip()
    data = None
    mm = re.search(r"\{.*\}", txt, re.S)
    if mm:
        try:
            data = json.loads(mm.group(0))
        except Exception:
            data = None
    if not data:
        return {"verdict": "unclear", "reply": txt[:300], "sources": _sources(m), "note": "parse-fail"}
    data["sources"] = _sources(m)
    data.setdefault("note", "")
    # Разделение: поиск отработал на дешёвой модели, рассуждение отдаём WEB_ANALYSIS_MODEL.
    if WEB_SPLIT and not WEB_ANALYSIS_MODEL.lower().startswith("deepseek"):
        try:
            re2 = _reanalyze_compat(question, product_name, card_summary, _snippets(m), data)
            if re2 and (re2.get("reply") or "").strip():
                data["verdict"] = re2.get("verdict", data.get("verdict"))
                data["reply"] = re2["reply"]
                data["note"] = "web-analysis: " + (re2.get("note") or "")
        except Exception as e:
            # тихий фолбэк: остаётся дипсиковый черновик
            data["note"] = (data.get("note") or "") + f" | reanalyze-skip: {str(e)[:80]}"
    return data


FACT_SYSTEM = """Ты — специалист поддержки магазина совместимых картриджей «Цифровой квадрат» (Wildberries, Ozon).
Покупатель задал вопрос о ХАРАКТЕРИСТИКАХ товара, которых нет в нашей карточке (вес/граммы тонера в тубе,
тип чернил — пигментные или водорастворимые, состав, типичный ресурс для модели), ЛИБО спрашивает, какой
картридж нужен его принтеру, ЛИБО задаёт ПРОЦЕДУРНЫЙ/технический вопрос по эксплуатации (как отключить
проверку чипа, нужна ли перепрошивка, как сбросить счётчик тонера, почему принтер «не видит» картридж или
просит оригинальный). Найди ответ в вебе по конкретной модели картриджа/принтера из НАШ ТОВАР.

Отвечай в нашем тоне: вежливо, на «Вы», 1–3 предложения, без ссылок и посторонних артикулов производителя
в тексте. Опирайся на веб-данные по конкретной модели. Если точных данных по нашей модели нет — дай типичное
для этого класса значение с оговоркой «обычно/как правило», НЕ выдумывай точных цифр. Про производителя:
совместимые картриджи — производство Китай.

ПРОЦЕДУРНЫЙ вопрос (чип/прошивка/сброс) — дай КОНКРЕТНЫЙ по модели ответ: для совместимых картриджей, как
правило, чип уже установлен и картридж ставится без прошивки; но если у модели включена «динамическая
защита» (dynamic security) / стоит новая прошивка, которая блокирует неоригинал — честно поясни, что и как
нужно (обновление отключить/не ставить, при необходимости откатить прошивку) и нужно ли это конкретно для
его модели. Не гони пользователя «написать нам», если ответ есть в вебе.

ЕСЛИ вопрос про подбор картриджа для принтера и в присланных ФАКТАХ есть блок «КАТАЛОГ» с нашим листингом
под эту модель — предложи его площадочный артикул (артикул ВБ / Ozon SKU) из КАТАЛОГ, не выдумывай. Если в
КАТАЛОГ подходящего нет — назови, картридж какой серии нужен этому принтеру, и предложи уточнить/подобрать.
Это вопрос ДО покупки — НЕ отправляй «в чат по QR-коду на упаковке/в чеке».

ЕСЛИ передан блок «УЖЕ ГОТОВАЯ ЧАСТЬ ОТВЕТА» — в ней совместимость уже подтверждена. НЕ повторяй её и НЕ
здоровайся заново: ответь ТОЛЬКО на оставшийся (процедурный/фактический) вопрос, продолжением в 1–2
предложения, чтобы текст естественно дописался к готовой части.

Верни СТРОГО один JSON без markdown:
{"answer":"<текст ответа покупателю>","note":"<источник/на чём основан вывод>"}"""


def web_fact(client, question, product_name, card_summary, model=None, draft=""):
    """Веб-поиск ФАКТА по ТТХ / подбор картриджа / процедурный вопрос. draft — уже готовая часть ответа
    (совместимость): при наличии веб дополняет её, не повторяя. → {answer, sources[], note} | error."""
    model = model or WEB_MODEL
    draft_line = (f"УЖЕ ГОТОВАЯ ЧАСТЬ ОТВЕТА (не повторяй её, только допиши недостающее):\n"
                  f"\"\"\"{(draft or '')[:400]}\"\"\"\n\n" if (draft or "").strip() else "")
    prompt = (f"НАШ ТОВАР: {product_name}\n"
              f"ФАКТЫ НАШЕЙ КАРТОЧКИ (в т.ч. блок КАТАЛОГ, если есть):\n{(card_summary or '(нет)')[:1400]}\n\n"
              f"{draft_line}"
              f"ВОПРОС ПОКУПАТЕЛЯ:\n\"\"\"{(question or '')[:600]}\"\"\"\n\n"
              f"Найди ответ в вебе. Верни JSON.")
    try:
        sysparam = ([{"type": "text", "text": FACT_SYSTEM, "cache_control": {"type": "ephemeral"}}]
                    if not model.lower().startswith("deepseek") else FACT_SYSTEM)
        m = client.messages.create(
            model=model, max_tokens=int(os.environ.get("FEEDBACK_WEB_MAX_TOKENS", "2500")), system=sysparam,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_MAX_USES}],
            messages=[{"role": "user", "content": prompt}])
    except Exception as e:
        return {"answer": "", "sources": [], "note": f"web-error: {str(e)[:120]}", "error": True}
    txt = "".join(b.text for b in _text_blocks(m)).strip()
    data = None
    mm = re.search(r"\{.*\}", txt, re.S)
    if mm:
        try:
            data = json.loads(mm.group(0))
        except Exception:
            data = None
    if not data:
        return {"answer": "", "sources": _sources(m), "note": "parse-fail"}
    data["sources"] = _sources(m)
    data.setdefault("note", "")
    data.setdefault("answer", "")
    return data


if __name__ == "__main__":
    import warnings
    warnings.filterwarnings("ignore")
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
    from anthropic import Anthropic
    import httpx
    c = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], base_url=os.environ["ANTHROPIC_BASE_URL"],
                  http_client=httpx.Client(verify=False))
    r = web_compat(c, "Подойдёт для Epson AcuLaser CX17NF MODEL L662B?",
                   "Картридж для Epson AcuLaser CX17",
                   "СОВМЕСТИМЫЕ МОДЕЛИ: Epson AcuLaser CX17, Epson AL 1700, X17; ресурс 2200; цвет Чёрный")
    print(json.dumps(r, ensure_ascii=False, indent=2))
