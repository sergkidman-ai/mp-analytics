# Выполненные команды и границы воспроизведения

Рабочая директория `/opt/mp-analytics`, Python `./venv/bin/python`, аккаунт `oz_acc1`.
Никакие команды записи бизнес-данных, запуска рабочих сборщиков, git checkout/reset/pull/commit/push не выполнялись.

Чтение правил и контекста: AGENTS.md, CLAUDE.md, .claude/rules/{kb-index,ozon,data-layer,fin,customer-orders}.md, docs/CUSTOMER_ORDER_RULES.md, docs/BRIEF_MKT_OZON.md и указанные пользователем HANDOFF. Поиск файлов через rg; анализ JSON/CSV скриптами, вывод ограничен сводками. Старые разрешения на изменения не использовались.

Первоначальное чтение (уже выполнено; не повторять автоматически):

```bash
./venv/bin/python docs/reports/ozon_family_audit_20261106/inspect_sources.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/snapshot_sources.py db
./venv/bin/python docs/reports/ozon_family_audit_20261106/snapshot_sources.py api
./venv/bin/python docs/reports/ozon_family_audit_20261106/snapshot_sources.py archive
./venv/bin/python docs/reports/ozon_family_audit_20261106/supplement.py coverage
./venv/bin/python docs/reports/ozon_family_audit_20261106/supplement.py attrs
./venv/bin/python docs/reports/ozon_family_audit_20261106/monthly_coverage.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/fulfillment_evidence.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/preserve_execution_example.py
```

У supplement attrs был технический второй запуск до завершения первого, остановлен Ctrl-C; исходный проход получил ошибку чтения JSON в момент записи второго. После остановки обоих процессов добавлены атомарная запись и файловая блокировка. Финальный запуск переиспользовал сохранённые ответы и завершил все 49 батчей. Конечные файлы проверены полностью. Неизвестное число повторных сетевых попыток не выдано за ноль. Самостоятельных выгрузок субагенты не делали. Исходный проход схем прочитал 276 пар включая архив: 269 HTTP200, 7 HTTP400; финальный код ограничен схемой текущей категории. Файлы остальных фактически полученных схем сохранены как источники B.

Первый monthly_coverage завершился read-only SQL syntax error на имени alias; исправлен AS period_month, весь read-only проход повторён. Данных в БД не менял. Первое чтение fulfillment_evidence и узкое дополнение posting→demand оба пусты по нужному источнику; дальнейшие попытки не предпринимались.

Офлайн-расчёты:

```bash
./venv/bin/python docs/reports/ozon_family_audit_20261106/agents/a_build.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/agents/a_finalize.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/agents/a_anchor_schemes.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/agents/b_build.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/agents/b_report.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/agents/c_inspect_year.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/agents/c_check_year_keys.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/agents/c_verify_maps.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/build_map.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/glue_schemes.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/agents/c_verify_final.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/render_readme.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/leader_validate.py
./venv/bin/python docs/reports/ozon_family_audit_20261106/finalize_manifest.py
```

Все выходы ограничены этим каталогом; A/B/C пишут отдельные agents/a_*,b_*,c_*; общие карты и README пишет ведущий. Проверки C являются проверками самого аудита, а не новыми тестами/изменениями рабочего проекта. Годовой сбор не запускался. `SHA256SUMS` проверяется из каталога аудита командой `sha256sum -c SHA256SUMS`.

После подтверждения 49 исходных attrs_*.json и 48 492/48 492 coverage удалён только собственный избыточный агрегат attributes.json (424 МБ); все используемые исходные ответы и provenance сохранены. Аналитические скрипты читают исходные батчи, агрегат не является зависимостью. Это уменьшает дублирование снимка и не требует повторных API-чтений.
