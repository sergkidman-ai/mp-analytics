# Пропущенные заказы МС — 03.11.2026

Прямая команда пользователя: разобрать задержку, перенести 11 заказов и запустить обработчик.

Причина: WB-заказы в AMD (51), BYN (933), KZT (398), KGS (417) не имели строки statistics.priceWithDisc. Импортёр разрешал fallback Orders.price только для RUB (643), игнорируя предоставленную WB рублёвую convertedPrice. В плановом прогоне 18:00 МСК четыре ошибки wb_price_ambiguous заняли 4 из 10 мест партии; 6 других заказов созданы, оставшиеся отложены, курсор не продвинут. Заказ 51726437-2057-1 поступил после этого запуска.

Исправление: scanner сохраняет convertedPrice/convertedCurrencyCode; импортёр при отсутствии statistics использует положительное целое convertedPrice только при convertedCurrencyCode=643. Доступная priceWithDisc остаётся приоритетной; неоднозначная статистика и неизвестная валюта блокируют запись. Собственного курса конвертации нет.

Внеплановый запуск ограничен 11 номерами, использует штатные create_one и status.run с их журналами и защитами. Проверка МС до POST исключила дубли. Созданы 11/11, обработаны 11/11, ошибок 0. Итог подтверждён GET каждого документа: 6 Подтвержден, 5 Неукомплектован, все с планом 05.11.2026. Отгрузочная очередь этих номеров пуста: текущая дата уже после порога, применено действующее правило следующего дня.

| Заказ | Статус | Сумма, ₽ | Документ |
|---|---|---:|---|
| 0110513166-0097-1 | Подтвержден | 23318.00 | [МС](https://online.moysklad.ru/app/#customerorder/edit?id=da3f178f-d799-11f1-0a80-10b90039b344) |
| 0110513166-0097-3 | Неукомплектован | 11659.00 | [МС](https://online.moysklad.ru/app/#customerorder/edit?id=ee67b93b-d799-11f1-0a80-07200039980b) |
| 51726437-2057-1 | Подтвержден | 10223.00 | [МС](https://online.moysklad.ru/app/#customerorder/edit?id=1c7a4949-d79a-11f1-0a80-182d003ad043) |
| 6184059332 | Подтвержден | 1787.92 | [МС](https://online.moysklad.ru/app/#customerorder/edit?id=aab8ed22-d799-11f1-0a80-0eae003b83c2) |
| 6185697433 | Подтвержден | 830.58 | [МС](https://online.moysklad.ru/app/#customerorder/edit?id=affa8b43-d799-11f1-0a80-11f30039b7d8) |
| 6186983096 | Подтвержден | 1921.44 | [МС](https://online.moysklad.ru/app/#customerorder/edit?id=b44799dc-d799-11f1-0a80-0bf3003a8bb9) |
| 6187742637 | Неукомплектован | 599.71 | [МС](https://online.moysklad.ru/app/#customerorder/edit?id=bf5e6779-d799-11f1-0a80-16d6003b9737) |
| 6188147145 | Неукомплектован | 1303.00 | [МС](https://online.moysklad.ru/app/#customerorder/edit?id=c8a7ff34-d799-11f1-0a80-1ac3003a9e8e) |
| 6188243033 | Неукомплектован | 1818.55 | [МС](https://online.moysklad.ru/app/#customerorder/edit?id=f41e719b-d799-11f1-0a80-0d53003b2490) |
| 88895909-0055-1 | Подтвержден | 14730.00 | [МС](https://online.moysklad.ru/app/#customerorder/edit?id=04f13deb-d79a-11f1-0a80-1ac3003aaa1d) |
| 95223690-0580-1 | Неукомплектован | 7114.00 | [МС](https://online.moysklad.ru/app/#customerorder/edit?id=0abb67a6-d79a-11f1-0a80-002d003b6c32) |
