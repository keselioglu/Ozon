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
   the day's **plan** is covered: `daily_plan.remaining_today()` (plan total,
   default 500 new offers, minus what's already in `new_items_submitted.json`
   today, capped by the remaining `daily_create` quota). The target is
   counted in estimated **offers** (in-stock size variants per product), not
   URLs, and aims `DISCOVERY_OVERSHOOT_FACTOR` (1.2×) over, because the
   upload's dedup checks drop some. Per-category allocations from
   `daily_plan.json` are respected: a full (or unallocated) category only
   gets more when `fill_remainder` is on and the total still has room.
   Per-category "found, not live" counts are merged into
   `category_inventory.json` under `discovery_found`. The upload is the hard
   cap; overflow goes to `deferred_items.json`. Business instruction,
   2026-11-24: aim for the daily plan, not the whole quota — this replaces
   the 2026-09-01 "walk until the whole quota is filled" rule.
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
12. **Content rating** (`content_rating.py`, 1800s timeout) — read-only
    snapshot of Ozon's per-SKU content rating. See Content rating below.
13. **Content improvement** (`improve_content.py`, 3600s timeout) — builds
    and pushes the missing content for the lowest-rated (article, color)
    groups. Neither content step fails the run; a failure/timeout shows as
    `content status unknown` in the summary line, otherwise e.g.
    `content: avg 63.2, 120 groups improved`.
14. **Auto-added campaign check** (`check_auto_added_campaign_products.py`) —
    detects (log-only) any campaign product Ozon added itself
    (`add_mode != "MANUAL"`) rather than through step 9. See note below.
15. **Account snapshot** (`account_snapshot.py`, 600s timeout) — read-only,
    offline: rebuilds `account_snapshot.json` (`"version": 2`, the Ozon
    Sales app's Overview data). Keys: `brands` / `total` (per brand `offers`,
    `for_sale`, `live_ids_count`, `stock_up_to_date`, `price_up_to_date`),
    `up_to_date` (`hours: 24`, `runs`), `notes`, `warehouses` (per label
    `skus`, `by_brand`, `price_count`, `avg_price` / `min_price` /
    `max_price` in USD), `offers_by_warehouse`, and `campaigns` / `sales` /
    `metrics_built_at` copied as they are from `overview_metrics.json`.
    Inputs: `catalog_status.json`, `catalog_live_offer_ids.json`,
    `catalog_live_prices.json` (each live SKU's Ozon `price.price`),
    `warehouse_assignments.json`, `overview_metrics.json`,
    `stock_update_history.jsonl`, `price_update_history.jsonl` and
    `hm_stock_last_run.json`. **24h up-to-date rule** (`UP_TO_DATE_HOURS`):
    M&S stock = the newest `stock_update_history.jsonl` run that finished in
    the last 24h → min(`products_pushed_ok`, M&S live); M&S price = the same
    with `price_update_history.jsonl` / `pushed_ok`; H&M stock = live H&M
    offer_ids with a numeric `stock` in `hm_stock_last_run.json` if its
    `date` is within 24h. H&M/Other price and Other stock are 0 (no such
    run exists). No run in the window → 0; a missing/corrupt source → null.
    These are approximate (run summaries, not per-SKU stamps). It never
    fails the run: a failure or timeout is an attention line, and the Daily
    tasks tab shows it as a warning. `catalog_status.json` and the two
    `catalog_live_*.json` files are written by `catalog_status.py`, not by
    daily_run, so at 4am the snapshot is only as fresh as those files; the
    10am `MandsOzonDailyReport` launcher refreshes them and rebuilds the
    snapshot again (see below).
16. Log to `TASKS.md`, commit & push, post summary to GitHub issue #13.

Other scheduled tasks (Windows Task Scheduler): `MandsOzonQuotaTopup` (5am),
`MandsOzonTodaysStockCheck` (6am, runs `check_todays_stock.py` — a narrower,
faster stock push scoped to just today's new offer_ids, for products that
clear moderation between the 4am run and later in the day),
`MandsOzonDailyReport` (10am), `MandsOzonQuotaFill` (10:15am, runs
`fill_remaining_quota.py` — if the day's plan (default 500 new offers) isn't
met yet and quota remains, finds and uploads more; if the plan is already
met it logs "Daily plan (N) already met — nothing to fill" and stops before
crawling). The 5am top-up and 10:15 fill both run `upload_to_ozon.py`, so
they share the same daily plan cap as the 4am run. The daily TASKS.md /
GitHub summary line includes `plan N/500 created`.
`MandsOzonDailyReport` (`daily_report.py`) also sends a short Telegram
summary (4am run state from `daily_run_status.json`, failed steps, new offers
vs. plan, content, stock/campaigns/sales); it needs `TELEGRAM_BOT_TOKEN` /
`TELEGRAM_CHAT_ID` in `.env`, otherwise it logs "Telegram not sent: not
configured". Since task #90 it also writes `overview_metrics.json`
(gitignored, atomic, `"version": 1`: `built_at`, `campaigns` = live SKUs in
campaigns + avg/min/max M&S cost ÷ action price %, `sales` = the last 34h,
local midnight yesterday → run time: revenue, orders, units, SKUs, avg M&S
cost %, small/regular warehouse %, brand %, top category). A failure there
never stops the report; the GitHub/Telegram content is unchanged (24h).
After `daily_report.py`, `run_daily_report.bat` runs `catalog_status.py`
(read-only: live ids, stock, prices → `catalog_status.json`,
`catalog_live_offer_ids.json`, `catalog_live_prices.json`) and then
`account_snapshot.py`, all logged to the same `daily_report_YYYY-MM-DD.log`.
The bat's exit code stays `daily_report.py`'s; the two refresh steps'
failures are only logged. This uses the existing task, no new scheduled
task.

## Ozon Sales app & daily plan (`planner_ui.pyw`, `daily_plan.py`)

**Ozon Sales app** (`planner_ui.pyw`, Desktop shortcut "Ozon Sales" via
`install_desktop_shortcut.py`, see "Launching the app" below): a tabbed
tkinter window with three tabs:

- **Overview**: account snapshot from `account_snapshot.json` (built daily by
  `account_snapshot.py`, pipeline step 15, and rebuilt again by the 10am
  `MandsOzonDailyReport` launcher after `catalog_status.py`): KPI cards
  (Total SKUs, For sale, Stock / Price up to date (24h), each split
  M&S / H&M / Other with its % of total SKUs), Brands (SKUs / for sale), Warehouses (live SKUs, avg/min/max
  live price in USD), a Campaigns card and a "Sales last 34 hours" card
  (from the 10:00 report's `overview_metrics.json`, copied in as
  `campaigns` / `sales` / `metrics_built_at`; warning colour past 26h, and
  "No sales/campaign data yet" until it has run), plus "data as of"
  (warning past 26h) and any source that isn't ok. Products, articles,
  unparsed, sizes and catalog freshness are no longer shown (snapshot v2).
  Its Refresh runs `account_snapshot.py`, which is offline and only writes
  that local file; fresh Ozon numbers arrive with the 10am job.
- **Daily tasks**: `task_status.py` → `schtasks /query /fo CSV /v` +
  `daily_run_status.json` + the run-history files. One scrollable card per
  scheduled task (display names: "MS stock update", "H&M stock update",
  "Price update"), each with next/last run, result and six counts from
  its latest run. View-only (see Task status below).
- **New Product Adding** (the app opens on this tab; it was called
  "Prioritization", the class is still `PrioritizationTab`, alias
  `NewProductAddingTab`): the quota / today's plan / deferred / brand
  cards (live `/v4/product/info/limit` read, `catalog_status.json`), then
  a scrollable table from `new_product_plan.build_rows()` over
  `category_inventory.json` + `new_product_plan.json`: Brand | Sub category
  (Ozon type label) | Priority (Very high … Very low, default Medium;
  changing it re-sorts the rows) | Total URLs | Eligible URLs (not live:
  crawled + found by the walk) | URLs to add (int entry, warning colour
  above eligible, invalid input is reverted) | Add all (eligible) / Add half
  (eligible // 2). H&M is one read-only row ("—", no add pipeline yet /
  website check pending). **Save** writes gitignored `new_product_plan.json`
  (`new_product_plan.save_plan()`, atomic, `{"version": 1, "categories":
  {"M&S/Pajama": {"priority", "urls_to_add"}}, "updated_at"}`, no per-day
  overrides; disabled while `validate_plan()` reports errors). These
  targets are **saved only**: discovery and the upload still follow
  `daily_plan.json` (offers) until a backlog task wires them in. **Add a new
  website** opens a form (URL + description → Create epic) that calls
  `new_sources.create_request()` on the UI thread (small local writes; see
  New product sources below): a record in `new_source_requests.json` and an
  `EPIC:` block under `## Todo` in TASKS.md, which the 4am run commits. The
  URL is never fetched and no GitHub issue is created. Below that: the
  `daily_plan.json` editor (day, total, fill remainder, Save, Clear
  override) and a collapsed **Daily plan allocations (offers)** section
  with the old per-category offer-allocation table and Spread evenly / By
  pool size.

The app is read-only toward Ozon: only `/v4/product/info/limit`, plus the
read-only `catalog_status.py` / `category_inventory.py` snapshots, and the
upload only runs as `--dry-run`. It only *queries* Task Scheduler; it never
creates, changes, runs or ends tasks. Keep it that way, including in new
tabs; uploads belong to the scheduled runs. New tabs plug in through
`planner_ui.register_tab(name, builder)` (`builder(app, parent_frame)`; tabs
must hand UI updates back through `app.post`, never touch Tk from a thread).

The daily plan rules:

Business instruction, 2026-11-24 (task #32): create at most **500 new offers
per day** (the unit is new Ozon offer_ids, one per color/size variant, the
same unit as `daily_create`), not the whole 2000/day quota.

- **`daily_plan.json`** (gitignored, written only by `daily_plan.save_plan()`,
  which validates it and writes it atomically). Schema:
  `{"version": 1, "default": {"total": 500, "allocations": {"M&S":
  {"Pajama": 120, ...}}, "fill_remainder": true}, "overrides":
  {"YYYY-MM-DD": <same shape as default>}, "updated_at": ...}`. `total` is an
  int from 0 to 2000 (`MAX_TOTAL`), and allocations must not add up to more
  than `total`. If the file is missing, corrupt or invalid, `load_plan()`
  falls back to the built-in default (`DEFAULT_TOTAL = 500`, no allocations,
  fill_remainder on) and never raises.
- **Dates are UTC** (`daily_plan.utc_today()`), the same date stamp as
  `new_items_submitted.json`. The cap covers all of that UTC day's runs
  together (4am, 5am top-up, 10:15 fill). `plan_for_date()` returns the
  date's override if there is one, otherwise `default`. Unused plan doesn't
  carry over to the next day.
- **The upload is the hard cap.** `upload_to_ozon.cap_new_items_by_plan()`
  limits new creates to min(Ozon `daily_create` left, plan left), minus what
  `created_today()` already counts in `new_items_submitted.json`. Pass 1 fills
  the per-category allocations. Pass 2 fills the rest in priority order, only
  when `fill_remainder` is on. Anything over the cap goes to
  `deferred_items.json` and goes first on the next run. If the quota read
  fails (`None`), the plan cap still applies. Updates to existing offers
  aren't capped. Discovery (`category_priority.py`) and
  `fill_remaining_quota.py` only *aim* at the plan (discovery aims 1.2× over
  because dedup drops some). Never treat their numbers as the limit.
- **"Category" = the Ozon product type label** from
  `daily_plan.CATEGORY_LABELS`, keyed by the `type_id` that
  `ozon_mapping.resolve_category_and_type()` already picks (Underwear,
  Underwear Set, Tank Top, Pajama, T-Shirt, Socks, Sweater, Trousers, Blouse,
  Skirt, Jacket, Dress, Top). We use these instead of the ~428 M&S site
  categories in `category_priority.csv` because the site categories overlap
  (e.g. "Külot" / "Yeni Gelen Külotlar"), so you can't allocate against
  them. Site sections are drill-down info only. A new product type needs a
  `CATEGORY_LABELS` entry, or it can only enter through `fill_remainder`.
  Offers whose category can't be resolved (e.g. `products.csv` is locked by
  Excel) count toward the total only.
- **H&M can't be added yet.** Brand comes from the offer_id prefix
  (`brand_for_offer_id()`: M&S / H&M / Other). H&M shows on the planner with
  its live/for-sale numbers, but its row is read-only ("no add pipeline
  yet"). The only H&M code is a stock refresh: there's no H&M crawl or
  upload. Building that is a separate task.
- **Pool numbers** come from `category_inventory.py` (a read-only snapshot
  per brand × category: live / crawled-not-uploaded / found-not-live, saved
  to `category_inventory.json`; H&M live/for-sale is copied from
  `catalog_status.json`). `--walk` fetches
  every product page of every supported category, so it's slow and puts
  load on the M&S site. It only runs when the owner clicks it in the UI,
  never automatically.
- **Launching the app:** double-click "Ozon Sales" on the Desktop, or
  run `pythonw planner_ui.pyw` from the repo root. To create or reinstall
  the shortcut, run `python install_desktop_shortcut.py` (add
  `--repo PATH` if the app is in another checkout). It's idempotent and
  rewrites `%USERPROFILE%\Desktop\Ozon Sales.lnk`, using the
  `pythonw.exe` next to the interpreter that runs it, so run it with the
  Python that should open the app. Once the new shortcut exists, the
  installer removes the old `Ozon Daily Planner.lnk` from the Desktop (if
  creating the new one fails, the old one is left alone). The window, its taskbar button and the shortcut use Ozon's own favicon, committed as `assets/ozon.ico` (multi-size) / `assets/ozon.png` and never fetched at runtime; a missing file just leaves the default icon, and re-running `install_desktop_shortcut.py` applies it to an existing shortcut. The app runs all network calls and
  subprocesses off the Tk main thread, so the window doesn't freeze.

## New product sources (`new_sources.py`)

Tk-free, offline (never fetches the URL; a test forbids network imports).
`create_request(url, description)` validates (http(s) URL with a host;
description 1-1000 chars), refuses a duplicate URL (host case / trailing
slash ignored), appends the record to gitignored `new_source_requests.json`
(atomic; a corrupt file is kept as `.bak`) and inserts an `EPIC:` bullet
block under `## Todo` in TASKS.md (rest of the file unchanged).

## Task status (`task_status.py`)

Read-only status reader for the Ozon Sales app's "Daily tasks" tab.
`collect_status(now=None)` never raises; errors go into `errors` /
`tasks_error`. Scheduled tasks are found by name marker (`TASK_NAME_MARKERS`:
`MandsOzon`, `OzonSales_`; the price/stock/H&M stock tasks behind `run_*.bat`
are `OzonSales_Daily*`), not a hard-coded list. The only scheduler call is
`schtasks /query /fo CSV /v`: never create, change, run or end a task from
here. Headers are matched by name (English plus Turkish aliases), then by
English column position. Also reads `daily_run_status.json` (local date,
`stale` if not today) and the optional `hm_stock_last_run.json` /
`catalog_status.json['runs']`. Run it off the Tk main thread, because schtasks
can be slow.
The app's Daily tasks tab (`planner_ui.DailyTasksTab`) is view-only: its
only button is Refresh, it re-collects every 60s on a worker thread (never
two collects at once), and it shows a `recheck_stockouts` timeout as a
warning, not a failure, and an `account_snapshot` failure or timeout as a
warning too (every other step failure/timeout is critical).

**Per-task sections** (task #99). `collect_status()` also returns
`task_metrics` (`load_task_metrics()`, from the last valid line of each
run-history file). `planner_ui.build_task_sections()` (pure, Tk-free, never
raises) turns these into one card per scheduled task, in the same order as
`build_task_rows`. Each card shows six metrics in this fixed order: URLs
checked, Prices checked, SKU prices updated, Campaign limit prices updated,
SKU stock checked, SKU stock updated. Sources (`TASK_METRIC_KEYS`):
- `OzonSales_DailyStockUpdate`: `stock_update_history.jsonl` (written by
  `refresh_live_stock.py`): `pages_checked`, `products_checked`,
  `comparison.increased + decreased`. The 4am `daily_run.py` also runs this
  script, so the card shows the latest M&S stock run, whichever task ran it.
- `OzonSales_DailyHMStockUpdate`: `hm_stock_update_history.jsonl`:
  `products_read`, `offers_live`, `stock_changed`.
- `OzonSales_DailyPriceUpdate`: `price_refresh_history.jsonl` (gitignored,
  one line per `refresh_prices.py` run: `urls_checked`, `urls_failed`,
  `prices_checked`, `prices_changed`) plus `price_update_history.jsonl`
  (`daily_price_update.py`). The "updated" counts come from the record's
  newer `price_changed_ok` / `limit_changed_ok` keys: offers Ozon accepted
  whose price / `min_price` really changed. They are not `pushed_ok`,
  because every SKU is re-pushed daily. An external dashboard reads
  `price_update_history.jsonl`, so only add keys there; never rename or
  remove them.

A metric that doesn't apply to a task, or one that has no value (e.g. an
older record), shows `—`. MandsOzon* tasks and unmapped tasks get all `—`.
If a record isn't from today, the card keeps its counts and adds a warning
note: "counts from YYYY-MM-DD (latest run not recorded)". For the price
task, the date is the older of the refresh and update dates. The friendly
names come from `planner_ui.TASK_DISPLAY_NAMES` / `task_display_name()`
(case-insensitive; unknown names pass through). They change the display
only. The Task Scheduler tasks themselves are NOT renamed, and the raw name
stays in each row's `task_name`.

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

## Content rating (`content_rating.py`, `improve_content.py`)

- **Rating endpoint**: `POST /v1/product/rating-by-sku` (≤100 SKUs per call;
  SKUs resolved via `/v3/product/info/list`, flat response). Each group
  (media / important_attributes / other_attributes / text) carries its
  unmet `conditions` and `improve_attributes`. `content_rating.py` is
  read-only: `content_rating_snapshot.json` (overwritten) +
  `content_rating_history.jsonl` (one line per run).
- **Single-item push + re-read**: `improve_content.py` sends ONE merged
  `/v3/product/import` item per offer_id (photos, video 21841/21837, rich
  content 11254, size table 13164, attributes together), one item at a time,
  and verifies by re-reading the product. A group stops on its first failed
  push and is retried another day. Actions are always derived from the live
  record, so re-runs are safe. MS- offer_ids only.
- **daily_update share**: the job uses at most `CONTENT_UPDATE_SHARE` (50%)
  of the *remaining* `daily_update` quota from `/v4/product/info/limit`,
  re-checked before each group, so `upload_to_ozon.py` (4am, 5am top-up,
  10:15 fill) keeps its headroom. Also capped by `--limit-groups` (150) and
  `--max-minutes` (50), with a 7-day per-group cooldown.
- **No fabrication in attributes** (`content_attributes.py`): dictionary
  attributes only take ids from Ozon's real dictionary (cached); protected /
  managed-elsewhere attributes are never touched; country of origin,
  certifications, warranty, measurements and codes are never filled; free
  text with percentages, measurements, links or Latin text is rejected. If
  unsure, leave it unset. Media is derived from real M&S photos only (zoom
  crops, slideshow video) — no AI-generated product images.
- **R2 cleanup**: once every offer_id of a group is verified, the group's
  generated files are deleted locally AND on R2 (business instruction
  2026-08-27). A group with a failed offer keeps its files for the retry.

## Warehouse routing (`warehouse_routing.py`)

Price-only rule (business instruction, 2026-11-24, task #47 — replaces the
old "under 500g AND under $80" rule): an item whose final sale price that day
(Ozon `price.price`, USD) is below $60 routes to the small-items warehouse
(`SMALL_WAREHOUSE_ID`) with 0 in the regular one; $60.00 or more routes to the
regular warehouse (`REGULAR_WAREHOUSE_ID`) with 0 in the small one. Weight is
not consulted; it applies to all brands (M&S and H&M alike). No valid price →
skipped, left where it is. Re-evaluated every stock-sync run, not just once at
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
Optional `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` for the 10am
Telegram summary (`telegram_notify.py`; the token is never printed).

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
