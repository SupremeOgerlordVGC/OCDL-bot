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
"""

import re

import database
import draft as rules

SOURCES = ("battle", "replay", "manual")
MAX_GAMES = 5


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
    row = conn.execute(
        "SELECT players.* FROM players JOIN pools ON pools.id = players.pool_id "
        "WHERE pools.league_id = ? AND players.user_id = ?", (league_id, user_id)).fetchone()
    if row is None:
        raise rules.DraftError("❌ You aren't playing in this league.")
    return row


def roster_of(conn, player):
    return [row["pokemon"] for row in conn.execute(
        "SELECT pokemon FROM rosters WHERE player_id = ? ORDER BY id", (player["id"],))]


def record_game(conn, league_id, week, match_number, game, source, winner_id=None, stats=None,
                replay=None, only_player=None):
    """Record (or correct) one game. `stats` is {player_id: [(pokemon, kills, deaths), ...]}: with
    only_player, just that player's side is replaced (a player reporting their own Pokémon)."""
    if not 1 <= game <= MAX_GAMES:
        raise rules.DraftError(f"❌ Games are numbered 1 to {MAX_GAMES}.")
    if source not in SOURCES:
        raise rules.DraftError("❌ Unknown kind of report.")
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
                     (set_winner(conn, report["id"]), database.now(), report["id"]))
    return match_view(conn, league_id, week, match_number)


def set_winner(conn, report_id):
    """Whoever won more games, or None while it's level"""
    counts = {}
    for row in conn.execute("SELECT winner_id FROM game_results WHERE report_id = ? AND winner_id IS NOT NULL",
                            (report_id,)):
        counts[row["winner_id"]] = counts.get(row["winner_id"], 0) + 1
    ranked = sorted(counts.items(), key=lambda item: -item[1])
    if not ranked or (len(ranked) > 1 and ranked[0][1] == ranked[1][1]):
        return None
    return ranked[0][0]


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
                     (set_winner(conn, report["id"]), database.now(), report["id"]))
    return match_view(conn, league_id, week, match_number)


def match_view(conn, league_id, week, match_number):
    """Everything reported for a match: {champions, winner_id, games: [{game, winner_id, source, replay,
    stats: {player_id: [(pokemon, kills, deaths)]}}], pokepastes: {player_id: url}, totals: {...}}"""
    report = get_report(conn, league_id, week, match_number)
    if report is None:
        return {"champions": False, "asked": [], "winner_id": None, "games": [], "pokepastes": {}, "totals": {}}
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
    return {"champions": bool(report["champions"]), "asked": asked, "winner_id": report["winner_id"],
            "games": games, "pokepastes": pastes, "totals": totals}
