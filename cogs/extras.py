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
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import draft as rules
import ui
from cogs.drafting import pokemon_autocomplete, tiering_tab_for

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
            return rules.require_league_by_name(self.bot.conn, interaction.guild_id, name)

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

    @app_commands.command(name="trade", description="Propose a trade for a moderator to approve.")
    @app_commands.describe(
        drops="Up to 5 Pokémon you are giving up, separated by commas",
        gets="Up to 5 Pokémon you want, separated by commas",
        partner="The player you are trading with. Leave blank for the undrafted pool.",
    )
    async def trade(self, interaction: discord.Interaction, drops: str, gets: str,
                    partner: discord.Member = None):
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        if pool["mode"] != "full_season":
            raise rules.DraftError(
                f"❌ **{pool['league_name']}** is a draft-only league, so it does not "
                f"handle trades."
            )
        await interaction.response.defer(ephemeral=True)

        drop_list = rules.parse_pokemon_list(drops, config.MAX_TRADE_SIZE)
        get_list = rules.parse_pokemon_list(gets, config.MAX_TRADE_SIZE)

        record, warnings = rules.propose_trade(
            self.bot.conn, pool, interaction.user.id, drop_list, get_list,
            partner_user_id=partner.id if partner else None,
        )

        # The bot does not judge trades - moderators do - but anything that
        # looks odd is put in front of them before they approve it.
        notes = ""
        if warnings:
            notes = "\n\n⚠️ **Worth checking**\n" + "\n".join(f"• {w}" for w in warnings)

        allowance = ""
        if pool["max_trades"]:
            used = rules.trades_used(self.bot.conn, pool["id"], record["proposer_id"])
            allowance = f"\n• Trades used by proposer: {used} / {pool['max_trades']}"

        league = rules.get_league(self.bot.conn, pool["league_id"])
        summary = (
            f"📨 **Trade #{record['trade_number']}** — *pending*\n"
            f"• Draft: **{league['name']}** — Pool {pool['label']} "
            f"(<#{pool['channel_id']}>)\n"
            f"• Proposed by: <@{interaction.user.id}>\n"
            f"• Partner: {partner.mention if partner else '*undrafted pool (free agency)*'}\n"
            f"• Giving up: {', '.join(drop_list)}\n"
            f"• Receiving: {', '.join(get_list)}\n"
            f"• Proposed at: {record['created_at']} UTC"
            f"{allowance}\n\n"
            f"Approve with `/approvetrade trade_id:{record['trade_number']} "
            f"name:{league['name']}` or reject with `/rejecttrade`."
            f"{notes}"
        )
        if not await self.post_to_admins(league, summary, interaction):
            return

        await interaction.followup.send(
            f"✅ Trade **#{record['trade_number']}** submitted for approval. "
            f"Nothing changes until a moderator approves it.",
            ephemeral=True,
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

        # A free-agency pickup has no price until the tiering list is consulted.
        if self.bot.sheets.enabled and pool["doc_url"]:
            for pokemon in json.loads(record["gets"]):
                try:
                    tab = await tiering_tab_for(self.bot, pool)
                    entry = await asyncio.to_thread(
                        self.bot.sheets.lookup, pool["doc_url"], pokemon, tab)
                except Exception:
                    log.exception("could not read the price of %s after a trade", pokemon)
                    continue
                if entry and entry["cost"] is not None:
                    rules.apply_sheet_cost(self.bot.conn, pool, pokemon, entry["cost"])

        proposer = rules.get_player_by_id(self.bot.conn, record["proposer_id"])
        partner = (rules.get_player_by_id(self.bot.conn, record["partner_id"])
                   if record["partner_id"] else None)
        drops = ", ".join(json.loads(record["drops"]))
        gets = ", ".join(json.loads(record["gets"]))

        report = f"✅ Trade #{trade_id} approved."
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
            + "Rosters and budgets have been updated.",
        )
        # A trade moves Pokémon between rosters, so push the whole picture.
        ui.resync_sheet(self.bot, pool["id"])

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

        icons = {"pending": "⏳", "approved": "✅", "rejected": "🚫"}
        lines = [f"**{league['name']} — trades**"]
        for row in rows[-25:]:
            proposer = rules.get_player_by_id(self.bot.conn, row["proposer_id"])
            lines.append(
                f"{icons.get(row['status'], '•')} **#{row['trade_number']}** "
                f"{proposer['display_name']}: {', '.join(json.loads(row['drops']))} → "
                f"{', '.join(json.loads(row['gets']))} *({row['status']})*"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

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
