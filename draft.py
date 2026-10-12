"""
All of the draft rules live here.

Nothing in this file imports discord. That is deliberate: every rule can be
tested from a plain Python script (see tests/test_draft.py), and the Discord
layer stays a thin wrapper that turns the "events" returned below into chat
messages.

Functions raise DraftError with a ready-to-show message when something is
not allowed. Because the check happens before any INSERT, a rejected command
can never leave a half-finished change in the database.
"""

import json
import random
import re

import config
import database


class DraftError(Exception):
    """A rule was broken. The message is shown to the user as-is."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------


def normalize(name):
    """Lowercase and collapse whitespace so 'Rotom-Wash ' == 'rotom-wash'."""
    return " ".join(str(name).strip().lower().split())


def snake_order(player_count, round_number):
    """Seat numbers (1-based) in the order they pick during this round.

    Odd rounds run forwards, even rounds run backwards. That is the whole
    snake draft:

        Round 1: 1 2 3 ... 15 16
        Round 2: 16 15 14 ... 2 1
        Round 3: 1 2 3 ... 15 16
    """
    seats = list(range(1, player_count + 1))
    return seats if round_number % 2 == 1 else seats[::-1]


def parse_pokemon_list(raw, limit):
    """Split a comma separated list into clean names, rejecting duplicates."""
    names = [part.strip() for part in str(raw).split(",")]
    names = [n for n in names if n]
    if not names:
        raise DraftError("❌ You need to name at least one Pokémon.")
    if len(names) > limit:
        raise DraftError(f"❌ You may list at most {limit} Pokémon.")

    seen = set()
    for name in names:
        key = normalize(name)
        if key in seen:
            raise DraftError(f"❌ **{name}** is listed twice.")
        seen.add(key)
    return names


# --------------------------------------------------------------------------
# Reading drafts and players
# --------------------------------------------------------------------------


def get_live_pool(conn, channel_id):
    """The unfinished draft in this channel, or None.

    Every command starts here. Because the lookup is by channel id, a
    command in #vgc can never touch the draft in #national-dex-singles.
    """
    return conn.execute(
        "SELECT * FROM pool_view WHERE channel_id = ? AND status IN ('setup', 'active')",
        (channel_id,),
    ).fetchone()


def get_pool(conn, pool_id):
    return conn.execute("SELECT * FROM pool_view WHERE id = ?", (pool_id,)).fetchone()


def get_channel_pool(conn, channel_id):
    """The channel's live draft, or its most recent finished one.

    Used by commands that still make sense after the final round - reading
    rosters and history, correcting a pick, and trading during the season.
    """
    live = get_live_pool(conn, channel_id)
    if live is not None:
        return live
    return conn.execute(
        "SELECT * FROM pool_view WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
        (channel_id,),
    ).fetchone()


def require_channel_pool(conn, channel_id):
    draft = get_channel_pool(conn, channel_id)
    if draft is None:
        raise DraftError("❌ This channel has never had a draft. An admin can create one with `/setup`.")
    return draft


def require_live_pool(conn, channel_id):
    draft = get_live_pool(conn, channel_id)
    if draft is None:
        raise DraftError("❌ There is no draft in this channel. An admin can create one with `/setup`.")
    return draft


def require_active_pool(conn, channel_id):
    draft = require_live_pool(conn, channel_id)
    if draft["status"] != "active":
        raise DraftError("❌ This draft has not started yet. An admin must run `/draft` first.")
    return draft


def get_players(conn, pool_id):
    """Every player in seat order."""
    return conn.execute(
        "SELECT * FROM players WHERE pool_id = ? ORDER BY draft_position",
        (pool_id,),
    ).fetchall()


def get_player(conn, pool_id, user_id):
    return conn.execute(
        "SELECT * FROM players WHERE pool_id = ? AND user_id = ?",
        (pool_id, user_id),
    ).fetchone()


def get_player_by_id(conn, player_id):
    return conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()


def require_player(conn, pool_id, user_id):
    player = get_player(conn, pool_id, user_id)
    if player is None:
        raise DraftError("❌ You are not a player in this channel's draft.")
    return player


def player_count(conn, pool_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM players WHERE pool_id = ?", (pool_id,)
    ).fetchone()["n"]


def current_seat(conn, draft):
    """Which seat number is on the clock right now."""
    order = snake_order(player_count(conn, draft["id"]), draft["current_round"])
    return order[draft["current_turn_index"]]


def current_player(conn, draft):
    """The player whose turn it is, or None if the draft is not running."""
    if draft["status"] != "active":
        return None
    seat = current_seat(conn, draft)
    return conn.execute(
        "SELECT * FROM players WHERE pool_id = ? AND draft_position = ?",
        (draft["id"], seat),
    ).fetchone()


def turn_is_upcoming(conn, draft, player, round_number):
    """True if this player's turn in `round_number` has not happened yet."""
    if round_number > draft["current_round"]:
        return True
    if round_number < draft["current_round"]:
        return False
    order = snake_order(player_count(conn, draft["id"]), round_number)
    return order.index(player["draft_position"]) >= draft["current_turn_index"]


# --------------------------------------------------------------------------
# Availability, budgets and pick counts
# --------------------------------------------------------------------------


def find_owner(conn, pool_id, pokemon_key):
    """Roster row for a Pokemon, or None when it is still available.

    Availability is based on the roster rather than the pick history so
    that a Pokemon dropped in a trade correctly returns to the pool.
    """
    return conn.execute(
        "SELECT * FROM rosters WHERE pool_id = ? AND pokemon_key = ?",
        (pool_id, pokemon_key),
    ).fetchone()


def drafted_keys(conn, pool_id):
    """Normalised names of every Pokemon currently on somebody's roster."""
    rows = conn.execute(
        "SELECT pokemon_key FROM rosters WHERE pool_id = ?", (pool_id,)
    ).fetchall()
    return {row["pokemon_key"] for row in rows}


def tera_captains(conn, pool_id, player_id):
    """How many Tera Captains this team currently has."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM rosters WHERE pool_id = ? AND player_id = ? "
        "AND tera_captain = 1",
        (pool_id, player_id),
    ).fetchone()["n"]


def picks_made(conn, pool_id, player_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM picks WHERE pool_id = ? AND player_id = ?",
        (pool_id, player_id),
    ).fetchone()["n"]


def roster_size(conn, pool_id, player_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM rosters WHERE pool_id = ? AND player_id = ?",
        (pool_id, player_id),
    ).fetchone()["n"]


def next_pick_order(conn, pool_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(pick_order), 0) + 1 AS n FROM picks WHERE pool_id = ?",
        (pool_id,),
    ).fetchone()
    return row["n"]


def player_can_pick(conn, draft, player):
    """False when a player can no longer take any pick at all.

    They have withdrawn, filled their roster, or cannot afford even the
    cheapest possible Pokemon. When this is true of everybody, the draft is
    over even if there are rounds left.
    """
    if player["status"] != "active":
        return False
    if picks_made(conn, draft["id"], player["id"]) >= draft["max_picks"]:
        return False
    return player["budget_remaining"] >= config.MINIMUM_PICK_COST


def anyone_can_pick(conn, draft):
    return any(player_can_pick(conn, draft, player)
               for player in get_players(conn, draft["id"]))


def turn_number(conn, draft):
    """A single number counting turns from the start of the draft.

    Round 1 seat 1 is turn 1. Used to tell whether two turns were back to
    back, which happens to one player at every snake round boundary.
    """
    total = player_count(conn, draft["id"])
    return (draft["current_round"] - 1) * total + draft["current_turn_index"] + 1


def validate_pick(conn, draft, player, round_number, pokemon, cost,
                  tera_captain=False, replacing_key=None):
    """Check every rule for a pick. Raises DraftError on the first problem.

    `replacing_key` is the Pokemon being swapped out by /editpick, which is
    allowed to "occupy" its own slot without counting as a duplicate.

    A Tera Captain is charged `cost` - its Tera Tax - against the normal
    budget AND the Tera budget, so both have to cover it.
    """
    name = str(pokemon).strip()
    if not name:
        raise DraftError("❌ You need to name a Pokémon.")
    if len(name) > 60:
        raise DraftError("❌ That Pokémon name is too long.")

    if not isinstance(cost, int):
        raise DraftError("❌ The cost must be a whole number.")
    if cost < 0:
        raise DraftError("❌ The cost cannot be negative.")

    if round_number < 1 or round_number > draft["max_picks"]:
        raise DraftError(f"❌ This draft only has rounds 1–{draft['max_picks']}.")

    key = normalize(name)
    owner = find_owner(conn, draft["id"], key)
    if owner is not None and key != replacing_key:
        owner_player = get_player_by_id(conn, owner["player_id"])
        raise DraftError(
            f"❌ **{name}** has already been drafted by {owner_player['display_name']}."
        )

    # Budget. When editing, the old cost has already been refunded by the
    # caller, so budget_remaining is always "what is available right now".
    if cost > player["budget_remaining"]:
        raise DraftError(
            f"❌ You do not have enough budget remaining. "
            f"**{name}** costs {cost} but you only have {player['budget_remaining']} left."
        )

    if tera_captain:
        if not draft["tera_budget"]:
            raise DraftError("❌ This draft is not a Tera format, so there are no Tera Captains.")

        if cost > player["tera_budget_remaining"]:
            raise DraftError(
                f"❌ You do not have enough Tera budget remaining. Making **{name}** a Tera "
                f"Captain costs {cost} but you only have "
                f"{player['tera_budget_remaining']} Tera points left."
            )

        already = tera_captains(conn, draft["id"], player["id"])
        if replacing_key is not None:
            # An edit that keeps a captain in place must not count it twice.
            current = conn.execute(
                "SELECT tera_captain FROM rosters WHERE pool_id = ? AND pokemon_key = ?",
                (draft["id"], replacing_key),
            ).fetchone()
            if current is not None and current["tera_captain"]:
                already -= 1
        if already >= config.MAX_TERA_CAPTAINS:
            raise DraftError(
                f"❌ You already have the maximum of {config.MAX_TERA_CAPTAINS} Tera Captains."
            )

    if picks_made(conn, draft["id"], player["id"]) >= draft["max_picks"]:
        raise DraftError(
            f"❌ You have already made the maximum of {draft['max_picks']} picks."
        )

    if config.ENFORCE_MIN_PICK_BUDGET:
        picks_after = picks_made(conn, draft["id"], player["id"]) + 1
        still_needed = max(0, draft["min_picks"] - picks_after)
        if player["budget_remaining"] - cost < still_needed:
            raise DraftError(
                f"❌ Spending {cost} would leave you unable to afford your "
                f"remaining {still_needed} required pick(s)."
            )


# --------------------------------------------------------------------------
# Creating and configuring a draft
# --------------------------------------------------------------------------


PLACEHOLDER_COACH = re.compile(r"^coach\s*\d+$")


def is_placeholder_coach(name):
    """True for a name the template ships with, like "Coach 7", or a blank.

    Those rows are unclaimed, so the bot may write a player into them. A row
    with a real username belongs to that person and is matched, never
    overwritten.
    """
    if name is None:
        return True
    text = normalize(name)
    return not text or bool(PLACEHOLDER_COACH.match(text))


def match_coaches(players, slots, sheet_names):
    """Match players to the Coaches rows that name them.

    `players` is [{user_id, display_name, username}, ...]; `slots` is the
    ticked coach numbers for this pool, in order; `sheet_names` maps a coach
    number to the name currently in that row.

    Two passes. First, any row holding a real username is given to the
    player whose Discord username or display name matches it - so the order
    somebody was @mentioned in does not have to match the spreadsheet.
    Then any player still unmatched fills the leftover rows in order, which
    is what happens with a fresh template full of "Coach 1" placeholders.

    Returns (assignment, to_write, unclaimed):
      assignment - {coach number: player}
      to_write   - coach numbers whose name the bot should fill in, because
                   the row was a placeholder
      unclaimed  - rows holding a real name that no listed player matched
    """
    remaining = list(players)
    assignment = {}

    for slot in slots:
        wanted = normalize(sheet_names.get(slot, ""))
        if is_placeholder_coach(wanted):
            continue
        for player in remaining:
            if wanted in (normalize(player.get("username")),
                          normalize(player.get("display_name"))):
                assignment[slot] = player
                remaining.remove(player)
                break

    # Only unclaimed rows may be filled. A row naming a real person who was
    # simply not listed must never be handed to somebody else - that would
    # put one player's roster in another player's block.
    to_write = []
    free = [slot for slot in slots
            if slot not in assignment
            and is_placeholder_coach(sheet_names.get(slot, ""))]
    for slot, player in zip(free, list(remaining)):
        assignment[slot] = player
        remaining.remove(player)
        to_write.append(slot)

    unclaimed = [slot for slot in slots
                 if slot not in assignment
                 and not is_placeholder_coach(sheet_names.get(slot, ""))]

    return assignment, to_write, unclaimed, remaining


def coach_numbers_for(label, count):
    """Which Coaches rows / roster blocks a pool's seats occupy.

    Pool A takes blocks 1 upwards, pool B starts at 11. That keeps each
    pool under the matching "Pool A" / "Pool B" heading on the Rosters tab,
    which is why a two-pool league is capped at ten coaches per pool.
    """
    first = 1 if label == "A" else config.POOL_B_FIRST_COACH
    return list(range(first, first + count))


def max_players_per_pool(doc_kind, pool_count):
    """How many coaches a pool may hold.

    Only a two-pool league is tight: pool B's blocks start at 11, so pool A
    may not run past 10. One pool can use all of them.
    """
    if doc_kind == "template" and pool_count == 2:
        return config.POOL_B_FIRST_COACH - 1
    return config.MAX_PLAYERS


def _roster_entries(entries):
    """Normalise a pool's player list to [(user_id, display_name), ...].

    Accepts plain tuples or the richer dicts the Discord layer builds, which
    also carry the account's username for matching against the sheet.
    """
    rows = []
    for entry in entries:
        if isinstance(entry, dict):
            rows.append((entry["user_id"], entry["display_name"]))
        else:
            rows.append((entry[0], entry[1]))
    return rows


def create_league(conn, guild_id, admin_channel_id, name, pools,
                  budget, min_picks, max_picks, base_timer,
                  mode="full_season", doc_kind="none", doc_url=None,
                  tera_budget=0, max_trades=0, season_weeks=0,
                  coach_slots=None):
    """Create a league in `setup` state, with one or two pools.

    `pools` is [(label, channel_id, [(user_id, display_name), ...]), ...],
    pool A first. Everything is validated before the first INSERT, so a bad
    /setup leaves the database untouched.
    """
    name = " ".join(str(name or "").split())
    if not name:
        raise DraftError("❌ The draft needs a name.")
    if len(name) > 80:
        raise DraftError("❌ That name is too long (80 characters maximum).")
    if get_live_league_by_name(conn, guild_id, name) is not None:
        raise DraftError(
            f"❌ A draft called **{name}** is already running on this server. "
            f"Names have to be unique so commands can tell them apart."
        )

    if mode not in config.LEAGUE_MODES:
        raise DraftError(
            f"❌ The mode must be one of: {', '.join(config.LEAGUE_MODES)}."
        )
    if doc_kind not in ("none", "tiering", "template"):
        raise DraftError("❌ Unknown document type.")

    if budget <= 0:
        raise DraftError("❌ The starting budget must be greater than 0.")
    if tera_budget < 0:
        raise DraftError("❌ The Tera budget cannot be negative (use 0 for no Tera format).")
    if min_picks < 1:
        raise DraftError("❌ The minimum number of picks must be at least 1.")
    if max_picks < min_picks:
        raise DraftError("❌ The maximum number of picks cannot be less than the minimum.")
    if not config.MIN_BASE_TIMER_SECONDS <= base_timer <= config.MAX_BASE_TIMER_SECONDS:
        raise DraftError(
            f"❌ The base timer must be between {config.MIN_BASE_TIMER_SECONDS} and "
            f"{config.MAX_BASE_TIMER_SECONDS} seconds."
        )
    if max_trades < 0:
        raise DraftError("❌ The maximum number of trades cannot be negative (0 = unlimited).")

    if not pools:
        raise DraftError("❌ A league needs at least one pool.")
    if len(pools) > 2:
        raise DraftError("❌ A league can have at most two pools.")
    if len(pools) == 2 and doc_kind != "template":
        raise DraftError(
            "❌ Two pools need a template document — the pool assignment lives on its "
            "Coaches tab. Link a template, or set up a single pool."
        )

    if doc_kind == "template":
        rows = config.SHEET_LAYOUT["roster_rows"]
        if max_picks > rows:
            raise DraftError(
                f"❌ The template gives each coach {rows} roster rows, so a draft linked "
                f"to it can have at most {rows} rounds (you asked for {max_picks})."
            )

    seen_channels = {admin_channel_id}
    everyone = []
    limit = max_players_per_pool(doc_kind, len(pools))
    coach_slots = coach_slots or {}

    pools = [(label, channel_id, _roster_entries(roster))
             for label, channel_id, roster in pools]

    for label, channel_id, roster in pools:
        if channel_id in seen_channels:
            raise DraftError(
                "❌ Each pool needs its own channel, and none of them may be the admin "
                "channel you ran `/setup` in."
            )
        seen_channels.add(channel_id)

        if get_live_pool(conn, channel_id) is not None:
            raise DraftError(
                f"❌ <#{channel_id}> already has a draft running. Finish or cancel it first."
            )
        seats = coach_slots.get(label)
        pool_limit = len(seats) if seats else limit
        if len(roster) > pool_limit:
            where = (f"the template only has {pool_limit} ticked coach row(s) for it"
                     if seats else f"a pool holds at most {pool_limit} players")
            raise DraftError(
                f"❌ Pool {label} has {len(roster)} players but {where}."
            )
        if len(roster) < config.MIN_PLAYERS:
            raise DraftError(
                f"❌ Pool {label} needs at least {config.MIN_PLAYERS} players "
                f"(you listed {len(roster)})."
            )
        everyone += [user_id for user_id, _ in roster]

    if len(set(everyone)) != len(everyone):
        raise DraftError("❌ The same player was listed twice.")

    cursor = conn.execute(
        """INSERT INTO leagues (guild_id, name, admin_channel_id, mode, doc_kind, doc_url,
                                budget, tera_budget, min_picks, max_picks, base_timer,
                                max_trades, season_weeks, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'setup', ?)""",
        (guild_id, name, admin_channel_id, mode, doc_kind, doc_url, budget, tera_budget,
         min_picks, max_picks, base_timer, max_trades, max(0, season_weeks),
         database.now()),
    )
    league_id = cursor.lastrowid

    for label, channel_id, roster in pools:
        pool_cursor = conn.execute(
            "INSERT INTO pools (league_id, label, channel_id, status) VALUES (?, ?, ?, 'setup')",
            (league_id, label, channel_id),
        )
        pool_id = pool_cursor.lastrowid
        # Which Coaches rows / roster blocks this pool owns. With a template
        # these come from its ticked "In schedule" boxes, so gaps are kept.
        coaches = coach_slots.get(label) or coach_numbers_for(label, len(roster))
        coaches = list(coaches)[:len(roster)]

        for seat, ((user_id, display_name), coach_number) in enumerate(
                zip(roster, coaches), start=1):
            conn.execute(
                """INSERT INTO players (pool_id, user_id, display_name, draft_position,
                                        coach_number, budget_remaining,
                                        tera_budget_remaining, current_timer)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (pool_id, user_id, display_name, seat, coach_number,
                 budget, tera_budget, base_timer),
            )

    conn.commit()
    return league_id


# --------------------------------------------------------------------------
# Finding leagues and pools
# --------------------------------------------------------------------------


def get_league(conn, league_id):
    return conn.execute("SELECT * FROM leagues WHERE id = ?", (league_id,)).fetchone()


def get_live_league_by_name(conn, guild_id, name):
    """An unfinished league on this server with this name. Case-insensitive."""
    return conn.execute(
        "SELECT * FROM leagues WHERE guild_id = ? AND lower(name) = lower(?) "
        "AND status IN ('setup', 'active')",
        (guild_id, " ".join(str(name or "").split())),
    ).fetchone()


def require_league_by_name(conn, guild_id, name):
    league = get_live_league_by_name(conn, guild_id, name)
    if league is None:
        running = [row["name"] for row in conn.execute(
            "SELECT name FROM leagues WHERE guild_id = ? AND status IN ('setup','active') "
            "ORDER BY name", (guild_id,))]
        hint = f" Running here: {', '.join(running)}." if running else ""
        raise DraftError(f"❌ There is no draft called **{name}** on this server.{hint}")
    return league


def get_league_pools(conn, league_id):
    return conn.execute(
        "SELECT * FROM pool_view WHERE league_id = ? ORDER BY label", (league_id,)
    ).fetchall()


def leagues_in_admin_channel(conn, channel_id):
    """Unfinished leagues whose trades are approved in this channel."""
    return conn.execute(
        "SELECT * FROM leagues WHERE admin_channel_id = ? AND status IN ('setup', 'active') "
        "ORDER BY name",
        (channel_id,),
    ).fetchall()


def update_settings(conn, ctx, budget=None, min_picks=None, max_picks=None, doc_url=None,
                    tera_budget=None, max_trades=None):
    """/edit - only legal while the league is still in `setup`."""
    league = get_league(conn, ctx["league_id"])
    if league["status"] != "setup":
        raise DraftError("❌ The draft has already started, so its settings can no longer be edited.")

    new_budget = league["budget"] if budget is None else budget
    new_min = league["min_picks"] if min_picks is None else min_picks
    new_max = league["max_picks"] if max_picks is None else max_picks
    new_tera = league["tera_budget"] if tera_budget is None else tera_budget
    new_trades = league["max_trades"] if max_trades is None else max_trades
    new_doc = league["doc_url"] if doc_url is None else doc_url

    if new_budget <= 0:
        raise DraftError("❌ The starting budget must be greater than 0.")
    if new_min < 1:
        raise DraftError("❌ The minimum number of picks must be at least 1.")
    if new_max < new_min:
        raise DraftError("❌ The maximum number of picks cannot be less than the minimum.")
    if new_tera < 0:
        raise DraftError("❌ The Tera budget cannot be negative (use 0 for no Tera format).")
    if new_trades < 0:
        raise DraftError("❌ The maximum number of trades cannot be negative (0 = unlimited).")
    if league["doc_kind"] == "template" and new_max > config.SHEET_LAYOUT["roster_rows"]:
        raise DraftError(
            f"❌ The template only has {config.SHEET_LAYOUT['roster_rows']} roster rows per coach."
        )

    conn.execute(
        """UPDATE leagues SET budget = ?, min_picks = ?, max_picks = ?, tera_budget = ?,
                              max_trades = ?, doc_url = ? WHERE id = ?""",
        (new_budget, new_min, new_max, new_tera, new_trades, new_doc, league["id"]),
    )
    # No picks exist yet, so every player simply gets the new budgets.
    conn.execute(
        """UPDATE players SET budget_remaining = ?, tera_budget_remaining = ?
           WHERE pool_id IN (SELECT id FROM pools WHERE league_id = ?)""",
        (new_budget, new_tera, league["id"]),
    )
    conn.commit()
    return get_league(conn, league["id"])


def cancel_league(conn, league_id):
    """Abandon a league and every pool in it. Nothing is deleted."""
    stamp = database.now()
    conn.execute(
        "UPDATE pools SET status = 'cancelled', finished_at = ?, turn_deadline = NULL "
        "WHERE league_id = ? AND status IN ('setup', 'active')",
        (stamp, league_id),
    )
    conn.execute("UPDATE leagues SET status = 'cancelled', finished_at = ? WHERE id = ?",
                 (stamp, league_id))
    conn.commit()


def archive_league(conn, league_id):
    """Close a finished league so its channels are free for a new /setup.

    Used by `draft_only` leagues once the last round is done, and available
    to a moderator afterwards. History is kept, only the status changes.
    """
    stamp = database.now()
    conn.execute(
        "UPDATE pools SET status = 'complete', finished_at = COALESCE(finished_at, ?), "
        "turn_deadline = NULL WHERE league_id = ? AND status IN ('setup', 'active')",
        (stamp, league_id),
    )
    conn.execute("UPDATE leagues SET status = 'archived', finished_at = COALESCE(finished_at, ?) "
                 "WHERE id = ?", (stamp, league_id))
    conn.commit()


# --------------------------------------------------------------------------
# Moderator roles (set per server with /draft-moderator)
# --------------------------------------------------------------------------


def add_moderator_role(conn, guild_id, role_id, added_by):
    conn.execute(
        "INSERT OR IGNORE INTO moderator_roles (guild_id, role_id, added_by, created_at) "
        "VALUES (?, ?, ?, ?)",
        (guild_id, role_id, added_by, database.now()),
    )
    conn.commit()


def remove_moderator_role(conn, guild_id, role_id):
    cursor = conn.execute(
        "DELETE FROM moderator_roles WHERE guild_id = ? AND role_id = ?", (guild_id, role_id)
    )
    conn.commit()
    return cursor.rowcount > 0


def moderator_role_ids(conn, guild_id):
    return {row["role_id"] for row in conn.execute(
        "SELECT role_id FROM moderator_roles WHERE guild_id = ?", (guild_id,))}


# --------------------------------------------------------------------------
# Running the draft
#
# Every function that changes whose turn it is returns a list of "events".
# An event is a plain dict such as
#     {"type": "skipped", "player_id": 3, "round": 2, ...}
# The Discord layer walks the list and posts one message per event.
# --------------------------------------------------------------------------


def _event(kind, player=None, **fields):
    event = {"type": kind}
    if player is not None:
        event["player_id"] = player["id"]
        event["user_id"] = player["user_id"]
        event["display_name"] = player["display_name"]
        event["position"] = player["draft_position"]
        # Which Coaches row / roster block this player owns on the sheet.
        event["coach_number"] = player["coach_number"]
    event.update(fields)
    return event


def start_pool(conn, draft):
    """/draft - flip the draft to active and open round 1."""
    if draft["status"] == "active":
        raise DraftError("❌ This draft is already running.")
    if draft["status"] != "setup":
        raise DraftError("❌ This draft has already finished.")
    if player_count(conn, draft["id"]) < config.MIN_PLAYERS:
        raise DraftError(f"❌ A draft needs at least {config.MIN_PLAYERS} players.")

    stamp = database.now()
    conn.execute(
        """UPDATE pools SET status = 'active', current_round = 1, current_turn_index = 0,
                            queue_exhausted = 0, halved_this_turn = 0, started_at = ?
           WHERE id = ?""",
        (stamp, draft["id"]),
    )
    conn.execute(
        "UPDATE leagues SET status = 'active', started_at = COALESCE(started_at, ?) "
        "WHERE id = ? AND status = 'setup'",
        (stamp, draft["league_id"]),
    )
    conn.commit()
    return begin_turn(conn, draft["id"])


def advance_turn(conn, pool_id):
    """Move the pointer to the next seat, rolling into the next round.

    Also finishes the draft when the last round is done, or when no player
    can afford another pick.
    """
    draft = get_pool(conn, pool_id)
    total = player_count(conn, pool_id)

    index = draft["current_turn_index"] + 1
    round_number = draft["current_round"]

    if index >= total:            # the round is over
        index = 0
        round_number += 1

    # The draft also ends early if nobody can afford anything any more.
    if round_number > draft["max_picks"] or not anyone_can_pick(conn, draft):
        conn.execute(
            """UPDATE pools SET status = 'complete', finished_at = ?, turn_deadline = NULL,
                                queue_exhausted = 0, halved_this_turn = 0 WHERE id = ?""",
            (database.now(), pool_id),
        )
        conn.commit()
        _finish_league_if_done(conn, draft["league_id"])
        return

    conn.execute(
        """UPDATE pools SET current_round = ?, current_turn_index = ?, turn_deadline = NULL,
                             queue_exhausted = 0, halved_this_turn = 0 WHERE id = ?""",
        (round_number, index, pool_id),
    )
    conn.commit()


def _finish_league_if_done(conn, league_id):
    """Close the league once every pool has finished drafting.

    A `draft_only` league is archived straight away, which frees its
    channels for a new /setup. History is kept either way.
    """
    unfinished = conn.execute(
        "SELECT COUNT(*) AS n FROM pools WHERE league_id = ? AND status IN ('setup', 'active')",
        (league_id,),
    ).fetchone()["n"]
    if unfinished:
        return False

    league = get_league(conn, league_id)
    if league is None or league["status"] not in ("setup", "active"):
        return False

    status = "archived" if league["mode"] == "draft_only" else "complete"
    conn.execute("UPDATE leagues SET status = ?, finished_at = ? WHERE id = ?",
                 (status, database.now(), league_id))
    conn.commit()
    return True


def _halve_timer(conn, pool_id, player):
    """Divide this player's personal timer, respecting the configured floor.

    A turn may only cost a player one reduction. Without this guard a player
    who lets a makeup lapse AND then misses the new round would be punished
    twice for what is really one absence: 120 -> 60 -> 30 in a single turn.

    Returns (timer, was_reduced).
    """
    draft = get_pool(conn, pool_id)
    if draft["halved_this_turn"]:
        return player["current_timer"], False

    reduced = max(config.MINIMUM_TIMER_SECONDS,
                  player["current_timer"] // config.TIMER_DIVISOR)
    conn.execute(
        "UPDATE players SET current_timer = ?, skip_count = skip_count + 1 WHERE id = ?",
        (reduced, player["id"]),
    )
    conn.execute("UPDATE pools SET halved_this_turn = 1 WHERE id = ?", (pool_id,))
    return reduced, True


def _expire_stale_makeups(conn, draft, player):
    """Expire makeup picks whose deadline has passed. Called as a turn opens.

    A makeup owed for round R must be used before the player's turn in round
    R + 1. Once that turn arrives the makeup is dead, and the caller skips
    that turn too.

    Each event carries `back_to_back`, which is true when the deadline turn
    followed the skip immediately. That happens to one player at every snake
    round boundary - seat 16 picks last in round 1 and first in round 2 - so
    they never had a moment to use the makeup. Both rounds are still lost,
    but the caller does not halve their timer a second time for it.
    """
    events = []
    stale = conn.execute(
        """SELECT * FROM makeups
           WHERE pool_id = ? AND player_id = ? AND status = 'pending' AND round <= ?
           ORDER BY round""",
        (draft["id"], player["id"], draft["current_round"] - 1),
    ).fetchall()

    now_turn = turn_number(conn, draft)
    for makeup in stale:
        back_to_back = now_turn - makeup["created_turn"] <= 1
        conn.execute(
            "UPDATE makeups SET status = 'expired', resolved_at = ? WHERE id = ?",
            (database.now(), makeup["id"]),
        )
        conn.execute(
            "INSERT INTO skips (pool_id, player_id, round, reason, halved, created_at) "
            "VALUES (?, ?, ?, 'makeup deadline passed', 0, ?)",
            (draft["id"], player["id"], makeup["round"], database.now()),
        )
        conn.commit()
        events.append(_event("makeup_expired", get_player_by_id(conn, player["id"]),
                             round=makeup["round"], back_to_back=back_to_back))
    return events


def _try_queue(conn, draft, player):
    """Use the player's queue for this round if any entry is still legal.

    Returns (pick_row, had_queue). pick_row is None when nothing was usable.
    """
    queued = conn.execute(
        """SELECT * FROM queued_picks
           WHERE pool_id = ? AND player_id = ? AND round = ?
           ORDER BY priority""",
        (draft["id"], player["id"], draft["current_round"]),
    ).fetchall()

    if not queued:
        return None, False

    for entry in queued:
        cost = entry["cost"] if entry["cost"] is not None else 0
        tera = bool(entry["tera_captain"])
        try:
            validate_pick(conn, draft, player, draft["current_round"], entry["pokemon"],
                          cost, tera_captain=tera)
        except DraftError:
            continue        # already taken, too expensive, ... try the next one
        pick = _record_pick(conn, draft, player, draft["current_round"],
                            entry["pokemon"], cost, tera_captain=tera,
                            is_makeup=False, is_auto=True)
        return pick, True

    return None, True


def begin_turn(conn, pool_id):
    """Open the current turn, resolving everything that happens automatically.

    Loops because one turn can lead straight into the next: a queued pick,
    a player who can no longer afford anything, or a lapsed makeup deadline
    all move the draft on without anybody typing a command.
    """
    events = []
    guard = 0

    while True:
        guard += 1
        if guard > 1000:            # impossible in practice; stops a runaway loop
            break

        draft = get_pool(conn, pool_id)
        if draft["status"] != "active":
            if draft["status"] == "complete":
                league = get_league(conn, draft["league_id"])
                events.append(_event(
                    "draft_complete", total_rounds=draft["max_picks"],
                    round_reached=draft["current_round"], pool=draft["label"],
                    league_name=draft["league_name"],
                    league_finished=league is not None
                    and league["status"] in ("complete", "archived"),
                    archived=league is not None and league["status"] == "archived"))
            break

        player = current_player(conn, draft)

        # 1. Somebody who has filled their roster or run out of budget is
        #    passed over silently-ish: no skip, no makeup, no timer penalty.
        if not player_can_pick(conn, draft, player):
            events.append(_event("passed", player, round=draft["current_round"],
                                 reason=("roster full"
                                         if picks_made(conn, draft["id"], player["id"])
                                         >= draft["max_picks"] else "out of budget")))
            advance_turn(conn, pool_id)
            continue

        # 2. An unused makeup pick dies as this turn opens, and takes this
        #    turn with it: skipped, and normally halved again.
        expired = _expire_stale_makeups(conn, draft, player)
        if expired:
            events.extend(expired)
            # At a snake round boundary the deadline arrives with no gap at
            # all, so the player loses this turn as well but is not charged a
            # second halving for it.
            instant = any(event["back_to_back"] for event in expired)
            events.extend(skip_current_player(
                conn, pool_id, reason="makeup deadline passed",
                halve=False if instant else None, _nested=True))
            continue

        # 3. Queued picks: draft automatically if anything in the list is legal.
        pick, had_queue = _try_queue(conn, draft, player)
        if pick is not None:
            events.append(_event("auto_pick", player, round=pick["round"],
                                 pokemon=pick["pokemon"], cost=pick["cost"],
                                 pick_order=pick["pick_order"]))
            advance_turn(conn, pool_id)
            continue    # the next player might also have a queue

        if had_queue:
            # Every queued Pokemon was gone. This must NOT be treated as a
            # normal skip, so flag the turn: if the clock later runs out the
            # player's timer is left alone.
            conn.execute("UPDATE pools SET queue_exhausted = 1 WHERE id = ?", (pool_id,))
            conn.commit()
            events.append(_event("queue_exhausted", player, round=draft["current_round"]))

        # 4. Nothing automatic happened - start the clock.
        events.append(_event("turn_started", player, round=draft["current_round"],
                             seconds=player["current_timer"],
                             pending_makeups=pending_makeup_rounds(conn, pool_id, player["id"])))
        break

    return events


def open_turn_clock(conn, pool_id, deadline):
    """Store when the current turn runs out (survives a bot restart)."""
    conn.execute("UPDATE pools SET turn_deadline = ? WHERE id = ?", (deadline, pool_id))
    conn.commit()


def skip_current_player(conn, pool_id, reason="timer expired", halve=None, _nested=False):
    """Skip whoever is on the clock and move on.

    `halve` defaults to "yes, unless the player's queue was exhausted" -
    a player whose queue was wiped out by other people should not be
    punished with a shorter timer.
    """
    draft = get_pool(conn, pool_id)
    if draft["status"] != "active":
        return []

    player = current_player(conn, draft)
    if halve is None:
        # A player whose whole queue was drafted out from under them keeps
        # their timer: that was not their fault.
        halve = not draft["queue_exhausted"]

    if halve:
        new_timer, halve = _halve_timer(conn, pool_id, player)
    else:
        new_timer = player["current_timer"]

    conn.execute(
        "INSERT INTO skips (pool_id, player_id, round, reason, halved, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (pool_id, player["id"], draft["current_round"], reason, 1 if halve else 0, database.now()),
    )
    # Owed a makeup pick for the round they just missed, due before their
    # next turn. INSERT OR IGNORE protects the UNIQUE(draft, player, round)
    # constraint. created_turn is what lets the boundary grace period work.
    conn.execute(
        "INSERT OR IGNORE INTO makeups (pool_id, player_id, round, status, created_turn, created_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (pool_id, player["id"], draft["current_round"], turn_number(conn, draft), database.now()),
    )
    conn.commit()

    events = [_event("skipped", get_player_by_id(conn, player["id"]),
                     round=draft["current_round"], new_timer=new_timer, halved=halve,
                     queue_exhausted=bool(draft["queue_exhausted"]))]

    advance_turn(conn, pool_id)
    if not _nested:
        events.extend(begin_turn(conn, pool_id))
    return events


# --------------------------------------------------------------------------
# Making picks
# --------------------------------------------------------------------------


def _record_pick(conn, draft, player, round_number, pokemon, cost,
                 tera_captain=False, is_makeup=False, is_auto=False):
    """Write a validated pick to history + roster and charge the budget(s).

    A Tera Captain costs its Tera Tax from the normal budget and the Tera
    budget both, which is exactly what the spreadsheet does: the Rosters
    tab subtracts every row, and the Tera budget subtracts ticked rows.
    """
    key = normalize(pokemon)
    order = next_pick_order(conn, draft["id"])

    cursor = conn.execute(
        """INSERT INTO picks (pool_id, player_id, round, pokemon, pokemon_key, cost,
                              tera_captain, pick_order, is_makeup, is_auto, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (draft["id"], player["id"], round_number, str(pokemon).strip(), key, cost,
         1 if tera_captain else 0, order, 1 if is_makeup else 0,
         1 if is_auto else 0, database.now()),
    )
    pick_id = cursor.lastrowid

    conn.execute(
        """INSERT INTO rosters (pool_id, player_id, pokemon, pokemon_key, cost,
                                tera_captain, pick_id, source)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'draft')""",
        (draft["id"], player["id"], str(pokemon).strip(), key, cost,
         1 if tera_captain else 0, pick_id),
    )
    conn.execute(
        "UPDATE players SET budget_remaining = budget_remaining - ? WHERE id = ?",
        (cost, player["id"]),
    )
    if tera_captain:
        conn.execute(
            "UPDATE players SET tera_budget_remaining = tera_budget_remaining - ? WHERE id = ?",
            (cost, player["id"]),
        )
    # The queue for this round has now been used up.
    conn.execute(
        "DELETE FROM queued_picks WHERE pool_id = ? AND player_id = ? AND round = ?",
        (draft["id"], player["id"], round_number),
    )
    conn.commit()

    return conn.execute("SELECT * FROM picks WHERE id = ?", (pick_id,)).fetchone()


def make_pick(conn, draft, user_id, round_number, pokemon, cost,
              tera_captain=False, acting_as_admin=False):
    """/pick - the normal on-the-clock pick. Returns (pick, events)."""
    if draft["status"] != "active":
        raise DraftError("❌ This draft is not running.")

    on_clock = current_player(conn, draft)
    player = get_player(conn, draft["id"], user_id)

    if player is None or player["id"] != on_clock["id"]:
        if not acting_as_admin:
            raise DraftError(
                f"❌ It is not your turn. It is currently **{on_clock['display_name']}**'s pick."
            )
        # An admin may pick on behalf of whoever is on the clock.
        player = on_clock

    if round_number != draft["current_round"]:
        raise DraftError(f"❌ Incorrect round. It is currently Round {draft['current_round']}.")

    validate_pick(conn, draft, player, round_number, pokemon, cost, tera_captain=tera_captain)
    pick = _record_pick(conn, draft, player, round_number, pokemon, cost,
                        tera_captain=tera_captain)

    events = [_event("pick_made", get_player_by_id(conn, player["id"]),
                     round=round_number, pokemon=pick["pokemon"], cost=cost,
                     tera_captain=tera_captain, pick_order=pick["pick_order"])]
    advance_turn(conn, draft["id"])
    events.extend(begin_turn(conn, draft["id"]))
    return pick, events


def pending_makeup_rounds(conn, pool_id, player_id):
    rows = conn.execute(
        "SELECT round FROM makeups WHERE pool_id = ? AND player_id = ? AND status = 'pending' "
        "ORDER BY round",
        (pool_id, player_id),
    ).fetchall()
    return [row["round"] for row in rows]


def make_makeup_pick(conn, draft, user_id, round_number, pokemon, cost, tera_captain=False):
    """/makeuppick - resolve a missed round. Never changes whose turn it is.

    Also allowed once the draft is complete, so that somebody skipped in the
    final round still gets their chance to catch up.
    """
    if draft["status"] not in ("active", "complete"):
        raise DraftError("❌ This draft is not running.")

    player = require_player(conn, draft["id"], user_id)

    makeup = conn.execute(
        "SELECT * FROM makeups WHERE pool_id = ? AND player_id = ? AND round = ?",
        (draft["id"], player["id"], round_number),
    ).fetchone()

    if makeup is None:
        pending = pending_makeup_rounds(conn, draft["id"], player["id"])
        hint = f" You currently owe: {', '.join(str(r) for r in pending)}." if pending else ""
        raise DraftError(f"❌ You do not have a makeup pick for Round {round_number}.{hint}")
    if makeup["status"] == "resolved":
        raise DraftError(f"❌ Your Round {round_number} makeup pick has already been used.")
    if makeup["status"] == "expired":
        raise DraftError(
            f"❌ Your Round {round_number} makeup pick expired when your Round "
            f"{round_number + 1} turn arrived."
        )
    # Belt and braces: the deadline is normally applied by begin_turn, but
    # check again here in case the draft advanced in an unusual way.
    if draft["current_round"] > round_number + 1:
        conn.execute("UPDATE makeups SET status = 'expired', resolved_at = ? WHERE id = ?",
                     (database.now(), makeup["id"]))
        conn.commit()
        raise DraftError(f"❌ Your Round {round_number} makeup pick deadline has passed.")

    validate_pick(conn, draft, player, round_number, pokemon, cost, tera_captain=tera_captain)
    pick = _record_pick(conn, draft, player, round_number, pokemon, cost,
                        tera_captain=tera_captain, is_makeup=True)

    conn.execute("UPDATE makeups SET status = 'resolved', resolved_at = ? WHERE id = ?",
                 (database.now(), makeup["id"]))
    conn.commit()
    return pick


def queue_picks(conn, draft, user_id, round_number, pokemon_names, costs=None, teras=None):
    """/queuepick - store up to five fallbacks for one of your own turns."""
    if draft["status"] != "active":
        raise DraftError("❌ This draft is not running.")

    player = require_player(conn, draft["id"], user_id)

    if round_number < 1 or round_number > draft["max_picks"]:
        raise DraftError(f"❌ This draft only has rounds 1–{draft['max_picks']}.")
    if not turn_is_upcoming(conn, draft, player, round_number):
        raise DraftError(f"❌ Your Round {round_number} turn has already passed.")
    if len(pokemon_names) > config.MAX_QUEUE_LENGTH:
        raise DraftError(f"❌ You may queue at most {config.MAX_QUEUE_LENGTH} Pokémon.")

    costs = costs or [None] * len(pokemon_names)
    teras = teras or [False] * len(pokemon_names)

    conn.execute(
        "DELETE FROM queued_picks WHERE pool_id = ? AND player_id = ? AND round = ?",
        (draft["id"], player["id"], round_number),
    )
    for priority, (name, cost, tera) in enumerate(zip(pokemon_names, costs, teras), start=1):
        conn.execute(
            """INSERT INTO queued_picks (pool_id, player_id, round, priority,
                                         pokemon, pokemon_key, cost, tera_captain)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (draft["id"], player["id"], round_number, priority,
             name.strip(), normalize(name), cost, 1 if tera else 0),
        )
    conn.commit()
    return pokemon_names


def get_queue(conn, pool_id, player_id, round_number):
    return conn.execute(
        "SELECT * FROM queued_picks WHERE pool_id = ? AND player_id = ? AND round = ? "
        "ORDER BY priority",
        (pool_id, player_id, round_number),
    ).fetchall()


def edit_pick(conn, draft, actor_user_id, round_number, pokemon, cost,
              tera_captain=False, target_user_id=None, acting_as_admin=False):
    """/editpick - replace a pick that has already been made."""
    target_user_id = target_user_id or actor_user_id
    if target_user_id != actor_user_id and not acting_as_admin:
        raise DraftError("❌ Only an admin can edit somebody else's pick.")

    player = require_player(conn, draft["id"], target_user_id)

    pick = conn.execute(
        "SELECT * FROM picks WHERE pool_id = ? AND player_id = ? AND round = ?",
        (draft["id"], player["id"], round_number),
    ).fetchone()

    if pick is None:
        # Nothing recorded for that round. An admin is allowed to fill it in
        # on the player's behalf - useful when somebody was skipped and never
        # made their makeup pick.
        if not acting_as_admin:
            raise DraftError(
                f"❌ You have no pick recorded for Round {round_number}. "
                f"If you were skipped, use `/makeuppick`."
            )
        if round_number < 1 or round_number > draft["max_picks"]:
            raise DraftError(f"❌ This draft only has rounds 1–{draft['max_picks']}.")

        validate_pick(conn, draft, player, round_number, pokemon, cost,
                      tera_captain=tera_captain)
        created = _record_pick(conn, draft, player, round_number, pokemon, cost,
                               tera_captain=tera_captain)
        # Filling in the round settles any makeup pick owed for it.
        conn.execute(
            "UPDATE makeups SET status = 'resolved', resolved_at = ? "
            "WHERE pool_id = ? AND player_id = ? AND round = ? AND status = 'pending'",
            (database.now(), draft["id"], player["id"], round_number),
        )
        conn.commit()
        return {
            "player": get_player_by_id(conn, player["id"]),
            "round": round_number,
            "old_pokemon": None,
            "old_cost": None,
            "old_tera": False,
            "tera_captain": tera_captain,
            "new_pokemon": created["pokemon"],
            "new_cost": cost,
            "created": True,
        }

    roster_row = conn.execute(
        "SELECT * FROM rosters WHERE pick_id = ?", (pick["id"],)
    ).fetchone()
    if roster_row is None or roster_row["player_id"] != player["id"]:
        raise DraftError(
            f"❌ **{pick['pokemon']}** is no longer on {player['display_name']}'s roster "
            f"(it was traded), so that pick can no longer be edited."
        )

    # Refund first so the budget check sees the money freed by the old pick,
    # then validate. If validation fails nothing has been committed yet.
    conn.execute("UPDATE players SET budget_remaining = budget_remaining + ? WHERE id = ?",
                 (pick["cost"], player["id"]))
    if pick["tera_captain"]:
        conn.execute(
            "UPDATE players SET tera_budget_remaining = tera_budget_remaining + ? WHERE id = ?",
            (pick["cost"], player["id"]))
    refreshed = get_player_by_id(conn, player["id"])

    try:
        validate_pick(conn, draft, refreshed, round_number, pokemon, cost,
                      tera_captain=tera_captain, replacing_key=pick["pokemon_key"])
    except DraftError:
        conn.rollback()
        raise

    key = normalize(pokemon)
    flag = 1 if tera_captain else 0
    conn.execute(
        "UPDATE picks SET pokemon = ?, pokemon_key = ?, cost = ?, tera_captain = ? WHERE id = ?",
        (str(pokemon).strip(), key, cost, flag, pick["id"]))
    conn.execute(
        "UPDATE rosters SET pokemon = ?, pokemon_key = ?, cost = ?, tera_captain = ? WHERE id = ?",
        (str(pokemon).strip(), key, cost, flag, roster_row["id"]))
    conn.execute("UPDATE players SET budget_remaining = budget_remaining - ? WHERE id = ?",
                 (cost, player["id"]))
    if tera_captain:
        conn.execute(
            "UPDATE players SET tera_budget_remaining = tera_budget_remaining - ? WHERE id = ?",
            (cost, player["id"]))
    conn.commit()

    return {
        "player": get_player_by_id(conn, player["id"]),
        "round": round_number,
        "old_pokemon": pick["pokemon"],
        "old_cost": pick["cost"],
        "old_tera": bool(pick["tera_captain"]),
        "tera_captain": tera_captain,
        "new_pokemon": str(pokemon).strip(),
        "new_cost": cost,
        "created": False,
    }


# --------------------------------------------------------------------------
# Trades
# --------------------------------------------------------------------------


def propose_trade(conn, draft, user_id, drops, gets, partner_user_id=None):
    """/trade - record a pending trade for an admin to approve.

    With a partner this is a normal two-player swap. Without one it is a
    free-agency move against the undrafted pool.

    Deliberately permissive: the admin who approves the trade is the one
    judging whether it is fair and legal, so the bot only checks the things
    it must know to do the bookkeeping. Anything that looks odd is returned
    as a warning for the admin to read, not as a refusal.
    """
    proposer = require_player(conn, draft["id"], user_id)
    partner = None
    if partner_user_id is not None:
        if partner_user_id == user_id:
            raise DraftError("❌ You cannot trade with yourself.")
        partner = require_player(conn, draft["id"], partner_user_id)

    if not drops or not gets:
        raise DraftError("❌ A trade needs at least one Pokémon on each side.")
    if len(drops) > config.MAX_TRADE_SIZE or len(gets) > config.MAX_TRADE_SIZE:
        raise DraftError(f"❌ A trade may involve at most {config.MAX_TRADE_SIZE} Pokémon per side.")

    check_trade_limit(conn, draft, proposer)
    if partner is not None:
        check_trade_limit(conn, draft, partner)

    warnings = trade_warnings(conn, draft, proposer, partner, drops, gets)

    # Numbered per league so that an admin channel shared by several pools
    # never has two trade #1s in it.
    number = conn.execute(
        "SELECT COALESCE(MAX(trade_number), 0) + 1 AS n FROM trades WHERE league_id = ?",
        (draft["league_id"],),
    ).fetchone()["n"]

    cursor = conn.execute(
        """INSERT INTO trades (league_id, pool_id, trade_number, proposer_id, partner_id,
                               drops, gets, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (draft["league_id"], draft["id"], number, proposer["id"],
         partner["id"] if partner else None,
         json.dumps(drops), json.dumps(gets), database.now()),
    )
    conn.commit()
    record = conn.execute("SELECT * FROM trades WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return record, warnings


def trades_used(conn, pool_id, player_id):
    """How many approved trades a player is already part of."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE pool_id = ? AND status = 'approved' "
        "AND (proposer_id = ? OR partner_id = ?)",
        (pool_id, player_id, player_id),
    ).fetchone()["n"]


def check_trade_limit(conn, draft, player):
    """Enforce the per-player trade cap set during /setup. 0 means unlimited."""
    limit = draft["max_trades"]
    if not limit:
        return
    used = trades_used(conn, draft["id"], player["id"])
    if used >= limit:
        raise DraftError(
            f"❌ **{player['display_name']}** has already used all {limit} of their "
            f"trades for this draft."
        )


def trade_warnings(conn, draft, proposer, partner, drops, gets):
    """Things an approving admin probably wants to know. Never blocks."""
    notes = []
    for name in drops:
        row = find_owner(conn, draft["id"], normalize(name))
        if row is None:
            notes.append(f"**{name}** is not on anybody's roster.")
        elif row["player_id"] != proposer["id"]:
            owner = get_player_by_id(conn, row["player_id"])
            notes.append(f"**{name}** is on {owner['display_name']}'s roster, "
                         f"not {proposer['display_name']}'s.")
    for name in gets:
        row = find_owner(conn, draft["id"], normalize(name))
        if partner is None and row is not None:
            owner = get_player_by_id(conn, row["player_id"])
            notes.append(f"**{name}** is already drafted by {owner['display_name']}.")
        elif partner is not None and (row is None or row["player_id"] != partner["id"]):
            notes.append(f"**{name}** is not on {partner['display_name']}'s roster.")
    return notes


def apply_sheet_cost(conn, draft, pokemon, cost):
    """Correct a roster entry's cost from the Pokédex, adjusting the budget.

    Free-agency pickups arrive with no cost, because the bot has no way of
    knowing one without the sheet. This fills it in afterwards.
    """
    row = find_owner(conn, draft["id"], normalize(pokemon))
    if row is None or row["cost"] == cost:
        return False

    difference = cost - row["cost"]
    conn.execute("UPDATE rosters SET cost = ? WHERE id = ?", (cost, row["id"]))
    if config.TRADE_ADJUSTS_BUDGET:
        conn.execute(
            "UPDATE players SET budget_remaining = budget_remaining - ? WHERE id = ?",
            (difference, row["player_id"]),
        )
    conn.commit()
    return True


def get_trade(conn, league_id, trade_number):
    return conn.execute(
        "SELECT * FROM trades WHERE league_id = ? AND trade_number = ?",
        (league_id, trade_number),
    ).fetchone()


def approve_trade(conn, draft, trade_number, approver_user_id):
    """/approvetrade - apply a pending trade to the rosters.

    The admin has already decided the trade is legal, so this applies it as
    written and reports anything it could not do rather than refusing.
    """
    trade = get_trade(conn, draft["league_id"], trade_number)
    if trade is None:
        raise DraftError(f"❌ There is no trade #{trade_number} in this draft.")
    if trade["status"] != "pending":
        raise DraftError(f"❌ Trade #{trade_number} has already been {trade['status']}.")

    proposer = get_player_by_id(conn, trade["proposer_id"])
    partner = get_player_by_id(conn, trade["partner_id"]) if trade["partner_id"] else None
    drops = json.loads(trade["drops"])
    gets = json.loads(trade["gets"])

    # The cap is checked again here in case other trades were approved first.
    check_trade_limit(conn, draft, proposer)
    if partner is not None:
        check_trade_limit(conn, draft, partner)

    notes = []
    drop_total = 0
    for name in drops:
        row = find_owner(conn, draft["id"], normalize(name))
        if row is None:
            notes.append(f"{name} was not on any roster, so nothing was removed.")
            continue
        drop_total += row["cost"]
        if partner is None:
            conn.execute("DELETE FROM rosters WHERE id = ?", (row["id"],))
        else:
            conn.execute("UPDATE rosters SET player_id = ?, source = 'trade' WHERE id = ?",
                         (partner["id"], row["id"]))

    get_total = 0
    for name in gets:
        row = find_owner(conn, draft["id"], normalize(name))
        if row is None:
            # Free agency: no roster row yet, so one is created. The cost is
            # filled in from the sheet by the caller when one is linked.
            conn.execute(
                """INSERT INTO rosters (pool_id, player_id, pokemon, pokemon_key, cost, source)
                   VALUES (?, ?, ?, ?, 0, 'trade')""",
                (draft["id"], proposer["id"], name.strip(), normalize(name)),
            )
        else:
            get_total += row["cost"]
            conn.execute("UPDATE rosters SET player_id = ?, source = 'trade' WHERE id = ?",
                         (proposer["id"], row["id"]))

    if config.TRADE_ADJUSTS_BUDGET:
        # The proposer gets back what they dropped and pays for what they
        # gained, which keeps budgets matching the spreadsheet, where each
        # coach's budget is worked out from whatever is on their roster.
        conn.execute("UPDATE players SET budget_remaining = budget_remaining + ? WHERE id = ?",
                     (drop_total - get_total, proposer["id"]))
        if partner is not None:
            conn.execute("UPDATE players SET budget_remaining = budget_remaining + ? WHERE id = ?",
                         (get_total - drop_total, partner["id"]))

    conn.execute(
        "UPDATE trades SET status = 'approved', resolved_at = ?, resolved_by = ? WHERE id = ?",
        (database.now(), approver_user_id, trade["id"]),
    )
    conn.commit()
    return get_trade(conn, draft["league_id"], trade_number), notes


def reject_trade(conn, draft, trade_number, approver_user_id):
    trade = get_trade(conn, draft["league_id"], trade_number)
    if trade is None:
        raise DraftError(f"❌ There is no trade #{trade_number} in this draft.")
    if trade["status"] != "pending":
        raise DraftError(f"❌ Trade #{trade_number} has already been {trade['status']}.")
    conn.execute(
        "UPDATE trades SET status = 'rejected', resolved_at = ?, resolved_by = ? WHERE id = ?",
        (database.now(), approver_user_id, trade["id"]),
    )
    conn.commit()
    return get_trade(conn, draft["league_id"], trade_number)


# --------------------------------------------------------------------------
# Substitutions
# --------------------------------------------------------------------------


def request_substitution(conn, draft, user_id, display_name, reason):
    """Record a request to join or leave a draft, for a moderator to act on.

    Somebody already drafting is asking to sub **out**; anybody else is
    offering to sub **in**. Nothing changes until a moderator runs
    /substitute - this only puts the request in front of them.
    """
    reason = " ".join(str(reason or "").split())
    if not reason:
        raise DraftError("❌ Please give a reason for the substitution.")
    if len(reason) > 400:
        raise DraftError("❌ That reason is too long (400 characters maximum).")

    player = get_player(conn, draft["id"], user_id)
    kind = "out" if player is not None else "in"

    existing = conn.execute(
        "SELECT * FROM sub_requests WHERE league_id = ? AND user_id = ? AND status = 'open'",
        (draft["league_id"], user_id),
    ).fetchone()
    if existing is not None:
        raise DraftError(
            f"❌ You already have an open substitution request "
            f"(#{existing['request_number']}). A moderator will deal with it."
        )

    number = conn.execute(
        "SELECT COALESCE(MAX(request_number), 0) + 1 AS n FROM sub_requests WHERE league_id = ?",
        (draft["league_id"],),
    ).fetchone()["n"]

    cursor = conn.execute(
        """INSERT INTO sub_requests (league_id, pool_id, request_number, user_id,
                                     display_name, kind, reason, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
        (draft["league_id"], draft["id"], number, user_id, display_name, kind,
         reason, database.now()),
    )
    conn.commit()
    return conn.execute("SELECT * FROM sub_requests WHERE id = ?",
                        (cursor.lastrowid,)).fetchone()


def open_sub_requests(conn, league_id):
    return conn.execute(
        "SELECT * FROM sub_requests WHERE league_id = ? AND status = 'open' "
        "ORDER BY request_number",
        (league_id,),
    ).fetchall()


def substitute_player(conn, draft, out_user_id, in_user_id, in_display_name):
    """Hand a seat over to somebody else, keeping the team intact.

    The seat is what persists: roster, budgets, draft position, picks and
    any makeup picks all stay exactly as they were, and only the person
    holding them changes. Their personal timer and skip count are reset to
    the base, because a replacement should not inherit somebody else's
    penalties - a moderator has to approve the swap, so that cannot be
    used to dodge a shrinking clock.
    """
    player = get_player(conn, draft["id"], out_user_id)
    if player is None:
        raise DraftError("❌ That player is not in this pool's draft.")
    if in_user_id == out_user_id:
        raise DraftError("❌ Those are the same person.")

    # One person may not hold two seats in the same league.
    clash = conn.execute(
        """SELECT players.*, pools.label FROM players
           JOIN pools ON pools.id = players.pool_id
           WHERE pools.league_id = ? AND players.user_id = ?""",
        (draft["league_id"], in_user_id),
    ).fetchone()
    if clash is not None:
        raise DraftError(
            f"❌ <@{in_user_id}> is already drafting in this league (pool {clash['label']})."
        )

    conn.execute(
        """UPDATE players SET user_id = ?, display_name = ?, current_timer = ?,
                              skip_count = 0 WHERE id = ?""",
        (in_user_id, in_display_name, draft["base_timer"], player["id"]),
    )
    # Any request either of them had open is now dealt with.
    conn.execute(
        "UPDATE sub_requests SET status = 'resolved', resolved_at = ? "
        "WHERE league_id = ? AND user_id IN (?, ?) AND status = 'open'",
        (database.now(), draft["league_id"], out_user_id, in_user_id),
    )
    conn.commit()

    return {
        "pool": draft["label"],
        "seat": player["draft_position"],
        "coach_number": player["coach_number"],
        "out_user_id": out_user_id,
        "out_name": player["display_name"],
        "in_user_id": in_user_id,
        "in_name": in_display_name,
        "roster": roster_view(conn, draft, get_player_by_id(conn, player["id"])),
    }


# --------------------------------------------------------------------------
# Adjusting a player's clock
# --------------------------------------------------------------------------


def timer_for_skips(base_timer, skips):
    """The timer a player should have after this many reductions.

    Applies the same halving the draft does, so setting somebody's skip
    count back to 0 restores their full timer, and setting it to 2 gives
    them the timer they would have had after two skips.
    """
    timer = base_timer
    for _ in range(max(0, int(skips))):
        timer = max(config.MINIMUM_TIMER_SECONDS, timer // config.TIMER_DIVISOR)
    return timer


def set_skip_count(conn, draft, user_id, skips, timer=None):
    """Correct a player's skip count, and their timer along with it.

    Skips and the personal timer normally move together, so changing one
    without the other would leave a player with a shrunken clock and no
    reason for it. By default the timer is recalculated from the new count -
    set it back to 0 and they get their full timer again. Pass `timer` to
    override that exactly.
    """
    player = require_player(conn, draft["id"], user_id)

    skips = int(skips)
    if skips < 0:
        raise DraftError("❌ A skip count cannot be negative.")
    if skips > 100:
        raise DraftError("❌ That skip count is implausibly high.")

    if timer is None:
        new_timer = timer_for_skips(draft["base_timer"], skips)
    else:
        new_timer = int(timer)
        if new_timer < config.MINIMUM_TIMER_SECONDS:
            raise DraftError(
                f"❌ A timer cannot be shorter than {config.MINIMUM_TIMER_SECONDS} "
                f"seconds (`MINIMUM_TIMER_SECONDS` in config.py)."
            )
        if new_timer > config.MAX_BASE_TIMER_SECONDS:
            raise DraftError(
                f"❌ A timer cannot be longer than {config.MAX_BASE_TIMER_SECONDS} seconds."
            )

    before = {"skips": player["skip_count"], "timer": player["current_timer"]}
    conn.execute("UPDATE players SET skip_count = ?, current_timer = ? WHERE id = ?",
                 (skips, new_timer, player["id"]))
    conn.commit()

    return {
        "display_name": player["display_name"],
        "user_id": user_id,
        "pool": draft["label"],
        "before": before,
        "skips": skips,
        "timer": new_timer,
        "recalculated": timer is None,
    }


# --------------------------------------------------------------------------
# Removing a player
# --------------------------------------------------------------------------


def remove_player(conn, draft, user_id, release_roster=True):
    """Take a player out of a draft.

    Before the draft starts there is nothing to preserve, so the seat is
    deleted and the seats behind it shuffle up.

    Once drafting has begun the seat is kept and marked `withdrawn`
    instead. That matters: the snake order is built from the seat count, so
    deleting one mid-draft would move everybody else's turn and could
    invalidate the turn pointer. A withdrawn seat is simply passed over.

    `release_roster` puts their Pokemon back in the pool for everybody else,
    which is usually what you want when somebody drops out. Their picks stay
    in the history either way.
    """
    player = get_player(conn, draft["id"], user_id)
    if player is None:
        raise DraftError("❌ That player is not in this pool's draft.")
    if player["status"] != "active":
        raise DraftError(f"❌ **{player['display_name']}** has already been removed.")

    released = []
    if release_roster:
        released = [row["pokemon"] for row in conn.execute(
            "SELECT pokemon FROM rosters WHERE pool_id = ? AND player_id = ?",
            (draft["id"], player["id"]))]
        conn.execute("DELETE FROM rosters WHERE pool_id = ? AND player_id = ?",
                     (draft["id"], player["id"]))

    conn.execute(
        "DELETE FROM queued_picks WHERE pool_id = ? AND player_id = ?",
        (draft["id"], player["id"]))
    conn.execute(
        "UPDATE makeups SET status = 'expired', resolved_at = ? "
        "WHERE pool_id = ? AND player_id = ? AND status = 'pending'",
        (database.now(), draft["id"], player["id"]))

    if draft["status"] == "setup":
        seat = player["draft_position"]
        # The roster blocks this pool owns, including the one being freed.
        # They are not necessarily contiguous - a template with unticked
        # coaches leaves gaps - so the remaining players are shuffled up into
        # the earliest of those blocks rather than simply decremented.
        slots = sorted(row["coach_number"] for row in get_players(conn, draft["id"]))
        conn.execute("DELETE FROM players WHERE id = ?", (player["id"],))
        conn.execute(
            "UPDATE players SET draft_position = draft_position - 1 "
            "WHERE pool_id = ? AND draft_position > ?",
            (draft["id"], seat))

        for index, row in enumerate(get_players(conn, draft["id"])):
            conn.execute("UPDATE players SET coach_number = ? WHERE id = ?",
                         (slots[index], row["id"]))
        removed_seat = True
    else:
        conn.execute("UPDATE players SET status = 'withdrawn' WHERE id = ?",
                     (player["id"],))
        removed_seat = False

    conn.commit()
    return {
        "display_name": player["display_name"],
        "user_id": user_id,
        "seat": player["draft_position"],
        "pool": draft["label"],
        "seat_deleted": removed_seat,
        "released": released,
        "players_left": player_count(conn, draft["id"]),
    }


# --------------------------------------------------------------------------
# Season schedule
#
# With a template the fixtures live on the spreadsheet. Without one the bot
# makes its own, so a "draft and schedule" league still has something to
# post.
# --------------------------------------------------------------------------


def round_robin(entrants, weeks=None, rng=None):
    """A randomised round-robin schedule as [[(home, away), ...], ...].

    Uses the circle method: fix the first entrant and rotate the rest, which
    pairs everybody exactly once over len(entrants) - 1 weeks. An odd number
    of entrants gets a bye each week, and home and away alternate so nobody
    is always on the same side.
    """
    rng = rng or random.Random()
    order = list(entrants)
    if len(order) < 2:
        return []
    rng.shuffle(order)

    if len(order) % 2:
        order.append(None)              # None is the bye
    count = len(order)
    per_cycle = count - 1
    weeks = per_cycle if weeks is None else max(1, weeks)

    fixed, rotating = order[0], order[1:]
    schedule = []
    for week in range(weeks):
        shift = week % per_cycle
        turned = rotating[-shift:] + rotating[:-shift] if shift else list(rotating)
        line = [fixed] + turned

        pairs = []
        for index in range(count // 2):
            home, away = line[index], line[count - 1 - index]
            if home is None or away is None:
                continue                # that entrant has the bye this week
            if week % 2:
                home, away = away, home
            pairs.append((home, away))
        schedule.append(pairs)
    return schedule


def generate_fixtures(conn, league_id, weeks=None, rng=None):
    """Build and store a schedule for every active player in the league.

    Fixtures can pair coaches from different pools - only the draft itself
    is split by pool. Replaces any schedule already generated.
    """
    players = []
    for pool in get_league_pools(conn, league_id):
        players += [row for row in get_players(conn, pool["id"])
                    if row["status"] == "active"]

    if len(players) < 2:
        raise DraftError("❌ A schedule needs at least two players.")

    league = get_league(conn, league_id)
    weeks = weeks or (league["season_weeks"] or None)
    schedule = round_robin([row["id"] for row in players], weeks=weeks, rng=rng)

    conn.execute("DELETE FROM fixtures WHERE league_id = ?", (league_id,))
    stamp = database.now()
    for week_number, pairs in enumerate(schedule, start=1):
        for match_number, (home, away) in enumerate(pairs, start=1):
            conn.execute(
                """INSERT INTO fixtures (league_id, week, match_number,
                                         home_player_id, away_player_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (league_id, week_number, match_number, home, away, stamp),
            )
    conn.commit()
    return len(schedule)


def get_fixtures(conn, league_id, week):
    """One week's fixtures as [(home player row, away player row), ...]."""
    rows = conn.execute(
        "SELECT * FROM fixtures WHERE league_id = ? AND week = ? ORDER BY match_number",
        (league_id, week),
    ).fetchall()
    return [(get_player_by_id(conn, row["home_player_id"]),
             get_player_by_id(conn, row["away_player_id"])) for row in rows]


def fixture_weeks(conn, league_id):
    rows = conn.execute(
        "SELECT DISTINCT week FROM fixtures WHERE league_id = ? ORDER BY week",
        (league_id,),
    ).fetchall()
    return [row["week"] for row in rows]


# --------------------------------------------------------------------------
# Read-only views used by /roster, /progress and /status
# --------------------------------------------------------------------------


def roster_view(conn, draft, player):
    rows = conn.execute(
        "SELECT * FROM rosters WHERE pool_id = ? AND player_id = ? ORDER BY id",
        (draft["id"], player["id"]),
    ).fetchall()
    spent = sum(row["cost"] for row in rows)
    captains = [(row["pokemon"], row["cost"]) for row in rows if row["tera_captain"]]
    return {
        "player": player,
        "pokemon": [(row["pokemon"], row["cost"]) for row in rows],
        "entries": [(row["pokemon"], row["cost"], bool(row["tera_captain"])) for row in rows],
        "spent": spent,
        "remaining": player["budget_remaining"],
        "count": len(rows),
        "max_picks": draft["max_picks"],
        "min_picks": draft["min_picks"],
        "tera_format": bool(draft["tera_budget"]),
        "tera_budget": draft["tera_budget"],
        "tera_remaining": player["tera_budget_remaining"],
        "tera_captains": captains,
        "max_tera": config.MAX_TERA_CAPTAINS,
    }


def round_picks(conn, pool_id, round_number):
    return conn.execute(
        """SELECT picks.*, players.display_name, players.user_id
           FROM picks JOIN players ON players.id = picks.player_id
           WHERE picks.pool_id = ? AND picks.round = ?
           ORDER BY picks.pick_order""",
        (pool_id, round_number),
    ).fetchall()


def completed_rounds(conn, draft):
    """Rounds that have at least one pick, most recent last."""
    rows = conn.execute(
        "SELECT DISTINCT round FROM picks WHERE pool_id = ? ORDER BY round", (draft["id"],)
    ).fetchall()
    return [row["round"] for row in rows]


def roster_block(conn, draft, player, rows):
    """One coach's roster column, as [(pokemon, is_tera_captain), ...].

    Index 0 is round 1. A Pokemon this player drafted sits in its own
    round's row, so the block reads like the draft did. Anything dropped in
    a trade simply leaves its row empty, and anything gained in a trade has
    no round of its own, so it drops into the first free row - repeating
    until the block is full.

    That keeps the block a true picture of the current roster, which is what
    the spreadsheet adds up to work out the coach's remaining budget.
    """
    block = [("", False)] * rows
    leftovers = []

    owned = conn.execute(
        "SELECT * FROM rosters WHERE pool_id = ? AND player_id = ? ORDER BY id",
        (draft["id"], player["id"]),
    ).fetchall()

    for entry in owned:
        item = (entry["pokemon"], bool(entry["tera_captain"]))
        round_number = None
        # Only a pick this player made themselves has a round on this block.
        # A traded-in Pokemon carries the original drafter's pick, which means
        # nothing here, so it is treated as having no round.
        if entry["source"] == "draft" and entry["pick_id"]:
            pick = conn.execute(
                "SELECT round, player_id FROM picks WHERE id = ?", (entry["pick_id"],)
            ).fetchone()
            if pick and pick["player_id"] == player["id"]:
                round_number = pick["round"]

        if round_number and 1 <= round_number <= rows and not block[round_number - 1][0]:
            block[round_number - 1] = item
        else:
            leftovers.append(item)

    for item in leftovers:
        for index in range(rows):
            if not block[index][0]:
                block[index] = item
                break

    return block


def export_for_sheet(conn, league_id):
    """Everything the spreadsheet needs, read here on the main thread.

    Returns (coaches, blocks, overflow):
      coaches  - {coach_number: (display_name, pool_label)} for Coaches!B and !Y
      blocks   - {coach_number: [(pokemon, is_tera_captain), ...]} per round
      overflow - {display_name: count} for anyone whose roster no longer fits
                 in a block, which can only happen if trades have added more
                 Pokemon than they removed

    Reading here means the background thread that talks to Google never
    touches the SQLite connection.
    """
    rows = config.SHEET_LAYOUT["roster_rows"]
    coaches, blocks, overflow = {}, {}, {}

    for pool in get_league_pools(conn, league_id):
        for player in get_players(conn, pool["id"]):
            coaches[player["coach_number"]] = (player["display_name"], pool["label"])

            if player["status"] != "active":
                # Somebody removed mid-draft keeps their row - they were in
                # the league - but their block is cleared, which matches
                # their Pokemon having gone back into the pool.
                blocks[player["coach_number"]] = [("", False)] * rows
                continue

            block = roster_block(conn, pool, player, rows)
            blocks[player["coach_number"]] = block

            owned = roster_size(conn, pool["id"], player["id"])
            shown = sum(1 for name, _ in block if name)
            if owned > shown:
                overflow[player["display_name"]] = owned - shown

    return coaches, blocks, overflow


def status_view(conn, draft):
    player = current_player(conn, draft)
    total_picks = conn.execute(
        "SELECT COUNT(*) AS n FROM picks WHERE pool_id = ?", (draft["id"],)
    ).fetchone()["n"]

    pending = conn.execute(
        """SELECT makeups.round, players.display_name, players.user_id
           FROM makeups JOIN players ON players.id = makeups.player_id
           WHERE makeups.pool_id = ? AND makeups.status = 'pending'
           ORDER BY makeups.round""",
        (draft["id"],),
    ).fetchall()

    return {
        "draft": draft,
        "current_player": player,
        "total_picks": total_picks,
        "expected_picks": draft["max_picks"] * player_count(conn, draft["id"]),
        "pending_makeups": [(row["display_name"], row["round"]) for row in pending],
        "tera_format": bool(draft["tera_budget"]),
    }
