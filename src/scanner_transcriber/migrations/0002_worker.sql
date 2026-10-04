-- M2: transcription worker columns; alt_transcript joins the full-text index.

ALTER TABLE transmissions ADD COLUMN raw_transcript TEXT;   -- model output before guards/substitutions
ALTER TABLE transmissions ADD COLUMN alt_transcript TEXT;   -- Parakeet's text from a speech check
ALTER TABLE transmissions ADD COLUMN flags TEXT;            -- JSON list, e.g. ["hallucination"]
ALTER TABLE transmissions ADD COLUMN claimed_by TEXT;
ALTER TABLE transmissions ADD COLUMN transcribed_at TEXT;

DROP TRIGGER transmissions_ai;
DROP TRIGGER transmissions_ad;
DROP TRIGGER transmissions_au;
DROP TABLE transmissions_fts;

CREATE VIRTUAL TABLE transmissions_fts USING fts5(
    transcript, alt_transcript, system, department, channel,
    content='transmissions', content_rowid='id',
    tokenize='porter unicode61'
);

CREATE TRIGGER transmissions_ai AFTER INSERT ON transmissions BEGIN
    INSERT INTO transmissions_fts(rowid, transcript, alt_transcript, system, department, channel)
    VALUES (new.id, new.transcript, new.alt_transcript, new.system, new.department, new.channel);
END;

CREATE TRIGGER transmissions_ad AFTER DELETE ON transmissions BEGIN
    INSERT INTO transmissions_fts(transmissions_fts, rowid, transcript, alt_transcript, system, department, channel)
    VALUES ('delete', old.id, old.transcript, old.alt_transcript, old.system, old.department, old.channel);
END;

-- Only indexed columns: queue/status updates don't churn the FTS index.
CREATE TRIGGER transmissions_au AFTER UPDATE OF transcript, alt_transcript, system, department, channel
ON transmissions BEGIN
    INSERT INTO transmissions_fts(transmissions_fts, rowid, transcript, alt_transcript, system, department, channel)
    VALUES ('delete', old.id, old.transcript, old.alt_transcript, old.system, old.department, old.channel);
    INSERT INTO transmissions_fts(rowid, transcript, alt_transcript, system, department, channel)
    VALUES (new.id, new.transcript, new.alt_transcript, new.system, new.department, new.channel);
END;

INSERT INTO transmissions_fts(transmissions_fts) VALUES ('rebuild');
