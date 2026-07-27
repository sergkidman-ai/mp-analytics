# ENV-переменные feedback-подсистемы (текущие значения, БЕЗ секретов)

## Флаги режима (текущие значения из .env)
| Переменная | Текущее | Дефолт в коде | Смысл |
|---|---|---|---|
| `FEEDBACK_MODERATION` | **1** | 0 | 1 = класть ответы в очередь модерации Telegram |
| `FEEDBACK_LIVE_SEND` | **не задано → 0** | 0 | 0 = dry-run (реально НЕ отправляет), 1 = живая отправка |
| `FEEDBACK_WEB_SPLIT` | **1** | 0 | 1 = разбор сложной совместимости на Sonnet (поиск на дипсике) |
| `FEEDBACK_MODEL` | deepseek-v4-pro | — | основная модель ответов |
| `FEEDBACK_WEB_MODEL` | deepseek-v4-pro | claude-sonnet-5 | модель веб-ПОИСКА |
| `FEEDBACK_WEB_ANALYSIS_MODEL` | не задано | claude-sonnet-5 | модель веб-АНАЛИЗА (при SPLIT=1) |
| `FEEDBACK_WEB_MAX_USES` | не задано | 1 | раундов веб-поиска (держим 1 = дёшево) |
| `FEEDBACK_WEB_MAX_TOKENS` | не задано | 2500 | потолок токенов веб-вызова |
| `FEEDBACK_MAX_TOKENS` | не задано | (см. feedback_llm) | потолок токенов основного ответа |
| `FEEDBACK_MOD_WINDOW_DAYS` | не задано | 30 | окно показа карточек/сводки (дней) |
| `FEEDBACK_MOD_BATCH_CAP` | не задано | 60 | предохранитель «показать всё» |
| `FEEDBACK_QUEUE_POLL_SEC` | не задано | 15 | (устар. — авто-рассылка убрана) |

## Telegram-бот модерации (значения ID не секретны)
| Переменная | Текущее | Смысл |
|---|---|---|
| `TG_FEEDBACK_BOT_TOKEN` | (задан) | токен ОТДЕЛЬНОГО бота @reviewswbozon2_bot (не invoice_bot!) |
| `TG_FEEDBACK_NOTIFY_ID` | 1031321444 | кому слать карточки (можно список через запятую) |
| `TG_FEEDBACK_ALLOWED_IDS` | 1031321444 | кто может жать кнопки |
| (фолбэк) `TG_BOT_TOKEN` / `TG_NOTIFY_ID` / `TG_ALLOWED_IDS` | общие с invoice_bot | используются, если TG_FEEDBACK_* не заданы (⚠️ конфликт 409) |

## Секреты API (значения НЕ показываю — в .env, gitignored)
Все заданы: `WB_TOKEN_ACC1`, `WB_TOKEN_ACC2`⚠️(без scope «Вопросы и отзывы» → 401),
`OZON_CLIENT_ID_ACC1/ACC2`, `OZON_API_KEY_ACC1/ACC2`, `YANDEX_API_KEY_ACC1`,
`YANDEX_BUSINESS_ID_ACC1`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_BASE_URL`.

## Что менять при боевом включении
1. Оставить `FEEDBACK_MODERATION=1`, `FEEDBACK_WEB_SPLIT=1` (уже так).
2. Для живой отправки — выставить `FEEDBACK_LIVE_SEND=1` (сейчас dry-run). Делать ТОЛЬКО после
   контролируемого теста на 1 ответе.
3. WB acc2 заработает автоматически после перевыпуска `WB_TOKEN_ACC2` со scope «Вопросы и отзывы».
