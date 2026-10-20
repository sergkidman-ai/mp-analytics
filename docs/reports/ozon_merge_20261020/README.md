# wb4c: публикация работы20.10.2026

Прямое поручение пользователя: закоммитить, запушить и влить сегодняшнюю работу вmain.
Ветка fin/wb4c-ozon-api-migration, worktree fin-wb4c-ozon-api.

Код объединён с origin/main5f74cc8, который уже содержит shipment/customer guard,
интерфейс заказов, сентябрьскую expense basis и уведомления по прибыли<=0.
Merge commit: dc0c33c55f1f0194d827e69825e39ad8773b4449. Он вошёл в общий main
и опубликован в origin/main и origin/fin/wb4c-ozon-api-migration.

Включены FBSv4/FBOv3, конечные ретраи/время/пагинация, общий account lock,
Money-совместимость финансовых/веб-читателей, loaded_at для upsert, ограниченный
by-day collector, единая versioned expense basis и read-only аудит трёх отправленных
уведомлений Наталии. Сохранена уже внедрённая правка FBS page limit100 вместо1000,
добавлены её3регрессионных теста и уточнён публичный контракт адаптера.

Конфликты сmain: сохранены loaded_at и тест свежести, принят комментарий общей
формулы; customer UI/router и upstream логика/тесты сохранены. Код всех57Python
файлов в плане обновления проверенAST. Слияние не меняет цели300₽/10%себестоимости.

Shared checkout оставлен наmain. С учётом уже внедрённых файлов подготовлено
трёхстороннее объединение; перед продвижением проверены исходные SHA256 и пустой
index. Сначала применены только нужные результаты, затем штатный git merge--ff-only
продвинул main5d5504f→dc0c33c.151путь нового дерева проверен,202посторонних dirty-файла
остались байт-в-байт без изменений. Чужие изменения не коммитились и не stash/reset.
Историческая untracked копияcustomer_guard/domain.py распознана по точному совпадению
с коммитом12bce8baa и обновлена до версии, уже опубликованной владельцем вmain.
Зафиксированный runtime/customer-guard-current по-прежнему указывает на507d3ef24f5f.

Проверка из итогового /opt/mp-analytics:
**444passed,72subtests passed**, SQL-тесты в отдельных схемах. Те же финансовые
и order/shipment тесты прошли в объединённом worktree до публикации.
Dashboard/marketing active; страницы заказов и финансовыеAPI HTTP200.
Сервисы/расписания этим слиянием не переключались. Полный run_daily, боевойapply,
массовая пересборка, изменение цен/участия, уведомления не запускались.

Ошибка пагинации ops.ozon_actions.products из read-only аудита остаётся открытой:
дневные action guard10:30/17:30 падали наoz_acc1 доoz_acc2. Этотmerge не объявляет
её исправленной. Продолжение — отдельная правка завершения курсора и изоляции кабинетов.

Доказательства: tests_final.txt, shared_result.json, web_checks.json,
fast_forward_preservation_probe.json. Полные локальные backup/план/рабочие патчи
не публикуются. Отчёты задач: ../ozon_runtime_deploy_20261020/README.md,
../ozon_expense_basis_20261020/README.md, ../ozon_action_guard_audit_20261020/README.md.
