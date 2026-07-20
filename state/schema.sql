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
