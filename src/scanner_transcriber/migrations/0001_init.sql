-- Initial schema (design §6, plus per-PTT fields learned in M0/M1).

CREATE TABLE channels (
    key            TEXT PRIMARY KEY,          -- "sys:<system>|tg:<tgid>" (trunked) or "sys:<system>|ch:<channel>|f:<hz>"
    system         TEXT,
    department     TEXT,
    channel        TEXT,
    tgid           TEXT,
    frequency_hz   INTEGER,
    alias          TEXT,                      -- user-defined display name
    priority       INTEGER NOT NULL DEFAULT 0,-- higher = transcribed first under backlog
    ignored        INTEGER NOT NULL DEFAULT 0,-- 1 = record but don't transcribe (hard ignore = Avoid on the scanner)
    first_heard_at TEXT NOT NULL,
    last_heard_at  TEXT NOT NULL
);

CREATE TABLE transmissions (
    id              INTEGER PRIMARY KEY,
    scanner_id      TEXT NOT NULL DEFAULT 'sds200-1',
    channel_key     TEXT REFERENCES channels(key),
    started_at      TEXT NOT NULL,            -- UTC ISO-8601
    ended_at        TEXT NOT NULL,
    duration_ms     INTEGER NOT NULL,
    system          TEXT,                     -- snapshot of the status that opened the clip
    department      TEXT,
    channel         TEXT,
    tgid            TEXT,
    unit_id         TEXT,                     -- first unit heard
    unit_ids        TEXT,                     -- JSON list of all units heard in the clip
    site            TEXT,
    frequency_hz    INTEGER,
    modulation      TEXT,
    rssi            INTEGER,
    continued       INTEGER NOT NULL DEFAULT 0,  -- continuation of a transmission split at max_clip_s
    status_missing  INTEGER NOT NULL DEFAULT 0,  -- cut from audio alone while status was stale
    status_changes  TEXT,                     -- JSON list of status changes during clip
    raw_status      TEXT,                     -- raw XML of the opening status
    audio_path      TEXT,                     -- relative to data_dir; NULL after retention purge
    audio_format    TEXT,
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','processing','done','failed','skipped')),
    priority        INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    claimed_at      TEXT,
    error           TEXT,
    transcript      TEXT,
    confidence      REAL,
    no_speech       INTEGER,
    stt_backend     TEXT,
    stt_model       TEXT,
    stt_ms          INTEGER,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX ix_tx_started      ON transmissions(started_at DESC);
CREATE INDEX ix_tx_channel_time ON transmissions(channel_key, started_at DESC);
CREATE INDEX ix_tx_queue        ON transmissions(status, priority DESC, started_at);
CREATE INDEX ix_tx_updated      ON transmissions(updated_at);

-- Full-text index over transcript + labels (external-content FTS5 table)
CREATE VIRTUAL TABLE transmissions_fts USING fts5(
    transcript, system, department, channel,
    content='transmissions', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER transmissions_ai AFTER INSERT ON transmissions BEGIN
    INSERT INTO transmissions_fts(rowid, transcript, system, department, channel)
    VALUES (new.id, new.transcript, new.system, new.department, new.channel);
END;

CREATE TRIGGER transmissions_ad AFTER DELETE ON transmissions BEGIN
    INSERT INTO transmissions_fts(transmissions_fts, rowid, transcript, system, department, channel)
    VALUES ('delete', old.id, old.transcript, old.system, old.department, old.channel);
END;

-- Only indexed columns: queue/status updates don't churn the FTS index.
CREATE TRIGGER transmissions_au AFTER UPDATE OF transcript, system, department, channel
ON transmissions BEGIN
    INSERT INTO transmissions_fts(transmissions_fts, rowid, transcript, system, department, channel)
    VALUES ('delete', old.id, old.transcript, old.system, old.department, old.channel);
    INSERT INTO transmissions_fts(rowid, transcript, system, department, channel)
    VALUES (new.id, new.transcript, new.system, new.department, new.channel);
END;
