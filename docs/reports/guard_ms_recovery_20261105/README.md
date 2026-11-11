# Автодобор отсутствующих заказов — событийный режим с05.11.2026

Периодическоготаймера нет. customer-guard-ms-recovery.path следит за появлениемJSONв runtime/customer-guard-ms-recovery-events/inbox/. Он запускает customer-guard-ms-recovery.service только по заявкам. Старый timer отключён и его файл удалён.

## Событие сторожа

После store.finish/успешного commit проверки missing_ms, независимо от доставки Telegram, customer_guard.service.work вызывает enqueue_missing. Ещёраз проверяются вБД: актуальнаяgeneration/последнийуспешныйmissing_ms,ms_idNULL,ожидание сборки,наступившийms_due_at(70мин). В заявку входят толькоorder_id/check_id/platform/account/number. Записьatomic+fsync; очередь внутри runtime разрешена ReadWritePaths финансовогосторожа. Ошибкапубликации неотменяетподтверждённуюпроверку ивыводитсявrecovery_errors/heartbeat.

## Обработка и восстановление

Воркер принимает событие вdurable ledger перед удалениемJSON. Повтортогожеcheck_idничегонеоткрываетзаново. Проверяются толькоIDизсобытий; нетзаявок→нетзапросовБД/МС/МП. Устаревшиезаявкиотсеиваютсясвежимчтениемсторожа. Далее прежние точныйпоискМС/свежийМП/штатныйcreate_one,общийschedulerlock,до3созданий,проверкицены/наборов/поставщиков/экспресса/intent/readback,узкийstatus+shipmentpipeline.

Неоконченные события/обработка сохраняютpending marker. При временномсбое илизанятомимпорте serviceвозвращает75→Restart через15мин толькоэтойочереди;приотсутствиизаявокпустогоповтора нет. НепроверенныйPOST/прежнееintent требуютсверкибезслепогоповтора;manual exit1неперезапускается(RestartPreventExitStatus=1). ServiceвключённастартеОС сConditionPathExists=pending:приперезагрузкевозобновляеттолькоисторические незаконченные события,при пустойочереди непросыпается. Изменениеpending-сигналасериализованосproducerочередью,чтобыновоесобытиенепотерялосигнал.

## Проверки и эксплуатация

44тестаPASS:публикацияпослекоммита,отсутствиесигналадлянеактуальногокейса,независимостьотTelegram,изоляцияошибкипубликации,приёмдоack,повторысобытий,нулевыесетевые/БДвызовыбеззаявок,блокировки/дубли/свежийМП,прерываниеобработки,границыобласти. systemd-analyze verifyпройден.

Живаяпроверка09:49МСК:изБДоднократноподняты2ранее подтверждённыхсторожемсигнала0125485641-0033-1и6199380102. Созданиефайловсамозапустилослужбучерез.path. ОбаMSдокументаужеесть;0созданий/0дополнительныхстатусныхзаписей/0ошибок. Событияприняты,inboxпуст,pendingудалён,retry_pending=false,службазавершилась;NRestarts0. СтатусыдокументовсохраненыНеукомплектован/06.11.

Статус:systemctl status customer-guard-ms-recovery.path customer-guard-ms-recovery.service. Очередьruntime/customer-guard-ms-recovery-events/,ledger.private/customer_guard_ms_recovery/. Дляотключения:systemctl disable --now customer-guard-ms-recovery.path customer-guard-ms-recovery.service. Ветка inv/guard-recovery-events-20261105.
