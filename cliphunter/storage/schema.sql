-- ClipHunter phase 1. Applied with CREATE TABLE IF NOT EXISTS.
-- Later phases can add columns; this file is the initial schema.

CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_key TEXT NOT NULL UNIQUE,
    platform TEXT NOT NULL,
    handle TEXT,
    url TEXT,
    authorized INTEGER NOT NULL DEFAULT 0,
    context TEXT NOT NULL DEFAULT '',
    weights_json TEXT NOT NULL DEFAULT '{}',
    thresholds_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    stream_url TEXT,
    stream_title TEXT,
    video_id TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    confirmed_authorized_at TEXT,
    status TEXT NOT NULL,
    CHECK (status IN ('active', 'ended', 'interrupted', 'failed'))
);

CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id),
    channel_id INTEGER NOT NULL REFERENCES channels(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    signal_score REAL,
    breakdown_json TEXT,
    title TEXT,
    hook TEXT,
    hashtags TEXT,
    attribution_json TEXT NOT NULL,
    file_path TEXT,
    duration_seconds REAL,
    weight REAL,
    review_claimed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    CHECK (kind IN ('manual', 'auto')),
    CHECK (status IN ('pending', 'ready', 'failed', 'approved', 'rejected'))
);

CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    action TEXT NOT NULL,
    adjust_seconds REAL,
    note TEXT,
    weight REAL,
    breakdown_json TEXT,
    reason TEXT,
    category TEXT,
    participants_json TEXT,
    created_at TEXT NOT NULL,
    CHECK (action IN ('approve', 'reject', 'adjust'))
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER REFERENCES sessions(id),
    cpu_pct REAL,
    rss_mb REAL,
    extra_json TEXT,
    sampled_at TEXT NOT NULL
);

-- Editorial memory. Old databases get the new decision columns from
-- _COLUMN_PATCHES in repo.py. These tables are created on the next init.
-- verified = 1 is a sourced fact. verified = 0 is a rumor and must stay labeled.

CREATE TABLE IF NOT EXISTS participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    description TEXT,
    persona TEXT,
    source TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS participant_nicknames (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    participant_id INTEGER NOT NULL REFERENCES participants(id),
    nickname TEXT NOT NULL,
    source TEXT,
    verified INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    left_name TEXT NOT NULL,
    right_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT,
    source TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS storylines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    detail TEXT,
    participants_json TEXT,
    source TEXT,
    verified INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS publish_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clip_id INTEGER,
    platform TEXT,
    retention REAL,
    shares REAL,
    comments REAL,
    replay_rate REAL,
    source TEXT,
    raw_json TEXT,
    imported_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel_id, started_at);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status);
CREATE INDEX IF NOT EXISTS idx_clips_channel ON clips(channel_id, created_at);
CREATE INDEX IF NOT EXISTS idx_metrics_session ON metrics(session_id, sampled_at);
