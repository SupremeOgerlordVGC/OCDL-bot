"""
Trades, substitutions and price-list lookups:
/trade, /approvetrade, /rejecttrade, /trades,
/subrequest, /substitute, /search, /available.

Trades and substitution requests are proposed in a pool's own channel and
sent to the league's admin channel, where a moderator acts on them. Both are
numbered per league, so an admin channel shared by several leagues never has
two #1s in it.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import draft as rules
import ui
from cogs.drafting import lookup_price, pokemon_autocomplete, tiering_tab_for

log = logging.getLogger("draftbot.extras")


class ExtrasCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---- finding the right league or pool -------------------------------

    def resolve_league(self, interaction, name=None):
        """Work out which league a moderator means.

        By name if they gave one; otherwise the league of the channel they
        are in, or the only one using this admin channel.
        """
        if name:
            return rules.require_season_league_by_name(self.bot.conn, interaction.guild_id, name)

        pool = rules.get_channel_pool(self.bot.conn, interaction.channel_id)
        if pool is not None:
            return rules.get_league(self.bot.conn, pool["league_id"])

        candidates = rules.leagues_in_admin_channel(self.bot.conn, interaction.channel_id)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise rules.DraftError(
                "❌ No draft is running here. Use the `name` option, or run this in a "
                "draft channel."
            )
        names = ", ".join(f"**{row['name']}**" for row in candidates)
        raise rules.DraftError(
            f"❌ Several drafts use this admin channel ({names}). Say which with the "
            f"`name` option."
        )

    def taken_keys(self, pool_id):
        return rules.drafted_keys(self.bot.conn, pool_id)

    async def post_to_admins(self, league, message, interaction):
        """Send something to the league's admin channel."""
        channel = self.bot.get_channel(league["admin_channel_id"])
        if await ui.safe_send(channel, message):
            return True
        await interaction.followup.send(
            f"⚠️ Saved, but it could not be posted to <#{league['admin_channel_id']}> — "
            f"I may not have permission to post there. Tell a moderator directly.",
            ephemeral=True,
        )
        return False

    # ---- /trade ---------------------------------------------------------

    async def resolve_trade_sides(self, pool, drop_list, get_list):
        """Check both sides of a trade against the league's tiering list.

        A trade is typed out by hand, and a misspelling that reaches the
        admins gets approved and then applied as nonsense: the name
        matches nothing on a roster, so nothing moves. So the names are
        rewritten in place to the list's own spelling, which is also what
        the spreadsheet needs to price them.

        Returns {normalised name: cost} for the budget check. With no
        document, or with Google unreachable, there is nothing to check
        against and the names are simply tidied up.
        """
        prices, unknown, unobtainable = {}, [], []
        for names, receiving in ((drop_list, False), (get_list, True)):
            for index, name in enumerate(names):
                status, entry = await lookup_price(self.bot, pool, name)
                if status == "missing":
                    unknown.append(name)
                    continue
                if status != "ok":
                    names[index] = rules.tidy_name(name)
                    continue
                names[index] = entry["name"]        # the list's own spelling
                if entry["cost"] is None:
                    if receiving:
                        unobtainable.append(entry["name"])
                else:
                    prices[rules.normalize(entry["name"])] = entry["cost"]

        if unknown:
            raise rules.DraftError(
                "❌ Not in this league's tiering list: "
                + ", ".join(f"**{name}**" for name in unknown)
                + ".\nCheck the spelling — `/search` finds the right name, and "
                  "the list's spelling is what the spreadsheet needs."
            )
        if unobtainable:
            raise rules.DraftError(
                "❌ Not draftable in this league: "
                + ", ".join(f"**{name}**" for name in unobtainable)
                + ". They have no price on the tiering list."
            )
        return prices

    @app_commands.command(name="trade", description="Propose a trade for a moderator to approve.")
    @app_commands.describe(
        drops="Up to 5 Pokémon you are giving up, separated by commas, or nil / 0 to give up nothing",
        gets="Up to 5 Pokémon you want, separated by commas",
        partner="The player you are trading with. Leave blank for the undrafted pool.",
        for_player="Moderators only: file this trade on somebody else's behalf",
    )
    async def trade(self, interaction: discord.Interaction, drops: str, gets: str,
                    partner: discord.Member = None, for_player: discord.Member = None):
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        if pool["mode"] != "full_season":
            raise rules.DraftError(
                f"❌ **{pool['league_name']}** does not handle trades — it is a "
                f"`{pool['mode']}` league."
            )

        # A moderator can enter a trade for somebody who cannot manage the
        # command themselves. It still needs approving afterwards.
        proposer_id = interaction.user.id
        on_behalf = False
        if for_player is not None and for_player.id != interaction.user.id:
            ui.require_moderator(self.bot, interaction)
            proposer_id = for_player.id
            on_behalf = True

        await interaction.response.defer(ephemeral=True)

        drop_list = rules.parse_pokemon_list(drops, config.MAX_TRADE_SIZE, allow_nothing=True)
        get_list = rules.parse_pokemon_list(gets, config.MAX_TRADE_SIZE)

        prices = await self.resolve_trade_sides(pool, drop_list, get_list)

        # Then the mechanical rules, so the admins only ever judge a trade
        # that could actually go through.
        if partner is not None and partner.id == proposer_id:
            raise rules.DraftError("❌ You cannot trade with yourself.")
        proposer = rules.require_player(self.bot.conn, pool["id"], proposer_id)
        partner_player = (rules.require_player(self.bot.conn, pool["id"], partner.id)
                          if partner is not None else None)
        maths = rules.check_trade(self.bot.conn, pool, proposer, partner_player,
                                  drop_list, get_list, prices)

        record, warnings = rules.propose_trade(
            self.bot.conn, pool, proposer_id, drop_list, get_list,
            partner_user_id=partner.id if partner else None,
        )

        # The bot does not judge trades - moderators do - but anything that
        # looks odd is put in front of them before they approve it.
        notes = ""
        if warnings:
            notes = "\n\n⚠️ **Worth checking**\n" + "\n".join(f"• {w}" for w in warnings)

        allowance = ""
        if record["free"]:
            allowance = "\n• Proposed in the free trade window: doesn't count toward the limit"
        elif pool["max_trades"]:
            used = rules.pickups_used(self.bot.conn, pool["id"], record["proposer_id"])
            allowance = (f"\n• Pokémon picked up in trades by proposer: {used} / {pool['max_trades']}"
                         f" (this trade adds {len(get_list)})")

        budget_line = (
            f"\n• Budget: **{proposer['budget_remaining']} → {maths['after']}** "
            f"(back {maths['refund']}, costs {maths['charge']})")
        if maths["partner_after"] is not None:
            budget_line += (f"\n• Partner's budget: "
                            f"**{partner_player['budget_remaining']} → "
                            f"{maths['partner_after']}**")

        entered_by = f" *(entered by <@{interaction.user.id}>)*" if on_behalf else ""
        partner_text = partner.mention if partner else "*undrafted pool (free agency)*"

        league = rules.get_league(self.bot.conn, pool["league_id"])
        summary = (
            f"📨 **Trade #{record['trade_number']}** — *pending*\n"
            f"• Draft: **{league['name']}** — Pool {pool['label']} "
            f"(<#{pool['channel_id']}>)\n"
            f"• Proposed by: <@{proposer_id}>{entered_by}\n"
            f"• Partner: {partner_text}\n"
            f"• Giving up: {rules.trade_side(drop_list)}\n"
            f"• Receiving: {', '.join(get_list)}\n"
            f"• Proposed at: {record['created_at']} UTC"
            f"{budget_line}{allowance}\n\n"
            f"Approve with `/approvetrade trade_id:{record['trade_number']} "
            f"name:{league['name']}` or reject with `/rejecttrade`."
            f"{notes}"
        )
        if not await self.post_to_admins(league, summary, interaction):
            return

        whose = f" for {for_player.mention}" if on_behalf else ""
        await interaction.followup.send(
            f"✅ Trade **#{record['trade_number']}** submitted{whose} for approval. "
            f"Nothing changes until a moderator approves it, and then rosters change "
            f"when the next week is posted.",
            ephemeral=True,
        )

    # ---- /edittrade -----------------------------------------------------

    @app_commands.command(name="edittrade",
                          description="Moderator only: fix the Pokémon in a trade before it applies.")
    @app_commands.describe(
        trade_id="The trade number",
        drops="What the proposer gives up, separated by commas, or nil / 0 for nothing",
        gets="What the proposer receives, separated by commas",
        name="The league's name, if this channel runs more than one")
    async def edittrade(self, interaction: discord.Interaction, trade_id: int,
                        drops: str, gets: str, name: str = None):
        ui.require_moderator(self.bot, interaction)
        league = self.resolve_league(interaction, name)

        await interaction.response.defer(ephemeral=True)

        trade = rules.get_trade(self.bot.conn, league["id"], trade_id)
        if trade is None:
            raise rules.DraftError(f"❌ There is no trade #{trade_id} in **{league['name']}**.")
        pool = rules.get_pool(self.bot.conn, trade["pool_id"])
        proposer = rules.get_player_by_id(self.bot.conn, trade["proposer_id"])
        partner = (rules.get_player_by_id(self.bot.conn, trade["partner_id"])
                   if trade["partner_id"] else None)

        drop_list = rules.parse_pokemon_list(drops, config.MAX_TRADE_SIZE, allow_nothing=True)
        get_list = rules.parse_pokemon_list(gets, config.MAX_TRADE_SIZE)

        # The edited trade is held to exactly the same rules as a new one.
        prices = await self.resolve_trade_sides(pool, drop_list, get_list)
        maths = rules.check_trade(self.bot.conn, pool, proposer, partner,
                                  drop_list, get_list, prices)

        record, before = rules.edit_trade(self.bot.conn, pool, trade_id,
                                          drop_list, get_list)

        was_approved = before["status"] == "approved"
        await interaction.followup.send(
            f"✏️ Trade #{trade_id} updated"
            + (" and sent back for approval, since you approved the old version."
               if was_approved else ".")
            + f"\n• Giving up: {rules.trade_side(before['drops'])} → "
              f"**{rules.trade_side(record['drops'])}**"
            + f"\n• Receiving: {rules.trade_side(before['gets'])} → "
              f"**{rules.trade_side(record['gets'])}**"
            + f"\n• Budget: **{proposer['budget_remaining']} → {maths['after']}**"
            + f"\n\nApprove with `/approvetrade trade_id:{trade_id} name:{league['name']}`.",
            ephemeral=True,
        )

        await ui.safe_send(
            self.bot.get_channel(pool["channel_id"]),
            f"✏️ **Trade #{trade_id} was corrected by a moderator.**\n"
            f"• **{proposer['display_name']}** gives up: "
            f"{rules.trade_side(record['drops'])}\n"
            f"• **{proposer['display_name']}** receives: "
            f"{rules.trade_side(record['gets'])}\n"
            + ("It needs approving again before it takes effect."
               if was_approved else "It is still waiting for a decision."),
        )

    # ---- /approvetrade --------------------------------------------------

    @app_commands.command(name="approvetrade",
                          description="Moderator only: approve a pending trade.")
    @app_commands.describe(trade_id="The trade number shown when it was proposed",
                           name="The league's name, if this channel runs more than one")
    async def approvetrade(self, interaction: discord.Interaction, trade_id: int,
                           name: str = None):
        ui.require_moderator(self.bot, interaction)
        league = self.resolve_league(interaction, name)

        await interaction.response.defer(ephemeral=True)

        trade = rules.get_trade(self.bot.conn, league["id"], trade_id)
        if trade is None:
            raise rules.DraftError(f"❌ There is no trade #{trade_id} in **{league['name']}**.")
        pool = rules.get_pool(self.bot.conn, trade["pool_id"])

        record, notes = rules.approve_trade(self.bot.conn, pool, trade_id,
                                            interaction.user.id)

        proposer = rules.get_player_by_id(self.bot.conn, record["proposer_id"])
        partner = (rules.get_player_by_id(self.bot.conn, record["partner_id"])
                   if record["partner_id"] else None)
        drops = rules.trade_side(record["drops"])
        gets = rules.trade_side(record["gets"])
        next_week = (league["current_week"] or 0) + 1

        report = (f"✅ Trade #{trade_id} approved. It takes effect when week {next_week} is "
                  f"posted with `/postschedule`.")
        if notes:
            report += "\n\nNotes:\n" + "\n".join(f"• {note}" for note in notes)
        await interaction.followup.send(report, ephemeral=True)

        await ui.safe_send(
            self.bot.get_channel(pool["channel_id"]),
            f"🤝 **Trade #{trade_id} approved!**\n"
            f"• **{proposer['display_name']}** gives up: {drops}\n"
            f"• **{proposer['display_name']}** receives: {gets}\n"
            + (f"• Partner: **{partner['display_name']}**\n" if partner else
               "• Traded with the undrafted pool\n")
            + f"Rosters and budgets change from week {next_week}, when it's posted.",
        )

    # ---- /unapprovetrade ------------------------------------------------

    @app_commands.command(name="unapprovetrade",
                          description="Moderator only: take back an approval given by mistake.")
    @app_commands.describe(trade_id="The trade number",
                           name="The league's name, if this channel runs more than one")
    async def unapprovetrade(self, interaction: discord.Interaction, trade_id: int,
                             name: str = None):
        ui.require_moderator(self.bot, interaction)
        league = self.resolve_league(interaction, name)

        await interaction.response.defer(ephemeral=True)

        trade = rules.get_trade(self.bot.conn, league["id"], trade_id)
        if trade is None:
            raise rules.DraftError(f"❌ There is no trade #{trade_id} in **{league['name']}**.")
        pool = rules.get_pool(self.bot.conn, trade["pool_id"])

        record = rules.unapprove_trade(self.bot.conn, pool, trade_id)
        proposer = rules.get_player_by_id(self.bot.conn, record["proposer_id"])

        await interaction.followup.send(
            f"↩️ Trade #{trade_id} is pending again — nothing had taken effect yet, "
            f"so no roster or budget changed. Decide it with `/approvetrade "
            f"trade_id:{trade_id}` or `/rejecttrade trade_id:{trade_id}`.",
            ephemeral=True,
        )

        # The approval was announced publicly, so the retraction has to be
        # too: the proposer is owed the news that it is not settled.
        await ui.safe_send(
            self.bot.get_channel(pool["channel_id"]),
            f"↩️ **Trade #{trade_id} is no longer approved.** A moderator has put it "
            f"back to pending for another look, so **{proposer['display_name']}**'s "
            f"trade has not gone through. Nothing had changed hands yet — rosters "
            f"and budgets are untouched.",
        )

    # ---- /rejecttrade ---------------------------------------------------

    @app_commands.command(name="rejecttrade",
                          description="Moderator only: reject a pending trade.")
    @app_commands.describe(trade_id="The trade number",
                           name="The league's name, if this channel runs more than one")
    async def rejecttrade(self, interaction: discord.Interaction, trade_id: int,
                          name: str = None):
        ui.require_moderator(self.bot, interaction)
        league = self.resolve_league(interaction, name)

        trade = rules.get_trade(self.bot.conn, league["id"], trade_id)
        if trade is None:
            raise rules.DraftError(f"❌ There is no trade #{trade_id} in **{league['name']}**.")

        pool = rules.get_pool(self.bot.conn, trade["pool_id"])
        rules.reject_trade(self.bot.conn, pool, trade_id, interaction.user.id)
        await interaction.response.send_message(
            f"🚫 Trade #{trade_id} rejected. No rosters were changed.", ephemeral=True
        )

    # ---- /trades --------------------------------------------------------

    @app_commands.command(name="trades", description="List a league's trades.")
    @app_commands.describe(name="The league's name, if this channel runs more than one")
    async def trades(self, interaction: discord.Interaction, name: str = None):
        league = self.resolve_league(interaction, name)
        rows = self.bot.conn.execute(
            "SELECT * FROM trades WHERE league_id = ? ORDER BY trade_number", (league["id"],)
        ).fetchall()

        if not rows:
            await interaction.response.send_message(
                f"No trades have been proposed in **{league['name']}** yet.", ephemeral=True)
            return

        icons = {"pending": "⏳", "approved": "✅", "rejected": "🚫", "waiting": "🕒"}
        lines = [f"**{league['name']} — trades**"]
        for row in rows[-25:]:
            proposer = rules.get_player_by_id(self.bot.conn, row["proposer_id"])
            status = row["status"]
            if status == "approved" and row["applied_at"] is None:
                status = "waiting"
                label = "approved, in effect from the next posted week"
            elif status == "approved" and row["applied_week"]:
                label = f"in effect from week {row['applied_week']}"
            else:
                label = status
            if row["free"]:
                label += ", free window"
            lines.append(
                f"{icons.get(status, '•')} **#{row['trade_number']}** "
                f"{proposer['display_name']}: {rules.trade_side(row['drops'])} → "
                f"{rules.trade_side(row['gets'])} *({label})*"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---- /extension -----------------------------------------------------

    @app_commands.command(
        name="extension",
        description="Ask for your match this week to be played next week instead.")
    @app_commands.describe(reason="Why you need it — the moderators read this")
    async def extension(self, interaction: discord.Interaction, reason: str = None):
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        await interaction.response.defer(ephemeral=True)

        record, home, away = rules.request_extension(
            self.bot.conn, pool, interaction.user.id, reason)
        league = rules.get_league(self.bot.conn, pool["league_id"])
        asked_by = rules.get_player_by_id(self.bot.conn, record["requested_by"])
        next_week = record["week"] + config.MAX_EXTENSION_WEEKS

        summary = (
            f"⏳ **Extension #{record['number']}** — *pending*\n"
            f"• Draft: **{league['name']}** — Pool {pool['label']}\n"
            f"• Week {record['week']} match: **{home['display_name']}** vs "
            f"**{away['display_name']}**\n"
            f"• Asked for by: <@{asked_by['user_id']}>\n"
            f"• Reason: {record['reason'] or '*none given*'}\n\n"
            f"Granting it posts this match again in week {next_week}, which is as "
            f"far as it can go.\n"
            f"`/grantextension number:{record['number']} name:{league['name']}` or "
            f"`/rejectextension number:{record['number']} name:{league['name']}`."
        )
        if not await self.post_to_admins(league, summary, interaction):
            return

        opponent = home if asked_by["id"] == away["id"] else away
        await interaction.followup.send(
            f"⏳ Extension **#{record['number']}** requested for your week "
            f"{record['week']} match against **{opponent['display_name']}**. "
            f"A moderator will decide — nothing changes until they do, and "
            f"the most it can move is to week "
            f"{record['week'] + config.MAX_EXTENSION_WEEKS}.",
            ephemeral=True,
        )

    # ---- /grantextension and /rejectextension ---------------------------

    @app_commands.command(
        name="grantextension",
        description="Moderator only: grant an extension, moving the match to next week.")
    @app_commands.describe(
        number="The extension number, if a player asked for one",
        player="Or give this player's match an extension with nobody having asked",
        name="The league's name, if this channel runs more than one")
    async def grantextension(self, interaction: discord.Interaction,
                             number: int = None, player: discord.Member = None,
                             name: str = None):
        if (number is None) == (player is None):
            raise rules.DraftError(
                "❌ Give either `number`, to decide a request, or `player`, to "
                "grant an extension nobody asked for."
            )
        if number is not None:
            await self.decide_extension(interaction, number, name, granted=True)
            return

        # Granted unasked: moderators often know before the players do.
        ui.require_moderator(self.bot, interaction)
        league = self.resolve_league(interaction, name)
        await interaction.response.defer(ephemeral=True)

        pool, seat = rules.player_in_league(self.bot.conn, league["id"], player.id)
        if seat is None:
            raise rules.DraftError(
                f"❌ **{player.display_name}** is not a player in **{league['name']}**."
            )

        record, home, away = rules.grant_extension_for_player(
            self.bot.conn, pool, player.id, interaction.user.id)
        next_week = record["week"] + config.MAX_EXTENSION_WEEKS

        await interaction.followup.send(
            f"✅ Extension **#{record['number']}** granted for **{home['display_name']}** "
            f"vs **{away['display_name']}** — nobody had to ask. The match will be "
            f"posted again with week {next_week}.",
            ephemeral=True,
        )
        await self.announce_extension(league, pool, record, home, away, granted=True)

    @app_commands.command(
        name="rejectextension",
        description="Moderator only: turn down an extension request.")
    @app_commands.describe(number="The extension number",
                           name="The league's name, if this channel runs more than one")
    async def rejectextension(self, interaction: discord.Interaction, number: int,
                              name: str = None):
        await self.decide_extension(interaction, number, name, granted=False)

    async def decide_extension(self, interaction, number, name, granted):
        ui.require_moderator(self.bot, interaction)
        league = self.resolve_league(interaction, name)
        await interaction.response.defer(ephemeral=True)

        decide = rules.grant_extension if granted else rules.reject_extension
        record = decide(self.bot.conn, league["id"], number, interaction.user.id)
        home = rules.get_player_by_id(self.bot.conn, record["home_player_id"])
        away = rules.get_player_by_id(self.bot.conn, record["away_player_id"])
        pool = rules.get_pool(self.bot.conn, record["pool_id"])
        next_week = record["week"] + config.MAX_EXTENSION_WEEKS

        await interaction.followup.send(
            f"{'✅' if granted else '❌'} Extension #{number} "
            + (f"granted. The match will be posted again with week {next_week}."
               if granted else "turned down."),
            ephemeral=True,
        )
        await self.announce_extension(league, pool, record, home, away, granted)

    async def announce_extension(self, league, pool, record, home, away, granted):
        """Tell both players, where the schedule lives."""
        next_week = record["week"] + config.MAX_EXTENSION_WEEKS
        where = (self.bot.get_channel(league["schedule_channel_id"])
                 if league["schedule_channel_id"] else None)
        await ui.safe_send(
            where or self.bot.get_channel(pool["channel_id"]),
            (f"⏳ **Extension granted** — <@{home['user_id']}> vs "
             f"<@{away['user_id']}> moves from week {record['week']} to week "
             f"{next_week}. It will be posted again then."
             if granted else
             f"⏳ **Extension turned down** — <@{home['user_id']}> vs "
             f"<@{away['user_id']}> still has to be played in week {record['week']}."))

    # ---- /extensions ----------------------------------------------------

    @app_commands.command(name="extensions",
                          description="List this draft's extension requests.")
    @app_commands.describe(name="The league's name, if this channel runs more than one")
    async def extensions(self, interaction: discord.Interaction, name: str = None):
        league = self.resolve_league(interaction, name)
        rows = rules.league_extensions(self.bot.conn, league["id"])
        if not rows:
            await interaction.response.send_message(
                f"No extensions have been requested in **{league['name']}**.",
                ephemeral=True)
            return

        marks = {"pending": "⏳", "granted": "✅", "rejected": "❌"}
        lines = []
        for row in rows:
            home = rules.get_player_by_id(self.bot.conn, row["home_player_id"])
            away = rules.get_player_by_id(self.bot.conn, row["away_player_id"])
            lines.append(
                f"{marks.get(row['status'], '•')} **#{row['number']}** week "
                f"{row['week']}: {home['display_name']} vs {away['display_name']} "
                f"— {row['status']}")
        await interaction.response.send_message(
            f"⏳ **Extensions — {league['name']}**\n" + "\n".join(lines),
            ephemeral=True)

    # ---- /subrequest ----------------------------------------------------

    @app_commands.command(
        name="subrequest",
        description="Ask to join or leave this draft. A moderator decides.",
    )
    @app_commands.describe(reason="Why you need a substitution")
    async def subrequest(self, interaction: discord.Interaction, reason: str):
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        await interaction.response.defer(ephemeral=True)

        record = rules.request_substitution(
            self.bot.conn, pool, interaction.user.id,
            interaction.user.display_name, reason,
        )
        league = rules.get_league(self.bot.conn, pool["league_id"])

        heading = ("wants to **sub out**" if record["kind"] == "out"
                   else "offers to **sub in**")
        summary = (
            f"🔄 **Substitution request #{record['request_number']}**\n"
            f"• Draft: **{league['name']}** — Pool {pool['label']} "
            f"(<#{pool['channel_id']}>)\n"
            f"• <@{record['user_id']}> {heading}\n"
            f"• Reason: {record['reason']}\n"
            f"• Asked at: {record['created_at']} UTC\n\n"
            f"Act on it with `/substitute out:@leaving in:@joining` in the draft channel."
        )
        if not await self.post_to_admins(league, summary, interaction):
            return

        await interaction.followup.send(
            f"✅ Request **#{record['request_number']}** sent to the moderators. "
            f"Nothing changes until one of them runs `/substitute`.",
            ephemeral=True,
        )

    # ---- /substitute ----------------------------------------------------

    @app_commands.command(
        name="substitute",
        description="Moderator only: hand a player's seat over to somebody else.",
    )
    @app_commands.describe(out="The player leaving", into="The player taking their place")
    async def substitute(self, interaction: discord.Interaction,
                         out: discord.Member, into: discord.Member):
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        ui.require_moderator(self.bot, interaction)

        await interaction.response.defer(ephemeral=True)
        result = rules.substitute_player(self.bot.conn, pool, out.id, into.id,
                                         into.display_name)

        roster = result["roster"]
        team = ", ".join(name for name, _ in roster["pokemon"]) or "no Pokémon yet"
        await ui.safe_send(
            interaction.channel,
            f"🔄 **Substitution in Pool {result['pool']}**: <@{result['in_user_id']}> takes "
            f"over seat {result['seat']} from **{result['out_name']}**.\n"
            f"• Team: {team}\n"
            f"• Budget remaining: **{roster['remaining']}** of {pool['budget']}\n"
            f"• Their timer is reset to **{ui.format_seconds(pool['base_timer'])}**.",
        )
        # The coach name on the spreadsheet changes with the seat.
        ui.resync_sheet(self.bot, pool["id"])

        await interaction.followup.send(
            f"✅ {into.mention} has taken over from {out.mention}.", ephemeral=True)

    # ---- /search --------------------------------------------------------

    @app_commands.command(name="search",
                          description="Look a Pokémon up in this league's tiering list.")
    @app_commands.describe(pokemon="The Pokémon to look for")
    @app_commands.autocomplete(pokemon=pokemon_autocomplete)
    async def search(self, interaction: discord.Interaction, pokemon: str):
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        self.require_document(pool)

        await interaction.response.defer(ephemeral=True)
        tab = await tiering_tab_for(self.bot, pool)
        entry = await asyncio.to_thread(
            self.bot.sheets.lookup, pool["doc_url"], pokemon, tab)

        if entry is None:
            await interaction.followup.send(
                f"❓ **{pokemon}** is not in this league's tiering list.", ephemeral=True)
            return

        # The database decides who owns what; the list only supplies prices.
        owner_row = rules.find_owner(self.bot.conn, pool["id"], entry["key"])
        if owner_row is not None:
            owner = rules.get_player_by_id(self.bot.conn, owner_row["player_id"])
            state = f"❌ **Drafted** by {owner['display_name']}"
        elif entry["banned"]:
            state = "🚫 **Banned** — no price in the tiering list"
        else:
            state = "✅ **Available**"

        lines = [f"**{entry['name']}**", f"• Status: {state}",
                 f"• Cost: **{'banned' if entry['cost'] is None else entry['cost']}**"]
        if pool["tera_budget"]:
            lines.append(
                "• As a Tera Captain: "
                + ("🚫 **Tera banned**" if entry["tera_banned"]
                   else f"**{entry['tera_cost']}** points from both budgets"))
        lines.append(f"• Tiering list row: {entry['row']}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    # ---- /available -----------------------------------------------------

    @app_commands.command(
        name="available",
        description="List undrafted Pokémon at a given point value.",
    )
    @app_commands.describe(cost="The point value to list",
                           tera="List Tera Captain prices instead")
    async def available(self, interaction: discord.Interaction, cost: int, tera: bool = False):
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        self.require_document(pool)
        if tera and not pool["tera_budget"]:
            raise rules.DraftError("❌ This draft is not a Tera format.")

        await interaction.response.defer(ephemeral=True)
        tab = await tiering_tab_for(self.bot, pool)
        entries = await asyncio.to_thread(
            self.bot.sheets.at_cost, pool["doc_url"], cost,
            self.taken_keys(pool["id"]), tab, tera,
        )

        label = "as Tera Captains" if tera else ""
        if not entries:
            await interaction.followup.send(
                f"No undrafted Pokémon cost **{cost}** points {label}.".strip(),
                ephemeral=True)
            return

        names = sorted(entry["name"] for entry in entries)
        header = f"**{len(names)} Pokémon available at {cost} points {label}**".strip() + "\n"

        # Long lists are chunked so they stay readable and inside Discord's
        # message size limit.
        for message in ui.pack_messages([header] + [", ".join(names[i:i + 30])
                                                    for i in range(0, len(names), 30)]):
            await interaction.followup.send(message, ephemeral=True)

    def require_document(self, pool):
        if not self.bot.sheets.enabled:
            raise rules.DraftError(
                f"❌ This command needs Google Sheets, which is not available: "
                f"{self.bot.sheets.reason}"
            )
        if not pool["doc_url"]:
            raise rules.DraftError(
                "❌ This league has no document linked. A moderator can add one with `/edit`."
            )


async def setup(bot):
    await bot.add_cog(ExtrasCog(bot))
