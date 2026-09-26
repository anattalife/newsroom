"""SQLite database: schema, connection handling and small query helpers.

One file (data/newsroom.db) holds everything except uploaded images and the
encryption key. SQLite in WAL mode handles a local news site comfortably.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA_DIR = Path(os.environ.get("NEWSROOM_DATA", Path(__file__).resolve().parent.parent / "data"))
UPLOADS = DATA_DIR / "uploads"
BACKUPS = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "newsroom.db"

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS secrets (key TEXT PRIMARY KEY, value BLOB NOT NULL);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'editor',          -- owner | editor | reviewer
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login TEXT
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,                            -- rss | weather | email | calendar | tips | manual
    trust TEXT NOT NULL DEFAULT 'official',        -- official | tip | outlet
    category TEXT NOT NULL DEFAULT 'Local News',
    interval_min INTEGER NOT NULL DEFAULT 30,
    enabled INTEGER NOT NULL DEFAULT 1,
    config TEXT NOT NULL DEFAULT '{}',
    approval TEXT NOT NULL DEFAULT 'always',       -- always | auto_high
    builtin INTEGER NOT NULL DEFAULT 0,
    last_checked TEXT, last_new_item TEXT,
    last_error TEXT, error_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    hash TEXT NOT NULL UNIQUE,
    url TEXT, title TEXT, text TEXT, published TEXT,
    extra TEXT NOT NULL DEFAULT '{}',
    fetched_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',            -- new | drafted | skipped
    skip_reason TEXT,
    story_id INTEGER
);
CREATE INDEX IF NOT EXISTS items_status ON items(status, source_id);

CREATE TABLE IF NOT EXISTS stories (
    id INTEGER PRIMARY KEY,
    source_id INTEGER REFERENCES sources(id) ON DELETE SET NULL,
    kind TEXT NOT NULL DEFAULT 'story',            -- story | event
    status TEXT NOT NULL DEFAULT 'draft',          -- draft | scheduled | published | rejected
    headline TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT 'Local News', byline TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL DEFAULT 'medium',
    checklist TEXT NOT NULL DEFAULT '[]',          -- [{text, checked}]
    cites TEXT NOT NULL DEFAULT '[]',              -- [{name, url, trust}]
    image TEXT, image_alt TEXT,
    breaking INTEGER NOT NULL DEFAULT 0,
    slug TEXT UNIQUE,
    publish_at TEXT, published_at TEXT,
    event_start TEXT, event_end TEXT, event_location TEXT,
    corrections TEXT NOT NULL DEFAULT '[]',        -- [{at, text}]
    reject_reason TEXT,
    tip_id INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, updated_by INTEGER
);
CREATE INDEX IF NOT EXISTS stories_status ON stories(status, published_at);
CREATE INDEX IF NOT EXISTS stories_source ON stories(source_id, status);

CREATE TABLE IF NOT EXISTS tips (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL, location TEXT, name TEXT, contact TEXT,
    anonymous INTEGER NOT NULL DEFAULT 0,
    photos TEXT NOT NULL DEFAULT '[]',
    ip_hash TEXT,
    status TEXT NOT NULL DEFAULT 'new',            -- new | used | dismissed
    story_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS blocked (ip_hash TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rate (bucket TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS rate_bucket ON rate(bucket, at);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY, user_id INTEGER, action TEXT NOT NULL,
    target TEXT, detail TEXT, at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weather (
    slug TEXT PRIMARY KEY, data TEXT, updated_at TEXT, error TEXT
);

CREATE TABLE IF NOT EXISTS ai_usage (
    id INTEGER PRIMARY KEY, at TEXT NOT NULL, model TEXT,
    input_tokens INTEGER, output_tokens INTEGER, cost REAL, purpose TEXT
);

-- ── community (Stage 2 redesign) ─────────────────────────
-- Members are readers with a public-site account. They never reach the dashboard.
CREATE TABLE IF NOT EXISTS members (
    id INTEGER PRIMARY KEY,
    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    display_name TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    bio TEXT NOT NULL DEFAULT '', town TEXT NOT NULL DEFAULT '', photo TEXT,
    points INTEGER NOT NULL DEFAULT 0,
    trusted INTEGER NOT NULL DEFAULT 0,               -- may publish own articles without approval
    status TEXT NOT NULL DEFAULT 'active',            -- active | held | suspended | banned
    suspended_until TEXT,
    confirmed INTEGER NOT NULL DEFAULT 0,
    staff_user_id INTEGER UNIQUE,                     -- set for staff public profiles
    note TEXT NOT NULL DEFAULT '',                    -- private moderation note
    created_at TEXT NOT NULL, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS member_visits (member_id INTEGER NOT NULL, week TEXT NOT NULL,
    PRIMARY KEY (member_id, week));

CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'info', text TEXT NOT NULL, link TEXT, seen INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS notices_member ON notices(member_id, seen);

CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                               -- article | facts
    status TEXT NOT NULL DEFAULT 'draft',             -- draft | waiting | sent_back | published | declined | withdrawn | developing
    headline TEXT NOT NULL DEFAULT '', body TEXT NOT NULL DEFAULT '',
    facts TEXT NOT NULL DEFAULT '{}', links TEXT NOT NULL DEFAULT '[]', photos TEXT NOT NULL DEFAULT '[]',
    category TEXT NOT NULL DEFAULT '', credit INTEGER NOT NULL DEFAULT 1, org_id INTEGER,
    note TEXT NOT NULL DEFAULT '',                    -- the newsroom's note (send back / decline)
    story_id INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, submitted_at TEXT
);
CREATE INDEX IF NOT EXISTS submissions_status ON submissions(status);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'visible',           -- visible | held | hidden | deleted
    hold_reason TEXT,
    upvotes INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL, edited_at TEXT
);
CREATE INDEX IF NOT EXISTS comments_story ON comments(story_id, status);

CREATE TABLE IF NOT EXISTS votes (
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    target TEXT NOT NULL, target_id INTEGER NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY (member_id, target, target_id)
);

CREATE TABLE IF NOT EXISTS flags (
    id INTEGER PRIMARY KEY,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    target TEXT NOT NULL, target_id INTEGER NOT NULL,
    reason TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', weight INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'open',              -- open | resolved
    outcome TEXT, created_at TEXT NOT NULL,
    UNIQUE (member_id, target, target_id)
);

CREATE TABLE IF NOT EXISTS points_log (
    id INTEGER PRIMARY KEY, member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    points INTEGER NOT NULL, reason TEXT NOT NULL, ref TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE (member_id, reason, ref)
);
CREATE INDEX IF NOT EXISTS points_when ON points_log(created_at);

CREATE TABLE IF NOT EXISTS badges (
    id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, name TEXT NOT NULL, icon TEXT NOT NULL DEFAULT '★',
    grp TEXT NOT NULL DEFAULT 'special', description TEXT NOT NULL DEFAULT '',
    rule TEXT NOT NULL DEFAULT 'manual', n INTEGER NOT NULL DEFAULT 1,
    active INTEGER NOT NULL DEFAULT 1, sort INTEGER NOT NULL DEFAULT 100
);
CREATE TABLE IF NOT EXISTS member_badges (
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    badge_id INTEGER NOT NULL REFERENCES badges(id) ON DELETE CASCADE,
    awarded_at TEXT NOT NULL, awarded_by INTEGER,
    PRIMARY KEY (member_id, badge_id)
);

CREATE TABLE IF NOT EXISTS orgs (
    id INTEGER PRIMARY KEY, member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
    name TEXT NOT NULL, slug TEXT UNIQUE, category TEXT NOT NULL, logo TEXT,
    description TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '', website TEXT NOT NULL DEFAULT '',
    contact TEXT NOT NULL DEFAULT '', calendar_url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',           -- pending | approved | declined
    created_at TEXT NOT NULL
);

-- ── today's specials, posted by community partners (restaurants and the like) ──
CREATE TABLE IF NOT EXISTS specials (
    id INTEGER PRIMARY KEY, org_id INTEGER NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
    member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
    title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', price TEXT NOT NULL DEFAULT '', image TEXT,
    day TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'live',   -- live | removed
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS specials_day ON specials(day, status);
-- game night: members' running updates, and live streams / videos people share
CREATE TABLE IF NOT EXISTS game_updates (
    id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    member_id INTEGER REFERENCES members(id) ON DELETE SET NULL,
    body TEXT NOT NULL, our_score INTEGER, their_score INTEGER,
    status TEXT NOT NULL DEFAULT 'visible',   -- visible | hidden
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS game_updates_game ON game_updates(game_id, id);
CREATE TABLE IF NOT EXISTS game_videos (
    id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    member_id INTEGER REFERENCES members(id) ON DELETE SET NULL, user_id INTEGER,
    url TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'video',   -- stream | video
    status TEXT NOT NULL DEFAULT 'visible', created_at TEXT NOT NULL
);
-- the fun: crowns people hold until someone passes them, site-wide shout-outs, and Call It predictions
CREATE TABLE IF NOT EXISTS crowns (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL, key TEXT NOT NULL, label TEXT NOT NULL,
    member_id INTEGER REFERENCES members(id) ON DELETE SET NULL, score INTEGER NOT NULL DEFAULT 0,
    since TEXT, updated_at TEXT NOT NULL, UNIQUE (kind, key)
);
CREATE TABLE IF NOT EXISTS crown_log (
    id INTEGER PRIMARY KEY, crown_id INTEGER NOT NULL REFERENCES crowns(id) ON DELETE CASCADE,
    member_id INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS shoutouts (
    id INTEGER PRIMARY KEY, kind TEXT NOT NULL DEFAULT 'info', text TEXT NOT NULL, link TEXT NOT NULL DEFAULT '',
    member_id INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY, story_id INTEGER REFERENCES stories(id) ON DELETE CASCADE,
    game_id INTEGER REFERENCES games(id) ON DELETE CASCADE,
    kind TEXT NOT NULL DEFAULT 'number',          -- number | choice | score
    question TEXT NOT NULL, unit TEXT NOT NULL DEFAULT '', lo REAL, hi REAL, step REAL,
    choices TEXT NOT NULL DEFAULT '[]', closes_at TEXT NOT NULL,
    answer TEXT, status TEXT NOT NULL DEFAULT 'open',   -- open | resolved | void
    resolved_at TEXT, created_by INTEGER, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS predictions_story ON predictions(story_id);
CREATE INDEX IF NOT EXISTS predictions_game ON predictions(game_id);
CREATE TABLE IF NOT EXISTS guesses (
    id INTEGER PRIMARY KEY, prediction_id INTEGER NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
    member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    value TEXT NOT NULL, num REAL, rank INTEGER, won INTEGER NOT NULL DEFAULT 0, exact INTEGER NOT NULL DEFAULT 0,
    points INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, UNIQUE (prediction_id, member_id)
);
-- folders for organizing sources
CREATE TABLE IF NOT EXISTS source_folders (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, parent_id INTEGER REFERENCES source_folders(id),
    created_at TEXT NOT NULL
);
-- ── sports: schools, their teams, and games (schedules and scores) ──
CREATE TABLE IF NOT EXISTS sports_schools (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL, short TEXT NOT NULL DEFAULT '', mascot TEXT NOT NULL DEFAULT '',
    slug TEXT UNIQUE NOT NULL, level TEXT NOT NULL DEFAULT 'High school', town TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '#1B2433', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sports_teams (
    id INTEGER PRIMARY KEY, school_id INTEGER NOT NULL REFERENCES sports_schools(id) ON DELETE CASCADE,
    sport TEXT NOT NULL, gender TEXT NOT NULL DEFAULT '', level TEXT NOT NULL DEFAULT 'Varsity',
    slug TEXT UNIQUE NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY, team_id INTEGER NOT NULL REFERENCES sports_teams(id) ON DELETE CASCADE,
    opponent TEXT NOT NULL, home TEXT NOT NULL DEFAULT 'home',        -- home | away | neutral
    starts_at TEXT NOT NULL, time_tbd INTEGER NOT NULL DEFAULT 0, location TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'scheduled',                         -- scheduled | live | final | postponed | cancelled
    our_score INTEGER, their_score INTEGER, detail TEXT NOT NULL DEFAULT '',   -- detail: "Halftime", "2OT"...
    note TEXT NOT NULL DEFAULT '', reported_by INTEGER REFERENCES members(id) ON DELETE SET NULL, reported_at TEXT,
    locked INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT 'newsroom',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS games_when ON games(starts_at);
CREATE INDEX IF NOT EXISTS games_team ON games(team_id, starts_at);
CREATE TABLE IF NOT EXISTS game_log (
    id INTEGER PRIMARY KEY, game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    member_id INTEGER, user_id INTEGER, change TEXT NOT NULL, before TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS game_confirms (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE, member_id INTEGER NOT NULL,
    score TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (game_id, member_id)
);

"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOADS.mkdir(exist_ok=True)
    BACKUPS.mkdir(exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)  # autocommit; use tx() for groups
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    return c


# columns added after the first release (safe to run every start)
MIGRATIONS = [
    ("submissions", "summary TEXT NOT NULL DEFAULT ''"), ("submissions", "subcategory TEXT NOT NULL DEFAULT ''"),
    ("submissions", "photo_credit TEXT NOT NULL DEFAULT ''"),
    ("badges", "tiers TEXT NOT NULL DEFAULT ''"), ("badges", "rarity TEXT NOT NULL DEFAULT 'common'"),
    ("badges", "secret INTEGER NOT NULL DEFAULT 0"), ("badges", "hint TEXT NOT NULL DEFAULT ''"),
    ("member_badges", "tier INTEGER NOT NULL DEFAULT 1"), ("member_badges", "shown INTEGER NOT NULL DEFAULT 1"),
    ("member_badges", "tier_at TEXT"), ("game_updates", "temp_f REAL"),
    ("sports_teams", "info TEXT NOT NULL DEFAULT ''"), ("sports_teams", "info_by INTEGER"),
    ("sports_teams", "info_at TEXT"), ("sports_teams", "info_locked INTEGER NOT NULL DEFAULT 0"),
    ("stories", "subcategory TEXT NOT NULL DEFAULT ''"),
    ("orgs", "updated_at TEXT"), ("orgs", "needs_look INTEGER NOT NULL DEFAULT 0"),   # partner changed their box
    ("members", "work_points INTEGER NOT NULL DEFAULT 0"), ("members", "social_points INTEGER NOT NULL DEFAULT 0"),
    ("sources", "folder_id INTEGER"),
    ("items", "claim TEXT"), ("weather", "attempted_at TEXT"),
    ("tips", "member_id INTEGER"), ("tips", "credit INTEGER NOT NULL DEFAULT 1"),
    ("stories", "dev TEXT NOT NULL DEFAULT '{}'"),           # Develop this story: material, questions, answers
    ("stories", "social_text TEXT NOT NULL DEFAULT ''"),
    ("stories", "social TEXT NOT NULL DEFAULT '{}'"),        # per platform: on, text, status, url, error
    ("stories", "featured INTEGER NOT NULL DEFAULT 0"),
    ("stories", "scope TEXT NOT NULL DEFAULT 'local'"),       # local | national | world
    ("stories", "story_type TEXT NOT NULL DEFAULT 'general'"),
    ("stories", "member_id INTEGER"), ("stories", "credit TEXT"),   # credit: byline | tip
    ("stories", "credit_public INTEGER NOT NULL DEFAULT 1"),
    ("stories", "submission_id INTEGER"), ("stories", "org_id INTEGER"),
    ("stories", "edited_note INTEGER NOT NULL DEFAULT 0"),
    ("stories", "trusted_publish INTEGER NOT NULL DEFAULT 0"),
    ("stories", "photo_credit TEXT"), ("stories", "photo_member_id INTEGER"),
    ("stories", "wire INTEGER NOT NULL DEFAULT 0"),
    ("stories", "upvotes INTEGER NOT NULL DEFAULT 0"),
    ("stories", "comment_count INTEGER NOT NULL DEFAULT 0"),
    ("stories", "comments_mode TEXT NOT NULL DEFAULT 'open'"),  # open | locked | off
    ("stories", "factcheck TEXT NOT NULL DEFAULT '[]'"),
    ("stories", "video TEXT"),                                # YouTube or Facebook video link, embedded
    ("submissions", "video TEXT"),
    ("ai_usage", "story_id INTEGER"), ("ai_usage", "searches INTEGER NOT NULL DEFAULT 0"),
]


def _remove_directory(c):
    """The business directory was removed (v2.11). Delete its data and uploaded photos, once."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='businesses'").fetchone():
        return
    files = set()
    queries = ["SELECT photo FROM businesses", "SELECT image FROM business_posts", "SELECT photos FROM reviews",
               "SELECT image FROM specials"]
    for q in queries:
        try:
            for (v,) in c.execute(q).fetchall():
                if not v:
                    continue
                if v.startswith("["):
                    files.update(x for x in json.loads(v) if isinstance(x, str))
                else:
                    files.add(v)
        except (sqlite3.OperationalError, ValueError):
            pass
    for name in files:
        if "/" not in name and "\\" not in name:
            try:
                (UPLOADS / name).unlink()
            except OSError:
                pass
    for t in ("business_posts", "reviews", "claims", "specials", "businesses"):
        c.execute(f"DROP TABLE IF EXISTS {t}")
    c.commit()


def init():
    c = connect()
    _remove_directory(c)
    c.executescript(SCHEMA)
    c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
    for table, col in MIGRATIONS:
        try:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
        except sqlite3.OperationalError:
            pass
    # built-in sources every newsroom has
    for name, typ, trust, cat in (("Reader tips", "tips", "tip", "Local News"),
                                  ("Manual add", "manual", "tip", "Local News")):
        if not c.execute("SELECT 1 FROM sources WHERE type=?", (typ,)).fetchone():
            c.execute("INSERT INTO sources(name,type,trust,category,interval_min,builtin,created_at)"
                      " VALUES(?,?,?,?,0,1,?)", (name, typ, trust, cat, now()))
    for sql in ("CREATE INDEX IF NOT EXISTS stories_member ON stories(member_id, status)",
                "CREATE INDEX IF NOT EXISTS stories_featured ON stories(status, featured, published_at)"):
        c.execute(sql)
    c.execute("INSERT OR IGNORE INTO meta(key,value) VALUES('community_start',?)", (now(),))
    c.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(SCHEMA_VERSION),))
    c.close()
    d = DB()
    try:
        from . import community
        community.seed_badges(d)
        from . import settings
        settings.migrate(d)
    finally:
        d.close()


# ── tiny helpers ──────────────────────────────────────────
class DB:
    """Per-request / per-job connection wrapper with convenience methods."""

    def __init__(self):
        self.c = connect()

    def close(self):
        self.c.close()

    def q(self, sql, args=()):
        return [dict(r) for r in self.c.execute(sql, args)]

    def one(self, sql, args=()):
        r = self.c.execute(sql, args).fetchone()
        return dict(r) if r else None

    def val(self, sql, args=()):
        r = self.c.execute(sql, args).fetchone()
        return r[0] if r else None

    def run(self, sql, args=()):
        return self.c.execute(sql, args)

    def insert(self, table, **fields):
        cols = ",".join(fields)
        marks = ",".join("?" * len(fields))
        return self.c.execute(f"INSERT INTO {table}({cols}) VALUES({marks})", tuple(fields.values())).lastrowid

    def update(self, table, id_, **fields):
        if not fields:
            return
        sets = ",".join(f"{k}=?" for k in fields)
        self.c.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*fields.values(), id_))

    def tx(self):
        return _Tx(self.c)


class _Tx:
    def __init__(self, c):
        self.c = c

    def __enter__(self):
        self.c.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc, *_):
        self.c.execute("ROLLBACK" if exc else "COMMIT")


def loads(s, default=None):
    try:
        return json.loads(s) if s else default
    except (TypeError, ValueError):
        return default


def story_row(r):
    """Decode JSON columns of a story row."""
    if not r:
        return r
    for k, d in (("checklist", []), ("cites", []), ("corrections", []), ("dev", {}), ("social", {}),
                 ("factcheck", [])):
        r[k] = loads(r.get(k), d)
    return r
