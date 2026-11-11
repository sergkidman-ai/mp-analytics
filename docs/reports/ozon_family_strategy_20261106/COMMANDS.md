# Воспроизведение этапа

Все команды из `/opt/mp-analytics`. Выходы только в этом каталоге; первичный аудит и исходники года читаются по ссылкам. Архивные Ozon-карточки исключаются по положительному текущему universe до расчётов.

Офлайн-порядок (API, БД и внедрение не запускает):

```bash
./venv/bin/python docs/reports/ozon_family_strategy_20261106/inspect_evidence.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/extract_history.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/inspect_boundaries.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/resolve_physical.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/build_families.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/aggregate_families.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/classify_and_queue.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/validate.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/render_report.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/finalize.py
./venv/bin/python docs/reports/ozon_family_strategy_20261106/finalize.py verify
```

Отдельно уже выполнено `read_exact_compositions.py`: семь кодов зафиксированы в скрипте, только чтение mix_data. Первая попытка в песочнице дала ConnectionError; после разрешённого сетевого запуска получены семь HTTP503. Составы из этих ответов не выдумывались. Повторять запросы автоматически не требуется.

Для сокращения артефактов history_by_card хранит точные деньги/исходные ID заказов и первые даты CPC, а не копию всех 1,6 млн дневных рекламных строк. sales_lines сохраняет проверяемый ключ каждой выбранной товарной строки и её источник. Никакой новый годовой сбор не запускался.

Ответа на два вопроса о бизнес-границах не получено. Отсутствие owner_rules.json означает pending; подтверждение сетевого чтения не является принятием правил семей. Предложенные C-кандидаты, расширения и межсемейные составы не получают произвольных назначений.
