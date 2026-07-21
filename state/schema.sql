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
