-- What the classifier saw when you marked an aircraft (callsign, owner, flags), as JSON.
-- Callsigns are transient, so the military review suggestions need this snapshot.
ALTER TABLE military_overrides ADD COLUMN snapshot_json TEXT;
