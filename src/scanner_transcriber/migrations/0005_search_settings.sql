-- M4: partial-word search (trigram index over transcripts) and "hide from live feed".

CREATE VIRTUAL TABLE transmissions_tri USING fts5(
    transcript, alt_transcript,
    content='transmissions', content_rowid='id', tokenize='trigram'
);

CREATE TRIGGER transmissions_tri_ai AFTER INSERT ON transmissions BEGIN
    INSERT INTO transmissions_tri(rowid, transcript, alt_transcript)
    VALUES (new.id, new.transcript, new.alt_transcript);
END;

CREATE TRIGGER transmissions_tri_ad AFTER DELETE ON transmissions BEGIN
    INSERT INTO transmissions_tri(transmissions_tri, rowid, transcript, alt_transcript)
    VALUES ('delete', old.id, old.transcript, old.alt_transcript);
END;

CREATE TRIGGER transmissions_tri_au AFTER UPDATE OF transcript, alt_transcript ON transmissions BEGIN
    INSERT INTO transmissions_tri(transmissions_tri, rowid, transcript, alt_transcript)
    VALUES ('delete', old.id, old.transcript, old.alt_transcript);
    INSERT INTO transmissions_tri(rowid, transcript, alt_transcript)
    VALUES (new.id, new.transcript, new.alt_transcript);
END;

INSERT INTO transmissions_tri(transmissions_tri) VALUES ('rebuild');

ALTER TABLE channels ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0;
