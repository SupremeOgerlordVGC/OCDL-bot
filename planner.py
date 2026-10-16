"""
Publishing a league to OgreClan's Showdown server, so the Draft Planner can import it.

The planner (in Showdown with `/planner`, or the Chrome extension) imports a league by opening its
pages on the Showdown server. Those leagues are not kept by the Showdown plugin: they live in the
site's database and the server loads them from there (see pokemon-showdown's
server/chat-plugins/draft/sync.ts), through `PUT /internal/leagues/{roomid}`. So publishing one of
the bot's leagues means writing it there in the shape league-core calls a `DraftLeague`.

**Matchups only, deliberately.** A roster would have to name every Pokémon by its *Showdown species
id* - "Mega Charizard Y" is `charizardmegay`, not `megacharizardy` - and the points the planner shows
come from a pool keyed the same way. The bot has the spreadsheet's spellings, so publishing rosters
would mean a species-id mapping that can quietly put the wrong Pokémon on a team. The schedule needs
none of it: a match is two player ids and a week, and "who do I play this week" is what the planner is
being asked for. See BRIDGE-NOTES.md.
"""

import json
import logging
import re

import config
import draft as rules

log = logging.getLogger("draftbot.planner")

# league-core's own defaults (packages/league-core/src/league.ts), so a published league is a
# complete DraftLeague however the shape grows: anything not set here keeps its default.
DEFAULT_SETTINGS = {
    "type": "points", "budget": 100, "picks": 10, "minPoints": 1, "maxPoints": 20,
    "snake": True, "shuffle": False, "pickTimer": 0, "tera": False, "teraCaptains": 2,
    "teraCaptainMaxPoints": 0, "freeAgents": 3, "trades": 3, "weeks": 7, "bestOf": 1,
}


def showdown_id(name):
    """A Showdown userid: lower case, letters and digits only. `toID` in Showdown."""
    return re.sub(r"[^a-z0-9]+", "", str(name or "").lower())


def players_for(conn, league_id):
    """Every active coach in the league, as DraftPlayers with empty rosters."""
    players = []
    for pool in rules.get_league_pools(conn, league_id):
        for player in rules.get_players(conn, pool["id"]):
            if player["status"] != rules.ACTIVE:
                continue
            players.append({
                "id": showdown_id(player["display_name"]),
                "name": player["display_name"],
                "roster": [], "captains": [], "teams": {},
                "freeAgentsUsed": 0, "tradesUsed": 0, "owedPicks": 0,
            })
    return players


def schedule_for(conn, league_id):
    """The fixtures as league-core wants them: a list of weeks, each a list of matches."""
    weeks = {}
    for row in conn.execute(
            """SELECT f.week, h.display_name AS home, a.display_name AS away
               FROM fixtures f JOIN players h ON h.id = f.home_player_id
               JOIN players a ON a.id = f.away_player_id
               WHERE f.league_id = ? ORDER BY f.week, f.match_number""", (league_id,)):
        weeks.setdefault(row["week"], []).append({
            "week": row["week"],
            "p1": showdown_id(row["home"]),
            "p2": showdown_id(row["away"]),
        })
    if not weeks:
        return []
    return [weeks.get(week, []) for week in range(1, max(weeks) + 1)]


def draft_league(conn, league, roomid):
    """The league as the Showdown server stores one, ready to PUT.

    `phase` is 'season': the draft is over and matches are being played, so the server has no pick
    to time out and nothing to run - it holds the league and serves its pages, which is all the
    planner needs. A free-type league is published because no points pool is sent with it; saying
    'points' with an empty pool would have the planner price every Pokémon at nothing.
    """
    schedule = schedule_for(conn, league["id"])
    settings = dict(DEFAULT_SETTINGS)
    settings.update({
        "format": config.PLANNER_FORMAT,
        "type": "free",
        "budget": league["budget"],
        "picks": league["max_picks"],
        "weeks": len(schedule) or (league["season_weeks"] or DEFAULT_SETTINGS["weeks"]),
        "bestOf": league["best_of"],
        "tera": bool(league["tera_budget"]),
        "teraCaptains": config.MAX_TERA_CAPTAINS,
    })
    return {
        "roomid": roomid,
        "phase": "season",
        "settings": settings,
        "pool": {},
        "players": players_for(conn, league["id"]),
        "picks": [],
        "pickNumber": 0,
        "pickDeadline": None,
        "schedule": schedule,
        "week": league["current_week"] or 0,
        "trades": [],
        "nextTradeId": 1,
        "transactions": [],
    }


def body_for(conn, league, roomid):
    """What `PUT /internal/leagues/{roomid}` takes, league and all."""
    return {
        "data": draft_league(conn, league, roomid),
        "title": league["name"],
        "isPrivate": league["visibility"] != "public",
        "staff": [],
        "mod": config.PLANNER_MOD,
        "event": {"actor": "ocdl-bot", "source": "server", "action": "publish",
                  "detail": f"{league['name']} week {league['current_week'] or 0}"},
    }


async def publish(league_body, roomid):
    """Write it to the site, which is where the Showdown server reads leagues from.

    Returns the new revision. Raises DraftError with something a moderator can act on.
    """
    import aiohttp

    site = config.get_site_internal()
    if site is None:
        raise rules.DraftError(
            "❌ I have no address or secret for draft.ogreclan.com's internal API, so I cannot "
            "publish a league to the Showdown server. Set `OGRECLAN_SITE_URL` and "
            "`OGRECLAN_INTERNAL_SECRET` for the bot.")
    url, secret = site
    try:
        async with aiohttp.ClientSession() as session:
            async with session.put(
                    f"{url}/internal/leagues/{roomid}", json=league_body,
                    headers={"X-OgreClan-Secret": secret},
                    timeout=aiohttp.ClientTimeout(total=20)) as response:
                text = await response.text()
                if response.status != 200:
                    raise rules.DraftError(
                        f"❌ draft.ogreclan.com refused the league ({response.status}): "
                        f"{text[:200]}")
                return json.loads(text).get("revision")
    except rules.DraftError:
        raise
    except Exception as error:
        log.exception("publishing %s to the planner failed", roomid)
        raise rules.DraftError(f"❌ I could not reach draft.ogreclan.com: {error}")
