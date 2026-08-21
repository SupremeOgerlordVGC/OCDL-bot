"""
SQLite storage for the draft bot.

The local database is the source of truth. Google Sheets is only ever a
copy of what is stored here.

Design notes
------------
* One row in `drafts` per draft. A draft belongs to exactly one Discord
  channel, and a channel may only have one draft that is not finished.
  That single rule is what keeps separate channels completely independent.
* `picks` is the immutable draft history: what was drafted, by whom, in
  which round, in what order. Trades never rewrite history.
* `rosters` is current ownership. Trades move rows here. A Pokemon counts
  as "already drafted" when it appears in `rosters`, so a Pokemon dropped
  in a trade correctly becomes available again.
"""

import os
import sqlite3
from datetime import datetime, timezone

import config

# The schema is plain SQL so it is easy to read and easy to inspect with the
# `sqlite3` command line tool.
SCHEMA = """
CREATE TABLE IF NOT EXISTS drafts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id            INTEGER NOT NULL,
    channel_id          INTEGER NOT NULL,
    admin_channel_id    INTEGER NOT NULL,
    admin_role_id       INTEGER NOT NULL,
    budget              INTEGER NOT NULL,
    min_picks           INTEGER NOT NULL,
    max_picks           INTEGER NOT NULL,
    base_timer          INTEGER NOT NULL,
    -- Most trades any one player may have approved. 0 means unlimited.
    max_trades          INTEGER NOT NULL DEFAULT 0,
    sheet_url           TEXT,
    -- 'setup'    : created, /edit allowed, /pick not allowed
    -- 'active'   : running
    -- 'complete' : every round finished
    -- 'cancelled': abandoned by an admin
    status              TEXT NOT NULL DEFAULT 'setup',
    current_round       INTEGER NOT NULL DEFAULT 0,
    -- 0-based seat index inside the current round's order
    current_turn_index  INTEGER NOT NULL DEFAULT 0,
    turn_deadline       REAL,
    -- 1 when the current player's queue was checked and nothing was usable
    queue_exhausted     INTEGER NOT NULL DEFAULT 0,
    -- 1 once the current player's timer has been reduced during this turn.
    -- A turn may only ever cost a player one halving.
    halved_this_turn    INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    started_at          TEXT,
    finished_at         TEXT
);

-- The heart of "one independent draft per channel": at most one unfinished
-- draft may exist per channel. Finished drafts stay around as history.
CREATE UNIQUE INDEX IF NOT EXISTS one_live_draft_per_channel
    ON drafts (channel_id) WHERE status IN ('setup', 'active');

CREATE TABLE IF NOT EXISTS players (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id          INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    user_id           INTEGER NOT NULL,
    display_name      TEXT NOT NULL,
    draft_position    INTEGER NOT NULL,   -- seat 1..N, decides the snake order
    budget_remaining  INTEGER NOT NULL,
    current_timer     INTEGER NOT NULL,   -- personal timer, halved on skips
    skip_count        INTEGER NOT NULL DEFAULT 0,
    UNIQUE (draft_id, user_id),
    UNIQUE (draft_id, draft_position)
);

CREATE TABLE IF NOT EXISTS picks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id     INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    player_id    INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    round        INTEGER NOT NULL,
    pokemon      TEXT NOT NULL,
    pokemon_key  TEXT NOT NULL,           -- lowercased name, for comparisons
    cost         INTEGER NOT NULL,
    pick_order   INTEGER NOT NULL,        -- chronological order within the draft
    is_makeup    INTEGER NOT NULL DEFAULT 0,
    is_auto      INTEGER NOT NULL DEFAULT 0,   -- drafted from a queue
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS picks_by_draft ON picks (draft_id, round);

CREATE TABLE IF NOT EXISTS rosters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id     INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    player_id    INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    pokemon      TEXT NOT NULL,
    pokemon_key  TEXT NOT NULL,
    cost         INTEGER NOT NULL,
    pick_id      INTEGER REFERENCES picks(id) ON DELETE SET NULL,
    source       TEXT NOT NULL DEFAULT 'draft',   -- 'draft' or 'trade'
    UNIQUE (draft_id, pokemon_key)
);

CREATE TABLE IF NOT EXISTS makeups (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id     INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    player_id    INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    round        INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',  -- pending/resolved/expired
    -- Global turn number when the player was skipped. Used to spot the
    -- snake-boundary case, where the deadline falls immediately afterwards.
    created_turn INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    resolved_at  TEXT,
    UNIQUE (draft_id, player_id, round)
);

CREATE TABLE IF NOT EXISTS queued_picks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id     INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    player_id    INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    round        INTEGER NOT NULL,
    priority     INTEGER NOT NULL,        -- 1 = try first
    pokemon      TEXT NOT NULL,
    pokemon_key  TEXT NOT NULL,
    cost         INTEGER,                 -- optional, may be looked up instead
    UNIQUE (draft_id, player_id, round, priority)
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id      INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    trade_number  INTEGER NOT NULL,       -- 1, 2, 3 ... within this draft
    proposer_id   INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    partner_id    INTEGER REFERENCES players(id) ON DELETE SET NULL,
    drops         TEXT NOT NULL,          -- comma separated Pokemon names
    gets          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    resolved_by   INTEGER,
    UNIQUE (draft_id, trade_number)
);

CREATE TABLE IF NOT EXISTS skips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id    INTEGER NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    player_id   INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    round       INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    halved      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);
"""


def connect(path=None):
    """Open the database and make sure the schema exists."""
    path = path or config.DB_PATH

    conn = sqlite3.connect(path, check_same_thread=False)
    # Rows behave like dicts: row["budget"] instead of row[4].
    conn.row_factory = sqlite3.Row
    # ON DELETE CASCADE only works when foreign keys are switched on.
    conn.execute("PRAGMA foreign_keys = ON")
    # Write-ahead logging survives a hard VM reset far better than the
    # default journal mode, which matters on a cloud box that may reboot.
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def now():
    """Current UTC time as an ISO string, used for every timestamp column."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def backup(conn, backup_dir=None):
    """Write a timestamped copy of the database and return its path.

    Uses SQLite's own backup API, which is safe to run while the bot is
    live - unlike copying the file with `cp`, which can catch a half
    written page.
    """
    backup_dir = backup_dir or config.BACKUP_DIR
    os.makedirs(backup_dir, exist_ok=True)

    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    target = os.path.join(backup_dir, f"draft-{stamp}.db")

    destination = sqlite3.connect(target)
    try:
        conn.backup(destination)
    finally:
        destination.close()
    return target


def prune_backups(backup_dir=None, keep=30):
    """Delete the oldest backups so the VM disk does not slowly fill up."""
    backup_dir = backup_dir or config.BACKUP_DIR
    if not os.path.isdir(backup_dir):
        return []

    files = sorted(
        (f for f in os.listdir(backup_dir) if f.startswith("draft-") and f.endswith(".db")),
        reverse=True,
    )
    removed = []
    for name in files[keep:]:
        try:
            os.remove(os.path.join(backup_dir, name))
            removed.append(name)
        except OSError:
            pass
    return removed
