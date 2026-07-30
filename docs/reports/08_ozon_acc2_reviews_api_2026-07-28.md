# Ozon oz_acc2 (Дисквэр): доступ к отзывам через Seller API + оценка объёма

Дата: 2026-07-28 · поток: rev · проба живого API (ключи из `.env`, чтение + один заведомо
несуществующий `review_id` = нулевой UUID, побочных эффектов нет).

## 1. Что вернул API (oz_acc2, точные коды и тексты)

| Метод | Код | Тело ответа |
|---|---|---|
| `POST /v1/review/list` | **403** | `{"code":7, "message":"Implementation.ReviewList: ReviewList error: rpc error: code = PermissionDenied desc = not available with existing subscription"}` |
| `POST /v2/review/list` | **403** | `{"code":7, "message":"i.service.ReviewInfoV2: ReviewListV2 s.CheckSellerReviewAccess: rpc error: code = PermissionDenied desc = not available with existing subscription"}` |
| `POST /v1/review/count` | **403** | `{"code":7, "message":"Implementation.ReviewCount error: ReviewCount error: rpc error: code = PermissionDenied desc = not available with existing subscription"}` |
| `POST /v1/review/info` | **403** | `{"code":7, "message":"Implementation.ReviewInfo error: ReviewInfo error: rpc error: code = PermissionDenied desc = not available with existing subscription"}` |
| `POST /v1/review/comment/list` | **403** | `{"code":7, "message":"get comments list error: CommentList error: rpc error: code = PermissionDenied desc = not available with existing subscription"}` |
| `POST /v1/review/comment/create` | **403** | `{"code":7, "message":"failed to create comment, error: CommentCreate error: rpc error: code = PermissionDenied desc = not available with existing subscription"}` |
| `POST /v1/review/comment/delete` | **403** | `{"code":7, "message":"delete comment error CommentDelete error: rpc error: code = PermissionDenied desc = not available with existing subscription"}` |
| `POST /v1/review/change-status` | **403** | `{"code":7, "message":"Implementation.ReviewChangeStatus error: ReviewChangeStatus error: rpc error: code = PermissionDenied desc = not available with existing subscription"}` |
| `POST /v3/review/list` | 404 | `404 page not found` (версии v3 не существует — то же самое на oz_acc1) |
| `POST /v1/feedback/list`, `/v2/review/comment/create`, `/v1/review/subscription/info` | 404 | `404 page not found` (таких методов нет) |

Контроль на **oz_acc1** (Premium Plus есть) теми же телами запросов: `review/list` **200**,
`review/count` **200** (`{"total":9826,"unprocessed":954,"processed":8872}`), `review/info` **404**
`review not found` (т.е. проверка подписки пройдена, не найден именно фиктивный UUID),
`change-status` **200**. Значит 403 на acc2 — это ровно подписка, а не наш ключ/права/формат.

Важная деталь: `limit` у `review/list` и `comment/list` валиден только в диапазоне **[20, 100]** —
при `limit=1` приходит 400 `Request validation error` ДО проверки подписки (легко принять за
«метод работает»).

**Работает на oz_acc2 без Premium Plus:** вся группа вопросов (`/v1/question/list` 200,
`/v1/question/count` 200 → `{"all":279,...}`, `/v1/question/info` 200) и `/v1/product/rating-by-sku`
200 — но последний отдаёт только контент-рейтинг карточки (медиа/текст/атрибуты), количества
отзывов там нет.

## 2. Альтернативы в свежей версии Seller API

Нет. Группа `review/*` существует только в v1 (плюс недокументированный `v2/review/list` —
на acc1 отвечает так же, как v1, на acc2 закрыт тем же гейтом); v3 не существует; отдельных
методов отзывов вне группы `review/*` в Seller API нет. Официальный гайд Ozon прямо пишет, что
методы управления отзывами доступны только по подписке Premium Plus. Обходного пути через API
для oz_acc2 нет — только ручная работа в ЛК.

## 3. Сколько отзывов в месяц приходит на Дисквэр (оценка)

Прямого счётчика нет (`review/count` закрыт), поэтому переносим наблюдаемую на oz_acc1 частоту
отзывов на объём заказов oz_acc2.

| Месяц | Отправления acc1 | Отзывы acc1 | Отзывов на отправление | Отправления acc2 | **Оценка отзывов acc2** |
|---|---|---|---|---|---|
| Май-2026 | 1546 | 441 | 0.285 | 536 | **≈ 153** |
| Июнь-2026 | 1436 | 413 | 0.288 | 420 | **≈ 121** |
| Июль (неполный) | 1081 | 317 | 0.293 | 313 | ≈ 92 (месяц не закрыт) |

Кросс-проверка по вопросам (единственный живой канал обратной связи на acc2):
- вопросов на отправление: июнь acc1 40/1436 = **2.8%**, acc2 12/420 = **2.9%** — вовлечённость
  покупателей на аккаунтах практически одинаковая, значит перенос коэффициента корректен;
- отзывов на вопрос у acc1 ≈ 10 (июнь 413/40, май 441/46) → по вопросам acc2 июнь 12 → ≈ 124,
  май 22 → ≈ 210 (месячный шум вопросов высок, но диапазон тот же).

**Вывод по объёму: ≈ 120–160 отзывов в месяц, около 4–5 в день.**

## 4. Окупаемость Premium Plus

- Цена подписки — **24 990 ₽/мес** (открытые источники; точную цифру для нашего юрлица видно в ЛК).
- При 120–160 отзывах это **≈ 160–210 ₽ за один отвеченный отзыв** — только за право отвечать
  роботом; на acc1 та же работа уже идёт бесплатно (подписка там есть).
- Оборот Дисквэр-Ozon (accruals): апрель 3.46 млн ₽, май 3.05, июнь 2.68, июль (неполный) 1.90 —
  подписка съедает **≈ 0.9% оборота** и заметную долю маржи.
- Ручной разбор 4–5 отзывов в день в ЛК — это порядка 2–4 часов в месяц; наш движок может готовить
  тексты по фактам карточки и без API, если отзывы выгружать/копировать вручную.

**Рекомендация:** ради одной автоматизации отзывов подписку брать не стоит — на этом объёме она
не окупается. Считать её имеет смысл только если нужны остальные её эффекты (буст в выдаче 12.5%,
бейдж, аналитика конкурентов); тогда автоответы идут бонусом, и подключение мы поддержим за
полчаса — код oz_acc2 уже написан и ждёт только снятия 403.
