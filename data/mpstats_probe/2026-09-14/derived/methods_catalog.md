# MPSTATS skill — full method catalog

Source: `/root/.claude/skills/mpstats/references/*.md` (contract docs) cross-checked against
`scripts/**/*.sh --help` output and script source (grep, no network calls made — see "How counted").
Read-only audit, no MPSTATS API calls were made, token was not read/printed.

## 0. Count reconciliation ("122 methods" claim)

Counting every `### GET ...` / `### POST ...` endpoint header across all `references/*.md` files:

| File | Endpoint headers |
|---|---|
| `references/account.md` | 1 |
| `references/wb-categories.md` | 34 |
| `references/wb-brands-sellers.md` | 27 |
| `references/wb-similar-sku.md` | 57 (40 in the identical/identical_wb/similar/in_similar family × 10 report shapes each, + 17 SKU-level `items/{id}/...`) |
| `references/ozon-categories.md` | 11 |
| `references/ozon-brands-sellers-sku.md` | 38 |
| `references/ym-categories.md` | 20 |
| **Total documented endpoints** | **188** |

I could **not** reconcile this to exactly 122. Things I tried:
- Collapsing the 4 near-identical "similar family" endpoint sets (`identical`, `identical_wb`, `similar`,
  `in_similar` — 10 report shapes each) into one set of 10 unique shapes: 188 − 30 = 158. Still not 122.
- Also collapsing WB `category/*` vs `subject/*` (which mirror each other, ~12 shapes each): saves ~10 more → ~148.
- Excluding the 8 forecast/season_effects (category+subject × daily/trend/annual/weekly): → ~140.
None of these principled collapses land on 122. My working conclusion: 122 is either from an older/smaller
snapshot of this skill (metadata says version 2.0.0, i.e. it has been extended), or it counts something
not present verbatim in these docs (e.g. a vendor/MPSTATS marketing count of "API methods" that doesn't
map 1:1 to what's in `references/`). **Use 188 (documented endpoint paths) as the authoritative count for
this skill as it exists on disk today.** Of these, all are reachable via a script — either a dedicated
wrapper or `scripts/request.sh` — **except** the four Ozon `items/{id}/versions*` /
`comments/ai_recommendations` endpoints and Ozon `brand/list`, `brand/info`, `seller/list`, which
`request.sh` currently cannot route correctly (see §4, routing bug).

---

## 1. Full method table

Legend: **dir** = direction/category. Base URLs — WB: `https://mpstats.io/api/analytics/v1/wb/`;
Ozon: `https://mpstats.io/api/analytics/v1/oz/`; YM: `https://mpstats.io/api/` (paths already start `ym/get/...`);
Account: `https://mpstats.io/api/`.

### 1.1 WB — Category

| dir | HTTP | path | required params | wrapped by |
|---|---|---|---|---|
| category | GET | `category/list` | — | `wb/wb-categories-list.sh` |
| category/SKU list | POST | `category/items` | d1,d2,path | `wb/wb-category.sh` |
| subcategory | POST | `category/categories` | d1,d2,path | `wb/wb-category-stats.sh subcategories` |
| brand | POST | `category/brands` | d1,d2,path | `wb/wb-category-stats.sh brands` |
| seller | POST | `category/sellers` | d1,d2,path | `wb/wb-category-stats.sh sellers` |
| trends | POST | `category/trends` | d1,d2,path | `wb/wb-category-stats.sh trends` |
| by_date | POST | `category/by_date` | d1,d2,path | `wb/wb-category-stats.sh by_date` |
| price_segmentation | POST | `category/price_segmentation` | d1,d2,path | `wb/wb-category-stats.sh price_segmentation` |
| compare | POST | `category/compare` | d1,d2,path | `wb/wb-compare.sh category` |
| subject/niche | POST | `category/subjects` | d1,d2,path | uncovered by dedicated script → `request.sh` |
| search queries | POST | `category/keywords` | d1,d2,path | uncovered → `request.sh` |
| warehouses/geo | POST | `category/warehouses` | d1,d2,path | uncovered → `request.sh` |
| AI forecast | GET | `category/forecast/daily` | path | `wb/wb-analytics.sh category forecast/daily` |
| AI forecast | GET | `category/forecast/trend` | path | `wb/wb-analytics.sh category forecast/trend` |
| seasonality | GET | `category/season_effects/annual` | path | `wb/wb-analytics.sh category season_effects/annual` |
| seasonality | GET | `category/season_effects/weekly` | path | `wb/wb-analytics.sh category season_effects/weekly` |

### 1.2 WB — Subject (niche)

| dir | HTTP | path | required params | wrapped by |
|---|---|---|---|---|
| subject/niche list | POST | `subject/list` | date (optional) | `wb/wb-search.sh` |
| subject info | GET | `subject/{id}` | id | uncovered → `request.sh` |
| SKU list | POST | `subject/items` | d1,d2,path=subject_id | `wb/wb-subject.sh <id> products` |
| category | POST | `subject/categories` | d1,d2,path | `wb/wb-subject.sh <id> categories` |
| brand | POST | `subject/brands` | d1,d2,path | `wb/wb-subject.sh <id> brands` |
| seller | POST | `subject/sellers` | d1,d2,path | `wb/wb-subject.sh <id> sellers` |
| trends | POST | `subject/trends` | d1,d2,path | `wb/wb-subject.sh <id> trends` |
| by_date | POST | `subject/by_date` | d1,d2,path | `wb/wb-subject.sh <id> by_date` |
| search queries | POST | `subject/keywords` | d1,d2,path | `wb/wb-subject.sh <id> keywords` |
| geography | POST | `subject/geography` | path only (no d1/d2) | `wb/wb-subject.sh <id> geography` |
| similar subjects | POST | `subject/similar` | d1,d2,path | `wb/wb-subject.sh <id> similar` |
| compare | POST | `subject/compare` | d1,d2,path | `wb/wb-compare.sh subject` |
| warehouses | POST | `subject/warehouses` | d1,d2,path | `wb/wb-subject.sh <id> warehouses` |
| competitors | GET | `subject/{id}/competitors` | id | uncovered → `request.sh` |
| AI forecast | GET | `subject/forecast/daily` | path | `wb/wb-analytics.sh subject forecast/daily` |
| AI forecast | GET | `subject/forecast/trend` | path | `wb/wb-analytics.sh subject forecast/trend` |
| seasonality | GET | `subject/season_effects/annual` | path | `wb/wb-analytics.sh subject season_effects/annual` |
| seasonality | GET | `subject/season_effects/weekly` | path | `wb/wb-analytics.sh subject season_effects/weekly` |

### 1.3 WB — Brand

| dir | HTTP | path | required params | wrapped by |
|---|---|---|---|---|
| SKU list | POST | `brand/items` | d1,d2,path | `wb/wb-brand.sh <name> products` |
| brand search | GET | `brand/list` | query | uncovered → `request.sh` |
| brand info | GET | `brand/info` | id | uncovered → `request.sh` |
| category | POST | `brand/categories` | d1,d2,path | `wb/wb-brand.sh <name> categories` |
| seller | POST | `brand/sellers` | d1,d2,path | `wb/wb-brand.sh <name> sellers` |
| trends | POST | `brand/trends` | d1,d2,path | `wb/wb-brand.sh <name> trends` |
| by_date | POST | `brand/by_date` | d1,d2,path | `wb/wb-brand.sh <name> by_date` |
| warehouses | POST | `brand/warehouses` | d1,d2,path | `wb/wb-warehouses.sh brand` |
| price_segmentation | POST | `brand/price_segmentation` | d1,d2,path | `wb/wb-brand.sh <name> price_segmentation` |
| compare | POST | `brand/compare` | d1,d2,path | `wb/wb-compare.sh brand` |
| subjects | POST | `brand/subjects` | d1,d2,path | `wb/wb-brand.sh <name> subjects` |
| search queries | POST | `brand/keywords` | d1,d2,path | `wb/wb-brand.sh <name> keywords` |
| reviews | POST | `brand/feedback/comments` | — | uncovered → `request.sh` |
| review stats | GET | `brand/feedback/comments/stats` | — | uncovered → `request.sh` |
| review graph | GET | `brand/feedback/comments/graph` | — | uncovered → `request.sh` |

### 1.4 WB — Seller

| dir | HTTP | path | required params | wrapped by |
|---|---|---|---|---|
| SKU list | POST | `seller/items` | d1,d2,path=supplier_id | `wb/wb-seller.sh <id> products` |
| seller search | GET | `seller/list` | query | uncovered → `request.sh` |
| seller info | GET | `seller/{id}` | id | uncovered → `request.sh` |
| category | POST | `seller/categories` | d1,d2,path | `wb/wb-seller.sh <id> categories` |
| brand | POST | `seller/brands` | d1,d2,path | `wb/wb-seller.sh <id> brands` |
| trends | POST | `seller/trends` | d1,d2,path | `wb/wb-seller.sh <id> trends` |
| by_date | POST | `seller/by_date` | d1,d2,path | `wb/wb-seller.sh <id> by_date` |
| warehouses | POST | `seller/warehouses` | d1,d2,path | `wb/wb-warehouses.sh seller` |
| price_segmentation | POST | `seller/price_segmentation` | d1,d2,path | `wb/wb-seller.sh <id> price_segmentation` |
| compare | POST | `seller/compare` | d1,d2,path | `wb/wb-compare.sh seller` |
| subjects | POST | `seller/subjects` | d1,d2,path | `wb/wb-seller.sh <id> subjects` |
| search queries | POST | `seller/keywords` | d1,d2,path | `wb/wb-seller.sh <id> keywords` |

### 1.5 WB — Similar/identical families (×4: `identical`, `identical_wb`, `similar`, `in_similar`; 10 report shapes each = 40 endpoints)

| dir | HTTP | path pattern | required params | wrapped by |
|---|---|---|---|---|
| SKU list | POST | `<family>/items` | d1,d2,SKU/url | `wb/wb-similar.sh <family> <sku> products` |
| category | POST | `<family>/categories` | d1,d2,SKU | `wb/wb-similar.sh <family> <sku> categories` |
| brand | POST | `<family>/brands` | d1,d2,SKU | `wb/wb-similar.sh <family> <sku> brands` |
| seller | POST | `<family>/sellers` | d1,d2,SKU | `wb/wb-similar.sh <family> <sku> sellers` |
| price_segmentation | POST | `<family>/price_segmentation` | d1,d2,SKU | `wb/wb-similar.sh <family> <sku> price_segmentation` |
| trends | POST | `<family>/trends` | d1,d2,SKU | `wb/wb-similar.sh <family> <sku> trends` |
| by_date | POST | `<family>/by_date` | d1,d2,SKU | `wb/wb-similar.sh <family> <sku> by_date` |
| search queries | POST | `<family>/keywords` | d1,d2,SKU | `wb/wb-similar.sh <family> <sku> keywords` |
| warehouses | POST | `<family>/warehouses` | d1,d2,SKU | `wb/wb-similar.sh <family> <sku> warehouses` |
| compare | POST | `<family>/compare` | d1,d2,SKU | `wb/wb-similar.sh <family> <sku> compare` |

`identical`/`identical_wb` = MPSTATS-AI-similar / WB-AI-similar; `similar` = WB "similar products";
`in_similar` = reverse lookup (who lists this SKU as similar to them).

### 1.6 WB — SKU-level (`items/{id}/...`, 17 endpoints)

| dir | HTTP | path | required params | wrapped by |
|---|---|---|---|---|
| SKU full details | GET | `items/{id}` | id | `wb/wb-sku.sh <id> info` |
| SKU full details | GET | `items/{id}/full` | id | `wb/wb-sku.sh <id> full` (alias `sales`) |
| sales+stock by day | GET | `items/{id}/by_period` | id | `wb/wb-sku.sh <id> by_period` (alias `balance_by_day`) |
| stock by region | GET | `items/{id}/balance/stores` | id | `wb/wb-sku.sh <id> balance_stores` |
| stock by size | GET | `items/{id}/balance/sizes` | id | `wb/wb-sku.sh <id> balance_sizes` |
| stock by store+size | GET | `items/{id}/balance/stores_and_sizes` | id | `wb/wb-sku.sh <id> balance_stores_and_sizes` |
| stock by color | GET | `items/{id}/balance/colors` | id | `wb/wb-sku.sh <id> balance_colors` |
| sales by region | GET | `items/{id}/sales/stores` | id | `wb/wb-sku.sh <id> sales_stores` |
| sales by size | GET | `items/{id}/sales/sizes` | id | `wb/wb-sku.sh <id> sales_sizes` |
| sales heatmap | GET | `items/{id}/sales/heatmap` | id | `wb/wb-sku.sh <id> sales_heatmap` |
| stores summary | GET | `items/{id}/stores` | id | `wb/wb-sku.sh <id> stores` |
| search position stats | GET | `items/{id}/search_stats` | id | `wb/wb-sku.sh <id> search_stats` |
| keyword positions | POST | `items/{id}/keywords` | id | `wb/wb-sku.sh <id> keywords` |
| keyword positions hourly | POST | `items/{id}/keywords/hourly` | id | `wb/wb-sku.sh <id> keywords_hourly` |
| reviews | GET | `items/{id}/comments` | id | `wb/wb-sku.sh <id> comments` |
| FAQ | GET | `items/{id}/faq` | id | `wb/wb-sku.sh <id> faq` |
| photo change history | GET | `items/{id}/photos_history` | id | `wb/wb-sku.sh <id> photos_history` |

Note: `items/{id}/recommended/` is mentioned in prose in `wb-similar-sku.md` ("available under...") but has
**no explicit `###` endpoint header / documented path or params** — treat as undocumented/uncovered, not a
confirmed callable method.

Card content (description/characteristics/dimensions) is **not** an MPSTATS endpoint at all —
`wb/wb-card-content.sh` fetches it straight from WB's own CDN (`basket-NN.wbbasket.ru/.../card.json`),
bypassing MPSTATS entirely (see §4).

### 1.7 Account

| dir | HTTP | path | required params | wrapped by |
|---|---|---|---|---|
| quota balance | GET | `user/report_api_limit` | — | `account/account-limits.sh` |

### 1.8 Ozon — Category

| dir | HTTP | path | required params | wrapped by |
|---|---|---|---|---|
| category list | POST | `category/list` | date (optional) | `ozon/ozon-categories-list.sh` |
| SKU list | POST | `category/items` | d1,d2,path | `ozon/ozon-category.sh <path> products` |
| subcategory | POST | `category/categories` | d1,d2,path | `ozon/ozon-category.sh <path> subcategories` |
| seller | POST | `category/sellers` | d1,d2,path | `ozon/ozon-category.sh <path> sellers` |
| brand | POST | `category/brands` | d1,d2,path | `ozon/ozon-category.sh <path> brands` |
| by_date | POST | `category/by_date` | d1,d2,path | `ozon/ozon-category.sh <path> by_date` |
| price_segmentation | POST | `category/price_segmentation` | d1,d2,path | `ozon/ozon-category.sh <path> price_segmentation` |
| trends | POST | `category/trends` | d1,d2,path | `ozon/ozon-category.sh <path> trends` |
| search queries | POST | `category/keywords` | d1,d2,path | `ozon/ozon-category.sh <path> keywords` |
| niches (subject-equivalent) | POST | `category/niches` | d1,d2,path | `ozon/ozon-category.sh <path> niches` |
| compare | POST | `category/compare` | d1,d2,path | `ozon/ozon-compare.sh category` |

### 1.9 Ozon — Brand

| dir | HTTP | path | required params | wrapped by |
|---|---|---|---|---|
| SKU list | POST | `brand/items` | d1,d2,path | `ozon/ozon-brand.sh <name> products` |
| brand list | GET | `brand/list` | pagination | **uncovered — `request.sh` cannot route it (see §4)** |
| brand info | GET | `brand/info` | id | **uncovered — same routing bug** |
| category | POST | `brand/categories` | d1,d2,path | `ozon/ozon-brand.sh <name> categories` |
| seller | POST | `brand/sellers` | d1,d2,path | `ozon/ozon-brand.sh <name> sellers` |
| by_date | POST | `brand/by_date` | d1,d2,path | `ozon/ozon-brand.sh <name> by_date` |
| price_segmentation | POST | `brand/price_segmentation` | d1,d2,path | `ozon/ozon-brand.sh <name> price_segmentation` |
| trends | POST | `brand/trends` | d1,d2,path | `ozon/ozon-brand.sh <name> trends` |
| search queries | POST | `brand/keywords` | d1,d2,path | `ozon/ozon-brand.sh <name> keywords` |
| niches | POST | `brand/niches` | d1,d2,path | `ozon/ozon-brand.sh <name> niches` |
| geography | POST | `brand/geography` | d1,d2,path | `ozon/ozon-brand.sh <name> geography` |
| compare | POST | `brand/compare` | d1,d2,path | `ozon/ozon-compare.sh brand` |

### 1.10 Ozon — Seller

| dir | HTTP | path | required params | wrapped by |
|---|---|---|---|---|
| SKU list | POST | `seller/items` | d1,d2,path | `ozon/ozon-seller.sh <id/name> products` |
| seller list | POST | `seller/list` | pagination | **uncovered — routing bug** |
| seller info | GET | `seller/{id}` | id | **uncovered — routing bug** |
| category | POST | `seller/categories` | d1,d2,path | `ozon/ozon-seller.sh <id/name> categories` |
| brand | POST | `seller/brands` | d1,d2,path | `ozon/ozon-seller.sh <id/name> brands` |
| by_date | POST | `seller/by_date` | d1,d2,path | `ozon/ozon-seller.sh <id/name> by_date` |
| price_segmentation | POST | `seller/price_segmentation` | d1,d2,path | `ozon/ozon-seller.sh <id/name> price_segmentation` |
| trends | POST | `seller/trends` | d1,d2,path | `ozon/ozon-seller.sh <id/name> trends` |
| search queries | POST | `seller/keywords` | d1,d2,path | `ozon/ozon-seller.sh <id/name> keywords` |
| niches | POST | `seller/niches` | d1,d2,path | `ozon/ozon-seller.sh <id/name> niches` |
| geography | POST | `seller/geography` | d1,d2,path | `ozon/ozon-seller.sh <id/name> geography` |
| compare | POST | `seller/compare` | d1,d2,path | `ozon/ozon-compare.sh seller` |

### 1.11 Ozon — SKU-level

| dir | HTTP | path | required params | wrapped by |
|---|---|---|---|---|
| sales+stock by warehouse | GET | `items/{id}/sales` | d1,d2 | `ozon/ozon-sku.sh <id> sales` |
| SKU full details | GET | `items/{id}/full` | id | `ozon/ozon-sku.sh <id> full` |
| intraday snapshot | GET | `items/{id}/by_day` | date | `ozon/ozon-sku.sh <id> by_day` |
| stock by warehouse/region | GET | `items/{id}/balance` | date | `ozon/ozon-sku.sh <id> balance` |
| category positions history | GET | `items/{id}/categories` | d1,d2 | `ozon/ozon-sku.sh <id> categories` |
| keyword positions history | GET | `items/{id}/keywords` | d1,d2 | `ozon/ozon-sku.sh <id> keywords` |
| period stats | GET | `items/{id}/by_period` | d1,d2 | `ozon/ozon-sku.sh <id> by_period` |
| search position/visibility | GET | `items/{id}/search_stats` | d1,d2 | `ozon/ozon-sku.sh <id> search_stats` |
| store-level data | GET | `items/{id}/stores` | id | `ozon/ozon-sku.sh <id> stores` |
| reviews | GET | `items/{id}/comments` | id | `ozon/ozon-sku.sh <id> comments` |
| AI review recommendations | GET | `items/{id}/comments/ai_recommendations` | id | **uncovered — routing bug** |
| card version history | GET | `items/{id}/versions` | id | **uncovered — routing bug** |
| specific card version | GET | `items/{id}/versions/{hash}` | id, hash | **uncovered — routing bug** |
| compare 2 card versions | GET | `items/{id}/versions/compare` | id | **uncovered — routing bug** |

### 1.12 Yandex Market — Category / Brand / Seller / SKU (20 endpoints, all under `https://mpstats.io/api/ym/get/...`)

| dir | HTTP | path | required params | wrapped by |
|---|---|---|---|---|
| SKU list (category) | POST | `ym/get/category` | d1,d2,path | `ym/ym-category.sh <path> products` |
| subcategory | GET | `ym/get/category/categories` | d1,d2,path | `ym/ym-category.sh <path> categories` |
| seller | GET | `ym/get/category/sellers` | d1,d2,path | `ym/ym-category.sh <path> sellers` |
| brand | GET | `ym/get/category/brands` | d1,d2,path | `ym/ym-category.sh <path> brands` |
| by_date | GET | `ym/get/category/by_date` | d1,d2,path | `ym/ym-category.sh <path> by_date` |
| price_segmentation | GET | `ym/get/category/price_segmentation` | d1,d2,path | `ym/ym-category.sh <path> price_segmentation` |
| compare | POST | `ym/get/category/compare` | d1,d2,path | `ym/ym-compare.sh category` |
| SKU list (brand) | POST | `ym/get/brand` | d1,d2,path | `ym/ym-brand.sh <name> products` |
| category (brand) | GET | `ym/get/brand/categories` | d1,d2,path | `ym/ym-brand.sh <name> categories` |
| seller (brand) | GET | `ym/get/brand/sellers` | d1,d2,path | `ym/ym-brand.sh <name> sellers` |
| by_date (brand) | GET | `ym/get/brand/by_date` | d1,d2,path | `ym/ym-brand.sh <name> by_date` |
| price_segmentation (brand) | GET | `ym/get/brand/price_segmentation` | d1,d2,path | `ym/ym-brand.sh <name> price_segmentation` |
| compare (brand) | POST | `ym/get/brand/compare` | d1,d2,path | `ym/ym-compare.sh brand` |
| SKU list (seller) | POST | `ym/get/seller` | d1,d2,path | `ym/ym-seller.sh <name> products` |
| category (seller) | GET | `ym/get/seller/categories` | d1,d2,path | `ym/ym-seller.sh <name> categories` |
| brand (seller) | GET | `ym/get/seller/brands` | d1,d2,path | `ym/ym-seller.sh <name> brands` |
| by_date (seller) | GET | `ym/get/seller/by_date` | d1,d2,path | `ym/ym-seller.sh <name> by_date` |
| price_segmentation (seller) | GET | `ym/get/seller/price_segmentation` | d1,d2,path | `ym/ym-seller.sh <name> price_segmentation` |
| compare (seller) | POST | `ym/get/seller/compare` | d1,d2,path | `ym/ym-compare.sh seller` |
| SKU sales history | GET | `ym/get/item/{id}/sales` | id | `ym/ym-sku.sh <id>` |

Note: YM has **no rubricator/category-list endpoint** at all (documented explicitly: "start from known
category paths") and **no keywords/search-query, no similar/identical, no reviews/comments, no card-version
endpoints** — those directions simply don't exist for YM in this API.

---

## 2. Direction matrix

| Direction | WB | Ozon | YM |
|---|---|---|---|
| Category rubricator (tree) | yes (1: `category/list`) | yes (1: `category/list`) | **no** — no tree endpoint |
| Category products/stats | yes (12: items+subcats+brands+sellers+trends+by_date+price_seg+compare+subjects+keywords+warehouses, ×1 rubricator) | yes (11: items+subcats+sellers+brands+by_date+price_seg+trends+keywords+niches+compare) | yes (7: products+subcats+sellers+brands+by_date+price_seg+compare) |
| Subject/niche (dedicated taxonomy) | yes (18, via `subject/*`) | partial — folded into `category/niches`, `brand/niches`, `seller/niches` (3), no standalone subject entity | **no** |
| Brand analytics | yes (15) | yes (12) | yes (6) |
| Seller analytics | yes (12) | yes (12) | yes (6) |
| SKU sales & stock (by day/region/size) | yes (11 of the 17 SKU endpoints) | yes (4: sales, by_day, balance, by_period) | yes (1: `item/{id}/sales`, only report) |
| SKU keywords/positions | yes (2: keywords, keywords/hourly, + hourly variant) | yes (1: `items/{id}/keywords`) | **no** |
| Search query frequency (standalone word-stat) | **no dedicated endpoint** — closest is `category/keywords` / `subject/keywords` / `brand/keywords` / `seller/keywords` / SKU `keywords` (positions, not raw frequency) | same pattern — `category/keywords`, `brand/keywords`, `seller/keywords`, SKU `keywords` | **no** |
| Similar/identical products | yes (40: 4 families × 10 shapes) | **no** | **no** |
| Price segmentation | yes (5: category/subject/brand/seller/similar-family) | yes (4: category/brand/seller — SKU-level not separate) | yes (3: category/brand/seller) |
| Category/subject trends & by_date | yes (4: category by_date+trends, subject by_date+trends) | yes (2: category by_date+trends) | yes (1: category by_date only, no trends) |
| AI forecast / seasonality | yes (8: category+subject × forecast daily/trend + season annual/weekly) | **no** | **no** |
| Sellers & brands within category/subject | yes (4: category/brands, category/sellers, subject/brands, subject/sellers) | yes (2: category/brands, category/sellers) | yes (2: category/brands, category/sellers) |
| Reviews/comments | yes (4: SKU comments, brand feedback/comments + stats + graph) | yes (2: SKU comments, comments/ai_recommendations) | **no** |
| Card version history | **no** (only `items/{id}/photos_history` — photos only, not full version diff) | yes (4: versions, versions/{hash}, versions/compare — but currently unreachable via `request.sh`, see §4) | **no** |
| Geography/warehouses | yes (4: subject/geography, subject/warehouses, brand/warehouses, seller/warehouses, + category/warehouses = 5) | yes (2: brand/geography, seller/geography) | **no** |
| Card content (description/specs) | yes, but **not MPSTATS** — WB CDN via `wb-card-content.sh` | **no** | **no** |
| Account/quota | yes (1, shared endpoint) | shared | shared |

---

## 3. Test candidates — cheapest single call per data class

All calls below use `endRow` 20 (or the smallest natural unit) and the account's own token via the scripts
(none were actually executed — see task constraint). Dates are illustrative; replace with a real recent
30-day window before running.

**a) Search product by keyword/SKU**
- No free-text product search endpoint exists on WB/Ozon/YM. Closest: WB niche discovery —
  `./scripts/wb/wb-search.sh` → `POST subject/list` (lists all WB subjects/niches with ids; grep the name
  to find "Картриджи"'s subject id). No date range needed beyond optional `date`.
- WB brand/seller name search: `./scripts/request.sh GET brand/list 'query=<name>'` (uncovered by wrapper).

**b) SKU full details**
- WB: `./scripts/wb/wb-sku.sh <sku> full` → `GET items/{id}/full`. Needs numeric WB SKU (nmId).
- Ozon: `./scripts/ozon/ozon-sku.sh <sku> full` → `GET items/{id}/full`. Needs numeric Ozon product id.
- YM: no `full`-details endpoint; only `ym/get/item/{id}/sales`.

**c) SKU sales & stock by day**
- WB: `./scripts/wb/wb-sku.sh <sku> by_period 2026-08-25 2026-09-14` → `GET items/{id}/by_period`, needs d1/d2 (small range keeps rows low; no explicit max-period documented).
- Ozon: `./scripts/ozon/ozon-sku.sh <sku> by_day '' '' '' 2026-09-13` → `GET items/{id}/by_day`, needs a single point `date` (not a range).
- YM: `./scripts/ym/ym-sku.sh <id> 2026-09-01 2026-09-14` → `GET ym/get/item/{id}/sales`, d1/d2 optional.

**d) SKU positions by search queries (keywords)**
- WB: `./scripts/wb/wb-sku.sh <sku> keywords 2026-08-25 2026-09-14` → `POST items/{id}/keywords`.
- Ozon: `./scripts/ozon/ozon-sku.sh <sku> keywords 2026-08-25 2026-09-14` → `GET items/{id}/keywords`.
- YM: not available.

**e) Search query frequency (word-stat)**
- No standalone frequency/word-stat endpoint documented for any marketplace. Nearest proxies are the
  `*/keywords` family (returns positions/traffic tied to a specific category/subject/brand/seller/SKU, not
  a raw phrase→frequency lookup). Cheapest probe: WB `./scripts/wb/wb-subject.sh <subject_id> keywords <d1> <d2>` → `POST subject/keywords`.

**f) Similar & identical products**
- WB only: `./scripts/wb/wb-similar.sh similar <sku> products` (or `identical`, `identical_wb`, `in_similar`) → `POST similar/items`, needs SKU or product URL as `path_value`; limit param caps rows (set small, e.g. 20).
- Ozon/YM: not available.

**g) Category products — finding the WB subject id for "Картриджи"**
- There is **no dedicated "get subject id by name" endpoint**. The documented path is: call
  `./scripts/wb/wb-search.sh` (`POST subject/list`) once, which returns the full subjects list with ids and
  names — then grep it locally for "Картридж*". `subject/list` is itself the WB "subject list endpoint" the
  task asks about — it already exists and is wrapped; no separate lookup call is needed or available.
  Once you have the id, `./scripts/wb/wb-subject.sh <id> products` → `POST subject/items` (d1,d2,path=subject_id, body pagination endRow=20).
- Ozon has no subject entity — nearest is `category/niches` inside a chosen category path.

**h) Category trends / by_date**
- WB: `./scripts/wb/wb-category-stats.sh "Электроника/Оргтехника/Картриджи" by_date <d1> <d2>` → `POST category/by_date`.
- Ozon: `./scripts/ozon/ozon-category.sh "<path>" by_date <d1> <d2>` → `POST category/by_date`.
- YM: `./scripts/ym/ym-category.sh "<path>" by_date <d1> <d2>` → `GET ym/get/category/by_date` (no `trends` report exists for YM).

**i) Price segmentation**
- WB: `./scripts/wb/wb-category-stats.sh "<path>" price_segmentation <d1> <d2>` → `POST category/price_segmentation`.
- Ozon: `./scripts/ozon/ozon-category.sh "<path>" price_segmentation <d1> <d2>` → `POST category/price_segmentation`.
- YM: `./scripts/ym/ym-category.sh "<path>" price_segmentation <d1> <d2>` → `GET ym/get/category/price_segmentation`.

**j) Sellers and brands in category/subject**
- WB: `./scripts/wb/wb-category-stats.sh "<path>" sellers <d1> <d2>` / `... brands ...` → `POST category/sellers` / `category/brands` (or `wb-subject.sh <id> sellers|brands` for the subject variant).
- Ozon: `./scripts/ozon/ozon-category.sh "<path>" sellers <d1> <d2>` / `... brands` → `POST category/sellers` / `category/brands`.
- YM: `./scripts/ym/ym-category.sh "<path>" sellers` / `... brands` → `GET ym/get/category/sellers` / `.../brands`.

**k) Reviews/comments**
- WB: `./scripts/wb/wb-sku.sh <sku> comments` → `GET items/{id}/comments`.
- Ozon: `./scripts/ozon/ozon-sku.sh <sku> comments` → `GET items/{id}/comments`.
- YM: not available.

**l) Card versions / history of card changes**
- Ozon only: `GET items/{id}/versions` (no dedicated wrapper — must go through `request.sh`, but see §4:
  `request.sh`'s routing for `analytics/v1/oz/*` paths is currently broken, so this endpoint is
  effectively unreachable with the shipped tooling as-is; would need a direct `curl` with the
  `analytics/v1/oz/` base and the token, or a fix to `request.sh`).
- WB/YM: no version-history endpoint; WB's closest is `items/{id}/photos_history` (photos only).

---

## 4. Notes from `references/auth.md` and `references/account.md`

**Limit consumption / accounting:**
- `references/account.md` documents exactly one quota endpoint, `GET user/report_api_limit`, returning
  `{"available": N, "use": N}` (remaining = available − use). **This is a single global counter** — the docs
  do not describe per-request vs per-row costing, and there is **no mention anywhere in this skill's
  references of a separate "external" limit bucket (e.g. an `available_wb_external`-style field)** — that
  concept is not present in `account.md` or `auth.md` as shipped. If such a field exists in the live API
  response, it is undocumented in this skill.
- No per-endpoint cost table exists in the docs (i.e. nothing says "category/items costs 5, items/{id} costs 1").

**Rate limits / response codes** (`references/auth.md`):
- 200 success; 202 "accepted but not complete — retry later"; 401 invalid/missing token; 429 rate limit
  exceeded (check `message` field + `Retry-After` header, seconds); 500 server error (see `message`).
- **402 and 403 are not documented at all** in this skill — no "tariff not available" semantics are
  described anywhere in `auth.md`/`account.md`. If the live API returns 402/403 for tariff-gated endpoints,
  that behavior is undocumented here and would need to be discovered empirically (out of scope — no calls
  made).
- Auth: header `X-Mpstats-TOKEN` (recommended) or query param `auth-token`; both `Content-Type: application/json` and the token header are required on every request.

**`wb-card-content.sh` / WB CDN:**
- Confirmed by reading the script source: it does **not** call MPSTATS at all. It computes a `basket-NN.wbbasket.ru`
  URL from the SKU number (`vol = sku/100000`, `part = sku/1000`) and fetches
  `.../vol{vol}/part{part}/{sku}/info/ru/card.json` directly from Wildberries' own product-image CDN, with a
  fallback that probes baskets 01–27 if the computed one 404s. **No token, no MPSTATS quota consumed** — it
  is free, but it is an undocumented/unofficial WB CDN path, not a public API, and may break silently.

**Routing bug found while cataloguing (`scripts/request.sh`):**
- `request.sh --help` and its source route any path prefixed `ozon/...` to the **legacy** base
  `https://mpstats.io/api/ozon/...`, and anything else that isn't `wb/get/...` or `analytics/v1/wb/...`
  falls through to the **default WB** base `https://mpstats.io/api/analytics/v1/wb/...`. But the actual
  (current, working) Ozon base used by the dedicated `ozon-*.sh` scripts and documented in
  `references/ozon-*.md` is `https://mpstats.io/api/analytics/v1/oz/...` — a base `request.sh` has **no
  routing rule for at all**. Practical effect: any Ozon endpoint not wrapped by a dedicated script
  (`brand/list`, `brand/info`, `seller/list`, `seller/{id}`, `items/{id}/versions*`,
  `items/{id}/comments/ai_recommendations`) **cannot be correctly reached through `request.sh`** as shipped —
  it either hits a stale legacy Ozon base or gets double-prefixed with `analytics/v1/wb/`. This contradicts
  `references/coverage.md`'s claim that "all documented methods are now invocable via scripts." Flagging for
  a future fix (add an `analytics/v1/oz/*` / bare-Ozon-path branch to `request.sh`), not fixed here since
  this was a read-only audit.
