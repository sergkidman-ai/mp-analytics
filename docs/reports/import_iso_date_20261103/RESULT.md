# Восстановление загрузчика после 21:28 МСК, 03.11.2026

Причина остановки: creationDate Маркета 2026-11-03T21:34:26.6+03:00 не принимается datetime.fromisoformat в Python3.10. source_rows прерывал весь импорт при ошибке одного кабинета. В 22:00 МСК также зафиксирована сетевая ошибка Маркета; в22:30/23:00 — ошибка даты.

В scanner.stamp и импортере market_created_at применён dateutil.isoparse. Проверены доли секунды1/2/3/6знаков, дата без доли, сохранение часового пояса и отказ для даты без зоны. Код скомпилирован. GET МС в scheduler ограничен45с/2попытки вместо180с/4; единичный POST импортера и защита намерениями сохранены. Ветка inv/import-iso-date-20261103, рабочий код обновлён.

Штатным pipeline перенесены9заказов; обработаны9:3Подтвержден,6Неукомплектован, план05.11. Два изменения остатков между preflight-проверками безопасно отложили заказы до следующего прохода. Последний запуск23:26–23:27МСК успешен: errors=[],held=[],cursor_advanced=true. Свежий GET подтвердил9уникальныхномеров и статусы. Расписание каждые30минут сохранено.

| Заказ | Статус |
|---|---|
| [03088648-0531-1](https://online.moysklad.ru/app/#customerorder/edit?id=c3b384b1-d7c4-11f1-0a80-1e6800470de5) | Неукомплектован |
| [59066575-0454-2](https://online.moysklad.ru/app/#customerorder/edit?id=b6c604f5-d7c4-11f1-0a80-182d0047ee0c) | Неукомплектован |
| [6190143340](https://online.moysklad.ru/app/#customerorder/edit?id=061d0843-d7c4-11f1-0a80-1d6b00480841) | Подтвержден |
| [6190269342](https://online.moysklad.ru/app/#customerorder/edit?id=8f5e9fc3-d7c4-11f1-0a80-16d60048b58c) | Неукомплектован |
| [6190269343](https://online.moysklad.ru/app/#customerorder/edit?id=a801f4ab-d7c4-11f1-0a80-10b900461541) | Неукомплектован |
| [6190269345](https://online.moysklad.ru/app/#customerorder/edit?id=ad45b180-d7c4-11f1-0a80-1ac300480e0f) | Подтвержден |
| [6190269346](https://online.moysklad.ru/app/#customerorder/edit?id=068a916b-d7c5-11f1-0a80-072000473fbe) | Неукомплектован |
| [63975242305](https://online.moysklad.ru/app/#customerorder/edit?id=018f7f68-d7c4-11f1-0a80-1d6b004807f9) | Подтвержден |
| [63977732675](https://online.moysklad.ru/app/#customerorder/edit?id=1a0b4904-d7c4-11f1-0a80-1bb00046da26) | Неукомплектован |
