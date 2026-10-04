-- Stable per-talkgroup colour for the web feed: assigned in first-heard order, so the first
-- ten talkgroups all differ (a hash of the TGID collided for busy pairs).
ALTER TABLE channels ADD COLUMN color INTEGER;

UPDATE channels SET color = (
    SELECT COUNT(*) FROM channels AS c2
    WHERE c2.first_heard_at < channels.first_heard_at
       OR (c2.first_heard_at = channels.first_heard_at AND c2.key < channels.key)
) % 10;
