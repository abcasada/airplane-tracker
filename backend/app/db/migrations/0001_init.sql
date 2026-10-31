CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE users (
    id              INTEGER PRIMARY KEY,
    username        TEXT NOT NULL UNIQUE,
    pw_hash         TEXT NOT NULL,
    totp_secret     TEXT NOT NULL,
    totp_confirmed  INTEGER NOT NULL DEFAULT 0,
    totp_last_step  INTEGER NOT NULL DEFAULT 0,
    recovery_codes  TEXT NOT NULL DEFAULT '[]',
    created_at      REAL NOT NULL
);

CREATE TABLE sessions (
    id            TEXT PRIMARY KEY,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token    TEXT NOT NULL,
    created_at    REAL NOT NULL,
    expires_at    REAL NOT NULL,
    last_seen_at  REAL NOT NULL,
    ip            TEXT,
    user_agent    TEXT
);
CREATE INDEX sessions_expires ON sessions(expires_at);

CREATE TABLE audit_log (
    id      INTEGER PRIMARY KEY,
    at      REAL NOT NULL,
    actor   TEXT,
    action  TEXT NOT NULL,
    detail  TEXT
);

CREATE TABLE places (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('viewpoint', 'airport', 'area')),
    lat             REAL NOT NULL,
    lon             REAL NOT NULL,
    elevation_ft    REAL NOT NULL DEFAULT 0,
    radius_nm       REAL NOT NULL,
    polygon         TEXT,
    alt_floor_ft    REAL,
    alt_ceiling_ft  REAL,
    airport_icao    TEXT,
    visibility      TEXT,
    active_from     REAL,
    active_until    REAL,
    schedule        TEXT,
    is_home         INTEGER NOT NULL DEFAULT 0,
    enabled         INTEGER NOT NULL DEFAULT 1,
    source          TEXT NOT NULL DEFAULT 'manual',
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
