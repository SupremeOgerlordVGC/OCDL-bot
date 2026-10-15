"""
Match results: each game of a week's match, who won it, and each Pokémon's kills and deaths.

They arrive four ways, and all end up in the same tables:

- 'battle'    a website-compatible league's match, played on OgreClan's Showdown server with Ready on
              draft.ogreclan.com: recorded by itself when each game ends.
- 'replay'    a player gives the game's replay link from replay.ogreclan.com (/reportreplay, or the site),
              and the site reads both sides' kills and deaths from it.
- 'manual'    a player types their own Pokémon's kills and deaths for a game (/reportgame, or the site).
- Champions:  if both players agree (/champions, or the site), the match is played on Pokémon Champions instead;
              each player reports their own side by hand and gives their Pokepaste (/pokepaste).

Each player reports their own side; a replay or a battle fills in both. Nothing needs the opponent to confirm it:
staff correct mistakes. The tables are new ones, added alongside the bot's own (see database.SCHEMA).

Finishing a match does one thing besides recording it: both players' approved trades take effect
(`match_finished`, and `bot.trades_after_match`), because the team that played the match is the team the
match was played with.
"""

import re

import config
import database
import draft as rules

SOURCES = ("battle", "replay", "manual")
MAX_GAMES = config.MAX_GAMES_PER_MATCH


def _tokens(name):
    return set(re.findall(r"[a-z0-9]+", str(name).lower())) - {"mega"}


def roster_name(species, roster):
    """The roster's spelling of a Pokémon a battle calls `species`: "Floette-Eternal" is the drafted
    "Mega Eternal-Floette". Falls back to the battle's name when nothing on the roster fits."""
    wanted = _tokens(species)
    for name in roster:
        if rules.normalize(name) == rules.normalize(species):
            return name
    for name in roster:
        if _tokens(name) == wanted:
            return name
    for name in roster:
        tokens = _tokens(name)
        if tokens and (tokens <= wanted or wanted <= tokens):
            return name
    return str(species)


def get_report(conn, league_id, week, match_number, create=False):
    row = conn.execute(
        "SELECT * FROM match_reports WHERE league_id = ? AND week = ? AND match_number = ?",
        (league_id, week, match_number)).fetchone()
    if row is None and create:
        conn.execute(
            "INSERT INTO match_reports (league_id, week, match_number, updated_at) VALUES (?, ?, ?, ?)",
            (league_id, week, match_number, database.now()))
        conn.commit()
        row = get_report(conn, league_id, week, match_number)
    return row


def find_match(conn, league_id, week, player_id):
    """The fixture a player has in a week, or a DraftError"""
    row = conn.execute(
        "SELECT * FROM fixtures WHERE league_id = ? AND week = ? AND (home_player_id = ? OR away_player_id = ?)",
        (league_id, week, player_id, player_id)).fetchone()
    if row is None:
        raise rules.DraftError(f"❌ You don't have a match in week {week}.")
    return row


def player_in_league(conn, league_id, user_id):
    """This player's seat anywhere in the league, or a refusal."""
    _, row = rules.player_in_league(conn, league_id, user_id)
    if row is None:
        raise rules.DraftError("❌ You aren't playing in this league.")
    return row


def roster_of(conn, player):
    return [row["pokemon"] for row in conn.execute(
        "SELECT pokemon FROM rosters WHERE player_id = ? ORDER BY id", (player["id"],))]


def reportable(conn, player, report):
    """What a player may report kills and deaths for in a match: their roster
    as it is now, plus anything already recorded for them in this match.

    Finishing a match is what puts their approved trades into effect, so by
    the time somebody corrects a game their team may have changed under
    them. What they played with is still written in the report, so it counts.
    """
    names = roster_of(conn, player)
    if report is None:
        return names
    for row in conn.execute(
            "SELECT DISTINCT game_stats.pokemon AS pokemon FROM game_stats "
            "JOIN game_results ON game_results.id = game_stats.result_id "
            "WHERE game_results.report_id = ? AND game_stats.player_id = ?",
            (report["id"], player["id"])):
        if row["pokemon"] not in names:
            names.append(row["pokemon"])
    return names


def trades_after_match(conn, league, week, match_number):
    """Both players' approved trades, put into effect because their match is over.

    Nothing moves while a match is being played, because the team that
    starts it has to be the team that finishes it. Once it is over it is
    over for both players, so both sides' trades land - except any that is
    waiting for another trade, which keeps waiting (draft.blocking_trade).

    Returns [(trade, notes)], empty when the match is not finished.
    """
    if not match_finished(conn, league, week, match_number):
        return []
    fixture = conn.execute(
        "SELECT * FROM fixtures WHERE league_id = ? AND week = ? AND match_number = ?",
        (league["id"], week, match_number)).fetchone()
    if fixture is None:
        return []
    seats = [fixture["home_player_id"], fixture["away_player_id"]]
    return rules.apply_trades(conn, league["id"], (league["current_week"] or 0) + 1,
                              player_ids=seats)


def require_open(conn, league, week, match_number, game):
    """Refuse a report that a finished match cannot have.

    Correcting a game that is already down is always allowed - that is how a
    mistake gets fixed - so only a new game is refused. Checked before a
    player's Pokémon are even read, because "that match is already over" is
    the answer they need, not "Garchomp isn't on your roster" after a trade
    took effect.
    """
    report = get_report(conn, league["id"], week, match_number)
    if report is None:
        return
    recorded = conn.execute(
        "SELECT 1 FROM game_results WHERE report_id = ? AND game = ?",
        (report["id"], game)).fetchone()
    if recorded is None and match_score(conn, league, week, match_number)["decided"]:
        raise rules.DraftError(
            f"❌ That match is already over, so there is no game {game} to "
            f"report. A moderator can undo a game with `/cleargame`, or take "
            f"back an awarded result with `/setresult`.")


def record_game(conn, league_id, week, match_number, game, source, winner_id=None, stats=None,
                replay=None, only_player=None):
    """Record (or correct) one game. `stats` is {player_id: [(pokemon, kills, deaths), ...]}: with
    only_player, just that player's side is replaced (a player reporting their own Pokémon).

    A game that the match cannot have is refused, and so is a new game once
    the match is over - a best-of-three has no game 4, and nothing after 2-0
    counts. Correcting a game that is already recorded is always allowed,
    because that is how a mistake gets fixed."""
    if source not in SOURCES:
        raise rules.DraftError("❌ Unknown kind of report.")
    league = rules.get_league(conn, league_id)
    best_of = int((league["best_of"] if league else None) or MAX_GAMES)
    if not 1 <= game <= min(best_of, MAX_GAMES):
        raise rules.DraftError(
            f"❌ This league plays **best of {best_of}**, so its games are "
            f"numbered 1 to {min(best_of, MAX_GAMES)}.")
    if league is not None:
        require_open(conn, league, week, match_number, game)
    report = get_report(conn, league_id, week, match_number, create=True)
    result = conn.execute("SELECT * FROM game_results WHERE report_id = ? AND game = ?",
                          (report["id"], game)).fetchone()
    with conn:
        if result is None:
            cursor = conn.execute(
                "INSERT INTO game_results (report_id, game, winner_id, replay, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (report["id"], game, winner_id, replay, source, database.now()))
            result_id = cursor.lastrowid
        else:
            result_id = result["id"]
            conn.execute(
                "UPDATE game_results SET winner_id = COALESCE(?, winner_id), replay = COALESCE(?, replay), "
                "source = ?, updated_at = ? WHERE id = ?",
                (winner_id, replay, source, database.now(), result_id))
        for player_id, mons in (stats or {}).items():
            if only_player is not None and player_id != only_player:
                continue
            conn.execute("DELETE FROM game_stats WHERE result_id = ? AND player_id = ?", (result_id, player_id))
            for pokemon, kills, deaths in mons:
                if kills < 0 or deaths < 0:
                    raise rules.DraftError("❌ Kills and deaths can't be negative.")
                conn.execute(
                    "INSERT INTO game_stats (result_id, player_id, pokemon, kills, deaths) VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT (result_id, player_id, pokemon) DO UPDATE SET kills = kills + excluded.kills, "
                    "deaths = deaths + excluded.deaths",
                    (result_id, player_id, pokemon, int(kills), int(deaths)))
        conn.execute("UPDATE match_reports SET winner_id = ?, updated_at = ? WHERE id = ?",
                     (set_winner(conn, report), database.now(), report["id"]))
    return match_view(conn, league_id, week, match_number)


def wins_for(best_of):
    """How many games win a best-of-N: a majority, so two of three."""
    return max(1, int(best_of or 1) // 2 + 1)


def wins_needed(league):
    """How many games win this league's match."""
    return wins_for(league["best_of"])


def game_wins(conn, report_id):
    """{player_id: games won} for a match"""
    counts = {}
    for row in conn.execute("SELECT winner_id FROM game_results WHERE report_id = ? "
                            "AND winner_id IS NOT NULL", (report_id,)):
        counts[row["winner_id"]] = counts.get(row["winner_id"], 0) + 1
    return counts


def decided_winner(conn, report, best_of):
    """Who has won the match, or None while it is still undecided.

    A moderator's award wins over the games: a forfeit or a no-show has a
    winner without the games to show for it.
    """
    if report is None:
        return None
    if report["walkover_id"]:
        return report["walkover_id"]
    needed = wins_for(best_of)
    for player_id, won in sorted(game_wins(conn, report["id"]).items(), key=lambda i: -i[1]):
        if won >= needed:
            return player_id
    return None


def match_score(conn, league, week, match_number):
    """How a match stands, for both the rules and the message about it:

        {wins: {player_id: games won}, needed, played, winner_id, decided,
         walkover, note}

    `decided` is the whole point: it is what puts a player's approved trades
    into effect, so it has to mean the match is over rather than somebody
    being ahead. Winning game 1 of a best-of-three is not the end of it, and
    the team that plays game 2 has to be the team that played game 1.
    """
    report = get_report(conn, league["id"], week, match_number)
    wins = game_wins(conn, report["id"]) if report is not None else {}
    winner_id = decided_winner(conn, report, league["best_of"])
    return {
        "wins": wins,
        "needed": wins_needed(league),
        "played": sum(wins.values()),
        "winner_id": winner_id,
        "decided": winner_id is not None,
        "walkover": bool(report["walkover_id"]) if report is not None else False,
        "note": (report["note"] if report is not None else None),
    }


def match_finished(conn, league, week, match_number):
    """Whether a match is over. See match_score."""
    return match_score(conn, league, week, match_number)["decided"]


def set_winner(conn, report):
    """The match's winner as things stand, for storing on the report."""
    league = rules.get_league(conn, report["league_id"])
    return decided_winner(conn, report, league["best_of"] if league else None)


def award_match(conn, league, week, match_number, winner_id, note=None):
    """Staff: award a match that was not played out - a forfeit, a no-show, a
    result settled elsewhere. `winner_id` of None takes the award back and
    leaves whatever the games say."""
    report = get_report(conn, league["id"], week, match_number, create=True)
    with conn:
        conn.execute("UPDATE match_reports SET walkover_id = ?, note = ?, updated_at = ? "
                     "WHERE id = ?",
                     (winner_id, (note or "").strip() or None, database.now(), report["id"]))
        report = get_report(conn, league["id"], week, match_number)
        conn.execute("UPDATE match_reports SET winner_id = ? WHERE id = ?",
                     (set_winner(conn, report), report["id"]))
    return match_view(conn, league["id"], week, match_number)


def parse_kd(text, roster):
    """A player's typed kills and deaths: "Garchomp 2-0, Rotom-Wash 1/1" -> [(roster name, 2, 0), ...]"""
    entries = []
    for part in re.split(r"[,;\n]", str(text or "")):
        part = part.strip()
        if not part:
            continue
        found = re.match(r"^(.+?)\s+(\d+)\s*[-/:]\s*(\d+)$", part)
        if not found:
            raise rules.DraftError(f"❌ Couldn't read “{part}”. Write each one like `Garchomp 2-0` (kills-deaths).")
        name, kills, deaths = found.group(1).strip(), int(found.group(2)), int(found.group(3))
        matched = [mon for mon in roster if rules.normalize(mon) == rules.normalize(name)] or \
            [mon for mon in roster if _tokens(mon) == _tokens(name)]
        if not matched:
            raise rules.DraftError(f"❌ {name} isn't on your roster.")
        entries.append((matched[0], kills, deaths))
    if not entries:
        raise rules.DraftError("❌ Give each of your Pokémon's kills and deaths, like `Garchomp 2-0, Rotom-Wash 1-1`.")
    return entries


def ask_champions(conn, league_id, week, match_number, player_id, on=True):
    """A player agrees (or stops agreeing) to play the match on Pokémon Champions; it's on once both have"""
    report = get_report(conn, league_id, week, match_number, create=True)
    asked = {int(x) for x in str(report["champions_requested_by"] or "").split(",") if x}
    if on:
        asked.add(player_id)
    else:
        asked.discard(player_id)
    fixture = conn.execute("SELECT * FROM fixtures WHERE league_id = ? AND week = ? AND match_number = ?",
                           (league_id, week, match_number)).fetchone()
    both = fixture is not None and {fixture["home_player_id"], fixture["away_player_id"]} <= asked
    conn.execute("UPDATE match_reports SET champions = ?, champions_requested_by = ?, updated_at = ? WHERE id = ?",
                 (1 if both else 0, ",".join(str(x) for x in sorted(asked)), database.now(), report["id"]))
    conn.commit()
    return match_view(conn, league_id, week, match_number)


def set_pokepaste(conn, league_id, week, match_number, player_id, url):
    url = str(url or "").strip()
    if not re.match(r"^https?://(www\.)?pokepast\.es/[A-Za-z0-9]+/?$", url):
        raise rules.DraftError("❌ Give a Pokepaste link, like https://pokepast.es/abc123.")
    report = get_report(conn, league_id, week, match_number, create=True)
    conn.execute(
        "INSERT INTO pokepastes (report_id, player_id, url, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT (report_id, player_id) DO UPDATE SET url = excluded.url, updated_at = excluded.updated_at",
        (report["id"], player_id, url, database.now()))
    conn.commit()
    return match_view(conn, league_id, week, match_number)


def clear_game(conn, league_id, week, match_number, game):
    """Staff: remove a game's result (to have it reported again)"""
    report = get_report(conn, league_id, week, match_number)
    if report is None:
        raise rules.DraftError("❌ Nothing has been reported for that match.")
    with conn:
        conn.execute("DELETE FROM game_results WHERE report_id = ? AND game = ?", (report["id"], game))
        conn.execute("UPDATE match_reports SET winner_id = ?, updated_at = ? WHERE id = ?",
                     (set_winner(conn, report), database.now(), report["id"]))
    return match_view(conn, league_id, week, match_number)


def sheet_match(conn, league, week, match_number):
    """One match in the shape the spreadsheet's Matches tab wants.

    Returns {"sides": [{"coach", "pokemon": [{"name", "games": [(kills,
    deaths) or None]}], "results": [...]}], "replays": [...], "walkover"}.

    The sheet gives each side six rows for the whole match, so the Pokémon
    are the ones that actually appeared, in the order they first did -
    anything past six is handed back by the writer as overflow rather than
    being dropped quietly.

    A walkover has no games to show. The template works a match result out
    by counting W's, so an awarded match is written as the wins it takes and
    no kills or deaths at all: the standings are then right, and the empty
    rows say plainly that nothing was played.
    """
    fixture = conn.execute(
        "SELECT * FROM fixtures WHERE league_id = ? AND week = ? AND match_number = ?",
        (league["id"], week, match_number)).fetchone()
    if fixture is None:
        raise rules.DraftError(f"❌ There is no match {match_number} in week {week}.")

    view = match_view(conn, league["id"], week, match_number)
    seats = [fixture["home_player_id"], fixture["away_player_id"]]
    needed = wins_needed(league)
    games = view["games"]

    sides = []
    for seat in seats:
        player = rules.get_player_by_id(conn, seat)
        order, stats = [], {}
        for index, game in enumerate(games):
            for pokemon, kills, deaths in game["stats"].get(seat, []):
                if pokemon not in stats:
                    order.append(pokemon)
                    stats[pokemon] = {}
                stats[pokemon][index] = (kills, deaths)
        results_row = []
        if view["walkover"]:
            # Enough W's (or L's) for the template's result formula, and
            # nothing else: a forfeit is a 2-0 in the standings.
            results_row = ["W" if view["winner_id"] == seat else "L"] * needed
        else:
            for game in games:
                if game["winner_id"] is None:
                    results_row.append("")
                else:
                    results_row.append("W" if game["winner_id"] == seat else "L")
        sides.append({
            "coach": player["display_name"],
            "pokemon": [{"name": name,
                         "games": [stats[name].get(index) for index in range(len(games))]}
                        for name in order],
            "results": results_row,
        })

    return {"sides": sides, "replays": [game["replay"] for game in games],
            "walkover": view["walkover"], "decided": view["decided"]}


def decided_matches(conn, league, week):
    """The week's match numbers that are over, in order."""
    numbers = [row["match_number"] for row in conn.execute(
        "SELECT match_number FROM fixtures WHERE league_id = ? AND week = ? "
        "ORDER BY match_number", (league["id"], week))]
    return [number for number in numbers if match_finished(conn, league, week, number)]


def match_view(conn, league_id, week, match_number):
    """Everything reported for a match: {champions, winner_id, games: [{game, winner_id, source, replay,
    stats: {player_id: [(pokemon, kills, deaths)]}}], pokepastes: {player_id: url}, totals: {...}},
    plus where the match stands (see match_score): wins, needed, decided, walkover, note."""
    report = get_report(conn, league_id, week, match_number)
    league = rules.get_league(conn, league_id)
    standing = (match_score(conn, league, week, match_number) if league is not None else
                {"wins": {}, "needed": 1, "played": 0, "winner_id": None, "decided": False,
                 "walkover": False, "note": None})
    if report is None:
        return {"champions": False, "asked": [], "winner_id": None, "games": [],
                "pokepastes": {}, "totals": {}, **standing}
    games = []
    totals = {}
    for result in conn.execute("SELECT * FROM game_results WHERE report_id = ? ORDER BY game", (report["id"],)):
        stats = {}
        for row in conn.execute("SELECT * FROM game_stats WHERE result_id = ? ORDER BY id", (result["id"],)):
            stats.setdefault(row["player_id"], []).append((row["pokemon"], row["kills"], row["deaths"]))
            mon = totals.setdefault(row["player_id"], {}).setdefault(row["pokemon"], [0, 0])
            mon[0] += row["kills"]
            mon[1] += row["deaths"]
        games.append({"game": result["game"], "winner_id": result["winner_id"], "source": result["source"],
                      "replay": result["replay"], "stats": stats})
    pastes = {row["player_id"]: row["url"] for row in conn.execute(
        "SELECT * FROM pokepastes WHERE report_id = ?", (report["id"],))}
    asked = [int(x) for x in str(report["champions_requested_by"] or "").split(",") if x]
    return {"champions": bool(report["champions"]), "asked": asked,
            "games": games, "pokepastes": pastes, "totals": totals, **standing}
