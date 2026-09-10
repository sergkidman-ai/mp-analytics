# поток: rev
"""Цена вызова модели — одно место на весь поток.

Отдельный модуль, потому что цену считают трое: синхронный счётчик в `feedback_today`,
сборщик результатов батча в `llm_batch` и замер «до/после» в `tools/rev_llm_cost.py`.
Пока прайс лежал внутри `feedback_today`, батч не мог его взять без кольцевого импорта.

Цены — $ за 1M токенов (вход, выход) по прайсу Anthropic. DeepSeek в контуре задачи не
тарифицируется (отдельный биллинг) и намеренно отсутствует: неизвестная модель считается по 0,
чтобы неверная цифра не выглядела точной.
"""

PRICING = {
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-sonnet-5": (3.00, 15.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5-20251001": (1.00, 5.00),
}

# Message Batches API — половина цены синхронного вызова.
BATCH_DISCOUNT = 0.5

# Суффикс имени модели в логе стоимости: в разрезе по моделям видно, сколько ушло батчем,
# а сколько одиночными вызовами. Без него экономию батча нечем показать.
BATCH_SUFFIX = " (batch)"


def is_batch(model):
    return bool(model) and model.endswith(BATCH_SUFFIX)


def base_model(model):
    return model[:-len(BATCH_SUFFIX)] if is_batch(model) else model


def cost(model, tokens_in, tokens_out):
    """$ за вызов. Имя модели с суффиксом ' (batch)' считается со скидкой 50 %."""
    pin, pout = PRICING.get(base_model(model), (0.0, 0.0))
    c = (tokens_in or 0) / 1e6 * pin + (tokens_out or 0) / 1e6 * pout
    return c * BATCH_DISCOUNT if is_batch(model) else c
