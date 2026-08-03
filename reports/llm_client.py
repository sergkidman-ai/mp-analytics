"""reports/llm_client.py — единый выбор LLM-провайдера по ИМЕНИ МОДЕЛИ.

Anthropic-совместимый SDK работает и для Anthropic (через relay Сокола), и для DeepSeek
(api.deepseek.com/anthropic). Выбор по префиксу модели:
  deepseek-*  → https://api.deepseek.com/anthropic + DEEPSEEK_API_KEY (прямой доступ, TLS штатный)
  иначе       → Anthropic relay (ANTHROPIC_BASE_URL, self-signed по IP → verify=False)

Переключение движка = смена FEEDBACK_MODEL / FEEDBACK_WEB_MODEL в .env, без правок логики.
Клиенты кэшируются на процесс (по провайдеру).
"""
import os
import time

_CACHE = {}


class LlmUnavailable(Exception):
    """Модель не ответила (529 Overloaded / таймаут / обрыв) после всех повторов.

    Отдельный тип, а не строка в ответе: текст ошибки НИКОГДА не должен становиться черновиком —
    инцидент 03.08.2026, когда «Error code: 529 … overloaded_error» уехал в карточку модерации как
    «Наш ответ» по вопросу про 62XL. Тот же класс дыры, что салваж битого JSON в ответ покупателю.
    Кто ловит — оставляет запись БЕЗ черновика до следующего цикла."""


RETRIES = int(os.environ.get("FEEDBACK_LLM_RETRIES", "3"))
RETRY_SLEEP = float(os.environ.get("FEEDBACK_LLM_RETRY_SLEEP", "5"))


def create_with_retry(client, **kw):
    """client.messages.create с повторами: RETRIES попыток, пауза 5 → 15 → 45 с (×3).
    Ошибка любая (529, таймаут, обрыв соединения); исчерпали попытки → LlmUnavailable."""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            return client.messages.create(**kw)
        except Exception as e:
            last = e
            if attempt < RETRIES:
                pause = RETRY_SLEEP * (3 ** (attempt - 1))
                print(f"   LLM попытка {attempt}/{RETRIES} не удалась ({type(e).__name__}: "
                      f"{str(e)[:80]}) — пауза {pause:.0f} с", flush=True)
                time.sleep(pause)
    raise LlmUnavailable(f"{type(last).__name__}: {str(last)[:200]}") from last


def is_deepseek(model):
    return bool(model) and model.lower().startswith("deepseek")


def client_for(model):
    """Anthropic-совместимый клиент для нужного провайдера (по имени модели). Кэшируется."""
    prov = "deepseek" if is_deepseek(model) else "anthropic"
    if prov in _CACHE:
        return _CACHE[prov]
    from anthropic import Anthropic
    import httpx
    if prov == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError("DEEPSEEK_API_KEY не задан в .env — модель deepseek-* недоступна")
        c = Anthropic(api_key=key, base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/anthropic"),
                      http_client=httpx.Client(timeout=180))
    else:
        key = os.environ["ANTHROPIC_API_KEY"]
        base = os.environ.get("ANTHROPIC_BASE_URL")
        c = (Anthropic(api_key=key, base_url=base, http_client=httpx.Client(verify=False, timeout=180))
             if base else Anthropic(api_key=key, http_client=httpx.Client(timeout=180)))
    _CACHE[prov] = c
    return c
