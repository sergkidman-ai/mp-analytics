# Бренд и API-восстановление автоархивных

{
  "groups": [
    {
      "account": "oz_acc1",
      "autoarchived_scope": "other",
      "brand": "Цифровой квадрат",
      "SKUs": 32,
      "positive_stock": 17
    },
    {
      "account": "oz_acc2",
      "autoarchived_scope": "other",
      "brand": "Dsquare",
      "SKUs": 61,
      "positive_stock": 23
    },
    {
      "account": "oz_acc2",
      "autoarchived_scope": "other",
      "brand": "Цифровой квадрат",
      "SKUs": 48,
      "positive_stock": 12
    },
    {
      "account": "oz_acc2",
      "autoarchived_scope": "children",
      "brand": "ДС принт",
      "SKUs": 4383,
      "positive_stock": 3519
    }
  ],
  "unknown_brand": 0,
  "brand_source": "live Seller attribute85 Бренд",
  "accounts_fully_covered": {
    "oz_acc2": 4492,
    "oz_acc1": 32
  },
  "unarchive_method": "POST /v1/product/unarchive",
  "request_identifier": "product_id, not SKU or offer_id",
  "acc2_current_key_explicitly_allows_method": true,
  "permission_observed_at_utc": "2026-11-08T19:10:13.575393+00:00",
  "unarchive_write_test_performed": false,
  "live_restore_success_verified": false,
  "documented_20260514_daily_autoarchive_restore_limit": 100,
  "documented_20260514_max_product_ids_per_request": 100,
  "documented_reset_moscow_time": "03:00",
  "current_account_remaining_daily_quota_verified": false,
  "current_daily_business_limit_live_verified": false,
  "product_info_limit_response_has_autoarchive_remaining": false,
  "limit_evidence_url": "https://ir.ozone.ru/s3/o4d-files/screen_001_574a2886.png",
  "limit_evidence_context_url": "https://dev.ozon.ru/community/2069-Poluchat-Limit-na-vozvrat-iz-Arkhiva/",
  "old_2024_news_limit_10_not_used_as_current": true,
  "full_current_API_documentation_readable": false,
  "Ozon_configuration_writes": 0,
  "production_DB_writes": 0
}

https://bi.beyindi.com/api/wb-bids/reports/ozon-autoarchive-brand-api-2026-11-08
