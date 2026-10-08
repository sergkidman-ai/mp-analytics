# BRIEF_FIN_YANDEX_GUARD_FINANCE — 08.10.2026

Поток fin, задача docs/TASK_YANDEX_GUARD_FINANCE.md, шаги 1–6 завершены в изоляции.
Worktree: `/opt/mp-analytics/.claude/worktrees/fin-yandex-guard-finance`.
Ветка: `fin/yandex-guard-finance`, база актуального origin/main `067c10b`.
Код: `f6c724a` (проверенная финансовая база/активация) и `2dba131` (точный preview/нет promoPrice).
Сводка и все проверяемые списки: `docs/reports/yandex_guard_finance_candidate/report.md`.

Сделано: read-only preflight, backup/hashes, свежий сбор 7 кампаний за 2025-11-01–08.10 (4749 заказов, 51 страница), услуги сентября DONE, сверка closure 546/546 без пропусков и 46 дополнительных payments. Сохранённый closure-job PENDING; данные payments не записывались в raw closure. Повторный API-refresh возобновил jobs и подтвердил суммы.
Сентябрь: оборот 2995262.45, расходы 2006996.80, COGS 467978.36, прибыль 346459.29, удержания 67.00570763%, keep 32.99429237%. Семантика возвратов/FIFO и формула пола не менялись.
Кандидат получает отдельные source/month/version, coverage, бухгалтерский provisional и price_usable. Пульт может читать проверенное evidence; trading читает только отдельно опубликованный active.json. Ни один кандидат не активирован.
Два read-only recompute сентября идентичны. Hist/HTML preview изолированы. 19 финансовых + 7 существующих WB bundle tests, 3 DB control tests (3741.77), compileall и diff-check прошли; exact offline replay сумм/версии выполнен.

Свежий promo snapshot: 2127 SKU. 1280 действуют и проходят оба пола; 320 могут входить; 443 не проходят потолок/механику; 83 без достаточных данных (68 live без явной promoPrice + 15 кандидатов без COGS); 7200 исключён.
57 кандидатов впервые проходят вход по новой базе: `newly_eligible_actions.json` — основной список для рассмотрения. Три контрольных сейчас NOT_PARTICIPATING и могут войти: 34749→4451, 347410→4440, 22262→2969. Все 2127 строк — comparison.csv. Ещё у 90 ранее допустимых кандидатов меняется разрешённая ступень; basis_changed_actions.json содержит 147 входов, общий proposed_actions.json — 320 возможностей. Это не поручение отправить весь пул.
Исходный guard на 68 live без promoPrice механически выводит 64 remove/4 raise (null→0). Это не доказанное сравнение цен; отдельная диагностическая выгрузка исключена из предложений. При шаге 7 нельзя активировать базу при работающих --apply и тем самым допустить эти дополнительные команды без отдельного разрешения.

Общий checkout main, его локальные интеграции, БД, cron и живой hist не изменялись. Чужой dirty quarantine сохранён в baseline и основном checkout; в свои коммиты включён только финансовый патч. После обновления origin/main повторно собран COGS с текущими WB bundle fixes, изменился один SKU 6982; чужой код не включался в свои коммиты. Push/deploy/API writes отсутствуют.
Raw ZIP/JSON, backups, jobs, диагностические dry-runs и логи оставлены локально в worktree и исключены из коммитов; source_manifest.json содержит хеши. Replay на другом checkout потребует переноса этих источников.

Следующий шаг: только отдельная команда на конкретные SKU×акция×target и версию кандидата. Повторно проверить выбранные строки, остановить/ограничить trading readers до публикации active-pointer, сохранить локальные интеграции при переносе, отправить только одобренные действия без неразрешённого delete fallback, после 4–6 часов разделить sent и confirmed с уникальным run_id. Подробный порядок и ограничения — в report.md. Новую задачу сторожа отгрузок не строили, Ozon/FIN4 не меняли.

Финальная сверка артефактов: candidate/hist/comparison имеют одну версию `7d041e402d4ec4727b0e016a286072490d5f135ac63eed802e1531e09e1092f4`; active-pointer отсутствует. CSV содержит 2127 строк и явные причины, LF-формат исправлен в `59707bd`. Последующий коммит фиксирует только синхронизацию версий изолированных preview и эту передачу.

## Применение только трёх SKU по прямой команде пользователя

08.10 пользователь: «возвращай эти 3». Свежий read-only preflight подтвердил исходные условия, затем одним /promos/offers/update возвращены 34749→4451, 347410→4440, 22262→2969. Следующее чтение API сразу подтвердило MANUAL и точную promoPrice всех трёх; журнал sent→confirmed, ровно 3 строки, уникальный run `fin-return3-20261008T121606Z-46f0f152`. Полный результат: `docs/reports/yandex_guard_finance_candidate/return_three_result.md`; raw execution локально в return_three/.
Другие SKU, активная база, cron и код живого checkout не изменены. Текущий старый guard может снова исключить три SKU в 15:35 UTC; долговременная защита не внедрялась. Следующий шаг — отдельное решение пользователя по активной финансовой базе/читателям либо защите конкретных трёх от ошибочного старого пола.

## Активировано по прямой команде «тогда активируй»

08.10 исправленная база опубликована в рабочем `/opt/mp-analytics/reports/data/yandex_finance_basis/active.json`, отдельное report evidence 2026-09.json; версия 7d041e402d4ec4727b0e016a286072490d5f135ac63eed802e1531e09e1092f4, 67.00570763%, verified payments / бухгалтерски provisional (closure PENDING). Собственный патч 8 исходников применён с бэкапами и проверкой хешей, dirty локальные интеграции сохранены, main HEAD не менялся. Деплой не коммитил чужое dirty состояние; собственный код — в ветке fin/yandex-guard-finance, дополнение защиты guard `313ca5f`.
До активации добавлен guard: отсутствующая/нулевая promoPrice → frozen, без update/delete. Живой dry-run 1283 ok / 67 frozen / 0 raise / 0 remove; 34749/347410/22262 проходят новый пол по подтверждённым ценам. Новых ценовых команд и ручных --apply при активации нет; cron не менялись. 27 unit/bundle tests, 20 финансовых тестов на live modules, 3 DB control tests прошли; repeated live September preview без DB writes проверяет отсутствие регрессии. Детали: docs/reports/yandex_guard_finance_candidate/activation_result.md; backups/live CSV/runtime audit в activation/ (локально, gitignored).
Ночной refresh обновляет проверенное report-evidence, не подменяет утверждённый active-pointer; для следующего закрытого месяца нужен новый проверенный кандидат/активация. Следующий шаг — наблюдение штатного cron и завершение бухгалтерской closure-сверки, без массового добавления остальных 57/320 кандидатов.

## Сентябрь досчитан и закрыт в Пульте

08.10 по прямой команде пользователя точечно пересчитаны только сентябрьские yandex_monthly/yandex_finance_monthly и frozen hist-slot, сформирована и через HTTP проверена рабочая страница /reports/yandex. Оборот 2995262.45, расходы 2006996.80, REFUND 173828.00, FIFO 467978.36, прибыль 346459.29. report_closed=true, accounting_provisional=true: полный расчёт закрыт в Пульте, два closure jobs (сохранённый и свежий) остаются PENDING; статус документов честно показан на странице. Остальные месяцы обеих таблиц и hist сравнивались целиком и не изменились. Active trading snapshot побайтово прежний, новых торговых команд нет.
Код 12e5292: ручное закрытие требует verified basis, status report_closed сохраняется при refresh, UI показывает закрытие отдельно от документов. Код 63f0052: неуспешный новый refresh сохраняет последнее verified month evidence, не откатывает на partial closure. 23 финансовых + 3 DB control tests прошли, UI HTTP 200. Доклад: docs/reports/yandex_guard_finance_candidate/pult_close_result.md. Backups/preview/full before-after/jobs/logs — pult_close/ (локально). Следующий шаг — новая final closure-сверка; без точной сходимости accounting provisional не снимать.
