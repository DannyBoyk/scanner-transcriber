-- M3 step 1: component heartbeats and a system event log for the status page.

CREATE TABLE component_state (           -- one row per component, overwritten every few seconds
    name       TEXT PRIMARY KEY,          -- 'capture', 'worker'
    state      TEXT NOT NULL,             -- capture: connecting|streaming|reconnecting|session_busy|replaying|stopped
                                          -- worker:  gpu|cpu|degraded|stopped
    detail     TEXT NOT NULL,             -- JSON
    pid        INTEGER,
    updated_at TEXT NOT NULL              -- heartbeat; stale = process gone
);

CREATE TABLE system_events (             -- state transitions only; kept ~30 days
    id         INTEGER PRIMARY KEY,
    at         TEXT NOT NULL,
    component  TEXT NOT NULL,             -- capture | worker | queue | host
    level      TEXT NOT NULL CHECK (level IN ('info', 'warn', 'error')),
    kind       TEXT NOT NULL,
    message    TEXT NOT NULL,
    detail     TEXT
);

CREATE INDEX ix_events_at ON system_events(at DESC);
