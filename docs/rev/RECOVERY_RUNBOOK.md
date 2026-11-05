<!-- поток: rev -->
# Rev: резервирование и перенос

Рабочий источник: cloud-001, отдельная `rev_recovery_20261103`.
GitHub проекта публичный: туда идут только код/инструкции, никогда дампы,
`.env`, ключи, ручные ответы покупателей или журналы публикаций.

## Что сохраняется

`ops/rev_backup.py` держит один экспортированный PostgreSQL snapshot:
полный custom pg_dump всех таблиц и согласованные количества строк.
Для 18 критичных таблиц дополнительно сохраняются digest содержимого:
обращения, модерация, ручная память (`approved_answers`, `brand_notes`,
`rev_expert_evidence`, `compat_ref`), журнал попыток публикации, архив, очереди ИИ,
Telegram inbox/checkpoint и устойчивые edit sessions.
Проверка восстановления сравнивает количества всех таблиц и эти digest.

В зашифрованный комплект входят действующий исходный код (включая ещё
не закоммиченные исправления), Python runtime/точные версии библиотек,
конфигурация `.env` и units только feedback-проекта. Архив не содержит
ключа расшифровки. Файлы `.env` и секреты никогда не выводятся в отчёт.

Шифрование: GnuPG AES-256 с проверкой целостности; случайный 256-битный
ключ `/root/.config/rev-recovery/backup.key` (0600). ЕГО ОБЯЗАТЕЛЬНО
сохранить отдельно вне VPS в менеджере паролей/защищённом хранилище
владельца. Потеря единственной копии ключа делает архив бесполезным.
Не помещать ключ в ту же папку общего доступа и не отправлять в чат.

## Регулярная внешняя копия

Пример конфигурации без секретов: `ops/rev_backup.env.example`.
Рабочий конфиг `/root/.config/rev-recovery/backup.env` (0600).
Поддерживаются SSH с отдельным ключом и проверенным known_hosts либо
частная папка Google Drive с автономным offline OAuth refresh token.
Для Google Drive нужен JSON с `client_id`, `client_secret`, `refresh_token`
(0600) и ID частной папки. Подключение Drive к чату не предоставляет
планировщику VPS автономные OAuth-права.

При SSH данные отправляются во временный файл, проверяются SHA-256 на
другом сервере и переименовываются. При Drive сверяются size и md5Checksum;
разрешена только частная папка с owner ACL (без других получателей). Удалённое удаление не выполняется.
Ошибка доступа/проверки не маскируется успехом. Локальная зашифрованная
копия остаётся; `latest.json` фиксирует `offsite.verified=false`.

Установка units выполняется только после успешной внешней копии:

```sh
install -m 644 feedback_bot/feedback-rev-backup.service /etc/systemd/system/
install -m 644 feedback_bot/feedback-rev-backup.timer /etc/systemd/system/
systemctl daemon-reload
systemctl start feedback-rev-backup.service
systemctl enable --now feedback-rev-backup.timer
systemctl status feedback-rev-backup.timer --no-pager
```

Таймер: каждый час, случайная задержка до двух минут, Persistent=true.
Целевой RPO после подтверждённого запуска — час + задержка и время
резервирования/передачи. При ошибке передачи RPO растёт: проверять
`systemctl --failed` и приватный `latest.json`, не полагаться на таймер.
Ротация локально: последние 12 копий + одна за каждый из 7 дней (до 19
копий), удаляются только свои копии с валидной квитанцией. При неполной
передаче не удаляются. Проверять запас диска; удалённую ротацию/квоту
настраивает владелец хранилища. Разовый upload через чат не равен таймеру.

## Перенос на новый VPS

Для копии runtime нужен тот же образ Ubuntu 22.04 x86_64, Python 3.10,
PostgreSQL 16 и GnuPG. Новый сервер заранее подготовить: установка ОС/БД,
DNS/доступы и скачивание внешней копии не входят в локальный drill.
Минимум свободного места: несколько размеров БД + место архивов.

1. Получить последнюю подтверждённую внешнюю `.tar.gz.gpg`, отдельно ключ
   из защищённого хранилища владельца; файлы/каталоги 0600/0700.
2. Проверить SHA-256 по сохранённой квитанции. Расшифровать только в
   закрытый bootstrap-каталог вне будущего каталога проекта:

```sh
install -d -m 700 /root/rev-bootstrap /root/rev-recovery
umask 077
gpg --batch --pinentry-mode loopback \
  --passphrase-file /private/rev-backup.key \
  --output /root/rev-bootstrap/recovery.tar.gz \
  --decrypt /private/latest.tar.gz.gpg
tar -xzf /root/rev-bootstrap/recovery.tar.gz -C /root/rev-bootstrap \
  recovery-tools runtime/venv
```

   Использовать только свой проверенный архив. Python symlink bundled
   runtime требует Python 3.10 на новом образе. После восстановления
   убрать только созданный bootstrap с расшифрованными данными.
3. Создать первоначальный закрытый конфиг с локальным DATABASE_URL для
   служебного доступа PostgreSQL (роль с CREATEDB либо локальный root со штатным postgres admin; БД-источник может
   отсутствовать на новом сервере, название `rev_recovery_*`). Командные
   строки/логи не должны содержать пароль. Нельзя использовать GitHub
   как хранилище этого конфига.
4. В `/root/.config/rev-recovery/backup.env` указать путь ключа через
   `REV_BACKUP_PASSPHRASE_FILE` (сам файл 0600). Запустить инструмент, указав НОВОЕ имя БД. Он отказывается перезаписывать
   любую существующую БД или непустой каталог:

```sh
/root/rev-bootstrap/runtime/venv/bin/python /root/rev-bootstrap/recovery-tools/ops/rev_restore.py /private/latest.tar.gz.gpg \
  --root /private/bootstrap-project \
  --database rev_restore_recovery_YYYYMMDD \
  --install-root /opt/mp-analytics
```

5. Восстановление сравнит все таблицы/критичные digest, сохранит `.env`,
   inbox/checkpoint/edit sessions; автоматически выключит публикацию
   (`FEEDBACK_LIVE_SEND=0`) и сменит callback epoch. Старые Telegram-кнопки
   не должны работать с вновь созданными ID. Units будут только в
   `recovery-systemd/`, службы автоматически не устанавливаются/не стартуют.
6. Проверить API площадок чтением, getMe текущего Rev, каталоги и память;
   сверить неизвестные результаты отправки с площадками. Не сбрасывать
   Telegram updates и не запускать другой poller. Убедиться, что публикует
   ровно один действующий сервер, затем осознанно включить публикацию.
7. Установить только project feedback units из `recovery-systemd/`,
   поправить пути новой БД/каталога, проверить сбор → черновик → модерация
   → публикация → «Отправлено»; повторный цикл не создаёт дубль.
8. Сразу настроить новую внешнюю копию и её проверку. Если автономный
   доступ был настроен до snapshot, его закрытые конфиги восстановлены
   только в `recovery-backup-config/`; осознанно перенести их в root config
   и проверить пути ключей. Ключ расшифровки берётся отдельно у владельца.

Для контрольного восстановления на текущем сервере достаточно:

```sh
venv/bin/python ops/rev_restore.py /private/latest.tar.gz.gpg \
  --database rev_restore_drill_YYYYMMDD_HHMMSS
```

Это создаёт только отдельную БД и не запускает services/бота/API.
Удалять drill-БД можно только по отдельному согласованию.

## Границы фактической проверки

Drill 2026-11-05: полный encrypted snapshot 171,114,198 bytes сделан за
161.2 s. Decrypt → PostgreSQL restore → сравнение 38 таблиц/17 critical
hashes → восстановление кода/runtime/конфига заняло 102.66 s. Bundled
Python 3.10.12 импортирует psycopg2/Anthropic/requests; публикации отключены,
callback epoch изменён, `.env` приватный. Следующий snapshot дополнительно
контролирует digest compat_ref (18 critical таблиц). Никакие API/службы
из drill не запускались. Эти времена не включают внешнюю передачу.

Локальная проверка не доказывает час на совершенно новом VPS: отдельно
нужно измерить загрузку внешнего архива, готовность образа и секретов,
доступы площадок и запуск полного цикла. Измеренные результаты drill
и подтверждение offsite хранятся приватно в `/root/rev-recovery`.

API Google Drive: [resumable upload](https://developers.google.com/workspace/drive/api/guides/manage-uploads),
[offline OAuth](https://developers.google.com/identity/protocols/oauth2/web-server#offline).

## Автосинхронизация закоммиченного recovery-кода

`ops/rev_git_sync.py --export` публикует безопасный deployment snapshot
в `refs/heads/rev/recovery-deploy` репозитория
`git@github.com:sergkidman-ai/mp-analytics.git`.
Источник — только SHA уже закоммиченной `rev/recovery-20261103`.
Исходная ветка, индекс, рабочие правки и origin/tracking не изменяются.
Новые рабочие правки никогда автоматически не коммитятся.

Экспорт сохраняет полный уже публичный main tree (включая core/card_facts,
каталог и зависимости) и накладывает исключительно whitelist rev-diff:
конкретные collectors/reports отзывов, feedback_bot, migrations 7xx,
ops/rev_*, tests/test_rev_* и test_publish_gate, docs/REV_RULES и этот
runbook. Из source не переносятся BRIEF, recovered_manual_rules,
данные, дампы, память или изменения других потоков. Каждый переносимый
blob проходит проверку path/размера/текста на секреты. Это автоматический
барьер; содержательный review экспортируемых изменений остаётся нужен.

Создаётся отдельный служебный commit-tree snapshot с source SHA в
сообщении. Parent — текущий remote deploy HEAD, при первом создании main.
Обычный push ограничен одной deploy-веткой; force/main/tags/submodules
не допускаются. Snapshot, созданный через GitHub API в той же ветке,
становится нормальным parent следующего FF. При совпадении tree новый
commit не создаётся. Неизвестный результат сначала сверяется с remote.

Изолированный private key:
`/root/.config/rev-recovery/github-deploy-ed25519` (0600).
Публичный файл владельцу:
`/root/rev-recovery/github-deploy-ed25519.pub`.
Private key никогда не передавать в GitHub или чат.
Hostkey получен через HTTPS `https://api.github.com/meta` и дополнительно
сверен с [официальным Ed25519 fingerprint GitHub](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/githubs-ssh-key-fingerprints).
Никакого ssh-keyscan/слепого доверия или credential из подключённого app.

**Одно действие владельца:** открыть
[Settings → Deploy keys](https://github.com/sergkidman-ai/mp-analytics/settings/keys),
нажать **Add deploy key**, вставить содержимое public файла, включить
**Allow write access**, сохранить. Название: `Rev cloud-001`.
Подробнее: [GitHub deploy keys](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys).

До выдачи deploy key проверка SSH установила: GitHub доступен, ключ ещё
не авторизован (Permission denied publickey). Службы не включены.

```sh
venv/bin/python ops/rev_git_sync.py --build-export-tree
```

Это формирует только локальные tree-объекты через отдельный временный
index, без смены refs/рабочих файлов и без SSH/push. JSON-план содержит
source/base/tree, changes(path/mode/type/sha) и excluded paths; его можно
использовать для эквивалентного API snapshot. После consolidation commit
план обязательно пересобрать, чтобы он включал закоммиченные исправления.

После выдачи deploy key и review плана:

```sh
venv/bin/python ops/rev_git_sync.py --export
```

Units `feedback-rev-git-sync.service/.timer` запускают именно --export,
каждые 15 минут плюс до 30 s задержки. Установить/включить их только после
успешного подтверждённого первого export. Состояние сохраняется приватно
в `/root/.config/rev-recovery/github-last-export.json`. Сетевые ошибки,
несоответствие hostkey, запрещённые payload и push race завершаются явно;
следующий tick сначала перечитывает remote, force не используется.

Отдельный raw-history режим --check оставлен для диагностики всей
исходящей истории: 17 исходящих коммитов/153 blob блокируются приватным
`docs/BRIEF_REV.md`. Raw sync исходной recovery-ветки не включается;
удаление BRIEF последующим коммитом не очистит прошлые версии.
Экспорт решает это, не перенося приватную source историю в deploy-ветку.
GitHub-копия кода не заменяет внешний encrypted backup БД/памяти/ledger.


## Фактическое состояние после объединения 05.11.2026

Финальный drill: 38 таблиц и 18 digest совпали, восстановление БД
143.638 секунды. Код/runtime/.env отдельно восстановлены и проверены.
Разовая внешняя копия в приватном Google Drive скачана обратно:
SHA-256 полного encrypted bundle и отдельная копия ключа совпали.
У коннектора ограничение 100 MiB на файл, поэтому bundle хранится двумя
частями вместе с manifest с точными именами/размерами/SHA-256. Собрать
строго part1 затем part2, проверить полный SHA-256 и только затем decrypt.
Ключ находится в отдельной owner-only папке; не помещать его в bundle.

На cloud-001 включён ежечасный локальный backup через явно обозначенный
`/etc/systemd/system/feedback-rev-backup.service.d/local-only.conf`
(ExecStart с `--local-only`). Это временная защита от потери отдельных
данных; она не защищает от потери самого VPS. Внешняя регулярная копия
ожидает автономного OAuth/SSH доступа, и не объявляется настроенной.
После записи credentials проверить штатный запуск без `--local-only`,
подтвердить offsite checksum и убрать только этот временный override.
Локальная ротация на текущем диске: последние 4 + по одной за 5 дней.

`feedback-rev-delivery.timer`: раз в час, только пять конкретных rejected404
и только одна дополнительная попытка каждому. Unknown никогда не
перепубликуется из-за пустого чтения; остаётся сверка/эскалация Маркету.

Для цели RTO≤1 час заранее подготовить совместимый образ и доступы.
План измерения: новый VPS/зависимости ≤20мин; загрузка и checksum ≤10мин;
restore/checks ≤10мин; API/Telegram/цикл и исключение дублей ≤20мин.
Это целевые интервалы, а не результат проведённого fresh-VPS испытания.
