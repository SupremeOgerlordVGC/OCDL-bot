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
import os
import random
import re

from aiohttp import web

import config
import database
import draft as rules
import results
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
            ("/pick", self.pick), ("/makeup", self.makeup), ("/editpick", self.edit_pick),
            ("/teracaptain", self.tera_captain),
            ("/archive", self.archive), ("/deleteleague", self.delete_league),
            ("/guilds", self.guilds), ("/setup", self.setup), ("/start", self.start_draft),
            ("/postweek", self.post_week), ("/trade", self.trade), ("/approvetrade", self.approve_trade),
            ("/updatetrades", self.update_trades),
            ("/rejecttrade", self.reject_trade), ("/gameresult", self.game_result), ("/champions", self.champions),
            ("/pokepaste", self.pokepaste), ("/cleargame", self.clear_game), ("/setresult", self.set_result), ("/prices", self.prices), ("/tierlist", self.tierlist),
            ("/queue", self.queue), ("/queue/set", self.set_queue), ("/queue/cancel", self.cancel_queue),
            ("/hosts/apply", self.apply_host), ("/hosts/status", self.host_status),
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

    async def tera_captain(self, body):
        """/teracaptain: make a Pokémon already on a roster a Tera Captain, or (as_admin) take
        one off. The only way something that arrived in a trade becomes one."""
        draft = self.pool(body)
        player = rules.require_player(self.bot.conn, draft["id"], int(body["user_id"]))
        on = bool(body.get("on", True))
        if not on and not body.get("as_admin"):
            raise rules.DraftError("Only staff can take a Tera Captaincy off.")
        status, entry = await self.cog.sheet_entry(draft, str(body["pokemon"]))
        if status == "missing":
            raise rules.DraftError(f"{body['pokemon']} is not in this league's tiering list.")
        if status == "unavailable":
            raise rules.DraftError(
                "The price list can't be read right now, and a Tera Captain is charged its "
                "Tera price, so try again shortly.")
        result = rules.set_tera_captain(
            self.bot.conn, draft, player, entry["name"], entry["cost"], entry["tera_cost"],
            on=on, tera_banned=entry["tera_banned"])
        if not draft["site_only"]:
            await ui.safe_send(
                self.bot.get_channel(draft["channel_id"]),
                f"⭐ **{result['player']['display_name']}** has made **{result['pokemon']}** a "
                f"**Tera Captain** *(on the website)* — charged at its Tera price "
                f"({result['was_cost']} → {result['cost']}). Tera points remaining: "
                f"**{result['tera_budget_remaining']}**" if on else
                f"⭐ **{result['pokemon']}** is no longer **{result['player']['display_name']}**'s "
                f"Tera Captain *(on the website)*, and is back to {result['cost']} points.")
        ui.resync_sheet(self.bot, draft["id"])
        return {"pokemon": result["pokemon"], "tera_captain": result["on"],
                "cost": result["cost"], "captains": result["captains"],
                "budget_remaining": result["budget_remaining"],
                "tera_budget_remaining": result["tera_budget_remaining"]}

    # ---- picks ------------------------------------------------------------

    async def pick(self, body):
        """/pick: on your turn, or (as_admin, for the site's admins) for whoever is on the clock"""
        draft = self.pool(body)
        if draft["status"] != "active":
            raise rules.DraftError("This draft isn't running.")
        tera = bool(body.get("tera_captain"))
        as_admin = bool(body.get("as_admin"))
        on_clock = rules.current_player(self.bot.conn, draft)
        pokemon, cost = await self.priced(draft, body["pokemon"], body.get("cost"), tera)
        pick, events = rules.make_pick(self.bot.conn, draft, int(body["user_id"]), int(body["round"]),
                                       pokemon, cost, tera_captain=tera, acting_as_admin=as_admin)
        # announced, timed and put on the sheet exactly like /pick
        await ui.announce(self.bot, draft["id"], events)
        if as_admin:
            await ui.safe_send(self.bot.get_channel(draft["channel_id"]),
                               f"🛠️ That pick was made for **{on_clock['display_name']}** by an admin "
                               f"on draft.ogreclan.com.")
        return {"pokemon": pick["pokemon"], "cost": cost, "tera_captain": tera}

    async def edit_pick(self, body):
        """/editpick as a moderator, for the site's admins: change any player's pick, or fill in a round they
        have none for (which settles a makeup pick they owe)"""
        draft = self.pool(body)
        tera = bool(body.get("tera_captain"))
        pokemon, cost = await self.priced(draft, body["pokemon"], body.get("cost"), tera)
        result = rules.edit_pick(self.bot.conn, draft, int(body["actor_id"]), int(body["round"]), pokemon, cost,
                                 tera_captain=tera, target_user_id=int(body["user_id"]), acting_as_admin=True)
        await self.cog.announce_edited_pick(draft, result, self.bot.get_channel(draft["channel_id"]),
                                            where=" on draft.ogreclan.com")
        return {"created": result["created"], "pokemon": result["new_pokemon"], "cost": result["new_cost"],
                "old_pokemon": result["old_pokemon"]}

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

    # ---- making a league (the site's staff) ---------------------------------

    @property
    def admin_cog(self):
        return self.bot.get_cog("AdminCog")

    @property
    def extras_cog(self):
        return self.bot.get_cog("ExtrasCog")

    async def guilds(self, body):
        """The Discord servers the bot is in, with their text channels, for the site's league form"""
        return {"guilds": [{
            "id": str(guild.id), "name": guild.name,
            "channels": [{"id": str(channel.id), "name": channel.name,
                          "category": channel.category.name if channel.category else ""}
                         for channel in guild.text_channels],
        } for guild in self.bot.guilds]}

    async def setup(self, body):
        """/setup from draft.ogreclan.com: a league run on Discord (with its server and channels), or a
        site-only league (no Discord at all; the bot still runs its rules and timers)"""
        # Hosting here is by approval; the bot itself asks nobody's permission.
        rules.require_approved_host(
            self.bot.conn, int(body["created_by"]) if body.get("created_by") else None)

        site_only = bool(body.get("site_only"))
        pools = body.get("pools") or []
        if not 1 <= len(pools) <= 2:
            raise rules.DraftError("A league has one or two pools.")
        labels = ["A", "B"][:len(pools)]
        rosters, shuffled = {}, {}
        guild = None
        if not site_only:
            guild = self.bot.get_guild(int(body["guild_id"]))
            if guild is None:
                raise rules.DraftError("The bot isn't in that Discord server.")
            admin_channel = guild.get_channel(int(body["admin_channel_id"]))
            if admin_channel is None:
                raise rules.DraftError("Choose the channel where trades are approved.")
            channels = {}
            for label, pool in zip(labels, pools):
                channel = guild.get_channel(int(pool.get("channel_id") or 0))
                if channel is None:
                    raise rules.DraftError(f"Choose pool {label}'s channel.")
                channels[label] = channel.id
        else:
            admin_channel = None
            channels = dict(zip(labels, rules.site_pool_channels(len(pools))))

        for label, pool in zip(labels, pools):
            entries = []
            for player in pool.get("players") or []:
                user_id = int(player["user_id"])
                member = guild.get_member(user_id) if guild else None
                if member is None and guild is not None:
                    try:
                        member = await guild.fetch_member(user_id)
                    except Exception:
                        member = None
                entry = ui._player_entry(member, user_id) if member else {
                    "user_id": user_id, "display_name": str(player.get("display_name") or f"User {user_id}"),
                    "username": str(player.get("username") or ""),
                }
                entries.append(entry)
            if pool.get("shuffle"):
                random.shuffle(entries)
            rosters[label] = entries
            shuffled[label] = bool(pool.get("shuffle"))

        number = lambda key: int(body[key]) if body.get(key) not in (None, "") else None  # noqa: E731
        league, pool_rows, notes, coach_slots = await self.admin_cog.build_league(
            guild_id=guild.id if guild else 0, admin_channel_id=admin_channel.id if admin_channel else 0,
            name=body["name"], rosters=rosters, channels=channels,
            min_picks=int(body["min_picks"]), max_picks=int(body["max_picks"]), timer=int(body["timer"]),
            mode=str(body.get("mode") or "full_season"), budget=number("budget"), tera_budget=number("tera_budget"),
            max_trades=number("max_trades") or 0, template_link=body.get("template_link") or None,
            tiering_link=body.get("tiering_link") or None, weeks=number("weeks"),
            visibility=str(body.get("visibility") or "public"), website=bool(body.get("website", True)),
            site_only=site_only, created_by=int(body["created_by"]) if body.get("created_by") else None,
        )
        if not site_only:
            await self.admin_cog.announce_setup(league, pool_rows, coach_slots, shuffled, admin_channel)
        return {"league_id": league["id"], "notes": notes}

    async def apply_host(self, body):
        """Apply to host leagues on draft.ogreclan.com."""
        user_id = int(body["user_id"])
        record = rules.apply_to_host(
            self.bot.conn, user_id,
            display_name=str(body.get("display_name") or "") or None,
            about=body.get("about"))
        await self.tell_owner(
            f"🧑‍⚖️ **{record['display_name'] or user_id}** (<@{user_id}>) has applied "
            f"to host on draft.ogreclan.com.\n"
            f"• About: {record['about'] or '*nothing said*'}\n\n"
            f"`/approvehost user:<@{user_id}>` or `/rejecthost user:<@{user_id}>`. "
            f"They can use the bot either way."
        )
        return {"status": record["status"]}

    async def host_status(self, body):
        """Where an application stands, for the site to show."""
        user_id = int(body["user_id"])
        return {"status": rules.host_status(self.bot.conn, user_id),
                "may_host": rules.is_approved_host(self.bot.conn, user_id)}

    async def tell_owner(self, message):
        """Direct message the bot's owner. Best effort: never breaks a request."""
        try:
            info = await self.bot.application_info()
            owner = info.owner
            if owner is not None:
                await owner.send(message)
                return True
        except Exception:
            log.exception("could not tell the owner about a host application")
        return False

    async def start_draft(self, body):
        """/draft from draft.ogreclan.com: start a pool now, or at a time (unix seconds)"""
        pool = self.pool(body)
        if pool["status"] != "setup":
            raise rules.DraftError("This pool has already started.")
        if body.get("at"):
            rules.schedule_pool_start(self.bot.conn, pool, float(body["at"]))
            return {"scheduled": float(body["at"])}
        events = rules.start_pool(self.bot.conn, pool)
        channel = self.bot.get_channel(pool["channel_id"])
        if channel is not None:
            await self.admin_cog.announce_start(channel, pool)
        await ui.announce(self.bot, pool["id"], events, channel=channel)
        return {"started": True}

    # ---- the season (players and the site's staff) --------------------------

    def league(self, body):
        league = rules.get_league(self.bot.conn, int(body["league_id"]))
        if league is None:
            raise rules.DraftError("That league doesn't exist.")
        return league

    async def post_week(self, body):
        """/postschedule from draft.ogreclan.com: the week's matches. Approved trades are not
        applied here - they land when their players' matches are reported, or on /updatetrades.
        A league on Discord gets the post in its trade approvals channel."""
        league = self.league(body)
        week = int(body["week"])
        result = await self.admin_cog.post_week(league, week)
        if result is None:
            raise rules.DraftError(f"No matches found for week {week}.")
        if not league["site_only"]:
            # Same posting as the slash command: the league's schedule
            # channel, one message per match, each with votes on it.
            await self.admin_cog.publish_week(
                league, result, self.bot.get_channel(league["admin_channel_id"]))
        return {"matches": result["matches"], "waiting": result["waiting"], "report": plain(result["report"])}

    async def trade(self, body):
        """/trade from draft.ogreclan.com: user_id proposes; with partner_user_id it's a swap,
        without it free agency. drops can be empty (a pickup paid with points left), and
        tera_on / tera_off change Tera Captains as part of the same transaction."""
        draft = self.pool(body)
        if draft["mode"] != "full_season":
            raise rules.DraftError("This league doesn't have trades.")

        def listed(key, limit=config.MAX_TRADE_SIZE):
            names = [str(name).strip() for name in body.get(key) or [] if str(name).strip()]
            return rules.parse_pokemon_list(", ".join(names), limit) if names else []

        drops, gets = listed("drops"), listed("gets")
        tera_on = listed("tera_on", config.MAX_TERA_CAPTAINS)
        tera_off = listed("tera_off", config.MAX_TERA_CAPTAINS)
        partner = int(body["partner_user_id"]) if body.get("partner_user_id") else None

        # The same checks the slash command makes, which is why they live on
        # the cog: spelling against the tiering list, both budgets, and the
        # Tera arithmetic, before any moderator is asked to judge it.
        on_entries, off_entries = [], []
        if self.extras_cog is not None:
            prices = await self.extras_cog.resolve_trade_sides(draft, drops, gets)
            on_entries, off_entries = await self.extras_cog.resolve_tera_sides(
                draft, tera_on, tera_off)
            proposer = rules.require_player(self.bot.conn, draft["id"], int(body["user_id"]))
            partner_player = (rules.require_player(self.bot.conn, draft["id"], partner)
                              if partner else None)
            rules.check_trade(self.bot.conn, draft, proposer, partner_player, drops, gets,
                              prices, tera_on=on_entries, tera_off=off_entries)

        record, warnings = rules.propose_trade(self.bot.conn, draft, int(body["user_id"]), drops, gets,
                                               partner_user_id=partner,
                                               tera_on=on_entries, tera_off=off_entries)
        if not draft["site_only"]:
            league = rules.get_league(self.bot.conn, draft["league_id"])
            proposer = rules.get_player_by_id(self.bot.conn, record["proposer_id"])
            notes = ("\n\n⚠️ **Worth checking**\n" + "\n".join(f"• {w}" for w in warnings)) if warnings else ""
            await ui.safe_send(self.bot.get_channel(league["admin_channel_id"]),
                f"📨 **Trade #{record['trade_number']}** — *pending* (proposed on draft.ogreclan.com)\n"
                f"• Draft: **{league['name']}** — Pool {draft['label']}\n"
                f"• Proposed by: <@{proposer['user_id']}>\n"
                f"• Partner: {f'<@{partner}>' if partner else '*undrafted pool (free agency)*'}\n"
                f"• Giving up: {rules.trade_side(drops)}\n"
                f"• Receiving: {rules.trade_side(gets)}\n"
                + (f"• ⭐ Becoming a Tera Captain: {rules.tera_side(on_entries)}\n"
                   if on_entries else "")
                + (f"• ⭐ No longer a Tera Captain: {rules.tera_side(off_entries)}\n"
                   if off_entries else "")
                + "\n"
                f"Approve with `/approvetrade trade_id:{record['trade_number']} name:{league['name']}`, "
                f"reject with `/rejecttrade`, or decide it on the site.{notes}")
        return {"trade_number": record["trade_number"], "free": bool(record["free"]),
                "warnings": [plain(w) for w in warnings]}

    async def update_trades(self, body):
        """/updatetrades from draft.ogreclan.com: put approved trades into effect now,
        for one player, one pool, or the whole league."""
        league = self.league(body)
        seats, pool_id = None, None
        if body.get("user_id"):
            where, seat = rules.player_in_league(self.bot.conn, league["id"],
                                                 int(body["user_id"]))
            if seat is None:
                raise rules.DraftError("They aren't playing in this league.")
            seats, pool_id = [seat["id"]], where["id"]
        elif body.get("pool"):
            found = next((row for row in rules.get_league_pools(self.bot.conn, league["id"])
                          if row["label"] == str(body["pool"])), None)
            if found is None:
                raise rules.DraftError(f"{league['name']} has no pool {body['pool']}.")
            pool_id = found["id"]

        week = (league["current_week"] or 0) + 1
        applied = rules.apply_trades(self.bot.conn, league["id"], week,
                                     pool_id=pool_id, player_ids=seats)
        await ui.trades_landed(self.bot, applied, "a moderator put it through")
        held = rules.held_trades(self.bot.conn, league["id"], pool_id=pool_id,
                                 player_ids=seats)
        return {"applied": [trade["trade_number"] for trade, _ in applied],
                "week": week,
                "notes": [plain(note) for _, notes in applied for note in notes],
                "waiting": [{"trade": trade["trade_number"],
                             "for": blocker["trade_number"]} for trade, blocker in held]}

    async def approve_trade(self, body):
        """/approvetrade from draft.ogreclan.com (user_id is the approving staff member's Discord ID)"""
        league = self.league(body)
        number = int(body["trade_number"])
        trade = rules.get_trade(self.bot.conn, league["id"], number)
        if trade is None:
            raise rules.DraftError(f"There is no trade #{number} in {league['name']}.")
        pool = rules.get_pool(self.bot.conn, trade["pool_id"])
        record, notes = rules.approve_trade(self.bot.conn, pool, number, int(body.get("user_id") or 0))
        next_week = (league["current_week"] or 0) + 1
        if not league["site_only"]:
            proposer = rules.get_player_by_id(self.bot.conn, record["proposer_id"])
            partner = (rules.get_player_by_id(self.bot.conn, record["partner_id"])
                       if record["partner_id"] else None)
            await ui.safe_send(self.bot.get_channel(pool["channel_id"]),
                f"🤝 **Trade #{number} approved!**\n"
                f"• **{proposer['display_name']}** gives up: {rules.trade_side(record['drops'])}\n"
                f"• **{proposer['display_name']}** receives: {rules.trade_side(record['gets'])}\n"
                + (f"• Partner: **{partner['display_name']}**\n" if partner else "• Traded with the undrafted pool\n")
                + f"Rosters and budgets change {ui.when_trades_land(league)}.")
        return {"week": next_week, "notes": [plain(note) for note in notes]}

    async def reject_trade(self, body):
        """/rejecttrade from draft.ogreclan.com"""
        league = self.league(body)
        number = int(body["trade_number"])
        trade = rules.get_trade(self.bot.conn, league["id"], number)
        if trade is None:
            raise rules.DraftError(f"There is no trade #{number} in {league['name']}.")
        rules.reject_trade(self.bot.conn, rules.get_pool(self.bot.conn, trade["pool_id"]), number,
                           int(body.get("user_id") or 0))
        return {}

    # ---- match results ------------------------------------------------------

    def match_of(self, body):
        """The league, the fixture (by match number, or the reporting player's that week) and its two players"""
        league = self.league(body)
        week = int(body["week"])
        if body.get("match"):
            fixture = self.bot.conn.execute(
                "SELECT * FROM fixtures WHERE league_id = ? AND week = ? AND match_number = ?",
                (league["id"], week, int(body["match"]))).fetchone()
            if fixture is None:
                raise rules.DraftError("That match doesn't exist.")
        else:
            player = results.player_in_league(self.bot.conn, league["id"], int(body["user_id"]))
            fixture = results.find_match(self.bot.conn, league["id"], week, player["id"])
        players = [rules.get_player_by_id(self.bot.conn, fixture["home_player_id"]),
                   rules.get_player_by_id(self.bot.conn, fixture["away_player_id"])]
        return league, week, fixture, players

    async def game_result(self, body):
        """A game's result from the site: a battle played with Ready or a replay (both sides), or a player's
        own Pokémon (source 'manual', only that player's side). stats: [{user_id, pokemon, kills, deaths}]"""
        league, week, fixture, players = self.match_of(body)
        was_decided = results.match_finished(self.bot.conn, league, week, fixture["match_number"])
        by_discord = {str(p["user_id"]): p for p in players}
        source = str(body.get("source") or "manual")
        reporter = by_discord.get(str(body.get("user_id"))) if body.get("user_id") else None
        if source == "manual" and reporter is None:
            raise rules.DraftError("Only the match's players report their own Pokémon.")
        stats = {}
        for entry in body.get("stats") or []:
            player = by_discord.get(str(entry.get("user_id")))
            if player is None:
                continue
            report = results.get_report(self.bot.conn, league["id"], week, fixture["match_number"])
            roster = results.reportable(self.bot.conn, player, report)
            name = (results.parse_kd(f"{entry['pokemon']} 0-0", roster)[0][0] if source == "manual"
                    else results.roster_name(entry["pokemon"], roster))
            stats.setdefault(player["id"], []).append((name, int(entry["kills"]), int(entry["deaths"])))
        winner = by_discord.get(str(body.get("winner_user_id") or ""))
        view = results.record_game(self.bot.conn, league["id"], week, fixture["match_number"], int(body["game"]),
                                   source, winner_id=winner["id"] if winner else None, stats=stats,
                                   replay=body.get("replay"), only_player=reporter["id"] if source == "manual" else None)
        # A finished match is posted where the matchup was and puts both
        # players' approved trades into effect - the same work the slash
        # command does, so it is the same code.
        outcome = await ui.finish_match(self.bot, league, week, fixture, players, was_decided)
        view = results.match_view(self.bot.conn, league["id"], week, fixture["match_number"])
        return {"match": fixture["match_number"], "winner_id": view["winner_id"],
                "wins": {str(seat): won for seat, won in view["wins"].items()},
                "decided": view["decided"], "games": len(view["games"]),
                "trades_applied": len(outcome["applied"])}

    async def champions(self, body):
        """A player agrees (or not) to play their match on Pokémon Champions"""
        league, week, fixture, players = self.match_of(body)
        player = next((p for p in players if str(p["user_id"]) == str(body["user_id"])), None)
        if player is None:
            raise rules.DraftError("Only the match's players can choose that.")
        view = results.ask_champions(self.bot.conn, league["id"], week, fixture["match_number"], player["id"],
                                     bool(body.get("on", True)))
        if view["champions"] and not league["site_only"]:
            pool = rules.get_pool(self.bot.conn, players[0]["pool_id"])
            await ui.safe_send(self.bot.get_channel(pool["channel_id"]),
                f"🏆 Both players agreed: the week {week} match between **{players[0]['display_name']}** and "
                f"**{players[1]['display_name']}** is played on Pokémon Champions.")
        return {"champions": view["champions"], "asked": view["asked"]}

    async def pokepaste(self, body):
        league, week, fixture, players = self.match_of(body)
        player = next((p for p in players if str(p["user_id"]) == str(body["user_id"])), None)
        if player is None:
            raise rules.DraftError("Only the match's players give a Pokepaste.")
        results.set_pokepaste(self.bot.conn, league["id"], week, fixture["match_number"], player["id"], body["url"])
        return {}

    async def set_result(self, body):
        """Staff: award a match that was not played out - a forfeit, a no-show, or a result
        settled elsewhere. winner_user_id of nothing takes the award back."""
        league, week, fixture, players = self.match_of(body)
        was_decided = results.match_finished(self.bot.conn, league, week, fixture["match_number"])
        winner = next((p for p in players
                       if str(p["user_id"]) == str(body.get("winner_user_id") or "")), None)
        if body.get("winner_user_id") and winner is None:
            raise rules.DraftError("The winner has to be one of the match's two players.")
        view = results.award_match(self.bot.conn, league, week, fixture["match_number"],
                                   winner["id"] if winner else None, body.get("reason"))
        await ui.finish_match(self.bot, league, week, fixture, players, was_decided)
        return {"match": fixture["match_number"], "winner_id": view["winner_id"],
                "walkover": view["walkover"], "note": view["note"]}

    async def clear_game(self, body):
        """Staff: remove a game's result"""
        league, week, fixture, players = self.match_of(body)
        results.clear_game(self.bot.conn, league["id"], week, fixture["match_number"], int(body["game"]))
        return {}

    # ---- ending a league (the site's admins) -------------------------------

    async def _end(self, body, verb):
        league = rules.get_league(self.bot.conn, int(body["league_id"]))
        if league is None:
            raise rules.DraftError("That league doesn't exist.")
        pools = rules.get_league_pools(self.bot.conn, league["id"])
        for pool in pools:
            self.bot.timers.cancel(pool["id"])
        return league, pools

    async def archive(self, body):
        """/archive, for the site's admins"""
        league, pools = await self._end(body, "archived")
        rules.archive_league(self.bot.conn, league["id"])
        return {"name": league["name"]}

    async def delete_league(self, body):
        """/deleteleague, for the site's admins: a backup first, then everything in the league goes"""
        league, pools = await self._end(body, "deleted")
        path = database.backup(self.bot.conn)
        rules.delete_league(self.bot.conn, league["id"])
        log.info("league %s (%s) deleted from draft.ogreclan.com; backup %s", league["id"], league["name"], path)
        for pool in pools:
            await ui.safe_send(self.bot.get_channel(pool["channel_id"]),
                               f"🗑️ **{league['name']}** has been deleted by an admin on draft.ogreclan.com.")
        return {"name": league["name"], "backup": os.path.basename(path)}

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
