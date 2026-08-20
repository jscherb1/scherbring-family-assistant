-- Shared state store for agent results.
-- This is what makes follow-up questions work: a subagent writes a summary of
-- what it did, and reads its own recent rows back before answering later.

CREATE TABLE IF NOT EXISTS agent_results (
    id          INTEGER PRIMARY KEY,
    agent       TEXT NOT NULL,          -- e.g. 'todoist'
    task        TEXT NOT NULL,          -- the user request / what was asked
    created_at  TEXT NOT NULL,          -- ISO-8601 UTC, e.g. 2026-07-19T20:34:00Z
    summary     TEXT NOT NULL,          -- human-readable summary of what happened
    detail_json TEXT                    -- structured detail (JSON) for follow-ups
);

CREATE INDEX IF NOT EXISTS idx_agent_results_agent_created
    ON agent_results (agent, created_at DESC);

-- Recipe library for the meal-planner subagent. Seeded once from recipes.json,
-- then grown over time as new AI-generated meals are approved.
CREATE TABLE IF NOT EXISTS recipes (
    id               TEXT PRIMARY KEY,
    title            TEXT NOT NULL,
    description      TEXT,
    ingredients_json TEXT NOT NULL,   -- [{"text":..., "include_in_shopping_list":bool}]
    steps_json       TEXT NOT NULL,   -- ["step 1", "step 2", ...]
    tags_json        TEXT,            -- ["Crockpot", "Kid Friendly", ...]
    protein_type     TEXT,            -- "chicken" | "vegetarian" | "salmon" | null
    meal_type        TEXT,            -- "dinner" | "breakfast" | "misc" | ...
    prep_time_min    INTEGER,
    cook_time_min    INTEGER,
    total_time_min   INTEGER,
    servings         INTEGER,
    source_url       TEXT,
    notes            TEXT,
    last_cooked_at   TEXT,             -- ISO date, updated when a planned meal is approved
    rating           INTEGER,          -- latest sentiment from user feedback; null = none yet
    feedback_json    TEXT              -- [{"date":"YYYY-MM-DD","comment":"...","rating":N}]
);

CREATE INDEX IF NOT EXISTS idx_recipes_meal_type ON recipes (meal_type);

-- Scheduled tasks: proactive prompts the orchestrator runs on a cron schedule,
-- delivered via the local scheduler channel (scripts/scheduler_channel/) and fired
-- by the shared poller (scripts/scheduler_poll.py). Adding a new task is just a row
-- here - no code change or new Windows Scheduled Task needed.
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id                 TEXT PRIMARY KEY,       -- uuid4
    name               TEXT NOT NULL UNIQUE,   -- human-readable, e.g. "weekly-meal-plan"
    prompt             TEXT NOT NULL,          -- delivered as the <channel> content
    cron_expression    TEXT NOT NULL,          -- 5-field, e.g. "0 9 * * 0"
    target_chat_id     TEXT NOT NULL,          -- Telegram chat_id to reply into
    enabled            INTEGER NOT NULL DEFAULT 1,  -- 0/1
    created_at         TEXT NOT NULL,          -- ISO-8601 UTC
    last_dispatched_at TEXT                    -- ISO-8601 UTC; NULL until first fire
);

CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_enabled ON scheduled_tasks (enabled);

-- One row per dispatch attempt / completion of a scheduled task. The poller inserts
-- a 'dispatched' row when it fires the event; the orchestrator flips it to 'ok' or
-- 'failed' after actually completing the work, so the heartbeat task can tell
-- "ran fine" apart from "silently dropped".
CREATE TABLE IF NOT EXISTS scheduled_task_runs (
    id            INTEGER PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES scheduled_tasks(id),
    run_at        TEXT NOT NULL,   -- nominal scheduled fire time (ISO-8601 UTC)
    dispatched_at TEXT,            -- actual wall-clock POST time, NULL if dispatch_failed
    status        TEXT NOT NULL,   -- 'dispatched' | 'dispatch_failed' | 'ok' | 'failed'
    summary       TEXT
);

CREATE INDEX IF NOT EXISTS idx_scheduled_task_runs_task_run
    ON scheduled_task_runs (task_id, run_at DESC);

-- Kids memory keeper: quick memories/anecdotes about the kids, captured locally
-- and mirrored to per-child Google Drive subfolders (scripts/kid_memories_store.py)
-- for long-term archival. A memory about both kids is one row tagged with both
-- children, but is duplicated into each child's Drive subfolder on sync.
CREATE TABLE IF NOT EXISTS kid_memories (
    id                    TEXT PRIMARY KEY,      -- uuid4
    created_at            TEXT NOT NULL,         -- ISO-8601 local wall-clock when this was LOGGED, e.g. 2026-07-20T14:30:12
    memory_date           TEXT NOT NULL,         -- ISO date (YYYY-MM-DD) the memory actually HAPPENED; defaults to created_at's date if not told otherwise
    memory_date_precision TEXT NOT NULL DEFAULT 'exact', -- 'exact' | 'approximate' (vague timeframe like "last spring")
    children_json    TEXT NOT NULL,         -- ["Ruth"] | ["Claire"] | ["Ruth","Claire"]
    raw_text         TEXT NOT NULL,         -- verbatim as originally told - NEVER edited after insert
    memory_text      TEXT NOT NULL,         -- working copy; starts equal to raw_text, may be refined later
    source           TEXT NOT NULL,         -- 'telegram' | 'direct'
    tags_json        TEXT,                  -- free-form tags, e.g. ["funny","bedtime"]
    media_type       TEXT NOT NULL DEFAULT 'text',  -- future: 'photo' | 'audio'
    drive_files_json TEXT,                  -- {"Ruth": {"file_id":..., "path":...}, ...}
    drive_status     TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'synced' | 'failed'
    metadata_json    TEXT                   -- open-ended catch-all for future fields
);

CREATE INDEX IF NOT EXISTS idx_kid_memories_created ON kid_memories (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_kid_memories_memory_date ON kid_memories (memory_date DESC);

-- Learned trigger phrasing feedback for the kids-memory subagent: phrases that
-- correctly (or incorrectly) signaled a kid-specific memory, so recognition of
-- natural-language anecdotes vs. false positives improves over time.
CREATE TABLE IF NOT EXISTS kid_memory_triggers (
    id          INTEGER PRIMARY KEY,
    phrase      TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- 'positive' | 'false_positive'
    created_at  TEXT NOT NULL,
    note        TEXT
);

CREATE INDEX IF NOT EXISTS idx_kid_memory_triggers_kind ON kid_memory_triggers (kind);

-- Lawn & garden agent: plant/bed locations (simple tracking — name, type, rough
-- location) for a ~10,000 sq ft yard in Rochester, MN.
CREATE TABLE IF NOT EXISTS lawn_garden_plants (
    id          TEXT PRIMARY KEY,      -- uuid4
    name        TEXT NOT NULL,         -- e.g. "hostas", "front foundation bed"
    plant_type  TEXT,                  -- e.g. "shrub", "perennial", "mulch bed" (free text)
    location    TEXT NOT NULL,         -- rough description, e.g. "front bed by garage"
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Chemical/product inventory (fertilizers, herbicides, etc. currently on hand).
CREATE TABLE IF NOT EXISTS lawn_garden_products (
    id                TEXT PRIMARY KEY,     -- uuid4
    name              TEXT NOT NULL,        -- e.g. "T-Zone SE", "Trimec Lawn Weed Killer"
    category          TEXT NOT NULL,        -- 'fertilizer' | 'herbicide' | 'other'
    active_ingredient TEXT,                 -- e.g. "Mesotrione", "Triclopyr/Sulfentrazone/2,4-D/Dicamba"
    epa_reg_no        TEXT,
    container_desc    TEXT,                 -- e.g. "1 quart concentrate", "4 lb/gal jug"
    in_stock          INTEGER NOT NULL DEFAULT 1,  -- 0/1, user-reported
    notes             TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

-- Reinders 6-Step program reference (seeded once from the printed program; editable
-- if the user changes products/timing in future years).
CREATE TABLE IF NOT EXISTS lawn_garden_program (
    id            TEXT PRIMARY KEY,     -- uuid4
    round_number  INTEGER NOT NULL,     -- 1-6
    task_label    TEXT NOT NULL,        -- e.g. "Pre-emerge fertilizer application"
    product_name  TEXT NOT NULL,        -- e.g. "15-0-0 Bar. 80% RxN or 19-0-6 Dim. 50% RxN"
    timing_desc   TEXT NOT NULL,        -- e.g. "Mid April (when grass starts greening up)"
    timing_month  INTEGER NOT NULL,     -- 1-12, approximate month for scheduling logic
    coverage_desc TEXT,                 -- e.g. "12500 sq. ft." / "1.5 oz per 1000 sq ft"
    notes         TEXT
);

CREATE INDEX IF NOT EXISTS idx_lawn_garden_program_round ON lawn_garden_program (round_number);

-- Log of actual applications/treatments performed (program rounds AND ad-hoc spot
-- sprays). Append-only history, like kid_memories.
CREATE TABLE IF NOT EXISTS lawn_garden_treatments (
    id                TEXT PRIMARY KEY,     -- uuid4
    treatment_date    TEXT NOT NULL,        -- ISO date (YYYY-MM-DD) it was applied
    round_number      INTEGER,              -- loosely refs lawn_garden_program.round_number, NULL for ad-hoc
    product_name      TEXT NOT NULL,
    method            TEXT NOT NULL,        -- 'broadcast' | 'spot-spray' | 'backpack'
    area              TEXT,                 -- e.g. "whole yard", "front bed", "back fence line"
    target_weeds_json TEXT,                 -- e.g. ["dandelion","clover"]
    notes             TEXT,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lawn_garden_treatments_date
    ON lawn_garden_treatments (treatment_date DESC);

-- Ongoing weed/pest/disease issues and their status, so recurring problems
-- (quackgrass, clover, dandelion, thistle) are tracked over time.
CREATE TABLE IF NOT EXISTS lawn_garden_issues (
    id            TEXT PRIMARY KEY,      -- uuid4
    issue         TEXT NOT NULL,         -- e.g. "quackgrass", "thistle patch"
    location      TEXT,
    status        TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'resolved'
    first_noted   TEXT NOT NULL,         -- ISO date
    resolved_date TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lawn_garden_issues_status ON lawn_garden_issues (status);

-- Free-form yard profile (size, location/coords for weather lookups, equipment on
-- hand). Simple key/value store — avoids a single-row table with nullable columns.
CREATE TABLE IF NOT EXISTS lawn_garden_config (
    key   TEXT PRIMARY KEY,   -- e.g. "yard_size_sqft", "latitude", "longitude", "equipment"
    value TEXT NOT NULL
);

-- Personal profile: the user, family, and friends. Single source of truth for
-- structured facts (birthdays, allergies, sizes, school, etc.) shared across all
-- subagents (scripts/profile_store.py). Ruth/Claire get rows here too for their
-- structured facts; kids-memory (kid_memories above) separately owns anecdotes/
-- stories and Drive sync — the two systems are complementary, not merged.
CREATE TABLE IF NOT EXISTS profile_people (
    id           TEXT PRIMARY KEY,      -- uuid4
    name         TEXT NOT NULL,
    relationship TEXT,                  -- 'self' | 'spouse' | 'child' | 'friend' | ...
    birthday     TEXT,                  -- MM-DD or YYYY-MM-DD
    notes        TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_profile_people_name ON profile_people (name);

-- Arbitrary structured facts per person (allergy, shirt_size, school, favorite_color,
-- etc.) — key/value per person, same rationale as lawn_garden_config: avoids a
-- single-row table with nullable columns for every possible fact type.
CREATE TABLE IF NOT EXISTS profile_people_facts (
    id         TEXT PRIMARY KEY,      -- uuid4
    person_id  TEXT NOT NULL REFERENCES profile_people(id),
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_profile_people_facts_person ON profile_people_facts (person_id);

-- Global facts not tied to a specific person (home_address, timezone, anniversary, etc.).
CREATE TABLE IF NOT EXISTS profile_facts (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Home maintenance agent: recurring indoor maintenance items (HVAC filters, smoke
-- detector batteries, etc.), same "definition + append-only log" split as the
-- lawn-garden plants/treatments tables above.
CREATE TABLE IF NOT EXISTS home_maintenance_items (
    id            TEXT PRIMARY KEY,      -- uuid4
    name          TEXT NOT NULL,         -- e.g. "HVAC filter change"
    category      TEXT NOT NULL,         -- 'hvac' | 'safety' | 'appliance' | 'cleaning' | 'seasonal' | 'other'
    interval_days INTEGER NOT NULL,      -- recurrence cadence, e.g. 90
    active        INTEGER NOT NULL DEFAULT 1,  -- 0/1, paused items excluded from due-checks
    notes         TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_home_maintenance_items_active ON home_maintenance_items (active);

-- Log of completed maintenance (append-only, like lawn_garden_treatments). An
-- item's "last done" date is MAX(completed_date) here, falling back to the
-- item's created_at if it has never been completed.
CREATE TABLE IF NOT EXISTS home_maintenance_completions (
    id             TEXT PRIMARY KEY,     -- uuid4
    item_id        TEXT NOT NULL REFERENCES home_maintenance_items(id),
    completed_date TEXT NOT NULL,        -- ISO date (YYYY-MM-DD)
    notes          TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_home_maintenance_completions_item
    ON home_maintenance_completions (item_id, completed_date DESC);

-- Weather-reminders agent: location/coords for the Open-Meteo lookup and the
-- alert thresholds (rain %, snow inches), same key/value rationale as
-- lawn_garden_config — avoids a single-row table with nullable columns.
CREATE TABLE IF NOT EXISTS weather_config (
    key   TEXT PRIMARY KEY,   -- e.g. "latitude", "longitude", "location", "rain_probability_threshold"
    value TEXT NOT NULL
);

-- Log of proactive weather alerts already sent, keyed by alert type + the date
-- the event is expected, so the daily check doesn't re-alert the same rain/snow/
-- storm event on consecutive mornings while it's still in the forecast window.
CREATE TABLE IF NOT EXISTS weather_alerts (
    id                   TEXT PRIMARY KEY,     -- uuid4
    alert_type           TEXT NOT NULL,        -- 'rain_cushions' | 'snow_shoveling' | 'severe_weather'
    target_date          TEXT NOT NULL,        -- ISO date (YYYY-MM-DD) the event is expected
    todoist_task_created INTEGER NOT NULL DEFAULT 0,  -- 0/1
    created_at           TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_weather_alerts_type_date
    ON weather_alerts (alert_type, target_date);

-- Hy-Vee cart builder: synced purchase-history snapshot (frequency/recency source).
CREATE TABLE IF NOT EXISTS hyvee_purchase_history (
    id           TEXT PRIMARY KEY,      -- uuid4 hex
    upc          TEXT,                  -- from product image URL (may be null)
    product_name TEXT NOT NULL,         -- from image altText
    order_date   TEXT NOT NULL,         -- ISO date (YYYY-MM-DD)
    purchase_id  TEXT NOT NULL,         -- Hy-Vee order uuid
    synced_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_hyvee_history_unique
    ON hyvee_purchase_history (purchase_id, product_name);
CREATE INDEX IF NOT EXISTS idx_hyvee_history_name
    ON hyvee_purchase_history (product_name, order_date DESC);

-- Learned item->product map, one row per normalized item key (e.g. "milk").
CREATE TABLE IF NOT EXISTS hyvee_item_prefs (
    item                 TEXT PRIMARY KEY,   -- normalized key
    preferred_product_id TEXT,
    preferred_upc        TEXT,
    product_name         TEXT,
    size                 TEXT,
    confidence           REAL NOT NULL DEFAULT 0.0,  -- 0.0..1.0
    pref_brand           TEXT,
    max_price            REAL,
    prefer_on_sale       INTEGER NOT NULL DEFAULT 0, -- 0/1
    pref_size            TEXT,
    source               TEXT NOT NULL DEFAULT 'history', -- history|user|mealplan
    times_confirmed      INTEGER NOT NULL DEFAULT 0,
    times_rejected       INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL
);

-- Append-only feedback driving confidence changes (auditable).
CREATE TABLE IF NOT EXISTS hyvee_feedback_log (
    id                  TEXT PRIMARY KEY,   -- uuid4 hex
    ts                  TEXT NOT NULL,
    item                TEXT NOT NULL,
    proposed_product_id TEXT,
    action              TEXT NOT NULL,      -- accepted|rejected|substituted
    chosen_product_id   TEXT,
    note                TEXT
);
CREATE INDEX IF NOT EXISTS idx_hyvee_feedback_item ON hyvee_feedback_log (item, ts DESC);

-- Per cart-build run log.
CREATE TABLE IF NOT EXISTS hyvee_cart_runs (
    id            TEXT PRIMARY KEY,   -- uuid4 hex
    ts            TEXT NOT NULL,
    items_json    TEXT NOT NULL,      -- list of parsed input items
    resolved_json TEXT NOT NULL,      -- item -> product + auto/flagged
    cart_verified INTEGER NOT NULL DEFAULT 0,
    summary       TEXT
);

-- Personal finance agent (Monarch Money, via the local monarch-mcp-server since
-- the official MCP is paused). Learned merchant/account -> WHO-tag map, same
-- "learned map + append-only feedback log" shape as hyvee_item_prefs /
-- hyvee_feedback_log above, since it's the same confidence-building problem:
-- infer a WHO tag, act if confident, learn from the user's confirm/correct.
CREATE TABLE IF NOT EXISTS finance_who_map (
    signal_key       TEXT PRIMARY KEY,   -- normalized merchant name OR "account:<account_id>"
    signal_type      TEXT NOT NULL,      -- 'merchant' | 'account'
    who_tag_name     TEXT NOT NULL,      -- e.g. "WHO:Justin", "Adults", "Kids", "Family"
    confidence       REAL NOT NULL DEFAULT 0.0,  -- 0.0..1.0
    times_confirmed  INTEGER NOT NULL DEFAULT 0,
    times_rejected   INTEGER NOT NULL DEFAULT 0,
    source           TEXT NOT NULL DEFAULT 'inferred',  -- 'inferred' | 'user'
    updated_at       TEXT NOT NULL
);

-- Append-only audit of every tagging decision the finance agent makes or
-- proposes, so a review batch can be reconstructed/explained later and
-- finance_who_map confidence changes are traceable (mirrors hyvee_feedback_log).
CREATE TABLE IF NOT EXISTS finance_tag_log (
    id                TEXT PRIMARY KEY,   -- uuid4 hex
    ts                TEXT NOT NULL,
    transaction_id    TEXT NOT NULL,      -- Monarch transaction id
    merchant_name     TEXT,
    account_id        TEXT,
    amount            REAL,
    txn_date          TEXT,               -- ISO date (YYYY-MM-DD)
    proposed_who_tag  TEXT,               -- WHO tag inferred/proposed (null if none inferred)
    confidence        REAL,
    action            TEXT NOT NULL,      -- 'tagged_auto' | 'tagged_confirmed' | 'skipped_ambiguous' | 'marked_reviewed'
    user_decision      TEXT,              -- 'confirmed' | 'corrected' | 'rejected' | null (pending)
    note              TEXT
);
CREATE INDEX IF NOT EXISTS idx_finance_tag_log_txn ON finance_tag_log (transaction_id, ts DESC);

-- Proposed Monarch transaction rules (merchant -> WHO tag) awaiting the user's
-- approval before being created via create_transaction_rule. Once approved and
-- created, monarch_rule_id records the live rule's id for reference.
CREATE TABLE IF NOT EXISTS finance_rule_proposals (
    id               TEXT PRIMARY KEY,   -- uuid4 hex
    created_at       TEXT NOT NULL,
    merchant_name    TEXT NOT NULL,
    who_tag_name     TEXT NOT NULL,
    evidence_json    TEXT,               -- e.g. matching transaction ids/dates that justified the proposal
    status           TEXT NOT NULL DEFAULT 'proposed',  -- 'proposed' | 'approved' | 'rejected' | 'created'
    monarch_rule_id  TEXT,               -- set once created in Monarch
    decided_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_finance_rule_proposals_status ON finance_rule_proposals (status);

-- Free-form finance agent config (Drive working-folder id, cached WHO tag ids,
-- last historical-sweep cursor for resumability). Same key/value rationale as
-- lawn_garden_config / weather_config.
CREATE TABLE IF NOT EXISTS finance_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Phase 2: history of generated spending-summary reports (finance-reporter
-- subagent), so "link me last month's report" is answerable and reports are
-- traceable. Mirrors the finance_tag_log / finance_rule_proposals shape.
CREATE TABLE IF NOT EXISTS finance_report_log (
    id            TEXT PRIMARY KEY,   -- uuid4 hex
    created_at    TEXT NOT NULL,      -- local ISO
    period        TEXT NOT NULL,      -- 'weekly' | 'monthly' | 'annual'
    range_start   TEXT NOT NULL,      -- YYYY-MM-DD
    range_end     TEXT NOT NULL,
    drive_file_id TEXT,               -- set after Drive upload
    drive_url     TEXT,
    summary       TEXT                -- the Telegram brief that was sent
);
CREATE INDEX IF NOT EXISTS idx_finance_report_log_period ON finance_report_log (period, range_end DESC);

-- Local cache of the COROS Training Hub Workout Library (t.coros.com), synced
-- periodically by scripts/fitness/library_sync.py + scripts/fitness_store.py.
-- Source of truth for the fitness subagent's weekly plan: reuse an existing
-- row here before asking Coros to build a brand-new custom workout.
CREATE TABLE IF NOT EXISTS fitness_workout_library (
    id                TEXT PRIMARY KEY,      -- uuid4
    coros_workout_id  TEXT NOT NULL UNIQUE,  -- Coros's own numeric id (data-id on the card)
    name              TEXT NOT NULL,         -- e.g. "Peloton - 45 min", "800m Speed Workout"
    workout_type      TEXT NOT NULL,         -- Coros sport-icon slug: 'outrun' | 'strength' | 'cycle' | etc.
    sets_desc         TEXT,                  -- e.g. "15 set(s)"
    target_distance   TEXT,                  -- e.g. "4.8 mi", NULL if not applicable
    target_time       TEXT,                  -- e.g. "45 min"
    estimated_load    TEXT,                  -- Coros "Estimated load" TL figure, NULL if not shown
    last_synced_at    TEXT NOT NULL,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fitness_workout_library_type ON fitness_workout_library (workout_type);

-- One row per workout the fitness subagent has planned for a given week,
-- written after the user approves a proposed weekly plan. completion_status
-- is set later (not yet by any automation) so a future history-informed
-- planning phase has real data to read without needing a schema change.
CREATE TABLE IF NOT EXISTS fitness_weekly_plans (
    id                  TEXT PRIMARY KEY,     -- uuid4
    week_start_date     TEXT NOT NULL,        -- YYYY-MM-DD, Monday of the planned week
    day_of_week         TEXT NOT NULL,        -- 'Mon' | 'Tue' | ... | 'Sun'
    workout_type        TEXT NOT NULL,        -- 'run' | 'strength' | 'peloton' | 'other'
    subtype             TEXT,                 -- e.g. 'lower_body', 'tempo', free text
    matched_library_id  TEXT REFERENCES fitness_workout_library (id),
    is_custom           INTEGER NOT NULL DEFAULT 0,  -- 0/1 — built as a new Coros workout rather than reused
    planned_time        TEXT,                 -- HH:MM local
    coros_status        TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'created' | 'manual_needed' | 'failed'
    calendar_event_id   TEXT,                 -- Google Calendar event id once created
    completion_status   TEXT NOT NULL DEFAULT 'planned',  -- 'planned' | 'completed' | 'skipped'
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fitness_weekly_plans_week ON fitness_weekly_plans (week_start_date);
