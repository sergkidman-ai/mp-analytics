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


# Веб — редкий фолбэк, читает страницы: держим ДЁШЕВО. Модель по env (дефолт sonnet, не Opus),
# один поиск (max_uses=1) вместо трёх — именно агентный многораундовый цикл раздувал стоимость.
WEB_MODEL = os.environ.get("FEEDBACK_WEB_MODEL", "claude-sonnet-5")
WEB_MAX_USES = int(os.environ.get("FEEDBACK_WEB_MAX_USES", "1"))


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
    return data


FACT_SYSTEM = """Ты — специалист поддержки магазина совместимых картриджей «Цифровой квадрат» (Wildberries, Ozon).
Покупатель задал вопрос о ХАРАКТЕРИСТИКАХ товара, которых нет в нашей карточке (вес/граммы тонера в тубе,
тип чернил — пигментные или водорастворимые, состав, типичный ресурс для модели), ИЛИ спрашивает, какой
картридж нужен его принтеру. Найди ответ в вебе по конкретной модели картриджа/принтера из НАШ ТОВАР.

Отвечай в нашем тоне: вежливо, на «Вы», 1–3 предложения, без ссылок и посторонних артикулов производителя
в тексте. Опирайся на веб-данные по конкретной модели. Если точных данных по нашей модели нет — дай типичное
для этого класса значение с оговоркой «обычно/как правило», НЕ выдумывай точных цифр. Про производителя:
совместимые картриджи — производство Китай.

ЕСЛИ вопрос про подбор картриджа для принтера и в присланных ФАКТАХ есть блок «КАТАЛОГ» с нашим листингом
под эту модель — предложи его площадочный артикул (артикул ВБ / Ozon SKU) из КАТАЛОГ, не выдумывай. Если в
КАТАЛОГ подходящего нет — назови, картридж какой серии нужен этому принтеру, и предложи уточнить/подобрать.
Это вопрос ДО покупки — НЕ отправляй «в чат по QR-коду на упаковке/в чеке».

Верни СТРОГО один JSON без markdown:
{"answer":"<текст ответа покупателю>","note":"<источник/на чём основан вывод>"}"""


def web_fact(client, question, product_name, card_summary, model=None):
    """Веб-поиск ФАКТА по ТТХ или подбор картриджа по модели принтера. → {answer, sources[], note} | error."""
    model = model or WEB_MODEL
    prompt = (f"НАШ ТОВАР: {product_name}\n"
              f"ФАКТЫ НАШЕЙ КАРТОЧКИ (в т.ч. блок КАТАЛОГ, если есть):\n{(card_summary or '(нет)')[:1400]}\n\n"
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
