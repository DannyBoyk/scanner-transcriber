-- M5 code review: indexes for a year of data (~0.5-0.7M rows).

-- talkgroup-filtered feed, detail-page neighbours, talkgroup chips' counts
CREATE INDEX ix_tx_tg_time ON transmissions(tgid, started_at);
-- transcription stats on /status (done in the last hour)
CREATE INDEX ix_tx_transcribed ON transmissions(transcribed_at);
-- Admin: "used by N clips" per prompt / substitutions version
CREATE INDEX ix_tx_prompt_version ON transmissions(prompt_version);
CREATE INDEX ix_tx_subs_version ON transmissions(subs_version);
