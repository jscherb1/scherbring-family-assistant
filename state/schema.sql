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
