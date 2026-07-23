# Alfa API (Альфа-Банк H2H) — интеграция

> Поток: **inv**. Статус: **проектирование** (2026-07-23). Живые вызовы заблокированы паролем к архиву сертификата.

Цель — три задачи по расчётному счёту в Альфа-Банке:
1. **Выписки** — забирать по расписанию (ежедневно) и/или по событию поступления платежа.
2. **В МойСклад** — проводки выписки → входящие/исходящие платежи МС.
3. **Черновики платёжных поручений** — формировать из МС/счетов, отправлять в банк как **неподписанный черновик** (человек подписывает вручную в вебе Альфа-Бизнес).

## Какой это API

У Альфы два H2H-продукта — **не путать**:
- **Современный REST «Alfa API»** — `baas.alfabank.ru` / `sandbox.alfabank.ru`, JSON, OpenID Connect + mutual-TLS. **← наш** (по scopes и по имени `baas_swagger_2026.p12`).
- Легаси **1С-DirectBank** — XML поверх `grampus-int.alfabank.ru/API/v1/directbank`, сессии Logon/SendPack, `.pfx`-подпись. Это **не мы** (репы `github.com/alfa-laboratory/*` — про него).

## Тестовый доступ (что получено)

- `client_id`: `5052e56f-8bca-4cda-9d81-57422e0ebf93`
- `scope`: `openid customer transactions signature profile email phone eio role inn`
- `redirect_uri`: `http://localhost`
- Архив `test_cert.zip` (**запаролен** — пароль по запросу на `alfa_api@alfabank.ru`):
  - `sandbox_cert_2026.cer` + `sandbox_key_2026.key` — клиентский mTLS серт+ключ (sandbox)
  - `baas_swagger_2026.p12` — PKCS#12 бандл
  - `root_apica_2022.cer`, `sub_root_apica_2022.cer`, `apica_2022_chain.cer` — CA-цепочка Альфы (APICA)

Секреты — только в `.env` и на диске вне git (правило 4 проекта). Ключ/пароли в чат не выводить.

## Базовые хосты

| Назначение | Sandbox | Prod |
|---|---|---|
| Authorize (получить code) | `https://id-sandbox.alfabank.ru/oidc/authorize` | `https://id.alfabank.ru/oidc/authorize` |
| Token / refresh | `https://sandbox.alfabank.ru/oidc/token` | `https://baas.alfabank.ru/oidc/token` |
| UserInfo | `https://sandbox.alfabank.ru/oidc/userinfo` | `https://baas.alfabank.ru/oidc/userinfo` |
| API-методы (mTLS) | `https://sandbox.alfabank.ru/api/...` | `https://baas.alfabank.ru/api/...` |

mTLS-сертификат предъявляется на **API-хосте** (`sandbox.alfabank.ru`) и **сам выбирает режим**: тестовый серт → тестовая инфра/заглушки, даже на prod-хосте.

## OAuth 2.0 / OpenID Connect

- **Флоу:** Authorization Code Flow H2H. (`client_credentials` существует, но требует более тяжёлой сертификации — H2H идёт через authorization_code.)
- `GET /oidc/authorize?response_type=code&client_id=…&redirect_uri=http://localhost&scope=…&state=…` → `code`.
- `POST /oidc/token` меняет `code` → `access_token` + `refresh_token`.
- **refresh_token одноразовый** — повторное использование → `token not found`, тогда заново с authorize. **Ротировать на каждом refresh**, сохранять новый.
- Время жизни access_token настраивается банком (~3600с, уточнить). Ошибки: `invalid_token`, `insufficient_scope`, `Client was not found`.
- На каждый вызов: `Authorization: Bearer {access_token}` + клиентский TLS-серт.

### Проблема автоматизации (важно для задачи 1)

Authorization Code Flow требует **интерактивного входа** представителя компании через Alfa-ID (браузер, `redirect_uri=http://localhost`). Для headless-крона это значит:
- **Разовый ручной вход** → получаем первый `refresh_token`, кладём в `.env`/защищённый стор.
- Далее демон **ротирует** refresh_token сам (каждый вызов сохраняет новый).
- Если refresh протух (банк-сайд лимит) — нужен повторный ручной вход.
- Уточнить у Альфы, доступен ли `client_credentials` для нашего договора — он снял бы интерактивность. Иначе живём на ротации refresh.

## Эндпоинты под наши задачи

### Выписки (scope `transactions`, read-only — подпись НЕ нужна)

Генерация файла выписки **асинхронная: request → poll → download**.

| Действие | Метод + путь |
|---|---|
| Создать запрос на файл выписки | `POST /api/jp/v1/accounts/{accountNumber}/transactions/files/requests` |
| Статус/результат запроса | `GET /api/jp/v1/accounts/transactions/files/requests/{requestId}` |
| Выписка MT940 (SWIFT, синхронно) | `GET /api/jp/v1/accounts/{accountNumber}/transactions/MT940` (`Accept: text/plain`) |
| Выписка по корсчёту | `GET /api/jp/v1/correspondent-accounts/{accountNumber}/transactions` |
| История операций | `GET /api/.../operations-history/operations-list/v1` |

- Параметры: `accountNumber` (path), диапазон дат (body/query).
- Мин. дата: не раньше 5 лет до 1 января текущего года (в 2026 → `2021-01-01`).

### Платёжные поручения (scope `signature`)

| Действие | Метод + путь |
|---|---|
| Создать рублёвую платёжку (черновик/подписанную) | `POST /api/jp/v2/payments` |
| Отправить на подпись / сменить статус | `PATCH /api/jp/v2/payments/{externalId}/state` body `{"bankStatus":"PARTSIGNED"}` |
| Атрибуты платёжки | `GET /api/jp/v2/payments/{externalId}` |
| Статус | `GET /api/jp/v2/payments/{externalId}/state` |
| Печатная форма PDF | `POST /api/jp/v2/payments/{externalId}/print-form/download` |
| Реестр платежей | `POST /api/jp/v2/payments/registries` |

- **Неподписанный черновик (наш кейс):** `POST /payments` **без** `digestSignatures` → черновик; виден в вебе Альфа-Бизнес → «Платежи в работе» → «На подпись», где человек подписывает. (Если слать сразу подписанным из API — включить `digestSignatures`, тогда `payeeInn` обязателен.)
- **Обязательные поля:** `number`, `date`, `amount`, `urgencyCode` (напр. `NORMAL`), `deliveryKind` («электронно»); блок плательщика `payerName/payerInn/payerKpp/payerAccount/payerBankBic/payerBankCorrAccount`; блок получателя `payeeName/payeeInn/payeeKpp/payeeAccount/payeeBankBic/payeeBankCorrAccount`; бюджетные — `departmentalInfo` (`uip`, `drawerStatus101`, …). BIC — поля `payerBankBic`/`payeeBankBic`.
- **Подпись** (если понадобится подписывать из API): PKCS#7 Detached, DER; подписывается тело POST; `digestSignatures[] = {base64Encoded, certificateUuid}`. Одинарная/двойная подпись.
- ⚠️ Уточнить на живой схеме: точные имена полей `назначение платежа` (purpose) и НДС.

### Счета/клиент (scopes `customer profile inn role eio`)

| Действие | Метод + путь |
|---|---|
| Список счетов + остатки | `GET /api/pp/v1/accounts` |
| Реквизиты счёта | `GET /api/.../accounts/requisites/v1` |
| Профиль представителя | `GET /oidc/userinfo` |

- Ответ `accounts[]`: `mnemonic, number, type, typeDescription, status, dateCreated, balance{currency, amount, minorUnits, holds}`. `number` отсюда → в вызовы выписок/платежей.
- ⚠️ Префикс списка счетов: индексируется как `/api/pp/v1/accounts`, а выписки/платежи — `/api/jp/v1|v2/...` (`jp`=юрлицо). Проверить `jp` vs `pp` на живой схеме.

## Архитектура (план реализации)

```
collectors/alfa_bank.py        # клиент: mTLS-сессия + OIDC-токены (get/post/patch)
collectors/alfa_statements.py  # задача 1: запрос выписки → poll → raw-слой
collectors/alfa_to_ms.py       # задача 2: raw-проводки → paymentin/paymentout МС (идемпотентно)
invoice_bot/alfa_payment_draft.py  # задача 3: счёт/МС → POST /payments (черновик без подписи)
migrations/1NN_alfa_bank_raw.sql   # raw_alfa_statement (JSONB), alfa_oauth_token, idempotency
```

- **Сырьё отдельно от расчётов** (правило 2): ответ API выписки — как есть в `raw_alfa_statement` (JSONB). Разбор в МС-платежи пересобирается из сырья.
- **Идемпотентность** (правило 3): upsert по банковскому ID операции; повторная выписка за период не плодит дублей в МС.
- **Расписание:** ежедневный крон (окно «вчера-сегодня») + опционально более частый поллинг «по мере поступления». Вебхуки Альфы — проверить, есть ли push-уведомления о зачислениях (иначе поллинг).
- **МойСклад:** каждая приходная проводка → `POST /entity/paymentin`, расходная → `/entity/paymentout`; контрагент матчится по ИНН; переиспользуем хелпер `invoice_bot/ms.py`. Связь с конвейером счёт→заказ (`invoice_bot`) для черновиков платёжек поставщикам.

## .env (имена ключей — значения только в .env)

```
ALFA_CLIENT_ID=5052e56f-8bca-4cda-9d81-57422e0ebf93
ALFA_SCOPE=openid customer transactions signature profile email phone eio role inn
ALFA_REDIRECT_URI=http://localhost
ALFA_ENV=sandbox                 # sandbox|prod → выбор хостов
ALFA_CERT_PATH=/opt/mp-analytics/secrets/alfa/sandbox_cert_2026.cer
ALFA_KEY_PATH=/opt/mp-analytics/secrets/alfa/sandbox_key_2026.key
ALFA_KEY_PASSWORD=…              # если ключ зашифрован
ALFA_CA_BUNDLE=/opt/mp-analytics/secrets/alfa/apica_2022_chain.cer
ALFA_REFRESH_TOKEN=…             # ротируется демоном после разового ручного входа
```

## Открытые вопросы / блокеры

1. **[БЛОКЕР] Пароль к `test_cert.zip`** — без него не распаковать серт → нет живых вызовов. Запрос на `alfa_api@alfabank.ru`.
2. Отдельный ли пароль у `.p12`/`.key` (увидим после распаковки).
3. `client_credentials` vs authorization_code — доступен ли неинтерактивный флоу для нашего договора.
4. Есть ли **вебхуки/push** о зачислениях (для «по мере поступления») или только поллинг.
5. Живая проверка флагов: префикс `jp` vs `pp` для счетов; точные имена полей purpose/НДС в платёжке.
6. Номер тестового расчётного счёта в песочнице (из `GET /accounts`).

## Проверка (когда разблокируется)

Порядок первого прогона (всё read-only, кроме шага 5):
1. mTLS-хендшейк на `sandbox.alfabank.ru` — серт принят.
2. OIDC: authorize (ручной вход) → token → `access_token`+`refresh_token`.
3. `GET /api/pp|jp/v1/accounts` → номер тестового счёта, остаток.
4. Выписка: `POST …/transactions/files/requests` → poll → download; сохранить сырьё.
5. Черновик платёжки: `POST /api/jp/v2/payments` без подписи → проверить, что появился в вебе «На подпись».
