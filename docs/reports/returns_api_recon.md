# Разведка returns-API площадок

Снято: 2026-08-02 14:13. Поток `ret`. Только чтение.

## Ozon — POST /v1/returns/list

### oz_acc1 — всего 2793

| visual.status | шт | схемы | с target_place | с barcode | медиана дней на складе |
|---|---|---|---|---|---|
| На складе Ozon | 1832 | Fbo/Fbs | 1832 | 1775 | 0 |
| Получен | 709 | Fbo/Fbs | 709 | 709 | 0 |
| Отклонена вами | 57 | Fbo/Fbs | 0 | 0 | 0 |
| Отклонена Ozon | 37 | Fbs | 0 | 0 | 0 |
| Отклонена. Спор не открыт | 32 | Fbs | 0 | 0 | 0 |
| Списали товар | 32 | Fbo/Fbs | 32 | 31 | 0 |
| Деньги возвращены | 31 | Fbs | 0 | 0 | 0 |
| Отменена покупателем | 26 | Fbo/Fbs | 0 | 0 | 0 |
| Привезет курьер | 8 | Fbs | 8 | 8 | 0 |
| Отказано в компенсации | 5 | Fbs | 5 | 5 | 0 |
| Вы вернули часть денег | 5 | Fbs | 0 | 0 | 0 |
| Едет к вам | 5 | Fbs | 5 | 5 | 0 |
| Едет на склад Ozon | 4 | Fbs | 4 | 4 | 0 |
| Ищем товар | 4 | Fbo/Fbs | 4 | 4 | 0 |
| Ожидает отправки | 3 | Fbs | 3 | 2 | 0 |
| Утилизирован | 2 | Fbo/Fbs | 2 | 2 | 11 |
| Одобрена вами | 1 | Fbs | 0 | 0 | 0 |

Топ place: [('-', 189), ('САНКТ-ПЕТЕРБУРГ_161', 123), ('FBS/217678/Москва Ozon', 110), ('FBS/217678/Москва наличие', 89), ('СПБ_БУГРЫ_РФЦ_ВОЗВРАТЫ', 84)]

Топ target_place: [('-', 189), ('ПУШКИНО_1_РФЦ', 150), ('САНКТ-ПЕТЕРБУРГ_161', 125), ('FBS/217678/Москва Ozon', 113), ('FBS/217678/Москва наличие', 110)]

Со storage.sum>0: 2793; с utilization_forecast_date: 1

Всего схем: [(('Fbs', 'Cancellation'), 1435), (('Fbs', 'ClientReturn'), 909), (('Fbs', 'FullReturn'), 200), (('Fbo', 'Cancellation'), 139), (('Fbo', 'ClientReturn'), 94), (('Fbs', 'PartialReturn'), 10), (('Fbo', 'FullReturn'), 6)]

<details><summary>пример объекта</summary>

```json
{
  "id": 188639,
  "company_id": 217678,
  "return_reason_name": "Отказ при вручении: покупатель передумал",
  "type": "Cancellation",
  "schema": "Fbs",
  "order_id": 0,
  "order_number": "78090901-0018",
  "place": {
    "id": 1020000407848000,
    "name": "САНКТ-ПЕТЕРБУРГ_1308",
    "address": "Россия, 194100, г. Санкт-Петербург, Россия, Санкт-Петербург, Кантемировская улица, 35"
  },
  "target_place": {
    "id": 1020000407848000,
    "name": "САНКТ-ПЕТЕРБУРГ_1308",
    "address": "Россия, 194100, г. Санкт-Петербург, Россия, Санкт-Петербург, Кантемировская улица, 35"
  },
  "storage": {
    "sum": {
      "currency_code": "",
      "price": 0
    },
    "tariffication_first_date": "2023-07-13T12:23:42.049937Z",
    "tariffication_start_date": "2023-07-12T12:23:42.049937Z",
    "arrived_moment": "2023-07-09T12:23:42.050Z",
    "days": 0,
    "utilization_sum": {
      "currency_code": "",
      "price": 0
    },
    "utilization_forecast_date": null
  },
  "product": {
    "sku": 652456524,
    "offer_id": "0306",
    "name": "Картридж DS №78XL (C6578A) цветной",
    "price": {
      "currency_code": "RUB",
      "price": 2840
    },
    "price_without_commission": {
      "currency_code": "RUB",
      "price": 2840
    },
    "commission_percent": 0,
    "commission": {
      "currency_code": "",
      "price": 0
    },
    "quantity": 1
  },
  "logistic": {
    "technical_return_moment": null,
    "final_moment": "2023-07-10T11:52:29.003Z",
    "cancelled_with_compensation_moment": null,
    "return_date": "2023-06-28T13:32:22.340Z",
    "barcode": "%101%20537124757"
  },
  "visual": {
    "status": {
      "id": 16,
      "display_name": "Получен",
      "sys_name": "ReceivedBySeller"
    },
    "change_moment": "2023-07-10T11:52:29.003Z"
  },
  "exemplars": [
    {
      "id": 1019875172464133,
      "exemplar_id": 0
    }
  ],
  "additional_info": {
    "is_opened": false,
    "is_super_econom": false
  },
  "clearing_id": 400129441964000,
  "posting_number": "78090901-0018-1",
  "return_clearing_id": 400129441964000,
  "source_id": 42212492,
  "compensation_status": null
}
```
</details>

### oz_acc2 — всего 424

| visual.status | шт | схемы | с target_place | с barcode | медиана дней на складе |
|---|---|---|---|---|---|
| На складе Ozon | 298 | Fbo/Fbs | 298 | 289 | 0 |
| Получен | 75 | Fbo/Fbs | 75 | 75 | 1 |
| Отклонена вами | 19 | Fbo/Fbs | 0 | 0 | 0 |
| Отменена покупателем | 5 | Fbs | 0 | 0 | 0 |
| Отклонена Ozon | 5 | Fbs | 0 | 0 | 0 |
| Деньги возвращены | 4 | Fbs | 0 | 0 | 0 |
| Едет к вам | 4 | Fbs | 4 | 4 | 0 |
| Утилизирован | 4 | Fbs | 4 | 4 | 9 |
| Отклонена. Спор не открыт | 3 | Fbs | 0 | 0 | 0 |
| Списали товар | 3 | Fbo/Fbs | 3 | 3 | 0 |
| В пункте выдачи | 2 | Fbs | 2 | 2 | 1 |
| Вы вернули часть денег | 1 | Fbs | 0 | 0 | 0 |
| Ищем товар | 1 | Fbs | 1 | 1 | 0 |

Топ place: [('МОСКВА_4048', 49), ('-', 37), ('МОСКВА_2566', 18), ('НОГИНСК_РФЦ_ВОЗВРАТЫ', 17), ('СОФЬИНО_РФЦ_ВОЗВРАТЫ', 16)]

Топ target_place: [('МОСКВА_4048', 58), ('-', 37), ('ПУШКИНО_1_РФЦ', 19), ('МОСКВА_2566', 18), ('ПУШКИНО_1_РФЦ_ВОЗВРАТЫ', 15)]

Со storage.sum>0: 424; с utilization_forecast_date: 7

Всего схем: [(('Fbs', 'Cancellation'), 234), (('Fbs', 'ClientReturn'), 142), (('Fbs', 'FullReturn'), 21), (('Fbo', 'Cancellation'), 14), (('Fbo', 'ClientReturn'), 13)]

<details><summary>пример объекта</summary>

```json
{
  "id": 1000293099,
  "company_id": 2523124,
  "return_reason_name": "Не удалось доставить заказ",
  "type": "Cancellation",
  "schema": "Fbs",
  "order_id": 27829673385,
  "order_number": "0185183545-0013",
  "place": {
    "id": 18824332040000,
    "name": "ЕКАТЕРИНБУРГ_РФЦ_НОВЫЙ_ВОЗВРАТЫ",
    "address": "Россия, 620961, обл. Свердловская, г. Екатеринбург, промышленная зона Логопарк Кольцовский, строение 15"
  },
  "target_place": {
    "id": 18044570445000,
    "name": "ЕКАТЕРИНБУРГ_РФЦ_НОВЫЙ",
    "address": "Россия, 620961, обл. Свердловская, г. Екатеринбург, промышленная зона Логопарк Кольцовский, строение 15"
  },
  "storage": {
    "sum": {
      "currency_code": "",
      "price": 0
    },
    "tariffication_first_date": null,
    "tariffication_start_date": null,
    "arrived_moment": null,
    "days": 0,
    "utilization_sum": {
      "currency_code": "",
      "price": 0
    },
    "utilization_forecast_date": null
  },
  "product": {
    "sku": 1873151358,
    "offer_id": "3856TLAG4150",
    "name": "Фотобарабан DS SP-230H DU для принтеров Ricoh Aficio SP230 черный",
    "price": {
      "currency_code": "RUB",
      "price": 2377
    },
    "price_without_commission": {
      "currency_code": "RUB",
      "price": 2377
    },
    "commission_percent": 0,
    "commission": {
      "currency_code": "",
      "price": 0
    },
    "quantity": 1
  },
  "logistic": {
    "technical_return_moment": null,
    "final_moment": null,
    "cancelled_with_compensation_moment": null,
    "return_date": "2025-02-26T17:38:09.763Z",
    "barcode": "%101%31726563385"
  },
  "visual": {
    "status": {
      "id": 34,
      "display_name": "На складе Ozon",
      "sys_name": "ReturnedToOzon"
    },
    "change_moment": "2025-02-28T23:07:18.208359Z"
  },
  "exemplars": [
    {
      "id": 300703333972000,
      "exemplar_id": 17216545753
    }
  ],
  "additional_info": {
    "is_opened": false,
    "is_super_econom": false
  },
  "clearing_id": 300699572182000,
  "posting_number": "0185183545-0013-1",
  "return_clearing_id": 0,
  "source_id": 90469682,
  "compensation_status": null
}
```
</details>

## Ozon — штрихкод получения возвратов (/v1/return/giveout/*)

- **oz_acc1**: barcode есть=True, png байт≈1224, заявок giveout=0
- **oz_acc2**: barcode есть=True, png байт≈1232, заявок giveout=0

## Яндекс — GET /campaigns/{id}/returns

### 148691041 (Москва наш склад) — всего 132

- shipmentStatus: [('PICKED', 112), (None, 10), ('CREATED', 3), ('READY_FOR_PICKUP', 3), ('IN_TRANSIT', 1), ('LOST', 1), ('RECEIVED_FOR_EXPROPRIATION', 1), ('CANCELLED', 1)]
- returnType: [('UNREDEEMED', 78), ('RETURN', 54)]
- refundStatus: [(None, 78), ('REFUNDED', 44), ('CANCELLED', 6), ('REJECTED', 3), ('STARTED_BY_USER', 1)]
- с адресом ПВЗ: 118/132; с pickupTillDate: 3/132
- возраст (дней) min/медиана/max: 1/107/367

<details><summary>пример объекта</summary>

```json
{
  "id": 113268549,
  "orderId": 59312969794,
  "creationDate": "2026-08-01T16:32:16.701+03:00",
  "updateDate": "2026-08-02T15:06:53.938+03:00",
  "logisticPickupPoint": {
    "id": 10025186314,
    "name": "Пункт выдачи заказов Яндекс Маркета",
    "address": {
      "country": "Россия",
      "city": "Москва,Москва",
      "street": "улица Годовикова",
      "house": "11 к.2",
      "postcode": "129075"
    },
    "instruction": "М. Алексеевская, улица Годовикова, 11к2 «Выйдя из метро, пройдите вдоль проспекта Мира до улицы Бочкова. Далее по улице Бочкова до пересечения с улицей Годовикова. Пункт выдачи расположен в жилом комплексе iLove. Обойдите здание, ориентир - вывеска Яндекс Маркет»",
    "type": "PICKUP_POINT",
    "logisticPartnerId": 810314
  },
  "shipmentRecipientType": "SHOP",
  "shipmentStatus": "IN_TRANSIT",
  "refundAmount": 344500,
  "amount": {
    "value": 3445.0,
    "currencyId": "RUR"
  },
  "items": [
    {
      "marketSku": 5396383539,
      "shopSku": "239330",
      "count": 1,
      "instances": [
        {
          "status": "IN_TRANSIT"
        }
      ],
      "tracks": [
        {
          "trackCode": "59312969794-1"
        }
      ]
    }
  ],
  "returnType": "UNREDEEMED"
}
```
</details>

### 87623061 (Москва Звездный) — всего 186

- shipmentStatus: [('PICKED', 161), (None, 12), ('CANCELLED', 8), ('RECEIVED', 2), ('READY_FOR_PICKUP', 1), ('PREPARED_FOR_UTILIZATION', 1), ('EXPIRED', 1)]
- returnType: [('UNREDEEMED', 111), ('RETURN', 75)]
- refundStatus: [(None, 111), ('REFUNDED', 59), ('CANCELLED', 10), ('REJECTED', 6)]
- с адресом ПВЗ: 165/186; с pickupTillDate: 1/186
- возраст (дней) min/медиана/max: 2/125/854

<details><summary>пример объекта</summary>

```json
{
  "id": 26839123,
  "orderId": 58430694721,
  "creationDate": "2026-06-27T13:59:14.22+03:00",
  "updateDate": "2026-08-01T17:25:41.118+03:00",
  "refundStatus": "REFUNDED",
  "logisticPickupPoint": {
    "id": 10025186314,
    "name": "Пункт выдачи заказов Яндекс Маркета",
    "address": {
      "country": "Россия",
      "city": "Москва,Москва",
      "street": "улица Годовикова",
      "house": "11 к.2",
      "postcode": "129075"
    },
    "instruction": "М. Алексеевская, улица Годовикова, 11к2 «Выйдя из метро, пройдите вдоль проспекта Мира до улицы Бочкова. Далее по улице Бочкова до пересечения с улицей Годовикова. Пункт выдачи расположен в жилом комплексе iLove. Обойдите здание, ориентир - вывеска Яндекс Маркет»",
    "type": "PICKUP_POINT",
    "logisticPartnerId": 810314
  },
  "pickupTillDate": "2026-08-16T17:25:36.03+03:00",
  "shipmentRecipientType": "SHOP",
  "shipmentStatus": "READY_FOR_PICKUP",
  "refundAmount": 317000,
  "amount": {
    "value": 3170.0,
    "currencyId": "RUR"
  },
  "items": [
    {
      "marketSku": 102443721223,
      "shopSku": "3902del",
      "count": 1,
      "decisions": [
        {
          "returnItemId": 29607840,
          "count": 1,
          "comment": "не оригинальные \n",
          "reasonType": "DOES_NOT_FIT",
          "subreasonType": "UNKNOWN",
          "decisionType": "REFUND_MONEY",
          "refundAmount": 317000,
          "amount": {
            "value": 3170.0,
            "currencyId": "RUR"
          },
          "images": [
            "87c1f54a258ddd425063709476c640a4264979cc"
          ]
        }
      ],
      "instances": [
        {
          "status": "READY_FOR_PICKUP"
        }
      ],
      "tracks": [
        {
          "trackCode": "FSN_RET_L_0000175110"
        }
      ]
    }
  ],
  "returnType": "RETURN",
  "fastReturn": false
}
```
</details>

### 99559900 (Москва экспресс) — всего 29

- shipmentStatus: [('PICKED', 23), ('IN_TRANSIT', 4), (None, 2)]
- returnType: [('UNREDEEMED', 15), ('RETURN', 14)]
- refundStatus: [(None, 15), ('REFUNDED', 12), ('REJECTED', 1), ('CANCELLED', 1)]
- с адресом ПВЗ: 26/29; с pickupTillDate: 0/29
- возраст (дней) min/медиана/max: 12/185/605

<details><summary>пример объекта</summary>

```json
{
  "id": 112083066,
  "orderId": 59361504769,
  "creationDate": "2026-07-21T13:29:58.625+03:00",
  "updateDate": "2026-07-23T16:55:49.802+03:00",
  "logisticPickupPoint": {
    "id": 10025186314,
    "name": "Пункт выдачи заказов Яндекс Маркета",
    "address": {
      "country": "Россия",
      "city": "Москва,Москва",
      "street": "улица Годовикова",
      "house": "11 к.2",
      "postcode": "129075"
    },
    "instruction": "М. Алексеевская, улица Годовикова, 11к2 «Выйдя из метро, пройдите вдоль проспекта Мира до улицы Бочкова. Далее по улице Бочкова до пересечения с улицей Годовикова. Пункт выдачи расположен в жилом комплексе iLove. Обойдите здание, ориентир - вывеска Яндекс Маркет»",
    "type": "PICKUP_POINT",
    "logisticPartnerId": 810314
  },
  "shipmentRecipientType": "SHOP",
  "shipmentStatus": "PICKED",
  "refundAmount": 362500,
  "amount": {
    "value": 3625.0,
    "currencyId": "RUR"
  },
  "items": [
    {
      "marketSku": 102005249283,
      "shopSku": "2156",
      "count": 1,
      "instances": [
        {
          "status": "PICKED"
        }
      ],
      "tracks": [
        {
          "trackCode": "59361504769-1"
        }
      ]
    }
  ],
  "returnType": "UNREDEEMED"
}
```
</details>

---

## WB — добивка 02.08.2026 (токены со скоупами получены)

Токены выданы: `WB_TOKEN_RETURNS_ACC1` (Цифровой), `WB_TOKEN_RETURNS_ACC2` (Дисквэр).
Скоупы проверены по битовой маске JWT (`s`): **2064 = бит 4 «Маркетплейс» + бит 11 «Возвраты»**,
боевые (`t=false`), срок до 31.01.2027. Нумерация битов сверена по известным ключам
(`WB_TOKEN_CONTENT_ACC1` = маска 2 = бит 1 «Контент»).

### Итог: API физических возвратов у WB НЕТ

| Путь | Ответ | Вывод |
|---|---|---|
| `GET marketplace-api /api/v3/supplies/returns` | 400 `IncorrectParameter` при ЛЮБЫХ параметрах (7 комбинаций: limit/next, даты ISO и unix, isCancel, srids) | **пути не существует** |
| `GET marketplace-api /api/v3/supplies/zzz-not-a-supply` (контроль) | 400 `IncorrectParameter` — **та же ошибка** | `returns` парсится как `{supplyId}`, отсюда 400 вместо 404 |
| `GET marketplace-api /api/v3/{returns,orders/returns}`, `/api/v1/returns` | 404 `path not found` | нет |
| `GET returns-api /api/v1/returns`, `/api/v3/returns`, `/api/v1/claims/actions` | 404 `path not found` | нет |
| `GET marketplace-api /api/v3/supplies/orders/reshipment` (контроль-жив) | **200**, `orders=0` | токен и хост рабочие |
| `GET returns-api /api/v1/claims` | **200** | единственный живой источник, см. ниже |

Прежняя 401 «token scope not allowed» на `/api/v3/supplies/returns` вводила в заблуждение:
авторизация отвечает ДО маршрутизации, поэтому несуществующий путь выглядел как «нет скоупа».

### Что реально отдаёт `returns-api /api/v1/claims`

Это **заявки покупателей на возврат/брак**, а не «коробка лежит в ПВЗ». Активных сейчас
**0 по обоим аккаунтам** (архив: acc1 = 2, acc2 = 1). Поля: `id, claim_type, status, status_ex,
nm_id, imt_name, user_comment, wb_comment, price, srid, dt, order_dt, dt_update, delivery_dt,
photos, video_paths, actions`. **Нет ни адреса пункта, ни срока забрать, ни штрихкода получения** —
для ежедневного «куда ехать» непригодно. Действие по заявке — ответить/согласовать, это ближе
к потоку `rev`. Лимит хоста — 20 запросов/мин.

Забор возвратов со склада WB продавцом — функция личного кабинета («Поставки и заказы →
Возвраты»), наружу отдаётся только XLSX-выгрузкой. Аналога `giveout/barcode` (Ozon) у WB нет.

### Развязка: возвраты WB есть в АНАЛИТИКЕ

`GET seller-analytics-api.wildberries.ru/api/v1/analytics/goods-return` — **200**, базовым
токеном (скоуп «Аналитика»), возвратный токен для него не нужен.

Ограничения: окно **≤31 дня**, режется по `orderDt` → идём тремя окнами назад (93 дня).
Поля: `srid, shkId, stickerId, nmId, barcode, brand, subjectName, techSize, status,
isStatusActive, returnType, reason, dstOfficeId, dstOfficeAddress, orderId, orderDt,
readyToReturnDt, completedDt, expiredDt`.

Статусы (02.08.2026, 164 строки за 93 дня): `Выдано` 109, `В пути в пвз` 42,
`Готов к выдаче` 12, `Отмена по задержке` 1. `isStatusActive` = 1 ровно у первых двух групп.
`expiredDt` у живых строк **пустой** — срока «забрать до» WB не даёт.
Строка = одна коробка (`shkId`), названия товара нет (только `subjectName` = «Картриджи для
принтеров») → имя берём из `wb_cards` по `nm_id`, покрытие 38/38.

Адреса пунктов: «МСК Улица Годовикова 11к5» и «…11к2» — одной строкой, без запятых.
