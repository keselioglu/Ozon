# CLAUDE.md — M&S Turkey → Ozon Pipeline

This repo crawls Marks & Spencer Turkey (marksandspencer.com.tr), maps products
onto Ozon's (Russian marketplace) category/attribute dictionaries, translates
listing content to Russian, and keeps stock/price/campaign-enrollment in sync
daily. Business owner: Tugberk (tugberk.keselioglu@gmail.com). GitHub:
keselioglu/Ozon (daily reports posted to issue #13 from a separate `Tugberk-AI`
bot account — self-mentions don't trigger GitHub notifications, a second
account does).

For open task tracking and the daily run log, see `TASKS.md`.

## Mental model

Everything is a standalone script, chained by `daily_run.py` via subprocess
calls (not imports) so one step's crash never corrupts another's process
state. Each script is independently runnable and idempotent — re-running any
step after a partial failure is always safe. State lives in gitignored
`*.json`/`*.jsonl`/`*.log`/`*.txt` files at the repo root (see `.gitignore`),
never in a database.

## Daily pipeline (`daily_run.py`, 4am via Task Scheduler)

1. **Category discovery** (`category_priority.py`) — walks
   `category_priority.csv` (priority-ordered) queuing not-yet-live product
   URLs into `product_urls.txt`, until either every category is checked or
   the day's remaining `daily_create` quota is reached. Stops are **not**
   "first category with anything new" — it walks the whole list so no quota
   goes unused (business instruction, 2026-09-01).
2. **Crawl** (`crawler.py`) — fetches full detail for whatever's newly queued.
3. **Stockout re-check** (`recheck_stockouts.py`) — re-fetches pages behind
   previously zero-stock-skipped sizes so a restock gets uploaded. Has a
   3600s timeout in `daily_run.py`; frequently times out on it — not a real
   failure, just under-provisioned.
4. **Auto-translate** (`auto_translate.py`) — Claude-generated Russian listing
   content for anything not yet in `ozon_translations.py`. Timeout in
   `daily_run.py` is 3 hours (raised from 30min after the daily quota jumped
   250→2000 and translation volume grew to match).
5. **Upload** (`upload_to_ozon.py`) — pushes new/updated products to Ozon.
6. **Verify** (`check_upload_status.py`).
7. **Stock sync** — `update_stocks.py` (today's rows) then
   `refresh_live_stock.py` (every live M&S-sourced product regardless of
   whether it was touched today).
8. **Price refresh** (`refresh_prices.py`) — tracking-only, does not push to
   Ozon. Answers "do we have daily M&S price history" (business instruction,
   2026-09-02).
9. **Campaign enrollment** (`enroll_campaigns.py`) — sweeps in-stock, healthy-
   margin products into active Ozon campaigns. See Margin rule below.
10. **Campaign margin re-scan** (`rescan_campaign_margins.py`) — re-checks
    every *already-enrolled* product's live price (Elastic Boosting's
    `current_boost` drifts after enrollment) and removes/re-homes anything
    that's drifted past the margin threshold.
11. **Catalog margin report** (`catalog_margin_report.py`) — read-only, covers
    every live in-stock product, campaign or not.
12. **Auto-added campaign check** (`check_auto_added_campaign_products.py`) —
    detects (log-only) any campaign product Ozon added itself
    (`add_mode != "MANUAL"`) rather than through step 9. See note below.
13. Log to `TASKS.md`, commit & push, post summary to GitHub issue #13.

Other scheduled tasks (Windows Task Scheduler): `MandsOzonQuotaTopup` (5am),
`MandsOzonTodaysStockCheck` (6am, runs `check_todays_stock.py` — a narrower,
faster stock push scoped to just today's new offer_ids, for products that
clear moderation between the 4am run and later in the day),
`MandsOzonDailyReport` (10am), `MandsOzonQuotaFill` (10:15am, runs
`fill_remaining_quota.py` — if the 4am run didn't use the full daily quota,
finds and uploads more).

## Category/type mapping (`ozon_mapping.py`)

`resolve_category_and_type(name, is_set_hint)` is the single source of truth
for M&S product name → Ozon `(description_category_id, type_id)`. It's a
**keyword cascade, order matters**: underwear → tank top → pajama → t-shirt →
socks → sweater → trousers → blouse → skirt → jacket → dress → generic top →
`(None, None)` (unmapped, caller must skip rather than guess).

`category_priority.py`'s `is_supported_category()` MUST delegate to
`ozon_mapping.is_known_product_type()` rather than keeping its own keyword
list — an earlier drift where it had a separately-maintained, stale list
silently caused category discovery to skip categories the mapper could
actually handle. If you add a new keyword group, it must flow into
`is_known_product_type()` (usually via `KNOWN_PRODUCT_TYPE_KEYWORDS_NO_DRESS`)
or discovery won't ever queue it.

**When adding a new product type**, the established recipe (repeated ~8 times
now for Underwear/TankTop/Pajama/TShirt/Socks/Sweater/Dress/Trousers/Blouse/
Skirt/Jacket):
1. Find the Turkish keyword(s) M&S uses in the product name.
2. Look up Ozon's real `type_id` — check `category_tree_clothing.txt` (cached
   `/v1/description-category/tree` dump) or query live.
3. Verify required attributes via `/v1/description-category/attribute` with
   `description_category_id=200000933` (Clothing) — every type added so far
   needs the exact same set: `9163` (gender), `10096` (color), `31` (brand),
   `4295` (RU size), `8292` ("Merge on One PDP" — NOT material, despite the
   confusing name), `8229` (type). If a new type needs a different set,
   that's a real complication worth surfacing, not silently working around.
4. **Check for keyword collisions before wiring in** — a plain substring
   match is dangerous. Real examples caught: "üst" (top) also matches inside
   "üstü" (needs word-boundary regex, see `is_top_word()`); "elbise" (dress)
   also matches inside "takım elbise" (men's suit — needs explicit exclusion,
   see `is_dress_word()`); "pantolon" (trousers) also matches inside
   "Pantolon Çorabı" (tights/pantyhose — resolved by ordering Socks before
   Trousers in the cascade, since Socks already has a dedicated keyword).
   Also check Turkish possessive/case suffixing doesn't silently miss real
   products — "çorap"→"çorabı" (p→b softening) was found missing 16 already-
   crawled products; `SOCKS_KEYWORDS` now includes both stem forms.
5. Add a few-shot example to `auto_translate.py`'s prompt if the material is
   meaningfully different from existing examples (see Material below).

## Material dictionary (`auto_translate.py`'s `KNOWN_MATERIAL_IDS`)

Ozon attribute `4496` ("Material", NOT `8292`) is a real dictionary attribute,
but it's optional, and the model is instructed to only set it when the source
material clearly matches a whitelisted name — **never invent a numeric id**.
Confirmed live, 2026-09-07: without an explicit whitelist the model
occasionally guesses plausible-looking ids that don't exist in Ozon's
dictionary at all. If a new product type surfaces a material with no
whitelist entry, query the real dictionary
(`/v1/description-category/attribute/values`, `attribute_id=4496`,
`type_id=<the type>`, paginate with `last_value_id`) rather than guessing, and
add both the whitelist entry and a prompt example.

## Margin / campaign rule (business instruction, confirmed inclusive ≤46%)

A product is campaign-eligible when `M&S cost (converted USD/TRY) ÷ Ozon
enrollment price ≤ 0.46`. Inclusive, not strict — exact-46.0% cases are
eligible (a real bug once wrongly excluded them). Shared logic lives in
`margin_pricing.py` (`fetch_usd_try_rate()`, `load_ms_prices()`,
`get_real_stock()`, `compute_ratio_pct()`, `qualifies()`) — every
margin-aware script should import from there, not reimplement the ratio math.

**`get_real_stock()` must be used for stock, never
`/v1/actions/candidates`'s own `stock` field** — that field is confirmed
broken/unreliable (reads 0 for real RFBS inventory) and was the root cause of
a long-standing, silent eligibility bug. The only reliable stock source is
`/v4/product/info/stocks`.

**`/v3/product/info/list`'s response is flat** (`items` at the top level),
**not nested under `"result"`** — this exact bug has been independently found
and fixed at least twice (`enroll_campaigns.py`, then
`check_auto_added_campaign_products.py`). Check this first if a script using
that endpoint is silently returning null offer_id/name.

**Auto-added campaign products** (`add_mode` from Ozon itself, not our own
enrollment): confirmed live 2026-09-08 that the non-`"MANUAL"` value is
`"AUTO"`. `check_auto_added_campaign_products.py` currently only detects and
logs this (to `auto_added_campaign_products.jsonl`) — the actual removal call
(`POST /v1/actions/products/deactivate`) is intentionally NOT wired up yet
pending a business decision on whether removal should be unconditional or
gated by the same margin check as everything else. Don't add it without
re-confirming which behavior is wanted.

## Warehouse routing (`warehouse_routing.py`)

Products under 500g AND under $80 route to the small-items warehouse
(`SMALL_WAREHOUSE_ID`); everything else to the regular warehouse
(`REGULAR_WAREHOUSE_ID`). Re-evaluated every stock-sync run, not just once at
upload (a price change can move eligibility). `warehouse_assignments.json` is
self-tracked state (Ozon doesn't reliably report back which warehouse holds
stock) — **`save_warehouse_assignments()` must merge into the existing file,
never overwrite** (different daily scripts call it with different-sized
subsets of the catalog; overwriting caused an earlier data-loss bug).

## Color-based duplicate prevention (`color_dedup.py`)

Business rule: if a product's color is clearly different from what's already
listed under a similar name, create it as a new product; if the color match
is ambiguous, don't — skip rather than risk a wrong duplicate. Implemented via
`TURKISH_COLOR_FAMILY`/`ENGLISH_COLOR_FAMILY_KEYWORDS` many-to-many family
sets and `is_clearly_different_color()` (true only when family sets are fully
disjoint). Wired into `upload_to_ozon.py` alongside the existing prefix-based
duplicate check.

## Size handling

- `map_size_to_eu()` builds the offer_id's size token; `map_size_to_ozon()`
  builds the RU size *attribute* shown to buyers. These are deliberately
  separate — don't conflate them.
- **Letter-only size labels** (M&S shows no numeric size anywhere, e.g. label
  exactly `"M (UK M)"`) use the letter verbatim in the offer_id (`-M`, not a
  fabricated EU number) — business-confirmed 2026-09-03. This is the general
  behavior for every clothing type, not something to special-case per
  category.
- **Socks** are the one exception with genuinely different sizing: M&S gives
  shoe-size *ranges* ("35.5-38"), so the RU size attribute resolves to
  "universal" (dictionary id `35646`) rather than picking an arbitrary number
  from the range. `is_socks(name)` tells callers which path to use.
- Size labels with a trailing "REG" fit qualifier (e.g. `"42 (UK 14REG)"`) —
  `extract_uk_size()`/`extract_letter_size()` strip it; a plain regex without
  this silently failed to parse 158 real articles.

## Known Ozon API quirks

- Daily `daily_create` quota usage in `/v4/product/info/limit` **lags actual
  submissions** — don't treat a low usage number as proof items were lost.
- `/v1/product/update/offer-id`'s real batch limit is **25**, not 250 (a
  community-doc number that's wrong — found via a live 400).
- `/v1/actions` and `/v1/actions/candidates` are GET-shaped read endpoints;
  `/v1/actions/candidates`'s `stock` field specifically is unreliable (see
  Margin section above).
- `/v1/description-category/attribute/values` needs `last_value_id`-based
  pagination (`limit` max 200) — a big `limit` value alone 404s with a
  confusing "dictionary not found" error.

## Environment

Requires `.env` with `OZON_CLIENT_ID`, `OZON_API_KEY`, and an Anthropic API
key for `auto_translate.py` (loaded via `anthropic.Anthropic()`'s default env
var). `gh` CLI must be reachable (installed at
`C:\Program Files\GitHub CLI\gh.exe`, not necessarily on PATH) with the
`Tugberk-AI` account authenticated for daily report posting.

## Working conventions

- Never guess a size/color/category mapping — return `None`/skip and log it.
  Every mapping function in this repo follows "skip rather than guess" as a
  hard rule; a wrong guess becomes a wrong live listing.
- Scratch/diagnostic one-off scripts belong in `_temp_*.py` (gitignored) and
  should be deleted after use, not left in the repo.
- Never fabricate specific facts in translated content (fiber percentages,
  certifications, warranty terms) — state only what the source page actually
  says.
- When a script's output looks empty right after backgrounding it on
  Windows, it's very likely stdout buffering, not a real failure — check the
  process is still running before concluding a step produced nothing.
