"""
A private web API for draft.ogreclan.com, so players can make their picks and manage their pick queues on the site.

The bot does exactly what it does for the commands: /pick, /makeuppick, /queuepick, /myqueue and /cancelqueue. So the
draft rules, the turn timer, the Discord announcements and the league's spreadsheet all behave the same, whichever
way a pick was made. Players are named by their Discord user ID, which the site knows from Discord sign-in, and a
pool by its league's number and label.

It only listens on the server itself (127.0.0.1), every request must carry the shared secret, and it's off unless
OGRECLAN_API_PORT and OGRECLAN_SECRET are set (on the server, systemd sets them, so the .env file isn't involved).
"""

import asyncio
import hmac
import json
import logging
import re

from aiohttp import web

import config
import draft as rules
import ui
from cogs.drafting import tiering_tab_for

log = logging.getLogger("draftbot.webapi")


def plain(message):
    """A bot message for the site: without Discord's markdown and the emoji in front"""
    text = re.sub(r"\*\*|`", "", str(message)).strip()
    return re.sub(r"^[^\w(]+", "", text)


class WebApi:
    def __init__(self, bot, port, secret):
        self.bot = bot
        self.port = port
        self.secret = secret
        self.runner = None

    @property
    def cog(self):
        return self.bot.get_cog("DraftingCog")

    def app(self):
        app = web.Application(client_max_size=64 * 1024)
        for path, handler in (
            ("/pick", self.pick), ("/makeup", self.makeup), ("/prices", self.prices), ("/tierlist", self.tierlist),
            ("/queue", self.queue), ("/queue/set", self.set_queue), ("/queue/cancel", self.cancel_queue),
        ):
            app.router.add_post(path, self.wrap(handler))
        return app

    async def start(self):
        self.runner = web.AppRunner(self.app())
        await self.runner.setup()
        await web.TCPSite(self.runner, "127.0.0.1", self.port).start()
        log.info("web API for draft.ogreclan.com on 127.0.0.1:%s", self.port)

    async def stop(self):
        if self.runner:
            await self.runner.cleanup()

    def wrap(self, handler):
        async def handle(request):
            given = request.headers.get("X-OgreClan-Secret", "")
            if not hmac.compare_digest(given.encode(), self.secret.encode()):
                return web.json_response({"ok": False, "error": "Forbidden"}, status=403)
            try:
                body = await request.json()
            except (json.JSONDecodeError, UnicodeDecodeError):
                return web.json_response({"ok": False, "error": "Not JSON"}, status=400)
            try:
                return web.json_response({"ok": True, **await handler(body)})
            except rules.DraftError as error:
                return web.json_response({"ok": False, "error": plain(error)}, status=400)
            except (KeyError, ValueError, TypeError) as error:
                return web.json_response({"ok": False, "error": f"Bad request: {error}"}, status=400)
            except Exception:
                log.exception("web API request to %s failed", request.path)
                return web.json_response({"ok": False, "error": "The bot hit an error."}, status=500)
        return handle

    # ---- finding things ---------------------------------------------------

    def pool(self, body):
        row = self.bot.conn.execute(
            "SELECT id FROM pools WHERE league_id = ? AND label = ?",
            (int(body["league_id"]), str(body["pool"])),
        ).fetchone()
        if row is None:
            raise rules.DraftError("That draft doesn't exist.")
        return rules.get_pool(self.bot.conn, row["id"])

    async def priced(self, draft, pokemon, cost, tera_captain):
        """The Pokémon's name and cost as the price list has them (a cost given by the player is checked against it)"""
        pokemon = str(pokemon).strip()
        if not pokemon:
            raise rules.DraftError("Choose a Pokémon.")
        if cost is None:
            status, entry = await self.cog.sheet_entry(draft, pokemon)
            if status == "missing":
                raise rules.DraftError(f"{pokemon} is not in this league's tiering list.")
            if status == "unavailable":
                raise rules.DraftError("The price list can't be read right now, so give the cost too.")
            cost = entry["tera_cost"] if tera_captain else entry["cost"]
        name = await self.cog.resolve_pokemon(draft, pokemon, int(cost) if cost is not None else None, tera_captain)
        return name, cost

    # ---- picks ------------------------------------------------------------

    async def pick(self, body):
        """/pick: on your turn"""
        draft = self.pool(body)
        if draft["status"] != "active":
            raise rules.DraftError("This draft isn't running.")
        tera = bool(body.get("tera_captain"))
        pokemon, cost = await self.priced(draft, body["pokemon"], body.get("cost"), tera)
        pick, events = rules.make_pick(self.bot.conn, draft, int(body["user_id"]), int(body["round"]),
                                       pokemon, cost, tera_captain=tera, acting_as_admin=False)
        # announced, timed and put on the sheet exactly like /pick
        await ui.announce(self.bot, draft["id"], events)
        return {"pokemon": pick["pokemon"], "cost": cost, "tera_captain": tera}

    async def makeup(self, body):
        """/makeuppick: a round you were skipped in"""
        draft = self.pool(body)
        tera = bool(body.get("tera_captain"))
        pokemon, cost = await self.priced(draft, body["pokemon"], body.get("cost"), tera)
        user_id = int(body["user_id"])
        pick = rules.make_makeup_pick(self.bot.conn, draft, user_id, int(body["round"]),
                                      pokemon, cost, tera_captain=tera)
        await self.cog.announce_makeup_pick(draft, user_id, pick, self.bot.get_channel(draft["channel_id"]))
        return {"pokemon": pick["pokemon"], "cost": cost, "tera_captain": tera}

    async def prices(self, body):
        """The league's price list: everything still draftable, with its cost and Tera cost"""
        draft = self.pool(body)
        if not self.bot.sheets.enabled or not draft["doc_url"]:
            return {"available": False, "entries": []}
        try:
            tab = await tiering_tab_for(self.bot, draft)
            entries = await asyncio.to_thread(self.bot.sheets.pool, draft["doc_url"], tab)
        except Exception:
            log.exception("price list for the site failed")
            return {"available": False, "entries": []}
        taken = rules.drafted_keys(self.bot.conn, draft["id"])
        return {"available": True, "entries": [
            {"name": e["name"], "cost": e["cost"], "tera_cost": e["tera_cost"]}
            for e in entries if not e["banned"] and e["key"] not in taken
        ]}

    async def tierlist(self, body):
        """The pool's whole tiering list: every Pokémon with its costs, whether it's banned, and who has it now"""
        draft = self.pool(body)
        if not self.bot.sheets.enabled or not draft["doc_url"]:
            return {"available": False, "entries": []}
        try:
            tab = await tiering_tab_for(self.bot, draft)
            entries = await asyncio.to_thread(self.bot.sheets.pool, draft["doc_url"], tab)
        except Exception:
            log.exception("tiering list for the site failed")
            return {"available": False, "entries": []}
        owners = {row["pokemon_key"]: row["player_id"] for row in self.bot.conn.execute(
            "SELECT pokemon_key, player_id FROM rosters WHERE pool_id = ?", (draft["id"],))}
        return {"available": True, "entries": [
            {"name": e["name"], "cost": e["cost"], "tera_cost": e["tera_cost"], "banned": bool(e["banned"]),
             "tera_banned": bool(e["tera_banned"]), "taken_by": owners.get(e["key"])}
            for e in entries
        ]}

    # ---- queues -----------------------------------------------------------

    async def queue(self, body):
        """/myqueue: only ever for the player themselves"""
        draft = self.pool(body)
        view = rules.queue_view(self.bot.conn, draft, int(body["user_id"]))
        return {
            "rounds": [{"round": number, "entries": [
                {"priority": e["priority"], "pokemon": e["pokemon"], "cost": e["cost"], "tera_captain": bool(e["tera_captain"])}
                for e in entries]} for number, entries in view["rounds"]],
            "picked": sorted(view["picked"]),
        }

    async def set_queue(self, body):
        """/queuepick: replaces one round's queue"""
        draft = self.pool(body)
        if draft["status"] not in ("setup", "active"):
            raise rules.DraftError("This draft isn't running.")
        names = [str(name).strip() for name in body["pokemon"] if str(name).strip()]
        if not names:
            raise rules.DraftError("Queue at least one Pokémon.")
        if len(names) > config.MAX_QUEUE_LENGTH:
            raise rules.DraftError(f"A queue holds at most {config.MAX_QUEUE_LENGTH} Pokémon.")
        captains = [bool(flag) for flag in (body.get("tera") or [])] or [False] * len(names)
        if len(captains) != len(names):
            raise rules.DraftError("Say yes or no to Tera for each Pokémon.")
        if any(captains) and not draft["tera_budget"]:
            raise rules.DraftError("This draft is not a Tera format.")
        costs = await self.cog.resolve_queue(draft, names, [], captains)
        rules.queue_picks(self.bot.conn, draft, int(body["user_id"]), int(body["round"]), names, costs, captains)
        return {"entries": [{"priority": i, "pokemon": n, "cost": c, "tera_captain": t}
                            for i, (n, c, t) in enumerate(zip(names, costs, captains), 1)]}

    async def cancel_queue(self, body):
        """/cancelqueue: one round, or every round"""
        draft = self.pool(body)
        round_number = body.get("round")
        result = rules.cancel_queue(self.bot.conn, draft, int(body["user_id"]),
                                    int(round_number) if round_number else None)
        return {"removed": len(result["removed"])}
