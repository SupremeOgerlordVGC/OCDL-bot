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


def get_live_draft(conn, channel_id):
    """The unfinished draft in this channel, or None.

    Every command starts here. Because the lookup is by channel id, a
    command in #vgc can never touch the draft in #national-dex-singles.
    """
    return conn.execute(
        "SELECT * FROM drafts WHERE channel_id = ? AND status IN ('setup', 'active')",
        (channel_id,),
    ).fetchone()


def get_draft(conn, draft_id):
    return conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()


def get_channel_draft(conn, channel_id):
    """The channel's live draft, or its most recent finished one.

    Used by commands that still make sense after the final round - reading
    rosters and history, correcting a pick, and trading during the season.
    """
    live = get_live_draft(conn, channel_id)
    if live is not None:
        return live
    return conn.execute(
        "SELECT * FROM drafts WHERE channel_id = ? ORDER BY id DESC LIMIT 1",
        (channel_id,),
    ).fetchone()


def require_channel_draft(conn, channel_id):
    draft = get_channel_draft(conn, channel_id)
    if draft is None:
        raise DraftError("❌ This channel has never had a draft. An admin can create one with `/setup`.")
    return draft


def require_live_draft(conn, channel_id):
    draft = get_live_draft(conn, channel_id)
    if draft is None:
        raise DraftError("❌ There is no draft in this channel. An admin can create one with `/setup`.")
    return draft


def require_active_draft(conn, channel_id):
    draft = require_live_draft(conn, channel_id)
    if draft["status"] != "active":
        raise DraftError("❌ This draft has not started yet. An admin must run `/draft` first.")
    return draft


def get_players(conn, draft_id):
    """Every player in seat order."""
    return conn.execute(
        "SELECT * FROM players WHERE draft_id = ? ORDER BY draft_position",
        (draft_id,),
    ).fetchall()


def get_player(conn, draft_id, user_id):
    return conn.execute(
        "SELECT * FROM players WHERE draft_id = ? AND user_id = ?",
        (draft_id, user_id),
    ).fetchone()


def get_player_by_id(conn, player_id):
    return conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()


def require_player(conn, draft_id, user_id):
    player = get_player(conn, draft_id, user_id)
    if player is None:
        raise DraftError("❌ You are not a player in this channel's draft.")
    return player


def player_count(conn, draft_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM players WHERE draft_id = ?", (draft_id,)
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
        "SELECT * FROM players WHERE draft_id = ? AND draft_position = ?",
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


def find_owner(conn, draft_id, pokemon_key):
    """Roster row for a Pokemon, or None when it is still available.

    Availability is based on the roster rather than the pick history so
    that a Pokemon dropped in a trade correctly returns to the pool.
    """
    return conn.execute(
        "SELECT * FROM rosters WHERE draft_id = ? AND pokemon_key = ?",
        (draft_id, pokemon_key),
    ).fetchone()


def drafted_keys(conn, draft_id):
    """Normalised names of every Pokemon currently on somebody's roster."""
    rows = conn.execute(
        "SELECT pokemon_key FROM rosters WHERE draft_id = ?", (draft_id,)
    ).fetchall()
    return {row["pokemon_key"] for row in rows}


def picks_made(conn, draft_id, player_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM picks WHERE draft_id = ? AND player_id = ?",
        (draft_id, player_id),
    ).fetchone()["n"]


def roster_size(conn, draft_id, player_id):
    return conn.execute(
        "SELECT COUNT(*) AS n FROM rosters WHERE draft_id = ? AND player_id = ?",
        (draft_id, player_id),
    ).fetchone()["n"]


def next_pick_order(conn, draft_id):
    row = conn.execute(
        "SELECT COALESCE(MAX(pick_order), 0) + 1 AS n FROM picks WHERE draft_id = ?",
        (draft_id,),
    ).fetchone()
    return row["n"]


def player_can_pick(conn, draft, player):
    """False when a player can no longer take any pick at all.

    Either they have filled their roster, or they cannot afford even the
    cheapest possible Pokemon. When this is true of everybody, the draft is
    over even if there are rounds left.
    """
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


def validate_pick(conn, draft, player, round_number, pokemon, cost, replacing_key=None):
    """Check every rule for a pick. Raises DraftError on the first problem.

    `replacing_key` is the Pokemon being swapped out by /editpick, which is
    allowed to "occupy" its own slot without counting as a duplicate.
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


def create_draft(conn, guild_id, channel_id, admin_channel_id, admin_role_id,
                 budget, min_picks, max_picks, base_timer, players,
                 max_trades=0, sheet_url=None):
    """Create a draft in `setup` state. `players` is [(user_id, name), ...].

    Everything is validated before the first INSERT, so a bad /setup leaves
    the database untouched.
    """
    if get_live_draft(conn, channel_id) is not None:
        raise DraftError(
            "❌ This channel already has a draft. Finish it, or an admin can "
            "remove it with `/canceldraft`, before creating another."
        )

    if budget <= 0:
        raise DraftError("❌ The starting budget must be greater than 0.")
    if min_picks < 1:
        raise DraftError("❌ The minimum number of picks must be at least 1.")
    if max_picks < min_picks:
        raise DraftError("❌ The maximum number of picks cannot be less than the minimum.")
    if max_picks > 50:
        raise DraftError("❌ 50 rounds is the sensible upper limit for a draft.")
    if not config.MIN_BASE_TIMER_SECONDS <= base_timer <= config.MAX_BASE_TIMER_SECONDS:
        raise DraftError(
            f"❌ The base timer must be between {config.MIN_BASE_TIMER_SECONDS} and "
            f"{config.MAX_BASE_TIMER_SECONDS} seconds."
        )

    if max_trades < 0:
        raise DraftError("❌ The maximum number of trades cannot be negative (use 0 for unlimited).")

    if not config.MIN_PLAYERS <= len(players) <= config.MAX_PLAYERS:
        raise DraftError(
            f"❌ A draft needs between {config.MIN_PLAYERS} and {config.MAX_PLAYERS} players "
            f"(you listed {len(players)})."
        )

    user_ids = [user_id for user_id, _ in players]
    if len(set(user_ids)) != len(user_ids):
        raise DraftError("❌ The same player was listed twice.")

    cursor = conn.execute(
        """INSERT INTO drafts (guild_id, channel_id, admin_channel_id, admin_role_id,
                               budget, min_picks, max_picks, base_timer, max_trades,
                               sheet_url, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'setup', ?)""",
        (guild_id, channel_id, admin_channel_id, admin_role_id, budget,
         min_picks, max_picks, base_timer, max_trades, sheet_url, database.now()),
    )
    draft_id = cursor.lastrowid

    for seat, (user_id, display_name) in enumerate(players, start=1):
        conn.execute(
            """INSERT INTO players (draft_id, user_id, display_name, draft_position,
                                    budget_remaining, current_timer)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (draft_id, user_id, display_name, seat, budget, base_timer),
        )

    conn.commit()
    return draft_id


def update_settings(conn, draft, budget=None, min_picks=None, max_picks=None, sheet_url=None):
    """/edit - only legal while the draft is still in `setup`."""
    if draft["status"] != "setup":
        raise DraftError("❌ The draft has already started, so its settings can no longer be edited.")

    new_budget = draft["budget"] if budget is None else budget
    new_min = draft["min_picks"] if min_picks is None else min_picks
    new_max = draft["max_picks"] if max_picks is None else max_picks

    if new_budget <= 0:
        raise DraftError("❌ The starting budget must be greater than 0.")
    if new_min < 1:
        raise DraftError("❌ The minimum number of picks must be at least 1.")
    if new_max < new_min:
        raise DraftError("❌ The maximum number of picks cannot be less than the minimum.")

    conn.execute(
        "UPDATE drafts SET budget = ?, min_picks = ?, max_picks = ?, sheet_url = ? WHERE id = ?",
        (new_budget, new_min, new_max, sheet_url if sheet_url is not None else draft["sheet_url"],
         draft["id"]),
    )
    # No picks exist yet, so every player simply gets the new budget.
    conn.execute("UPDATE players SET budget_remaining = ? WHERE draft_id = ?",
                 (new_budget, draft["id"]))
    conn.commit()
    return get_draft(conn, draft["id"])


def cancel_draft(conn, draft_id):
    conn.execute(
        "UPDATE drafts SET status = 'cancelled', finished_at = ?, turn_deadline = NULL WHERE id = ?",
        (database.now(), draft_id),
    )
    conn.commit()


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
        # The seat number doubles as the coach number on the spreadsheet.
        event["position"] = player["draft_position"]
    event.update(fields)
    return event


def start_draft(conn, draft):
    """/draft - flip the draft to active and open round 1."""
    if draft["status"] == "active":
        raise DraftError("❌ This draft is already running.")
    if draft["status"] != "setup":
        raise DraftError("❌ This draft has already finished.")
    if player_count(conn, draft["id"]) < config.MIN_PLAYERS:
        raise DraftError(f"❌ A draft needs at least {config.MIN_PLAYERS} players.")

    conn.execute(
        """UPDATE drafts SET status = 'active', current_round = 1, current_turn_index = 0,
                             queue_exhausted = 0, halved_this_turn = 0, started_at = ? WHERE id = ?""",
        (database.now(), draft["id"]),
    )
    conn.commit()
    return begin_turn(conn, draft["id"])


def advance_turn(conn, draft_id):
    """Move the pointer to the next seat, rolling into the next round.

    Also finishes the draft when the last round is done, or when no player
    can afford another pick.
    """
    draft = get_draft(conn, draft_id)
    total = player_count(conn, draft_id)

    index = draft["current_turn_index"] + 1
    round_number = draft["current_round"]

    if index >= total:            # the round is over
        index = 0
        round_number += 1

    # The draft also ends early if nobody can afford anything any more.
    if round_number > draft["max_picks"] or not anyone_can_pick(conn, draft):
        conn.execute(
            """UPDATE drafts SET status = 'complete', finished_at = ?, turn_deadline = NULL,
                                 queue_exhausted = 0, halved_this_turn = 0 WHERE id = ?""",
            (database.now(), draft_id),
        )
        conn.commit()
        return

    conn.execute(
        """UPDATE drafts SET current_round = ?, current_turn_index = ?, turn_deadline = NULL,
                             queue_exhausted = 0, halved_this_turn = 0 WHERE id = ?""",
        (round_number, index, draft_id),
    )
    conn.commit()


def _halve_timer(conn, draft_id, player):
    """Divide this player's personal timer, respecting the configured floor.

    A turn may only cost a player one reduction. Without this guard a player
    who lets a makeup lapse AND then misses the new round would be punished
    twice for what is really one absence: 120 -> 60 -> 30 in a single turn.

    Returns (timer, was_reduced).
    """
    draft = get_draft(conn, draft_id)
    if draft["halved_this_turn"]:
        return player["current_timer"], False

    reduced = max(config.MINIMUM_TIMER_SECONDS,
                  player["current_timer"] // config.TIMER_DIVISOR)
    conn.execute(
        "UPDATE players SET current_timer = ?, skip_count = skip_count + 1 WHERE id = ?",
        (reduced, player["id"]),
    )
    conn.execute("UPDATE drafts SET halved_this_turn = 1 WHERE id = ?", (draft_id,))
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
           WHERE draft_id = ? AND player_id = ? AND status = 'pending' AND round <= ?
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
            "INSERT INTO skips (draft_id, player_id, round, reason, halved, created_at) "
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
           WHERE draft_id = ? AND player_id = ? AND round = ?
           ORDER BY priority""",
        (draft["id"], player["id"], draft["current_round"]),
    ).fetchall()

    if not queued:
        return None, False

    for entry in queued:
        cost = entry["cost"] if entry["cost"] is not None else 0
        try:
            validate_pick(conn, draft, player, draft["current_round"], entry["pokemon"], cost)
        except DraftError:
            continue        # already taken, too expensive, ... try the next one
        pick = _record_pick(conn, draft, player, draft["current_round"],
                            entry["pokemon"], cost, is_makeup=False, is_auto=True)
        return pick, True

    return None, True


def begin_turn(conn, draft_id):
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

        draft = get_draft(conn, draft_id)
        if draft["status"] != "active":
            if draft["status"] == "complete":
                events.append(_event("draft_complete", total_rounds=draft["max_picks"],
                                     round_reached=draft["current_round"]))
            break

        player = current_player(conn, draft)

        # 1. Somebody who has filled their roster or run out of budget is
        #    passed over silently-ish: no skip, no makeup, no timer penalty.
        if not player_can_pick(conn, draft, player):
            events.append(_event("passed", player, round=draft["current_round"],
                                 reason=("roster full"
                                         if picks_made(conn, draft["id"], player["id"])
                                         >= draft["max_picks"] else "out of budget")))
            advance_turn(conn, draft_id)
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
                conn, draft_id, reason="makeup deadline passed",
                halve=False if instant else None, _nested=True))
            continue

        # 3. Queued picks: draft automatically if anything in the list is legal.
        pick, had_queue = _try_queue(conn, draft, player)
        if pick is not None:
            events.append(_event("auto_pick", player, round=pick["round"],
                                 pokemon=pick["pokemon"], cost=pick["cost"],
                                 pick_order=pick["pick_order"]))
            advance_turn(conn, draft_id)
            continue    # the next player might also have a queue

        if had_queue:
            # Every queued Pokemon was gone. This must NOT be treated as a
            # normal skip, so flag the turn: if the clock later runs out the
            # player's timer is left alone.
            conn.execute("UPDATE drafts SET queue_exhausted = 1 WHERE id = ?", (draft_id,))
            conn.commit()
            events.append(_event("queue_exhausted", player, round=draft["current_round"]))

        # 4. Nothing automatic happened - start the clock.
        events.append(_event("turn_started", player, round=draft["current_round"],
                             seconds=player["current_timer"],
                             pending_makeups=pending_makeup_rounds(conn, draft_id, player["id"])))
        break

    return events


def open_turn_clock(conn, draft_id, deadline):
    """Store when the current turn runs out (survives a bot restart)."""
    conn.execute("UPDATE drafts SET turn_deadline = ? WHERE id = ?", (deadline, draft_id))
    conn.commit()


def skip_current_player(conn, draft_id, reason="timer expired", halve=None, _nested=False):
    """Skip whoever is on the clock and move on.

    `halve` defaults to "yes, unless the player's queue was exhausted" -
    a player whose queue was wiped out by other people should not be
    punished with a shorter timer.
    """
    draft = get_draft(conn, draft_id)
    if draft["status"] != "active":
        return []

    player = current_player(conn, draft)
    if halve is None:
        # A player whose whole queue was drafted out from under them keeps
        # their timer: that was not their fault.
        halve = not draft["queue_exhausted"]

    if halve:
        new_timer, halve = _halve_timer(conn, draft_id, player)
    else:
        new_timer = player["current_timer"]

    conn.execute(
        "INSERT INTO skips (draft_id, player_id, round, reason, halved, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (draft_id, player["id"], draft["current_round"], reason, 1 if halve else 0, database.now()),
    )
    # Owed a makeup pick for the round they just missed, due before their
    # next turn. INSERT OR IGNORE protects the UNIQUE(draft, player, round)
    # constraint. created_turn is what lets the boundary grace period work.
    conn.execute(
        "INSERT OR IGNORE INTO makeups (draft_id, player_id, round, status, created_turn, created_at) "
        "VALUES (?, ?, ?, 'pending', ?, ?)",
        (draft_id, player["id"], draft["current_round"], turn_number(conn, draft), database.now()),
    )
    conn.commit()

    events = [_event("skipped", get_player_by_id(conn, player["id"]),
                     round=draft["current_round"], new_timer=new_timer, halved=halve,
                     queue_exhausted=bool(draft["queue_exhausted"]))]

    advance_turn(conn, draft_id)
    if not _nested:
        events.extend(begin_turn(conn, draft_id))
    return events


# --------------------------------------------------------------------------
# Making picks
# --------------------------------------------------------------------------


def _record_pick(conn, draft, player, round_number, pokemon, cost,
                 is_makeup=False, is_auto=False):
    """Write a validated pick to history + roster and charge the budget."""
    key = normalize(pokemon)
    order = next_pick_order(conn, draft["id"])

    cursor = conn.execute(
        """INSERT INTO picks (draft_id, player_id, round, pokemon, pokemon_key, cost,
                              pick_order, is_makeup, is_auto, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (draft["id"], player["id"], round_number, str(pokemon).strip(), key, cost,
         order, 1 if is_makeup else 0, 1 if is_auto else 0, database.now()),
    )
    pick_id = cursor.lastrowid

    conn.execute(
        """INSERT INTO rosters (draft_id, player_id, pokemon, pokemon_key, cost, pick_id, source)
           VALUES (?, ?, ?, ?, ?, ?, 'draft')""",
        (draft["id"], player["id"], str(pokemon).strip(), key, cost, pick_id),
    )
    conn.execute(
        "UPDATE players SET budget_remaining = budget_remaining - ? WHERE id = ?",
        (cost, player["id"]),
    )
    # The queue for this round has now been used up.
    conn.execute(
        "DELETE FROM queued_picks WHERE draft_id = ? AND player_id = ? AND round = ?",
        (draft["id"], player["id"], round_number),
    )
    conn.commit()

    return conn.execute("SELECT * FROM picks WHERE id = ?", (pick_id,)).fetchone()


def make_pick(conn, draft, user_id, round_number, pokemon, cost, acting_as_admin=False):
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

    validate_pick(conn, draft, player, round_number, pokemon, cost)
    pick = _record_pick(conn, draft, player, round_number, pokemon, cost)

    events = [_event("pick_made", get_player_by_id(conn, player["id"]),
                     round=round_number, pokemon=pick["pokemon"], cost=cost,
                     pick_order=pick["pick_order"])]
    advance_turn(conn, draft["id"])
    events.extend(begin_turn(conn, draft["id"]))
    return pick, events


def pending_makeup_rounds(conn, draft_id, player_id):
    rows = conn.execute(
        "SELECT round FROM makeups WHERE draft_id = ? AND player_id = ? AND status = 'pending' "
        "ORDER BY round",
        (draft_id, player_id),
    ).fetchall()
    return [row["round"] for row in rows]


def make_makeup_pick(conn, draft, user_id, round_number, pokemon, cost):
    """/makeuppick - resolve a missed round. Never changes whose turn it is.

    Also allowed once the draft is complete, so that somebody skipped in the
    final round still gets their chance to catch up.
    """
    if draft["status"] not in ("active", "complete"):
        raise DraftError("❌ This draft is not running.")

    player = require_player(conn, draft["id"], user_id)

    makeup = conn.execute(
        "SELECT * FROM makeups WHERE draft_id = ? AND player_id = ? AND round = ?",
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

    validate_pick(conn, draft, player, round_number, pokemon, cost)
    pick = _record_pick(conn, draft, player, round_number, pokemon, cost, is_makeup=True)

    conn.execute("UPDATE makeups SET status = 'resolved', resolved_at = ? WHERE id = ?",
                 (database.now(), makeup["id"]))
    conn.commit()
    return pick


def queue_picks(conn, draft, user_id, round_number, pokemon_names, costs=None):
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

    conn.execute(
        "DELETE FROM queued_picks WHERE draft_id = ? AND player_id = ? AND round = ?",
        (draft["id"], player["id"], round_number),
    )
    for priority, (name, cost) in enumerate(zip(pokemon_names, costs), start=1):
        conn.execute(
            """INSERT INTO queued_picks (draft_id, player_id, round, priority,
                                         pokemon, pokemon_key, cost)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (draft["id"], player["id"], round_number, priority,
             name.strip(), normalize(name), cost),
        )
    conn.commit()
    return pokemon_names


def get_queue(conn, draft_id, player_id, round_number):
    return conn.execute(
        "SELECT * FROM queued_picks WHERE draft_id = ? AND player_id = ? AND round = ? "
        "ORDER BY priority",
        (draft_id, player_id, round_number),
    ).fetchall()


def edit_pick(conn, draft, actor_user_id, round_number, pokemon, cost,
              target_user_id=None, acting_as_admin=False):
    """/editpick - replace a pick that has already been made."""
    target_user_id = target_user_id or actor_user_id
    if target_user_id != actor_user_id and not acting_as_admin:
        raise DraftError("❌ Only an admin can edit somebody else's pick.")

    player = require_player(conn, draft["id"], target_user_id)

    pick = conn.execute(
        "SELECT * FROM picks WHERE draft_id = ? AND player_id = ? AND round = ?",
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

        validate_pick(conn, draft, player, round_number, pokemon, cost)
        created = _record_pick(conn, draft, player, round_number, pokemon, cost)
        # Filling in the round settles any makeup pick owed for it.
        conn.execute(
            "UPDATE makeups SET status = 'resolved', resolved_at = ? "
            "WHERE draft_id = ? AND player_id = ? AND round = ? AND status = 'pending'",
            (database.now(), draft["id"], player["id"], round_number),
        )
        conn.commit()
        return {
            "player": get_player_by_id(conn, player["id"]),
            "round": round_number,
            "old_pokemon": None,
            "old_cost": None,
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
    refreshed = get_player_by_id(conn, player["id"])

    try:
        validate_pick(conn, draft, refreshed, round_number, pokemon, cost,
                      replacing_key=pick["pokemon_key"])
    except DraftError:
        conn.rollback()
        raise

    key = normalize(pokemon)
    conn.execute("UPDATE picks SET pokemon = ?, pokemon_key = ?, cost = ? WHERE id = ?",
                 (str(pokemon).strip(), key, cost, pick["id"]))
    conn.execute("UPDATE rosters SET pokemon = ?, pokemon_key = ?, cost = ? WHERE id = ?",
                 (str(pokemon).strip(), key, cost, roster_row["id"]))
    conn.execute("UPDATE players SET budget_remaining = budget_remaining - ? WHERE id = ?",
                 (cost, player["id"]))
    conn.commit()

    return {
        "player": get_player_by_id(conn, player["id"]),
        "round": round_number,
        "old_pokemon": pick["pokemon"],
        "old_cost": pick["cost"],
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

    number = conn.execute(
        "SELECT COALESCE(MAX(trade_number), 0) + 1 AS n FROM trades WHERE draft_id = ?",
        (draft["id"],),
    ).fetchone()["n"]

    cursor = conn.execute(
        """INSERT INTO trades (draft_id, trade_number, proposer_id, partner_id,
                               drops, gets, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)""",
        (draft["id"], number, proposer["id"], partner["id"] if partner else None,
         json.dumps(drops), json.dumps(gets), database.now()),
    )
    conn.commit()
    record = conn.execute("SELECT * FROM trades WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return record, warnings


def trades_used(conn, draft_id, player_id):
    """How many approved trades a player is already part of."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM trades WHERE draft_id = ? AND status = 'approved' "
        "AND (proposer_id = ? OR partner_id = ?)",
        (draft_id, player_id, player_id),
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


def get_trade(conn, draft_id, trade_number):
    return conn.execute(
        "SELECT * FROM trades WHERE draft_id = ? AND trade_number = ?",
        (draft_id, trade_number),
    ).fetchone()


def approve_trade(conn, draft, trade_number, approver_user_id):
    """/approvetrade - apply a pending trade to the rosters.

    The admin has already decided the trade is legal, so this applies it as
    written and reports anything it could not do rather than refusing.
    """
    trade = get_trade(conn, draft["id"], trade_number)
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
                """INSERT INTO rosters (draft_id, player_id, pokemon, pokemon_key, cost, source)
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
    return get_trade(conn, draft["id"], trade_number), notes


def reject_trade(conn, draft, trade_number, approver_user_id):
    trade = get_trade(conn, draft["id"], trade_number)
    if trade is None:
        raise DraftError(f"❌ There is no trade #{trade_number} in this draft.")
    if trade["status"] != "pending":
        raise DraftError(f"❌ Trade #{trade_number} has already been {trade['status']}.")
    conn.execute(
        "UPDATE trades SET status = 'rejected', resolved_at = ?, resolved_by = ? WHERE id = ?",
        (database.now(), approver_user_id, trade["id"]),
    )
    conn.commit()
    return get_trade(conn, draft["id"], trade_number)


# --------------------------------------------------------------------------
# Read-only views used by /roster, /progress and /status
# --------------------------------------------------------------------------


def roster_view(conn, draft, player):
    rows = conn.execute(
        "SELECT * FROM rosters WHERE draft_id = ? AND player_id = ? ORDER BY id",
        (draft["id"], player["id"]),
    ).fetchall()
    spent = sum(row["cost"] for row in rows)
    return {
        "player": player,
        "pokemon": [(row["pokemon"], row["cost"]) for row in rows],
        "spent": spent,
        "remaining": player["budget_remaining"],
        "count": len(rows),
        "max_picks": draft["max_picks"],
        "min_picks": draft["min_picks"],
    }


def round_picks(conn, draft_id, round_number):
    return conn.execute(
        """SELECT picks.*, players.display_name, players.user_id
           FROM picks JOIN players ON players.id = picks.player_id
           WHERE picks.draft_id = ? AND picks.round = ?
           ORDER BY picks.pick_order""",
        (draft_id, round_number),
    ).fetchall()


def completed_rounds(conn, draft):
    """Rounds that have at least one pick, most recent last."""
    rows = conn.execute(
        "SELECT DISTINCT round FROM picks WHERE draft_id = ? ORDER BY round", (draft["id"],)
    ).fetchall()
    return [row["round"] for row in rows]


def roster_block(conn, draft, player, rows):
    """The column of names to write into one coach's roster block.

    Index 0 is round 1. A Pokemon this player drafted sits in its own round's
    row, so the block reads like the draft did. Anything dropped in a trade
    simply leaves its row empty, and anything gained in a trade has no round
    of its own, so it drops into the first free row - repeating until the
    block is full.

    That keeps the block a true picture of the current roster, which is what
    the spreadsheet adds up to work out the coach's remaining budget.
    """
    block = [""] * rows
    leftovers = []

    owned = conn.execute(
        "SELECT * FROM rosters WHERE draft_id = ? AND player_id = ? ORDER BY id",
        (draft["id"], player["id"]),
    ).fetchall()

    for entry in owned:
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

        if round_number and 1 <= round_number <= rows and not block[round_number - 1]:
            block[round_number - 1] = entry["pokemon"]
        else:
            leftovers.append(entry["pokemon"])

    for name in leftovers:
        for index in range(rows):
            if not block[index]:
                block[index] = name
                break

    return block


def export_for_sheet(conn, draft_id):
    """Everything the spreadsheet needs, read here on the main thread.

    Returns (coach_names, roster_blocks, overflow):
      coach_names   - usernames in draft order, for Coaches!B
      roster_blocks - {draft_position: [name per round]}, for the Rosters tab
      overflow      - {display_name: count} for anyone whose roster no longer
                      fits in a block, which can only happen if trades have
                      added more Pokemon than they removed

    Reading here means the background thread that talks to Google never
    touches the SQLite connection.
    """
    draft = get_draft(conn, draft_id)
    rows = config.SHEET_LAYOUT["roster_rows"]

    coach_names = []
    roster_blocks = {}
    overflow = {}

    for player in get_players(conn, draft_id):
        coach_names.append(player["display_name"])
        block = roster_block(conn, draft, player, rows)
        roster_blocks[player["draft_position"]] = block

        owned = roster_size(conn, draft_id, player["id"])
        shown = sum(1 for name in block if name)
        if owned > shown:
            overflow[player["display_name"]] = owned - shown

    return coach_names, roster_blocks, overflow


def status_view(conn, draft):
    player = current_player(conn, draft)
    total_picks = conn.execute(
        "SELECT COUNT(*) AS n FROM picks WHERE draft_id = ?", (draft["id"],)
    ).fetchone()["n"]

    pending = conn.execute(
        """SELECT makeups.round, players.display_name, players.user_id
           FROM makeups JOIN players ON players.id = makeups.player_id
           WHERE makeups.draft_id = ? AND makeups.status = 'pending'
           ORDER BY makeups.round""",
        (draft["id"],),
    ).fetchall()

    return {
        "draft": draft,
        "current_player": player,
        "total_picks": total_picks,
        "expected_picks": draft["max_picks"] * player_count(conn, draft["id"]),
        "pending_makeups": [(row["display_name"], row["round"]) for row in pending],
    }
