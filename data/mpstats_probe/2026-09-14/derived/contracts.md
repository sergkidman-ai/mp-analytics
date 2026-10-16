# MPSTATS skill — precise contract audit (read-only, no API calls)

All paths below relative to `/root/.claude/skills/mpstats/`. No network calls made, `config/.env` not
read/printed.

---

## 1. Pagination / filter / sort — `references/pagination-filter-sort.md`

**Where startRow/endRow/filterModel/sortModel go:** in the JSON **body** of POST requests. `d1`, `d2`,
`fbs` go in the **query string**, separately (lines 81-89 — "Query Parameters (common across
endpoints)").

Exact quote, request body shape (lines 5-14):
```json
{
  "startRow": 0,
  "endRow": 100,
  "filterModel": {},
  "sortModel": []
}
```
Table (lines 16-21):
| `startRow` | int | Start row index (0-based) |
| `endRow` | int | End row index. Max 5000 records per request |
| `filterModel` | object | Filters applied to results |
| `sortModel` | array | Sort configuration |

So the doc's only stated cap is **"Max 5000 records per request"** (line 19) — i.e. `endRow - startRow
≤ 5000`, not a fixed page size like "always 200". No fixed/default page size is documented anywhere in
this file or elsewhere in `references/`.

**Is endRow exclusive?** Not documented. The file gives no explicit statement of inclusive/exclusive
semantics for `endRow`; it only says "End row index." (line 18) and shows response echoing back the same
`startRow`/`endRow`/`total` (lines 23-43). Verdict: **not documented**.

**Filter model exact syntax** — only ONE worked example is given, a number filter (lines 47-59):
```json
{
  "filterModel": {
    "id": {
      "filterType": "number",
      "type": "equals",
      "filter": 13495594,
      "filterTo": null
    }
  }
}
```
Field names used: `filterType` (e.g. `"number"`), `type` (operator), `filter` (value), `filterTo`
(range upper bound, `null` if unused). Filter is keyed by column name (`id` in the example).

**Filter types documented** (lines 61-65) — this is the FULL list, no more, no less:
- `"type": "equals"` — exact match
- `"type": "greaterThan"` — greater than
- `"type": "lessThan"` — less than
- `"type": "inRange"` — range (use `filter` for min, `filterTo` for max)

**Text filters (e.g. `name contains`), set filters (e.g. `id in [list]`), or a `"filterType": "text"` /
`"filterType": "set"` shape:** **not documented anywhere in this file or any other reference file.**
Grepped all of `references/` and `scripts/` for `filterType`, `"text"`, `"set"`, `contains`, `in list`
— zero hits beyond the one number-filter example above. So:
- id equality → only the documented number-filter shape (`filterType:"number", type:"equals"`)
  applies by analogy; **id "in list" (a set filter) is not documented** — no example, no field names.
- **name contains → not documented** (no text-filter example/field names given).
- **revenue greaterThan → documented by direct analogy** to the number filter shown, using
  `"type":"greaterThan"` (line 63) on `filterModel.revenue`, but no explicit revenue-specific example is
  given — the exact shape has to be inferred from the one `id`/`equals` example plus the `type` list.

**Sort model** (lines 69-77):
```json
{
  "sortModel": [
    { "colId": "revenue", "sort": "desc" }
  ]
}
```
Direction: `"asc"` or `"desc"` (line 77). Common sort columns listed (line 79): `revenue`, `sales`,
`balance`, `rating`, `comments`, `final_price` — stated as "common", not exhaustive.

**Query params (line 83-87, common across endpoints):**
| `d1` | string | Start date `YYYY-MM-DD` |
| `d2` | string | End date `YYYY-MM-DD` |
| `fbs` | int | Include FBS (0 = off, 1 = on) |

---

## 2. WB POST subject/items, subject/sellers, subject/brands, similar/items family, seller/items,
   items/{id}/keywords — `references/wb-categories.md`, `references/wb-brands-sellers.md`,
   `references/wb-similar-sku.md`

### subject/items — `wb-categories.md:263-267`
```
### POST subject/items
Get products for a subject/niche.

**Query params:** `d1`, `d2`, `path` (subject_id), `fbs`
**Body:** pagination/filter/sort model
```
→ `d1`, `d2`, `path` (=subject_id, numeric), `fbs` all go in the **query string**; pagination/filter/sort
go in the **JSON body**. Same query/body split for `subject/sellers` (`wb-categories.md:285-289`,
params `d1,d2,path`, no `fbs` listed) and `subject/brands` (`wb-categories.md:278-282`, params
`d1,d2,path`, no `fbs` listed — note `fbs` is present for `subject/items` but NOT documented for
`subject/brands` / `subject/sellers`).

### similar/items (and identical/, identical_wb/, in_similar/ families) — `wb-similar-sku.md:83-87`
```
### POST similar/items
Get WB-similar products for a given product.

**Query params:** `d1`, `d2`, SKU identifier, `fbs`
**Body:** pagination/filter/sort model
```
Identical wording/params repeated for `identical/items` (lines 9-13), `identical_wb/items` (46-50),
`in_similar/items` (120-124). The doc says only **"SKU identifier"** for the param name — the actual
query key (e.g. `sku`, `id`, `path`) is **not documented**; script `scripts/wb/wb-similar.sh` builds
`QUERY="d1=${D1}&d2=${D2}&path=${ENCODED_PATH}&fbs=${FBS}"` (`wb-similar.sh:42`), i.e. the working
implementation uses **`path`** as the SKU-identifier query key — this is inferred from the script, not
stated in the reference doc itself.

### seller/items — `wb-brands-sellers.md:117-130`
```
### POST seller/items
Get products for a seller.

**Query params:**
| Param       | Type   | Required | Description |
|-------------|--------|----------|-------------|
| `d1`        | string | yes      | Start date |
| `d2`        | string | yes      | End date |
| `path` | int  | yes      | Seller (supplier) ID |
| `fbs`       | int    | no       | Include FBS |

**Body:** See `pagination-filter-sort.md`
```
So for **WB** seller/items, `path` is typed as **int** and documented as "Seller (supplier) ID" only —
**seller NAME is not documented as accepted for WB** (contrast with Ozon below, where `path` explicitly
accepts either id or name).

### items/{id}/keywords — `wb-similar-sku.md:219-230`
```
### POST items/{id}/keywords
Get search queries the SKU appears in and its position for each.

**Response item fields:**
| Field | Description |
|-------|-------------|
| `keyword` | Search query |
| `position` | Position in search results |
| `date` | Date |
```
`{id}` is a **path** parameter (the WB product ID, numeric — stated at `wb-similar-sku.md:157`: "All
endpoints use `{id}` as the WB product ID (numeric)"). No explicit `d1`/`d2`/`fbs` query params are
listed in the reference text for this endpoint (contrast with the script, `wb-sku.sh:132-136`, which
does append `d1`/`d2` query params for the `keywords` case — again, an implementation detail not stated
in the reference doc).

### Response field meanings — `sales`, `revenue`, `sales_graph`, `days_in_stock`, `final_price`,
   `revenue_estimated`, `sales_estimated`
Only documented in `wb-categories.md:44-101` (`category/items` response fields, stated to apply to
brand/seller/subject items responses too by cross-reference, e.g. `wb-brands-sellers.md:22` "Response:
Same product fields as `category/items`"):
- `sales` (line 65): "Sales count in period" — no further definition of methodology.
- `revenue` (line 67): "Revenue in period (RUB)".
- `final_price` (line 57): "Final price (RUB)".
- `days_in_stock` (line 72): "Days in stock".
- `graph` (line 94): "Sales history array" — **there is no field literally named `sales_graph`**; the
  closest documented fields are `graph` ("Sales history array"), `stocks_graph` ("Stock history array"),
  `price_graph` ("Price history array"), `category_graph` ("Category position history"),
  `product_visibility_graph` ("Visibility over time"). **`sales_graph` as a field name is not
  documented anywhere in `references/`.**
- **`revenue_estimated` and `sales_estimated`: not documented anywhere.** Grepped all of `references/`
  for `estimated` — zero matches for these exact field names (the only "estimated"-adjacent field is
  `revenue_potential`, `wb-categories.md:68`, "Potential revenue" — a different, also loosely-defined
  concept, not named "estimated" and not explained methodologically).

### fbs param values
`references/pagination-filter-sort.md:87`: `fbs — int — Include FBS (0 = off, 1 = on)`. This 0/1
convention is repeated identically (as prose "1 = yes") throughout `wb-categories.md`,
`wb-brands-sellers.md`, `ozon-categories.md`, `ozon-brands-sellers-sku.md`, `ym-categories.md` wherever
`fbs` appears, e.g. `wb-categories.md:40`: "`fbs` | int | no | Include FBS (1 = yes)". No endpoint
documents any value other than 0/1.

### Date format
`YYYY-MM-DD` throughout, e.g. `wb-categories.md:37`: "`d1` | string | yes | Start date `YYYY-MM-DD`".

---

## 3. WB GET items/{id}/full and items/{id}/by_period — `wb-similar-sku.md:164-171`

```
### GET items/{id}/full
Get detailed info and period stats for a SKU (replaces old `sales` endpoint).

---

### GET items/{id}/by_period
Get daily stats for a SKU (sales, balance data by day).
```

**Params for both:** not spelled out in the reference doc body at all — no `d1`/`d2`/`fbs` line is
given for either endpoint in `wb-similar-sku.md` (contrast with `wb-categories.md`/`ozon-*` sections
which explicitly list query params per endpoint). The working script `wb-sku.sh` appends `d1`/`d2` (not
`fbs`) as query params for both `full` (`wb-sku.sh:77-80`) and `by_period` (`wb-sku.sh:82-85`) — again
an implementation detail, not something the reference text itself states.

**"period_stats.sales" meaning:** **not documented** — there is no `period_stats` field or object
mentioned anywhere in `references/`. Grepped for `period_stats` — zero hits.

**Is `by_period`'s `sales` daily?** The endpoint description itself says "Get daily stats for a SKU
(sales, balance data by day)" (`wb-similar-sku.md:170`) — this is the only statement of granularity;
it implies `sales` in the by_period response is a per-day figure, but no explicit per-field schema
(field names/types) is given for the by_period response body anywhere in `references/`.

---

## 4. Ozon `analytics/v1/oz/items` list endpoint (ids/brands/sellers/keyword params)

**Not documented — and does not appear to exist as a route in this skill.** Full grep across
`references/` and `scripts/` for `oz/items` as a bare list path, and for query params `ids=`, `brands=`,
`sellers=`, `keyword=` found **zero matches** for a list-style `items` endpoint. The only `items`
routes under Ozon (`references/ozon-brands-sellers-sku.md:225-309`) are all **`items/{id}/...`** —
scoped to a single numeric SKU, not a searchable/filterable list. Ozon product **listing** happens only
via `category/items`, `brand/items`, `seller/items` (path = category/brand/seller, not arbitrary
ids/brands/sellers/keyword filters).

### Ozon items/{id}/by_period — `ozon-brands-sellers-sku.md:267-271`
```
### GET items/{id}/by_period
Get period statistics for a product.

**Query params:** `d1`, `d2`
```
No `fbs` param documented for this one.

### Ozon items/{id}/sales — `ozon-brands-sellers-sku.md:227-231`
```
### GET items/{id}/sales
Get sales and stock history for an Ozon SKU by warehouse.

**Query params:** `d1`, `d2`, `fbs`
```

### Ozon items/{id}/full — `ozon-brands-sellers-sku.md:234-236`
```
### GET items/{id}/full
Get detailed product information.
```
No params documented (none listed).

### Ozon items/{id}/keywords — `ozon-brands-sellers-sku.md:260-264`
```
### GET items/{id}/keywords
Get keyword positions history for a date range.

**Query params:** `d1`, `d2`
```
No `fbs` documented.

### Ozon items/{id}/versions, /versions/{hash}, /versions/compare — `ozon-brands-sellers-sku.md:296-309`
```
### GET items/{id}/versions
Get product version history.

### GET items/{id}/versions/{hash}
Get specific product version.

### GET items/{id}/versions/compare
Compare two product versions.
```
**No query/body params documented at all for any of the three** — not `d1`/`d2`, not which two hashes
`compare` takes. Confirmed by grep: no param table or prose params follow these three headers in the
file. Earlier catalog note (`methods_catalog.md`, "1. Full method table" preamble) also flags these
plus `comments/ai_recommendations`, `brand/list`, `brand/info`, `seller/list` as endpoints
`scripts/request.sh` "currently cannot route correctly."

### Ozon `fbs` semantics
Same 0/1 convention as WB — `references/pagination-filter-sort.md:87` applies globally ("Include FBS
(0 = off, 1 = on)"); Ozon-specific tables repeat "1 = yes" (e.g. `ozon-categories.md:46`).
`ozon-brands-sellers-sku.md:249`: `items/{id}/balance` takes `date`, optional `fbs` — no distinct
meaning given beyond the global 0/1 convention.

---

## 5. WB keyword/search item-search — full grep results

Grepped `references/` and `scripts/` for `keyword|search|query|\bword\b` (case-insensitive). **Every**
hit falls into one of these documented families — **there is no generic keyword-search-for-items
endpoint** (i.e. nothing like "find WB items matching keyword X"):

1. **Position-tracking for a known SKU**: `POST items/{id}/keywords` (`wb-similar-sku.md:221-230`,
   quoted in full in §2 above) and `POST items/{id}/keywords/hourly` (`wb-similar-sku.md:233-234`) —
   returns search queries a *given* SKU ranks for and its position, not items matching a query.
2. **Keyword aggregates scoped to an existing entity** (category/brand/seller/subject/similar-family),
   all requiring `d1,d2,path` of that entity, not a free-text query param:
   - `POST category/keywords` — "Get keywords for a category." (`wb-categories.md:209-212`)
   - `POST subject/keywords` — "Get keyword/search query data for a subject." (`wb-categories.md:306-309`)
   - `POST brand/keywords` — "Get keywords for a brand." (`wb-brands-sellers.md:97-100`)
   - `POST seller/keywords` — "Get keywords for a seller." (`wb-brands-sellers.md:199-202`)
   - `POST <family>/keywords` for identical/identical_wb/similar/in_similar (`wb-similar-sku.md:33-34,
     70-71, 107-108, 144-145`) — keywords scoped to the AI-similar/similar product set of a given SKU.
3. **`items/{id}/search_stats`** — "Get search position stats for a SKU." (`wb-similar-sku.md:214-215`)
   — again scoped to a known SKU, not a search-by-keyword lookup.
4. **`scripts/wb/wb-search.sh`** — despite the filename, this is **not** a keyword search. Its own
   header comment: `scripts/wb/wb-search.sh:2`: `# wb-search.sh — Get WB niche/subject list for
   research (subjects selection)`, and it calls `POST subject/list` (per `methods_catalog.md`
   "subject/niche list | POST | `subject/list` | date (optional) | `wb/wb-search.sh`"). It lists
   subjects/niches, not items, and takes no keyword param.

**Explicit conclusion:** after searching every reference file and every script for `keyword`, `search`,
`query`, and `word`, **no endpoint exists in this skill's documented/implemented surface that takes a
free-text keyword/search string and returns matching WB items** (the closest thing, `brand/list` /
`seller/list`, is a name-search over brands/sellers, not products — `wb-brands-sellers.md:26-29`: "GET
brand/list — Search brands by name. Query params: brand search query"; `wb-brands-sellers.md:134-135`:
"GET seller/list — Search sellers."). Item discovery instead goes through
category/subject/brand/seller `items` list endpoints with `filterModel`, not keyword search.

---

## 6. Yandex Market sku/item endpoint — `references/ym-categories.md:131-136`

```
## SKU-Level Analytics

### GET ym/get/item/{id}/sales
Get sales and stock history for a Yandex Market item.
```
That is the **entire** documented contract — no params (no `d1`/`d2`/`fbs` listed for this endpoint,
unlike every other YM endpoint in the file), no response field list. Base URL for all YM endpoints:
`https://mpstats.io/api/` (line 3), so full path is
`https://mpstats.io/api/ym/get/item/{id}/sales`. Wrapper: `scripts/ym/ym-sku.sh` (per
`scripts/README.md:52`, "Item sales history with optional date range" — the script adds optional
`d1`/`d2` per its own `--help`, but this is a script-level addition, not stated in the reference doc).

---

## 7. MPSTATS sales-estimation methodology, data lag, is_demo / period restrictions

**Not documented anywhere in `references/`.** Grepped all reference files for
`estimated|updated|is_demo|demo|stock.?diff|sales_graph|days_in_stock|final_price|revenue` (broad
sweep). Findings:

- **No methodology text exists anywhere** explaining *how* MPSTATS derives `sales`/`revenue` (e.g.
  stock-diff inference, order data, etc.) — no prose in any file discusses estimation method.
- **No field or concept named `is_demo`** appears in any reference file — zero grep hits.
- **No field named `updated`** (a data-freshness/last-updated timestamp) is documented on any response
  — zero grep hits for that exact field name across all of `references/`.
- **No documented data-lag figure** (e.g. "data available with N-day delay") anywhere in `references/`.
  (Note: this is distinct from the project's own memory card
  `/opt/mp-knowledge/memory/ozon-search-queries-collector.md`, referenced in
  `.claude/rules/kb-index.md`, which mentions "лаг данных 2 дня" for a project-side Ozon
  search-queries **collector** — that is this codebase's own pipeline lag, not an MPSTATS API-documented
  concept, and out of scope for this MPSTATS-skill-only audit.)
- **No period-restriction / max-date-range rule** is stated for any endpoint (e.g. no "max N days per
  request" language found anywhere in `references/`).
- Fields that ARE documented and adjacent to "estimation" concepts: `revenue_potential` ("Potential
  revenue", `wb-categories.md:68`), `lost_profit` / `lost_profit_percent` (`wb-categories.md:70-71`),
  `average_if_in_stock` ("Avg sales if in stock", `wb-categories.md:74`) — none of these come with any
  explanation of the underlying calculation/methodology in the reference text.

**Overall verdict for §7: not documented.** The skill's reference files describe field *names* and
one-line glosses, not MPSTATS's internal estimation algorithm, freshness/lag guarantees, or demo/period
gating.

---

## Cross-cutting note: script behavior vs. reference-doc text

Several points above show the *working scripts* (`wb-sku.sh`, `wb-similar.sh`, `ozon-sku.sh`, etc.)
implement param names/placements (e.g. `path` as the SKU-identifier key for `similar/items`, `d1`/`d2`
appended for `items/{id}/full` and `by_period`) that the **reference `.md` files themselves do not
state explicitly**. Per `SKILL.md:13` ("Reference files in `references/` are API contracts only") and
`references/coverage.md:23` ("References remain the source-of-truth for exact endpoint contracts"), the
project's own convention is to treat `references/*.md` as the authority — so every such gap is reported
above as "not documented in the reference," even where a script's implementation suggests a de facto
answer.
