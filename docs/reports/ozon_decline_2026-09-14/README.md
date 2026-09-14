# поток: mkt
# Ozon — диагностика падения, 14.09.2026

| Файл | Что |
|---|---|
| `memo.md` | решение-memo: достоверность данных, мост падения, эксперименты, аудит E8 |
| `drop_real_sku_18.csv` | 18 реальных + 1 пограничный выпавший SKU с остатком: состояние и причина |
| `acc2_decline.md` | первичный разбор падения oz_acc2 |
| `x2_preflight.md` | дизайн X2-теста (не запускается) |
| `schema_ozon.md` | карта Ozon-таблиц БД, использованных в расчётах |
| `queries/` | скрипты расчётов в том виде, как они запускались |

## queries/

Все скрипты только читают БД (SELECT, DATABASE_URL из `.env`), HTTP-вызовов нет.
Промежуточные выгрузки (pickle/csv) в репозиторий не сохранены: скрипты пишут их в каталог сессии
(путь `/tmp/claude-0/.../scratchpad/<раздел>/` зашит в начале файлов — перед повторным запуском
заменить на свой каталог). Порядок запуска:

- `decline/` — s1_extract → s2_metrics → s3_bridge → s4_extra → s5_report → s6_md (месяцы, окна, мост);
- `removal/` — extract → s1_cohort … s7_groups, mk_top (волна 1, E8 DiD, таблица экспериментов);
- `bundles/` — pull → prep → main → did → fam_win → wave (экономика наборов);
- `drop118/` — s1_state → top20 (118 выпавших SKU);
- `acc2/` — x1_extract → x2_newsku → x3_main;
- `x2pre/` — pull2 → elig → final_elig → curve → power → risk;
- `e8audit/` — q1 … q14 (проверка доказательств E8).

Вердикт E8 CAUSAL_OK из `removal/s4_e8.py` отозван аудитом `e8audit/` → INCONCLUSIVE.
