"""reports/llm_client.py — единый выбор LLM-провайдера по ИМЕНИ МОДЕЛИ.

Anthropic-совместимый SDK работает и для Anthropic (через relay Сокола), и для DeepSeek
(api.deepseek.com/anthropic). Выбор по префиксу модели:
  deepseek-*  → https://api.deepseek.com/anthropic + DEEPSEEK_API_KEY (прямой доступ, TLS штатный)
  иначе       → Anthropic relay (ANTHROPIC_BASE_URL, self-signed по IP → verify=False)

Переключение движка = смена FEEDBACK_MODEL / FEEDBACK_WEB_MODEL в .env, без правок логики.
Клиенты кэшируются на процесс (по провайдеру).
"""
import os

_CACHE = {}


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
