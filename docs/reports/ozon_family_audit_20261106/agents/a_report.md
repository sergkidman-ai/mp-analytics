# A: артикулы, SEO, физические составы и контексты семейств

Обработан полный union 48492 карточек oz_acc1:22536 текущих,25956 архивных; пересечение product_id=0. Бизнес-данные, API, БД и службы не менялись.

## Источники и правила

Структура МС: точный ms_product.external_code и supplier_stock.external_code; code — товар конкретного поставщика. Первые четыре цифры offer_id — артикульный якорь. Он хранится отдельно от физического состава. Ведущие нули сохранены.

Физические составы: set_cost.components/n_components/resolved_at, первоисточник TheCartridge mix_data; раскрываются вложенные наборы и кратности. Проверены992 состава, циклов/ошибок0. Отдельно сохранены исходный тип ТК и контекстная физическая роль. У5133 TC parent=Комплект фотобарабанов, хотя5129 ошибочно type=Картридж: это набор барабанов.

Бандл содержит одинаковый товар; бандл набора умножает целый подтверждённый набор. Суффиксный XN проверен по названию/прямому подтверждению6452X3MS22I0. Набор разных товаров определяется BOM, не словом «комплект» и не числом4 в характеристике.

У ТК color=NULL и ink_colors=[] во всех6763 моделях; prc_tc_link пуст. Поэтому цвет/семья не выводятся из отсутствующих полей. Black candidates из title/mono сохранены как candidates. Сырые чипы взяты из raw.chip; неизвестный чип не означает отсутствие чипа.

Физический перенос на SEO требует соответствия полной модели и цвета либо прямо подтверждённого владельцем правила ordinary-single: numeric child, qty1, явно чёрная карточка, соответствующий mono/K якорь, одиночная роль, отсутствие признаков набора/другой роли/конфликтов. Эти уровни различаются mapping_status. Цветные/наборные SEO сохраняют артикульный якорь, но остаются physical unknown без точной спецификации.

Складское исполнение не выводится из суффикса артикула. В stock_by_exact_external_code только ссылки на ms_id/store на общей последней дате06.11; численные stock/reserve не публикуются и не суммируются. Это наличие записи точного внешнего кода, а не доказательство исполнения SEO.

## Результат

```json
{
  "cards": 48492,
  "unique_product_ids": 48492,
  "article_kinds": {
    "seo_numeric_child": 17253,
    "suffix_requires_review": 7050,
    "primary_exact_four_digits": 6776,
    "bundle_marker": 7830,
    "unrecognized": 9583
  },
  "product_kinds": {
    "unresolved": 25977,
    "single": 13723,
    "homogeneous_bundle": 7800,
    "heterogeneous_set": 811,
    "heterogeneous_drum_set": 37,
    "drum_and_cartridge_set": 143,
    "single_component_reference": 1
  },
  "composition_statuses": {
    "unknown_physical_mapping": 25843,
    "exact_single_catalog_item": 5642,
    "single_article_mapping": 15881,
    "confirmed_bom_exact_code": 992,
    "unresolved_missing_bom": 131,
    "unresolved_missing_tc_model": 3
  },
  "tc_contexts": 992,
  "tc_context_types": {
    "heterogeneous_set": 811,
    "drum_and_cartridge_set": 143,
    "heterogeneous_drum_set": 37,
    "single_component_reference": 1
  },
  "component_rows": 25391,
  "relation_rows": 33527,
  "bom_errors": 0,
  "tc_color_present": 0,
  "tc_raw_colors_present": 0,
  "tc_link_rows": 0,
  "card_stock_code_exists_latest": 4421,
  "no_business_writes": true,
  "no_transitive_union": true,
  "mapping_statuses": {
    "article_anchor_only_physical_unknown": 16222,
    "exact_catalog_code": 6744,
    "owner_article_rule_ordinary_single": 7284,
    "article_convention_bundle": 7824,
    "verified_model_color_article_mapping": 797,
    "unresolved": 9446,
    "article_prefix_only_unconfirmed": 32,
    "article_anchor_conflicting_specs_physical_unknown": 143
  },
  "current_composition_statuses": {
    "exact_single_catalog_item": 5466,
    "single_article_mapping": 14937,
    "confirmed_bom_exact_code": 976,
    "unknown_physical_mapping": 1037,
    "unresolved_missing_bom": 117,
    "unresolved_missing_tc_model": 3
  },
  "archived_composition_statuses": {
    "unknown_physical_mapping": 24806,
    "single_article_mapping": 944,
    "exact_single_catalog_item": 176,
    "unresolved_missing_bom": 14,
    "confirmed_bom_exact_code": 16
  },
  "example_types": [
    "confirmed_toner_and_drum_distinct_roles",
    "ordinary_parent_and_proven_child",
    "archived_color_attribute_conflict",
    "operator_confirmed_suffixed_bundle"
  ],
  "validated_invariants": [
    "48492 unique product_id",
    "unknown mappings emit no components",
    "5133 drum set not toner+drum",
    "no numeric stock/reserve published",
    "no transitive component union"
  ]
}
```

## Границы семейств

Один подтверждённый контекст — полный точный multiset leaf external_code из BOM. physical_fingerprint учитывает кратности, модели, ресурсы, типы и raw chip. family_fingerprint может игнорировать лишь кратности при равном полном множестве компонентов; это не меняет физическое количество. Shared black/shared printer не объединяют контексты. Один leaf в нескольких разных контекстах → несколько candidate_family_ids, физическая карточка не назначается произвольно.

Одинаковый united_title без явной полной серии/согласованных исполнений остаётся candidate. Parent-only группы — provisional article contexts; их нельзя считать тысячами подтверждённых больших физических семейств.

## Прослеженные примеры

- confirmed_toner_and_drum_distinct_roles: 6642 → 5425 DR-316K [Фотобарабан], 4308 TN-328C [Картридж], 4309 TN-328K [Картридж], 4310 TN-328M [Картридж], 4311 TN-328Y [Картридж]; источник set_cost.components, дата2026-11-02 16:38:39.416997+00:00.
- ordinary_parent_and_proven_child: parent 0001 (Картридж Q2610A для принтеров HP LaserJet 2300 черный) → child 00011 (Картридж для HP LaserJet 2300); mapping=owner_article_rule_ordinary_single, physical={"0001":1}.
- archived_color_attribute_conflict: parent 0033 (Картридж ML-1710 для принтеров Samsung ML-1510, ML 1520 черный) → child 00331 (Картридж DS ML-1520); mapping=article_anchor_only_physical_unknown, physical={}.
- operator_confirmed_suffixed_bundle:6452X3MS22I0 →6452at×3 по исполненным историческим заказам; operator-confirmed кратность, текущий склад не наследуется.

Машинные файлы:a_cards.csv (все карточки),a_components.csv (только разрешённые физические связи),a_relations.csv (артикульные и контекстные связи с источником/статусом/датой),a_tc_contexts.json (BOM proof),a_family_fingerprints.json,a_named_examples.json. Неопределённость явна в каждой карточке; отсутствие компоненты в a_components.csv не превращается в нулевой физический состав.

## Схемы артикульных якорей

Текущих numeric SEO:8203; точных BOM в общем snapshot:992 (не994).
Якорь одиночного картриджа/тонер-картриджа:7285; фотобарабана:313; заправочного тонера:8.
Якорь отдельного смешанного набора «Фотобарабан + картридж»:4 SEO на2 якорях.
Два реальных примера mixed-set SEO сохранены в a_anchor_schemes.json; это доказанная артикульная привязка, физсостав дочерних не подтверждён.
Остальные якоря наборов:90; прочие:503.
Цветовой scope:821 BOM-контекстов по явному типу набора + raw.colors_count≥2; это candidate полного цветового семейства.
В scope найдено3399 уникальных текущих numeric children: под K-компонентом3271, под неK30, под кодом набора33, цвет родителя неизвестен/смешан65.
У328 children якорный leaf участвует в нескольких разных BOM; контексты не объединены.
6642 назван generic toner+drum: TN/DR встречаются и у Konica Minolta, принадлежность Brother не выводится из букв модели.
00331 M при Samsung ML1520 — archived_color_attribute_conflict; это конфликт атрибута источника, не подтверждённая цветная SEO.
