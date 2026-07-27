# Инструкция запуска: сбор фидбека, движок ответов, бот-модератор

Все команды из `/opt/mp-analytics`, интерпретатор `./venv/bin/python`.

## 1. Сбор фидбека (raw_feedback) — все каналы одной командой
```bash
./venv/bin/python collectors/feedback_collect_all.py
```
Собирает: WB acc1 (отзывы+вопросы), WB acc2 (⛔ 401 — пропускается), Ozon acc1 (отзывы+вопросы),
Ozon acc2 (только вопросы, отзывы 403 пропускаются), Яндекс (отзывы). Каждый канал изолирован —
сбой одного не срывает остальные. Регулярность НЕ автоматизирована (нет таймера) — запускать
вручную или повесить на cron/systemd-timer.

Отдельные каналы (если нужно точечно):
```bash
./venv/bin/python collectors/wb_feedbacks.py wb_acc1
./venv/bin/python collectors/ozon_feedbacks.py oz_acc2
./venv/bin/python collectors/yandex_feedbacks.py
```

## 2. Движок ответов (черновики + постановка в очередь модерации)
```bash
# окно последнего месяца (по дате отзыва/вопроса):
./venv/bin/python -m reports.feedback_today --since 2026-06-24
```
- Пишет черновики в `raw_feedback.draft_*`, строит HTML-артефакт `docs/feedback_today_artifact.html`.
- При `FEEDBACK_MODERATION=1` ставит вопросы и отзывы-с-текстом в `feedback_moderation` (очередь бота).
- Пустые оценки-звёзды в очередь НЕ идут (только шаблон в артефакте).

## 3. Бот-модератор (Telegram)
Systemd (уже установлен и запущен):
```bash
systemctl status  feedback-moderation.service     # проверить
systemctl restart feedback-moderation.service      # перезапуск (после правок кода/.env)
journalctl -u feedback-moderation.service -f       # логи вживую
```
Запуск вручную (для отладки): `./venv/bin/python feedback_bot/tg_moderation.py`

### Работа в боте (@reviewswbozon2_bot)
- `/menu` — сводка за 30 дней (неотвечено по всем 5 каналам + очередь) + кнопки.
- Кнопка «📥 Показать всё за 30 дн.» / `/all` — прислать все карточки окна; «5»/«10» / `/next` — порцией.
- Карточка: тип (вопрос/⭐отзыв N★), 📅 дата, текст покупателя, черновик ответа, источник.
- Кнопки под карточкой: ✅ Отправить · ✏️ Править (прислать свой текст) · 🕒 Позже (напомнит через 5ч) · 🚫 Пропустить.

## 4. Боевая отправка (ТОЛЬКО после контролируемого теста)
1. Проверить в dry-run: карточки приходят, кнопки работают, в логе «ушло бы».
2. В `.env` выставить `FEEDBACK_LIVE_SEND=1`, `systemctl restart feedback-moderation.service`.
3. Нажать ✅ на ОДНОЙ карточке, сверить в ЛК WB/Ozon/Яндекс + `raw_feedback.posted_ok=true`.
4. Убедиться и продолжать. Откат — вернуть `FEEDBACK_LIVE_SEND=0` + restart.

## 5. Миграции БД (если разворачивать с нуля)
```bash
./venv/bin/python -c "from core import db; db.apply_sql_file('migrations/049_feedback_moderation.sql')"
./venv/bin/python -c "from core import db; db.apply_sql_file('migrations/050_compat_cache.sql')"
./venv/bin/python -c "from core import db; db.apply_sql_file('migrations/053_feedback_moderation_snooze.sql')"
```

## Доступ по каналам (факт на 2026-07)
| Канал | Отзывы | Вопросы |
|---|---|---|
| WB acc1 | ✅ | ✅ |
| WB acc2 | ⛔ токен без scope | ⛔ |
| Ozon acc1 | ✅ (Premium) | ✅ |
| Ozon acc2 | ⛔ нет Premium | ✅ |
| Яндекс acc1 | ✅ | — (в API нет) |
