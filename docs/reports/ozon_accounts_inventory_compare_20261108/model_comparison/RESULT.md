# Исправленное сравнение склеек

Дочерние исключены из сравнительных показателей; четыре фактические пары подтверждены API. Равенство карт не доказано; состав бандлов различается.

{
  "corrected_at_utc": "2026-11-08T20:14:50.215809+00:00",
  "definition": "native model_id shared by >=2 own nonarchived nonchild cards; singleton is not glue",
  "metrics_excluding_children": {
    "oz_acc1_all_nonarchived": {
      "cards_excluding_children": 14333,
      "model_ids_including_singletons": 3526,
      "glue_groups_2plus_excluding_children": 2515,
      "single_card_models_excluding_children": 1011,
      "cards_in_glue_excluding_children": 13322,
      "missing_model_id": 0
    },
    "oz_acc1_stock_nonarchived": {
      "cards_excluding_children": 9022,
      "model_ids_including_singletons": 2538,
      "glue_groups_2plus_excluding_children": 1831,
      "single_card_models_excluding_children": 707,
      "cards_in_glue_excluding_children": 8315,
      "missing_model_id": 0
    },
    "oz_acc2_all_nonarchived": {
      "cards_excluding_children": 16892,
      "model_ids_including_singletons": 3457,
      "glue_groups_2plus_excluding_children": 2521,
      "single_card_models_excluding_children": 936,
      "cards_in_glue_excluding_children": 15953,
      "missing_model_id": 3
    },
    "oz_acc2_stock_nonarchived": {
      "cards_excluding_children": 9775,
      "model_ids_including_singletons": 2519,
      "glue_groups_2plus_excluding_children": 1809,
      "single_card_models_excluding_children": 710,
      "cards_in_glue_excluding_children": 9065,
      "missing_model_id": 0
    }
  },
  "children_separate": {
    "oz_acc1": {
      "nonarchived_children": 8203,
      "in_stock_children": 6206,
      "single_own_card_model_children": 8197,
      "children_sharing_native_model_with_own_card": 6,
      "groups_with_children_2plus": 3
    },
    "oz_acc2": {
      "nonarchived_children": 2874,
      "in_stock_children": 1217,
      "single_own_card_model_children": 2872,
      "children_sharing_native_model_with_own_card": 2,
      "groups_with_children_2plus": 1
    }
  },
  "source_windows_utc": {
    "oz_acc1": "2026-11-08T18:06:26.144468+00:00",
    "oz_acc2": "2026-11-08T18:06:27.775355+00:00",
    "oz_acc2_restore_overlay": "2026-11-08T19:24:19.869201+00:00"
  },
  "targeted_child_readbacks_utc": {
    "oz_acc1": "2026-11-08T20:12:49.982163+00:00",
    "oz_acc2": "2026-11-08T20:12:50.478560+00:00"
  },
  "child_readback_requests": 2,
  "child_readback_cards": 8,
  "equal_counts_do_not_prove_equal_composition": true,
  "article_family_is_not_native_glue": true,
  "cross_account_same_native_id_not_required": true,
  "ad_or_card_mutations": 0
}
