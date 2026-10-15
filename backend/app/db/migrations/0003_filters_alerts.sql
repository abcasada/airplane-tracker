CREATE TABLE filters (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    enabled             INTEGER NOT NULL DEFAULT 1,
    mode                TEXT NOT NULL DEFAULT 'live' CHECK (mode IN ('live', 'shadow')),
    sort_order          INTEGER NOT NULL DEFAULT 0,
    current_version_id  INTEGER,
    created_at          REAL NOT NULL,
    updated_at          REAL NOT NULL
);

CREATE TABLE filter_versions (
    id          INTEGER PRIMARY KEY,
    filter_id   INTEGER NOT NULL REFERENCES filters(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    definition  TEXT NOT NULL,
    name        TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL,
    UNIQUE (filter_id, version)
);

CREATE TABLE exclusions (
    id          INTEGER PRIMARY KEY,
    filter_id   INTEGER REFERENCES filters(id) ON DELETE CASCADE,  -- NULL = all filters
    kind        TEXT NOT NULL CHECK (kind IN ('hex', 'registration', 'type_code', 'operator', 'callsign')),
    value       TEXT NOT NULL,
    until       REAL,  -- NULL = forever
    note        TEXT NOT NULL DEFAULT '',
    created_at  REAL NOT NULL
);

CREATE TABLE alerts (
    id                 INTEGER PRIMARY KEY,
    filter_id          INTEGER REFERENCES filters(id) ON DELETE SET NULL,
    filter_version_id  INTEGER,
    filter_name        TEXT NOT NULL,
    mode               TEXT NOT NULL,
    hex                TEXT NOT NULL,
    callsign           TEXT,
    registration       TEXT,
    type_code          TEXT,
    type_desc          TEXT,
    military           INTEGER NOT NULL DEFAULT 0,
    place_id           INTEGER,
    state              TEXT NOT NULL CHECK (state IN ('active', 'ended')),
    started_at         REAL NOT NULL,
    last_matched_at    REAL NOT NULL,
    ended_at           REAL,
    closest_nm         REAL,
    closest_at         REAL,
    notified           INTEGER NOT NULL DEFAULT 0,
    suppressed_reason  TEXT,
    title              TEXT,
    message            TEXT,
    photo_url          TEXT,
    facts              TEXT NOT NULL DEFAULT '{}',
    feedback           TEXT CHECK (feedback IN ('up', 'down'))
);
CREATE INDEX alerts_state ON alerts(state);
CREATE INDEX alerts_started ON alerts(started_at);
CREATE INDEX alerts_filter ON alerts(filter_id, started_at);
CREATE INDEX alerts_hex ON alerts(hex, started_at);

CREATE TABLE notifications (
    id         INTEGER PRIMARY KEY,
    hex        TEXT NOT NULL,
    alert_ids  TEXT NOT NULL,
    channel    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    sent_at    REAL NOT NULL,
    title      TEXT,
    message    TEXT,
    ok         INTEGER NOT NULL,
    error      TEXT
);
CREATE INDEX notifications_sent ON notifications(sent_at);
