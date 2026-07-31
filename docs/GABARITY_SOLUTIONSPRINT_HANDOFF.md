# Solutions Print (cartridge.ru) → габариты WB — ХЕНДОФФ рабочей сессии 2026-07-22

Поток **gab**. Цель: спарсить реальные короба/вес поставщика **Solutions Print** с их B2B-сайта
**cartridge.ru**, влить в `supplier_dims`, закрыть «висяки» габаритов карточек WB (Solutions Print сейчас
83% реал.матч + SKIP-хвост карантина 128). Клиент дал вход, попросил спарсить, потом — ускорить обход,
потом — прогнать join через Codex перед записью в WB. Клиент выбрал путь **«захардить join, потом писать»**.

## ДОСТУП (cartridge.ru = 1С-Битрикс, B2B-магазин Solutions Print)
- Вход: POST `https://cartridge.ru/personal/` c `AUTH_FORM=Y&TYPE=AUTH&backurl=/personal/&USER_LOGIN=<e-mail>&USER_PASSWORD=<пароль>&USER_REMEMBER=Y`
  → редирект на `/b2b/personal/`. Логин/пароль — у клиента в чате (e-shop@digitalsquare.ru).
- Габариты **только под авторизацией** (анонимно 0). На карточке: `itemprop="sku"`=Код товара,
  `Габариты, мм`=ДхШхВ (2 вёрстки: `<td itemprop=value>` ИЛИ `resource__title">Габариты:</span><span>`),
  `Объём`(м³), `Вес`(кг), `Аналог`=OEM-модель.
- **ВАЖНО про скорость:** Битрикс сериализует запросы с ОДНИМ PHPSESSID (блокировка PHP-сессии) —
  8 и 24 потока дали одинаковые 1.8/с. Решение: **мульти-сессия** (K независимых логинов, шард URL).
  6 сессий → ~3.3–4.0/с. Есть ещё глобальный троттл сервера (не масштабируется линейно).

## ФАЙЛЫ (scratchpad: /tmp/claude-0/-root/138832d6-f4fd-4402-8b96-ac7244632593/scratchpad/)
- `crawl_dims_ms.py <K>` — мульти-сессионный краулер (логины внутри), resume от `dims.jsonl`, пишет `crawl.log`.
- `crawl_urls.full.txt` — 10 087 URL продуктов (из sitemap-iblock-1.xml, single-segment /catalog/*/).
- `dims.jsonl` — РЕЗУЛЬТАТ обхода: {url, code, elem_id, title, analog, dims_mm[L,W,H мм], vol_l(литры), weight_kg}.
- `cj.txt`, `sj1.txt` — cookie-jar'ы (могут протухнуть — перелогиниться).
- Загрузчик: `/opt/mp-analytics/scratch_load_solutionsprint_dims.py` — dims.jsonl → supplier_dims
  (supplier='solutionsprint', мм→см /10, м³ уже в vol_l). dry-run по умолч., `--execute` пишет. ИДЕМПОТЕНТНО.

## СТАТУС ФОНА (на момент записи 2026-07-22 ~в процессе)
- Краулер ИДЁТ (PID был 1589439, 6 сессий), ~7100/10087, 4.0/с, ETA ~13 мин, err=0, ~4133 с габаритами.
- Проверка завершения: `grep DONE crawl.log`. Если процесс умер — перезапустить `nohup python3 crawl_dims_ms.py 6 >> crawl.log 2>&1 &` (допарсит остаток).

## CODEX-РЕВЬЮ join-логики — НАЙДЕНЫ КРИТ.ДЕФЕКТЫ (перепроверены мной на реальных данных)
Авто-запись модельных матчей в WB СЕЙЧАС НЕБЕЗОПАСНА (ложноположительный занижающий матч → штраф WB). Подтверждено:
1. **MODEL-regex в `scratch_dims_fixlist.py:15` обрезает коды:** `106R01481`→`EROX106R`, `MLT-D111L`→`D111L`,
   `C13T03V14A`→`C13T`+`V14A`, `ISO 19752`→ложн.`ISO1975`. Широкие мусорные ключи объединяют разнородные товары.
2. **Баг МОЕГО парсера:** `analog__content` берёт ПЕРВЫЙ блок = из карусели «сопутствующие», а не главного товара.
   Аналог `HP 89A (CF289A/...)` налип на 117 чужих товаров (пластик/бумага/смола). **Вывод: analog как ключ НЕ юзать —
   dims/vol/weight/title/code надёжны (главный товар идёт первым в HTML), analog — нет.**
3. median разнородных кандидатов может выбрать чужой заниженный короб (общие ключи численно побеждают точный).
4. цвет/yield (A/X/XL/H) не сверяются семантически (но полный код их различает — см. фикс ниже).
5. `pack_count()` считает только по WB-названию; supplier-набор vs одиночка не сверяется → риск занижения набора.
6. article-join в fixlist глобальный без ключа поставщика (коллизии 6-значных кодов), без ORDER BY.
7. **Флаги ELONG/TINY НЕ ловят обычное занижение** (11.1→4 л проходит без флага) — главная дыра защиты.
+ мусорные объёмы в harvest (смола vol=1000 л) — нужен guard консистентности.

## РЕШЕНИЕ (согласовано с клиентом: захардить → писать). НЕ трогаем общий scratch_dims_fixlist.py —
строим ОТДЕЛЬНЫЙ строгий join-модуль только для Solutions Print. Прогресс:

### ✅ Задача 1 (в работе, почти готов): извлекатель полных OEM-кодов
Whitelist картриджных форм с границами `(?<![A-Z0-9])...(?![A-Z0-9])` (см. рабочий код ниже). Даёт ВЫСОКУЮ
точность: топ-коды все картриджные, модели принтеров (C8600, FSC5100DN, MC853, QL-1050) корректно отсекаются.
Покрытие WB 27% / cartridge.ru 21% — намеренно консервативно (пропуск безопасен; полноту добирать whitelist'ом).
Полный код паттернов — в scratchpad-экспериментах этой сессии (FAM=[...] whitelist, ниже воспроизводим).

Рабочий whitelist (FAM), проверенный:
```
FAM=[r"\d{3}R\d{4,5}", r"MLT-?D\d{3}[A-Z]?", r"CLT-?[KCMY]\d{3}[A-Z]?", r"C-?EXV\d{1,3}",
 r"[CG]PR-?\d{1,3}", r"CRG-?\d{2,3}[A-Z]{0,2}", r"TN-?\d{3,4}[A-Z]{0,2}", r"DR-?\d{3,4}[A-Z]{0,2}",
 r"TK-?\d{3,4}[A-Z]?", r"DK-?\d{3,4}", r"C[EFB]\d{3}[AXYUD]?", r"W[12]\d{3}[AX]?", r"C[CN]\d{3}[A-Z]?",
 r"Q\d{4}[AX]?", r"C13[A-Z]\d{2}[A-Z0-9]\d{2}[A-Z]?", r"106R\d{5}|108R\d{5}|013R\d{5}|101R\d{5}"]
RX=re.compile(r"(?<![A-Z0-9])(?:%s)(?![A-Z0-9])"%"|".join(FAM), re.I)
oem(text)= {norm(m) for m in RX.findall(text.upper()) if len(norm(m))>=5}   # norm=re.sub([^A-Z0-9],"")
```
Точный полный код сам различает цвет/yield (CF289A≠CF289X, 106R по цвету — разные коды).

### ⏳ Задача 2: чистка harvest/loader
- Парсер: analog брать ТОЛЬКО из main-product-wrap (`data-entity="main-product-wrap"`), либо не юзать analog вовсе (title-only).
- Loader: guard `|vol_l - L*W*H/1000|` ≤ ~15%, стороны>0, отсев vol>60л, дедуп по коду с выбором полной строки, guard пустого INSERT.

### ⏳ Задача 3: строгий join-модуль Solutions Print → CSV (схема docs/dims_fixlist.csv)
Матч ТОЛЬКО: точный полный OEM (из TITLE, не analog) ∩ один согласованный кластер (низкий разброс объёма),
supplier-aware, известный pack. Неоднозначное → карантин. Прогон продажных висяков (profiline_screen.csv 487,
SKIP-хвост quarantine_verdict.json 101) + Solutions Print МСК.

### ⏳ Задача 4: reduction-ratio guard + запись
Guard: new_vol < 0.5×card_vol без корроборации → карантин (ELONG/TINY это не ловят). Dry-run → свод на утверждение →
`scratch_apply_dims.py --execute --skip-flagged`. Откат: `scratch_apply_dims.py --rollback docs/apply_dims_log.json`.

## ГЛАВНОЕ ПРАВИЛО: запись в WB — только высокоточные (точный полный OEM + согласованный кластер + guard занижения).
Всё сомнительное — в карантин (безопасный ложноотрицательный). НЕ авто-писать модельные матчи старым пайплайном.

## Связь с существующим: [[main-consolidation-2026-07]] (WB уже применено 4111 карт), артефакт свода
claude.ai/code/artifact/fa141941-a56e-4f29-ba10-53c82a04f5bb. Схема supplier_dims: supplier,article,barcode,
length_cm,width_cm,height_cm,weight_kg,volume_l,title,src_file. Solutions Print там ДО этой сессии НЕ было.

## РЕЗУЛЬТАТ (2026-07-22, обход завершён, задачи 1-3 готовы)
Harvest: 10 053 товара, 6115 с ДхШхВ, 7652 с весом. Влито в supplier_dims: **6005 solutionsprint** (объём из короба).
Строгий join `scratch_solutionsprint_join.py` (Codex-хардненный) → `docs/solutionsprint_fixlist.csv`:
- **805 уверенных** (точный полный OEM + согласованный кластер + max-of-cluster + guard занижения; наборы/мульти-код → карантин).
- 693 = ПОДТВЕРЖДЕНИЕ уже применённых 4111 (Solutions Print совпал независимо → валидация).
- **112 ЧИСТО НОВЫХ** (не в 4111) → `docs/solutionsprint_apply_new.csv`, ~7 262 ₽/мес, ЖДУТ «ок» на запись.
- 1028 карантин (`solutionsprint_quarantine.json`): наборы (нужна сумма компонентов), неоднозначные, занижение без корроборации.
Честно: прямая новая выгода скромная — 73% висяков названы только по принтеру (матчить по принтеру опасно, Codex#1),
Solutions Print сильно пересекается с текущими поставщиками. Главная ценность: валидация 4111 + постоянный источник + 6 SKIP подтверждены.
ЗАПИСЬ (после «ок»): `./venv/bin/python scratch_apply_dims.py --csv docs/solutionsprint_apply_new.csv --execute --skip-flagged`
(откат: `scratch_apply_dims.py --rollback docs/apply_dims_log.json`). НИЧЕГО В WB ПОКА НЕ ЗАПИСАНО.
Ключевые скрипты: scratch_oem_extract.py (извлекатель OEM), scratch_load_solutionsprint_dims.py (загрузчик),
scratch_solutionsprint_join.py (строгий join).

## ЗАПИСАНО В WB (2026-07-22)
1. **Solutions Print — 50 карт** (реальные короба поставщика, высокое доверие), все HTTP200, пере-фетч подтвердил.
   Лог: общий docs/apply_dims_log.json.
2. **profiline-derived тонер — 176 карт** (~25 360 ₽/мес, НИЗКОЕ доверие: короб синтезирован из объёма профилайна,
   гейт по типу + пол, наборы исключены). Все HTTP200. Фотобарабаны (32) ПРИДЕРЖАНЫ по решению клиента.
   ИЗОЛИРОВАННЫЙ ОТКАТ: `scratch_apply_dims.py --rollback docs/apply_profiline_derived_log.json` (только эти 176).
Общий лог apply: 4111 → 4161 (SP) → 4337 (profiline-derived). Генератор: scratch_profiline_derived.py,
CSV: docs/profiline_derived_apply.csv (все 266), docs/profiline_derived_toner.csv (записанные 234→176).
ПРОВЕРКА ПОСЛЕ 1-2 ПОСТАВОК: сверить, не перемерил ли ВБ profiline-derived (риск занижения) → при штрафах откатить по метке.
ОТКРЫТО: 32 фотобарабана profiline-derived (крупные, ×5-9 ужатие — придержаны); карантин SP 1028 (наборы — сумма компонентов).
