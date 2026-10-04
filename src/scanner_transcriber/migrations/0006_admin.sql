-- M4.5: admin settings/auth, versioned prompt + substitutions, commands, re-transcribe
-- batches, prompt tests.

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);

CREATE TABLE prompt_versions (              -- kind 'prompt' (text) or 'substitutions' (JSON)
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('prompt', 'substitutions')),
    body         TEXT NOT NULL,
    note         TEXT,
    created_at   TEXT NOT NULL,
    activated_at TEXT
);
CREATE TABLE active_versions (kind TEXT PRIMARY KEY, version_id INTEGER NOT NULL);

CREATE TABLE commands (                     -- polled by capture / worker supervisor / worker
    id         INTEGER PRIMARY KEY,
    target     TEXT NOT NULL,               -- capture | worker
    action     TEXT NOT NULL,               -- reconnect | restart | prompt-test
    arg        TEXT,                        -- JSON
    created_at TEXT NOT NULL,
    taken_at   TEXT,
    done_at    TEXT,
    result     TEXT
);
CREATE INDEX ix_commands_open ON commands(target, done_at);

CREATE TABLE retranscribe_batches (
    id             INTEGER PRIMARY KEY,
    created_at     TEXT NOT NULL,
    since          TEXT,
    until          TEXT,
    tgids          TEXT,                    -- JSON list or NULL = all
    prompt_version INTEGER,
    total          INTEGER NOT NULL
);

CREATE TABLE prompt_evals (
    id             INTEGER PRIMARY KEY,
    prompt_version INTEGER,                 -- NULL when testing an unsaved draft
    draft_body     TEXT,
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL,           -- queued | running | done | failed
    clips          INTEGER,
    wer            REAL,
    cer            REAL,
    detail         TEXT                     -- JSON: worst clips
);

ALTER TABLE transmissions ADD COLUMN prompt_version INTEGER;
ALTER TABLE transmissions ADD COLUMN subs_version INTEGER;
ALTER TABLE transmissions ADD COLUMN prev_transcript TEXT;
ALTER TABLE transmissions ADD COLUMN prev_prompt_version INTEGER;
ALTER TABLE transmissions ADD COLUMN batch_id INTEGER;
CREATE INDEX ix_tx_batch ON transmissions(batch_id);
