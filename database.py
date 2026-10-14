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

import logging
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
    -- Where /postschedule posts the week's matchups. NULL means it posts
    -- wherever the command was run.
    schedule_channel_id INTEGER,
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
    -- The week /postschedule last posted (0 before the first)
    current_week      INTEGER NOT NULL DEFAULT 0,
    -- Who sees it on draft.ogreclan.com: 'public' (everyone) or 'private'
    -- (its own players, signed in with Discord)
    visibility        TEXT NOT NULL DEFAULT 'public',
    -- 1 when the league uses draft.ogreclan.com: picks and queues there,
    -- battles started with Ready on OgreClan's Showdown server, and kills and
    -- deaths recorded from them
    website           INTEGER NOT NULL DEFAULT 1,
    -- 1 for a league made on draft.ogreclan.com that lives only there: no
    -- Discord server or channels (its pools have placeholder channel IDs),
    -- so nothing is posted to Discord; the bot still runs its rules and timers
    site_only         INTEGER NOT NULL DEFAULT 0,
    -- The Discord user who made it (with /setup or on draft.ogreclan.com)
    created_by        INTEGER,
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
    -- Unix time the free-trade window after the draft closes (NULL: none)
    free_trades_until   REAL,
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
    -- An approved trade changes rosters when the next week is posted with
    -- /postschedule; until then these are empty.
    applied_at    TEXT,
    applied_week  INTEGER,
    -- 1 when proposed in the free-trade window after the draft: it doesn't
    -- count toward the league's trade limit
    free          INTEGER NOT NULL DEFAULT 0,
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

-- Extension requests: a player asking for their week's match to be played
-- next week instead. Granting one makes /postschedule list it again in the
-- following week. A match may only be extended once (config.MAX_EXTENSION_WEEKS).
CREATE TABLE IF NOT EXISTS extensions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id      INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    pool_id        INTEGER NOT NULL REFERENCES pools(id) ON DELETE CASCADE,
    number         INTEGER NOT NULL,       -- 1, 2, 3 ... within the league
    week           INTEGER NOT NULL,       -- the week being pushed back
    home_player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    away_player_id INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    requested_by   INTEGER NOT NULL REFERENCES players(id) ON DELETE CASCADE,
    reason         TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',   -- pending/granted/rejected
    created_at     TEXT NOT NULL,
    resolved_at    TEXT,
    resolved_by    INTEGER,
    UNIQUE (league_id, week, home_player_id, away_player_id)
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

-- Match results (results.py): each week's match, its games, and each
-- Pokémon's kills and deaths, from battles on OgreClan's Showdown server,
-- replay links, or players' own reports (and whether it was played on
-- Pokémon Champions, with each player's Pokepaste).
CREATE TABLE IF NOT EXISTS match_reports (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    league_id              INTEGER NOT NULL REFERENCES leagues(id) ON DELETE CASCADE,
    week                   INTEGER NOT NULL,
    match_number           INTEGER NOT NULL,
    -- 1 once both players have agreed to play it on Pokémon Champions
    champions              INTEGER NOT NULL DEFAULT 0,
    -- the player IDs (comma separated) who have asked for Champions
    champions_requested_by TEXT NOT NULL DEFAULT '',
    -- whoever has won more games (a player ID), or NULL while it's level
    winner_id              INTEGER,
    updated_at             TEXT NOT NULL,
    UNIQUE (league_id, week, match_number)
);

CREATE TABLE IF NOT EXISTS game_results (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id   INTEGER NOT NULL REFERENCES match_reports(id) ON DELETE CASCADE,
    game        INTEGER NOT NULL,
    winner_id   INTEGER,
    -- the game's replay on replay.ogreclan.com, if there is one
    replay      TEXT,
    -- 'battle' (played with Ready), 'replay' or 'manual'
    source      TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE (report_id, game)
);

CREATE TABLE IF NOT EXISTS game_stats (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id   INTEGER NOT NULL REFERENCES game_results(id) ON DELETE CASCADE,
    player_id   INTEGER NOT NULL,
    pokemon     TEXT NOT NULL,
    kills       INTEGER NOT NULL DEFAULT 0,
    deaths      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (result_id, player_id, pokemon)
);

CREATE TABLE IF NOT EXISTS pokepastes (
    report_id   INTEGER NOT NULL REFERENCES match_reports(id) ON DELETE CASCADE,
    player_id   INTEGER NOT NULL,
    url         TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (report_id, player_id)
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
       pools.free_trades_until AS free_trades_until,
       leagues.guild_id         AS guild_id,
       leagues.name             AS league_name,
       leagues.admin_channel_id AS admin_channel_id,
       leagues.schedule_channel_id AS schedule_channel_id,
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
       leagues.status           AS league_status,
       leagues.website          AS website,
       leagues.site_only        AS site_only
FROM pools JOIN leagues ON leagues.id = pools.league_id;
"""


# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` does
# nothing to a table that already exists, so an existing database needs them
# adding by hand - otherwise an upgrade breaks every command that touches
# the new column. Append to this list whenever you add one; it is applied on
# every connect and is safe to run repeatedly.
ADDED_COLUMNS = [
    ("leagues", "season_weeks", "INTEGER NOT NULL DEFAULT 0"),
    ("leagues", "schedule_channel_id", "INTEGER"),
    ("players", "status", "TEXT NOT NULL DEFAULT 'active'"),
    ("players", "tera_budget_remaining", "INTEGER NOT NULL DEFAULT 0"),
    ("pools", "scheduled_start", "REAL"),
    ("picks", "tera_captain", "INTEGER NOT NULL DEFAULT 0"),
    ("rosters", "tera_captain", "INTEGER NOT NULL DEFAULT 0"),
    ("queued_picks", "tera_captain", "INTEGER NOT NULL DEFAULT 0"),
    ("makeups", "created_turn", "INTEGER NOT NULL DEFAULT 0"),
    ("leagues", "current_week", "INTEGER NOT NULL DEFAULT 0"),
    ("trades", "applied_at", "TEXT"),
    ("trades", "applied_week", "INTEGER"),
    ("pools", "free_trades_until", "REAL"),
    ("trades", "free", "INTEGER NOT NULL DEFAULT 0"),
    ("leagues", "visibility", "TEXT NOT NULL DEFAULT 'public'"),
    ("leagues", "website", "INTEGER NOT NULL DEFAULT 1"),
    ("leagues", "site_only", "INTEGER NOT NULL DEFAULT 0"),
    ("leagues", "created_by", "INTEGER"),
]

# Run once, when the column is added to an existing database. Trades approved
# before approval and application were separate were applied straight away.
BACKFILLS = {
    ("trades", "applied_at"):
        "UPDATE trades SET applied_at = COALESCE(resolved_at, created_at) WHERE status = 'approved'",
}

# Views are rebuilt from scratch on every connect. `CREATE VIEW IF NOT
# EXISTS` would keep a stale definition that is missing newly added columns.
VIEWS = ["pool_view"]


def migrate(conn):
    """Bring an existing database up to the current schema.

    Deliberately simple: add any missing columns, then rebuild the views.
    Both steps are safe to repeat, so this just runs on every start rather
    than tracking version numbers.
    """
    added = []
    for table, column, definition in ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue                    # the table itself is new; SCHEMA made it
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            if (table, column) in BACKFILLS:
                conn.execute(BACKFILLS[(table, column)])
            added.append(f"{table}.{column}")

    for view in VIEWS:
        conn.execute(f"DROP VIEW IF EXISTS {view}")

    conn.commit()
    return added


def connect(path=None):
    """Open the database, bring the schema up to date, and return it."""
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
    # Create anything missing, add columns an older database lacks, then
    # create the views again now that every column exists.
    conn.executescript(SCHEMA)
    added = migrate(conn)
    conn.executescript(SCHEMA)
    conn.commit()

    if added:
        logging.getLogger("draftbot.database").info(
            "database upgraded, added: %s", ", ".join(added))
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
