"""
SQLite storage for the draft bot.

The local database is the source of truth. Google Sheets is only ever a
copy of what is stored here.

Shape of the data
-----------------
* A **league** is one competition: its name, budget, document, mode and the
  admin channel where trades are approved. Names are unique per server, so
  one admin channel can run several leagues at once.
* A league has one or two **pools**, each bound to its own Discord channel.
  A pool is what actually drafts: it has the turn pointer, the timer and the
  players. Two pools draft at the same time, independently, and the same
  Pokemon may be taken once in each.
* Everything a draft produces - picks, rosters, makeup picks, queues, skips -
  hangs off a pool. Trades hang off the league, because they are approved in
  the shared admin channel.
* `pool_view` flattens a pool together with its league's settings, so the
  rules in draft.py can read `ctx["budget"]` or `ctx["max_picks"]` without
  joining anything by hand.

Two tables deserve a note:

* `picks` is the immutable draft history: what was taken, by whom, in which
  round, in what order. Trades never rewrite it.
* `rosters` is current ownership. Trades move rows here, and a Pokemon counts
  as "already drafted" when it appears in `rosters` for that pool - so a
  Pokemon dropped in a trade correctly becomes available again.
"""

import os
import sqlite3
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS leagues (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    name              TEXT NOT NULL,
    -- The channel /setup was run in. Trades are sent here for approval.
    admin_channel_id  INTEGER NOT NULL,
    -- 'draft_only'     : run the draft, then archive it
    -- 'draft_schedule' : draft plus schedule posting, no trades
    -- 'full_season'    : draft, trades and schedule posting
    mode              TEXT NOT NULL DEFAULT 'full_season',
    -- 'none' | 'tiering' | 'template'
    doc_kind          TEXT NOT NULL DEFAULT 'none',
    doc_url           TEXT,
    budget            INTEGER NOT NULL,
    -- 0 means this is not a Tera format. Above 0 turns Tera Captains on.
    tera_budget       INTEGER NOT NULL DEFAULT 0,
    min_picks         INTEGER NOT NULL,
    max_picks         INTEGER NOT NULL,
    base_timer        INTEGER NOT NULL,
    max_trades        INTEGER NOT NULL DEFAULT 0,
    -- Weeks in the generated season. Only used when the bot makes its own
    -- schedule; with a template the sheet's own SeasonWeeks applies.
    season_weeks      INTEGER NOT NULL DEFAULT 0,
    -- 'setup' | 'active' | 'complete' | 'archived' | 'cancelled'
    status            TEXT NOT NULL DEFAULT 'setup',
    created_at        TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT
);

-- League names identify a draft when several share one admin channel, so
-- unfinished leagues on a server may not share a name. Case-insensitive.
CREATE UNIQUE INDEX IF NOT EXISTS one_live_league_name_per_guild
    ON leagues (guild_id, lower(name)) WHERE status IN ('setup', 'active');

CREATE TABLE IF NOT EXISTS pools (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id           INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    label               TEXT NOT NULL,            -- 'A' or 'B'
    channel_id          INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'setup',
    current_round       INTEGER NOT NULL DEFAULT 0,
    -- 0-based seat index inside the current round's order
    current_turn_index  INTEGER NOT NULL DEFAULT 0,
    turn_deadline       REAL,
    -- Unix time this pool should start drafting by itself, or NULL for
    -- "whenever a moderator runs /draft".
    scheduled_start     REAL,
    -- 1 when the current player's queue was checked and nothing was usable
    queue_exhausted     INTEGER NOT NULL DEFAULT 0,
    -- 1 once the current player's timer has been reduced during this turn.
    -- A turn may only ever cost a player one halving.
    halved_this_turn    INTEGER NOT NULL DEFAULT 0,
    started_at          TEXT,
    finished_at         TEXT,
    UNIQUE (league_id, label)
);

-- One draft per channel: at most one unfinished pool may live in a channel.
CREATE UNIQUE INDEX IF NOT EXISTS one_live_pool_per_channel
    ON pools (channel_id) WHERE status IN ('setup', 'active');

CREATE TABLE IF NOT EXISTS players (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id                INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    user_id                INTEGER NOT NULL,
    display_name           TEXT NOT NULL,
    draft_position         INTEGER NOT NULL,   -- seat 1..N inside this pool
    -- Row on the spreadsheet's Coaches tab, and therefore which roster block
    -- they own: pool A takes 1..10, pool B takes 11..20.
    coach_number           INTEGER NOT NULL,
    budget_remaining       INTEGER NOT NULL,
    tera_budget_remaining  INTEGER NOT NULL DEFAULT 0,
    current_timer          INTEGER NOT NULL,   -- personal, halved on skips
    skip_count             INTEGER NOT NULL DEFAULT 0,
    -- 'active' or 'withdrawn'. A withdrawn player keeps their seat so the
    -- snake order and turn pointer stay valid, but is passed over.
    status                 TEXT NOT NULL DEFAULT 'active',
    UNIQUE (pool_id, user_id),
    UNIQUE (pool_id, draft_position)
);

CREATE TABLE IF NOT EXISTS picks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id       INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    player_id     INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    round         INTEGER NOT NULL,
    pokemon       TEXT NOT NULL,
    pokemon_key   TEXT NOT NULL,          -- lowercased name, for comparisons
    cost          INTEGER NOT NULL,       -- TT when a Tera Captain, else COST
    -- 1 when this Pokemon is one of the team's Tera Captains. A captain is
    -- charged its Tera Tax against BOTH budgets.
    tera_captain  INTEGER NOT NULL DEFAULT 0,
    pick_order    INTEGER NOT NULL,       -- chronological within the pool
    is_makeup     INTEGER NOT NULL DEFAULT 0,
    is_auto       INTEGER NOT NULL DEFAULT 0,   -- drafted from a queue
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS picks_by_pool ON picks (pool_id, round);

CREATE TABLE IF NOT EXISTS rosters (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id       INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    player_id     INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    pokemon       TEXT NOT NULL,
    pokemon_key   TEXT NOT NULL,
    cost          INTEGER NOT NULL,
    tera_captain  INTEGER NOT NULL DEFAULT 0,
    pick_id       INTEGER REFERENCES picks(id) ON DELETE SET NULL,
    source        TEXT NOT NULL DEFAULT 'draft',   -- 'draft' or 'trade'
    UNIQUE (pool_id, pokemon_key)
);

CREATE TABLE IF NOT EXISTS makeups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id       INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    player_id     INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    round         INTEGER NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending/resolved/expired
    -- Global turn number when the player was skipped. Used to spot the
    -- snake-boundary case, where the deadline falls immediately afterwards.
    created_turn  INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    UNIQUE (pool_id, player_id, round)
);

CREATE TABLE IF NOT EXISTS queued_picks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id       INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    player_id     INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    round         INTEGER NOT NULL,
    priority      INTEGER NOT NULL,       -- 1 = try first
    pokemon       TEXT NOT NULL,
    pokemon_key   TEXT NOT NULL,
    cost          INTEGER,
    tera_captain  INTEGER NOT NULL DEFAULT 0,
    UNIQUE (pool_id, player_id, round, priority)
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id     INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    pool_id       INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    trade_number  INTEGER NOT NULL,       -- 1, 2, 3 ... within the league
    proposer_id   INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    partner_id    INTEGER REFERENCES players(id) ON DELETE SET NULL,
    drops         TEXT NOT NULL,          -- JSON list of Pokemon names
    gets          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending/approved/rejected
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    resolved_by   INTEGER,
    UNIQUE (league_id, trade_number)
);

CREATE TABLE IF NOT EXISTS skips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pool_id     INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    player_id   INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    round       INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    halved      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- The season schedule, when the bot generates one itself. With a template
-- the fixtures live on the spreadsheet instead and this stays empty.
-- Fixtures may pair coaches from different pools: only drafting is split.
CREATE TABLE IF NOT EXISTS fixtures (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id      INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    week           INTEGER NOT NULL,
    match_number   INTEGER NOT NULL,
    home_player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    away_player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    created_at     TEXT NOT NULL,
    UNIQUE (league_id, week, match_number)
);

-- Substitution requests, posted to the admin channel for a human to act on.
-- A player already in the draft asks to sub OUT; anybody else asks to sub IN.
-- Approving one is a moderator running /substitute, which is what actually
-- swaps the seat over.
CREATE TABLE IF NOT EXISTS sub_requests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id       INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    pool_id         INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    request_number  INTEGER NOT NULL,       -- 1, 2, 3 ... within the league
    user_id         INTEGER NOT NULL,       -- who asked
    display_name    TEXT NOT NULL,
    kind            TEXT NOT NULL,          -- 'out' or 'in'
    reason          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',   -- open/resolved/withdrawn
    created_at      TEXT NOT NULL,
    resolved_at     TEXT,
    UNIQUE (league_id, request_number)
);

-- Roles whose members may run moderator commands. Set per server with
-- /draft-moderator by anyone with the Administrator permission.
CREATE TABLE IF NOT EXISTS moderator_roles (
    guild_id    INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,
    added_by    INTEGER,
    created_at  TEXT NOT NULL,
    PRIMARY KEY (guild_id, role_id)
);

-- A pool plus the settings of the league it belongs to, so the draft rules
-- can read everything they need from one row.
CREATE VIEW IF NOT EXISTS pool_view AS
SELECT pools.id               AS id,
       pools.league_id        AS league_id,
       pools.label            AS label,
       pools.channel_id       AS channel_id,
       pools.status           AS status,
       pools.current_round    AS current_round,
       pools.current_turn_index AS current_turn_index,
       pools.turn_deadline    AS turn_deadline,
       pools.scheduled_start  AS scheduled_start,
       pools.queue_exhausted  AS queue_exhausted,
       pools.halved_this_turn AS halved_this_turn,
       leagues.guild_id         AS guild_id,
       leagues.name             AS league_name,
       leagues.admin_channel_id AS admin_channel_id,
       leagues.mode             AS mode,
       leagues.doc_kind         AS doc_kind,
       leagues.doc_url          AS doc_url,
       leagues.budget           AS budget,
       leagues.tera_budget      AS tera_budget,
       leagues.min_picks        AS min_picks,
       leagues.max_picks        AS max_picks,
       leagues.base_timer       AS base_timer,
       leagues.max_trades       AS max_trades,
       leagues.season_weeks     AS season_weeks,
       leagues.status           AS league_status
FROM pools JOIN leagues ON leagues.id = pools.league_id;
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
