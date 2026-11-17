-- FAA SWIM (TFMS) flights that touch your airport places or followed aircraft. Kept 3 days so
-- a restart doesn't lose plans filed hours ahead. Never exported.
CREATE TABLE swim_flights (
    key         TEXT PRIMARY KEY,
    acid        TEXT NOT NULL,
    updated_at  REAL NOT NULL,
    data_json   TEXT NOT NULL
) WITHOUT ROWID;
CREATE INDEX swim_flights_updated ON swim_flights(updated_at);
