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
