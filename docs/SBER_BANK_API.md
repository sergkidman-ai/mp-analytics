# SberBusinessAPI (Сбер, прямая интеграция Host-2-Host) — карта для ООО «ДИСКВЭР»

> Поток: **inv** (банковский контур). Статус: **проектирование** (2026-08-02).
> Организация: ООО «ДИСКВЭР», ИНН **7811803918**, р/с **40702810355000147717**,
> Северо-Западный банк ПАО Сбербанк, **БИК 044030653**.
> Второй контур рядом с Альфой (ООО «Цифровой Квадрат») — см. `docs/ALFA_BANK_API.md`.
> Источник: официальное зеркало спецификации https://sberbusinessapi-documentation.github.io/
> (единый документ с якорями) + developers.sber.ru/docs/ru/sberbusinessapi.

## Что это за продукт

**SberBusinessAPI**, режим **«Прямая интеграция Host-2-Host»** — получение и отправка документов
**по своей организации** (не «для холдингов», не «партнёрская» схема). Транспорт: REST/HTTPS,
**порт TCP 9443**, обязательный **клиентский TLS-сертификат, изданный банком**.

Аналогия с Альфой почти полная: OAuth (интерактивный вход представителя) + mTLS + одноразовый
refresh_token + черновик платёжки без ЭП. Код Альфы переиспользуется на ~70 %.

## Хосты

| Назначение | ТЕСТ | ПРОМ | TLS-серт |
|---|---|---|---|
| Back-to-Back: API + token + user-info + change-client-secret | `https://edupirfintech.sberbank.ru:9443` | `https://fintech.sberbank.ru:9443` | **да** |
| Авторизация пользователя (SMS) | `https://edupir.testsbi.sberbank.ru:9443` | `https://sbi.sberbank.ru:9443` | нет |
| Swagger UI (проверка серта) | `.../fintech/api/swagger-ui.html` | `.../fintech/api/swagger-ui.html` | да |

## Авторизация (Сбер Бизнес ID, OAuth 2.0 / OIDC)

- `GET /ic/sso/api/v2/oauth/authorize` → код авторизации (**интерактивный вход** представителя).
- `POST /ic/sso/api/v2/oauth/token` — `application/x-www-form-urlencoded`, **только POST**,
  параметры в query: `grant_type=authorization_code|refresh_token`, `code`, `client_id`,
  `client_secret`, `redirect_uri`, `refresh_token`. Ответ подписан JWT (`Accept: application/jose`),
  подпись проверяется сертификатом банка.
- `GET /ic/sso/api/v1/oauth/user-info`, `POST /ic/sso/api/v1/oauth/revoke`.
- **access_token — 60 минут.**
- **refresh_token одноразовый:** повторное использование → `invalid_grant / Unknown refresh token`.
  **Ротировать при каждом обновлении** (та же механика, что у Альфы).
- **client_secret живёт 40 дней** ⚠️ — ротация `POST /ic/sso/api/v1/change-client-secret`
  или вручную в ЛК SberBusinessAPI. **Это главное отличие от Альфы: нужен автообновлятор
  + напоминалка** (по образцу `ops/wb_token_reminder.py`).
- `client_id` / `client_secret` / `redirect_uri` выдаёт менеджер при регистрации сервиса.

## Эндпоинты под наши три задачи

### 1. Выписка (scope `GET_STATEMENT_ACCOUNT`)

```
GET /fintech/api/v1/statement/transactions?accountNumber={20 цифр}&statementDate=YYYY-MM-DD&page=1
GET /fintech/api/v1/statement/summary
GET /fintech/api/v1/statement/transactions/{id}      # реквизиты одной операции
```
- Все три параметра **обязательны**, выписка **за один день**, пагинация — `_links[rel=next]`.
- Операция (`transactions[]`): `amount{amount,currencyName}`, `amountRub`, `direction`
  (`CREDIT`/`DEBIT`), `documentDate`, `number`, `operationCode`, `operationDate`, **`paymentPurpose`**,
  `priority`, `correspondingAccount`, `filial`, + блок **`rurTransfer`**:
  `payerName/payerInn/payerKpp/payerAccount/payerBankBic/payerBankName/payerBankCorrAccount`,
  те же `payee*`, `valueDate`, `receiptDate`, `purposeCode`, `cartInfo`, `departmentalInfo`.
- Валютные блоки `curTransfer`/`swiftTransfer` — нам не нужны.
- Соответствие с Альфой: `paymentPurpose` ≈ назначение платежа (мост к приёмкам),
  `direction=CREDIT` → paymentin МС, `DEBIT` → paymentout.

### 2. Платёжное поручение / черновик (scope `PAY_DOC_RU`)

```
POST /fintech/api/v1/payments                    # создать РПП
GET  /fintech/api/v1/payments/{externalId}       # атрибуты
GET  /fintech/api/v1/payments/{externalId}/state  # статус
POST /fintech/api/v1/payments/from-invoice        # из счёта (scope PAY_DOC_RU_INVOICE)
POST /fintech/api/v1/payment-requests/outgoing    # исх. платёжное требование
```
**Черновик = POST без объекта `digestSignatures`.** Дословно из спецификации: передана ЭП —
банк сразу начинает обработку; ЭП не передана — документ создаётся **в статусе «черновик»**,
человек заходит в СберБизнес и подписывает. **Ровно наш сценарий** (как у Альфы).

Тело запроса (`Payment`) — состав тот же, что у Альфы, плюс отдельный блок НДС:
```
amount, date (YYYY-MM-DD), number, externalId (uuid), purpose,
operationCode ("01"), deliveryKind ("электронно"), urgencyCode ("INTERNAL"), priority ("5"),
payerName/payerInn/payerKpp/payerAccount/payerBankBic/payerBankCorrAccount,
payeeName/payeeInn/payeeKpp/payeeAccount/payeeBankBic/payeeBankCorrAccount,
vat: {amount, rate, type: "NO_VAT"|...},         ← у Альфы НДС писали текстом в purpose
departmentalInfo {uip, drawerStatus101, kbk, oktmo, ...}   ← только бюджетные
voCode, incomeTypeCode, crucialFieldsHash, digestSignatures[]  ← нам не нужны/пусто
```
⚠️ Проверить на живой схеме: обязательность `vat` для небюджетного платежа и допустимые
значения `urgencyCode` (в примере `INTERNAL`, у Альфы было `NORMAL`).

### 3. Прочее, что может пригодиться

`GET /fintech/api/v1/client-info` (реквизиты своей организации), `GET /fintech/api/v1/crypto`
и `POST /fintech/api/v1/crypto/cert-requests` (управление сертификатами ЭП),
`GET /fintech/api/v1/statement/summary` (остатки/обороты), реестр платежей, справочники.

## Подключение — ФАКТ на 2026-08-02

API подключено самостоятельно в ЛК СберБизнес, **бесплатно**, доступ выдан сразу на
**промышленный сервис** (вкладки «Промышленный сервис» / «Песочница API»). Общая схема из
спецификации (платная заявка на `fintech_API@sberbank.ru`) в нашем случае **не понадобилась** —
правило 13 по этому пункту закрыто, живых денег нет.

Состояние личного кабинета (скриншот 2026-08-02):

| Параметр | Значение / состояние |
|---|---|
| Наименование сервиса | `Sber API: 7811803918_Company` (ИНН Дисквэра) |
| **Client_id** | **80859** |
| Redirect URI | **не заполнен** (`https://`) — задать до авторизации |
| Client_secret | **не сгенерирован** — кнопка «Активировать» |
| Scope v1 | `openid di-a496a254-a7a5-4d09-9473-3557ee920b39` |
| Scope v2 | **подтверждён**: есть `GET_STATEMENT_ACCOUNT`, `GET_STATEMENT_TRANSACTION`, `PAY_DOC_RU` (+ `GET_CLIENT_ACCOUNTS`, `CERTIFICATE_REQUEST`, `PAYMENT_REQUEST_OUT`, `PAY_DOC_CUR`, `GET_CORRESPONDENTS`, `DICT`, `FILES`) |
| Сертификаты шифрования | пусто, доступно 3 шт., есть «Сгенерировать сертификат» |
| Ключи доступа | пусто, доступно 3 шт. |
| **«Активировать» нажата** | **2026-08-02** → `client_secret` сгенерирован, **отсчёт 40 дней пошёл, дедлайн ротации ≈ 2026-09-11** |

### Что уже лежит на диске (`secrets/sber/`, вне git)

| Файл | Что это |
|---|---|
| `prom-certs/{sberapi-ca.cer, sberapi-root-ca.cer, Sberbank Root CA.cer, sberca-ext.crt, sberca-root-ext.crt}` | цепочка доверенных TLS ПРОМ — в `verify` при запросах |
| `bank-sign/00CA63BJ.cer` | сертификат подписи банка (ПАО Сбербанк) — проверка JWT-подписи ответа `/oauth/token` |
| `diskver_fintech01.key` | наш приватный ключ клиентского TLS (chmod 600, **с сервера не уходит**) |
| `diskver_fintech01.csr` | запрос на сертификат, отправлен в ТГ на загрузку в ЛК |

CSR сформирован по документации Сбера (openssl-путь для не-Windows):
```
openssl req -nodes -newkey rsa:2048 -keyout diskver_fintech01.key -out diskver_fintech01.csr \
  -subj "/C=RU/O=OOO DISKVER/CN=FINTECH01/emailAddress=…/OU=7811803918"
```
Требования УЦ: **CN = `FINTECH`+номер ключа 01…99** (уникален на каждый запрос, следующий — `FINTECH02`),
**OU = ИНН организации**, O — наименование латиницей, E-mail — действующий адрес ответственного
за получение сертификатов, ключ RSA 2048, keyUsage = цифровая подпись + шифрование данных,
extendedKeyUsage = проверка подлинности клиента.

### Осталось получить

1. **`client_secret`** (сгенерирован при активации) — в `.env`, в чат не выводить.
2. **Выпущенный клиентский TLS-сертификат** — после загрузки CSR в ЛК; положить рядом с ключом.
3. **Redirect URI** — заполнить в ЛК; договорились на `https://bi.metaverseworld.ru/sber/callback`
   (домен наш, живой Let's Encrypt).

Поддержка по сертификатам: `supportdbo2@sberbank.ru` (услуга SberBusinessAPI, наименование, ИНН,
среда, `client_id`).

## Открытые вопросы (уточнять на живой схеме / у менеджера)

1. **Стоимость подключения и месячный тариф** — до заявки неизвестна, нужна цифра для решения.
2. Обязателен ли блок `vat` в `POST /payments` для обычного платежа поставщику.
3. `urgencyCode`: `INTERNAL` vs `NORMAL` — какое значение для обычного рублёвого платежа.
4. Даёт ли `client-info` номера счетов организации (или счёт брать из `.env`/МС).
5. Есть ли push/webhook о зачислениях (иначе — поллинг выписки, как у Альфы).
6. Ограничения по частоте запросов (`GET /fintech/api/v1/` статистика запросов есть — значит,
   лимиты считаются).

## Что переиспользуется из контура Альфы

| Слой | Файл Альфы | Переиспользование |
|---|---|---|
| mTLS-сессия + токены | `collectors/alfa_statement.py::_session/_cfg` | схема та же, другие URL/поля |
| Выписка → raw | `collectors/alfa_statement.py` | ~70 %, другая пагинация (по дням + `_links`) |
| Выписка → МС paymentin/out | `collectors/alfa_ms.py` | ~90 %, зависит только от нормализованной операции |
| Привязка платежа к приёмке | `collectors/alfa_link.py` | ~90 %, мосты по назначению — свои для поставщиков Дисквэра |
| Очередь черновиков | `invoice_bot/po_payment_watch.py` | банконезависима, нужен ключ по организации |
| Отправка черновика | `invoice_bot/alfa_payment_draft.py` | ~60 %, тело РПП почти совпадает |

**Вывод:** правильная форма — вынести общий движок и сделать **драйвер банка**
(`banks/alfa.py`, `banks/sber.py`) с интерфейсом `get_statement / create_draft / get_state`,
а не копировать модули под Сбер.
