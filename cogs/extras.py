"""
Trades and Google Sheets lookups:
/trade, /approvetrade, /rejecttrade, /trades, /search, /available.
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

log = logging.getLogger("draftbot.extras")


class ExtrasCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def draft_for(self, interaction, channel=None):
        """Find the draft being talked about.

        Trades are approved from the admin channel, which is usually not the
        draft channel, so admins can name the draft channel explicitly.
        """
        channel_id = channel.id if channel else interaction.channel_id
        draft = rules.get_channel_draft(self.bot.conn, channel_id)
        if draft is None:
            where = channel.mention if channel else "this channel"
            raise rules.DraftError(
                f"❌ There is no draft in {where}. Name the draft channel with the "
                f"`channel` option."
            )
        return draft

    def taken_keys(self, draft_id):
        rows = self.bot.conn.execute(
            "SELECT pokemon_key FROM rosters WHERE draft_id = ?", (draft_id,)
        ).fetchall()
        return {row["pokemon_key"] for row in rows}

    # ---- /trade ---------------------------------------------------------

    @app_commands.command(name="trade", description="Propose a trade for an admin to approve.")
    @app_commands.describe(
        drops="Up to 5 Pokémon you are giving up, separated by commas",
        gets="Up to 5 Pokémon you want, separated by commas",
        partner="The player you are trading with. Leave blank to trade with the undrafted pool.",
    )
    async def trade(self, interaction: discord.Interaction, drops: str, gets: str,
                    partner: discord.Member = None):
        draft = rules.require_channel_draft(self.bot.conn, interaction.channel_id)
        await interaction.response.defer(ephemeral=True)

        drop_list = rules.parse_pokemon_list(drops, config.MAX_TRADE_SIZE)
        get_list = rules.parse_pokemon_list(gets, config.MAX_TRADE_SIZE)

        record, warnings = rules.propose_trade(
            self.bot.conn, draft, interaction.user.id, drop_list, get_list,
            partner_user_id=partner.id if partner else None,
        )

        # The bot does not judge trades - admins do - but anything that looks
        # odd is put in front of them before they approve it.
        notes = ""
        if warnings:
            notes = "\n\n⚠️ **Worth checking**\n" + "\n".join(f"• {w}" for w in warnings)

        allowance = ""
        if draft["max_trades"]:
            used = rules.trades_used(self.bot.conn, draft["id"], record["proposer_id"])
            allowance = f"\n• Trades used by proposer: {used} / {draft['max_trades']}"

        summary = (
            f"📨 **Trade #{record['trade_number']}** — *pending*\n"
            f"• Draft: {interaction.channel.mention} (draft #{draft['id']})\n"
            f"• Proposed by: <@{interaction.user.id}>\n"
            f"• Partner: {partner.mention if partner else '*undrafted pool (free agency)*'}\n"
            f"• Giving up: {', '.join(drop_list)}\n"
            f"• Receiving: {', '.join(get_list)}\n"
            f"• Proposed at: {record['created_at']} UTC"
            f"{allowance}\n\n"
            f"Approve with `/approvetrade trade_id:{record['trade_number']} "
            f"channel:#{interaction.channel.name}` or reject with `/rejecttrade`."
            f"{notes}"
        )

        admin_channel = self.bot.get_channel(draft["admin_channel_id"])
        if admin_channel is None:
            await interaction.followup.send(
                f"⚠️ Trade #{record['trade_number']} was saved, but the admin channel is not "
                f"visible to the bot, so nobody has been notified. Tell an admin directly.",
                ephemeral=True,
            )
            return

        if not await ui.safe_send(admin_channel, summary):
            await interaction.followup.send(
                f"⚠️ Trade #{record['trade_number']} was saved, but it could not be posted to "
                f"{admin_channel.mention} — I may not have permission to post there. "
                f"Tell an admin directly.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Trade **#{record['trade_number']}** submitted for approval. "
            f"Nothing changes until an admin approves it.",
            ephemeral=True,
        )

    # ---- /approvetrade --------------------------------------------------

    @app_commands.command(name="approvetrade", description="Admin only: approve a pending trade.")
    @app_commands.describe(
        trade_id="The trade number shown when it was proposed",
        channel="The draft channel the trade belongs to",
    )
    async def approvetrade(self, interaction: discord.Interaction, trade_id: int,
                           channel: discord.TextChannel = None):
        draft = self.draft_for(interaction, channel)
        if not ui.has_admin_rights(interaction, draft):
            raise rules.DraftError("❌ Only admins can approve trades.")

        await interaction.response.defer(ephemeral=True)
        record, notes = rules.approve_trade(self.bot.conn, draft, trade_id, interaction.user.id)

        # A free-agency pickup has no cost until the Pokédex is consulted.
        if self.bot.sheets.enabled and draft["sheet_url"]:
            for name in json.loads(record["gets"]):
                try:
                    entry = await asyncio.to_thread(
                        self.bot.sheets.lookup, draft["sheet_url"], name)
                except Exception:
                    log.exception("could not read the cost of %s after a trade", name)
                    continue
                if entry and entry["cost"] is not None:
                    rules.apply_sheet_cost(self.bot.conn, draft, name, entry["cost"])

        proposer = rules.get_player_by_id(self.bot.conn, record["proposer_id"])
        partner = (rules.get_player_by_id(self.bot.conn, record["partner_id"])
                   if record["partner_id"] else None)
        drops = ", ".join(json.loads(record["drops"]))
        gets = ", ".join(json.loads(record["gets"]))

        report = f"✅ Trade #{trade_id} approved."
        if notes:
            report += "\n\nNotes:\n" + "\n".join(f"• {note}" for note in notes)
        await interaction.followup.send(report, ephemeral=True)

        draft_channel = self.bot.get_channel(draft["channel_id"])
        await ui.safe_send(
            draft_channel,
            (f"🤝 **Trade #{trade_id} approved!**\n"
                f"• **{proposer['display_name']}** gives up: {drops}\n"
                f"• **{proposer['display_name']}** receives: {gets}\n"
             + (f"• Partner: **{partner['display_name']}**\n" if partner else
                "• Traded with the undrafted pool\n")
             + "Rosters and budgets have been updated."),
        )

        # A trade moves Pokémon between rosters, so push the whole picture.
        ui.resync_sheet(self.bot, draft["id"])

    # ---- /rejecttrade ---------------------------------------------------

    @app_commands.command(name="rejecttrade", description="Admin only: reject a pending trade.")
    @app_commands.describe(
        trade_id="The trade number",
        channel="The draft channel the trade belongs to",
    )
    async def rejecttrade(self, interaction: discord.Interaction, trade_id: int,
                          channel: discord.TextChannel = None):
        draft = self.draft_for(interaction, channel)
        if not ui.has_admin_rights(interaction, draft):
            raise rules.DraftError("❌ Only admins can reject trades.")

        rules.reject_trade(self.bot.conn, draft, trade_id, interaction.user.id)
        await interaction.response.send_message(
            f"🚫 Trade #{trade_id} rejected. No rosters were changed.", ephemeral=True
        )

    # ---- /trades --------------------------------------------------------

    @app_commands.command(name="trades", description="List this draft's trades.")
    @app_commands.describe(channel="The draft channel. Defaults to this one.")
    async def trades(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        draft = self.draft_for(interaction, channel)
        rows = self.bot.conn.execute(
            "SELECT * FROM trades WHERE draft_id = ? ORDER BY trade_number", (draft["id"],)
        ).fetchall()

        if not rows:
            await interaction.response.send_message("No trades have been proposed yet.",
                                                    ephemeral=True)
            return

        icons = {"pending": "⏳", "approved": "✅", "rejected": "🚫"}
        lines = []
        for row in rows:
            proposer = rules.get_player_by_id(self.bot.conn, row["proposer_id"])
            lines.append(
                f"{icons.get(row['status'], '•')} **#{row['trade_number']}** "
                f"{proposer['display_name']}: {', '.join(json.loads(row['drops']))} → "
                f"{', '.join(json.loads(row['gets']))} *({row['status']})*"
            )

        await interaction.response.send_message("\n".join(lines[:25]), ephemeral=True)

    # ---- /search --------------------------------------------------------

    @app_commands.command(name="search", description="Look a Pokémon up in the linked Google Sheet.")
    @app_commands.describe(pokemon="The Pokémon to look for")
    async def search(self, interaction: discord.Interaction, pokemon: str):
        draft = rules.require_channel_draft(self.bot.conn, interaction.channel_id)
        self.require_sheet(draft)

        await interaction.response.defer(ephemeral=True)
        entry = await asyncio.to_thread(self.bot.sheets.lookup, draft["sheet_url"], pokemon)

        if entry is None:
            await interaction.followup.send(
                f"❓ **{pokemon}** is not in the Pokédex on this sheet.", ephemeral=True
            )
            return

        # The database decides who owns what; the sheet only supplies costs.
        owner_row = rules.find_owner(self.bot.conn, draft["id"], entry["key"])
        if owner_row is not None:
            owner = rules.get_player_by_id(self.bot.conn, owner_row["player_id"])
            state = f"❌ **Drafted** by {owner['display_name']}"
        elif entry["banned"]:
            state = "🚫 **Banned** — no point value in the Pokédex"
        else:
            state = "✅ **Available**"

        cost = "banned / unpriced" if entry["cost"] is None else f"{entry['cost']} points"
        await interaction.followup.send(
            f"**{entry['name']}**\n"
            f"• Status: {state}\n"
            f"• Cost: **{cost}**\n"
            f"• Pokédex row: {entry['row']}",
            ephemeral=True,
        )

    # ---- /available -----------------------------------------------------

    @app_commands.command(
        name="available",
        description="List undrafted Pokémon at a given point value.",
    )
    @app_commands.describe(cost="The point value to list")
    async def available(self, interaction: discord.Interaction, cost: int):
        draft = rules.require_channel_draft(self.bot.conn, interaction.channel_id)
        self.require_sheet(draft)

        await interaction.response.defer(ephemeral=True)
        entries = await asyncio.to_thread(
            self.bot.sheets.at_cost, draft["sheet_url"], cost, self.taken_keys(draft["id"])
        )

        if not entries:
            await interaction.followup.send(
                f"No undrafted Pokémon cost **{cost}** points.", ephemeral=True
            )
            return

        names = sorted(entry["name"] for entry in entries)
        header = f"**{len(names)} Pokémon available at {cost} points**\n"

        # Long lists are chunked so they stay readable and inside Discord's
        # message size limit.
        chunks = ui.pack_messages([header] + [", ".join(names[i:i + 30])
                                              for i in range(0, len(names), 30)])
        for message in chunks:
            await interaction.followup.send(message, ephemeral=True)

    def require_sheet(self, draft):
        if not self.bot.sheets.enabled:
            raise rules.DraftError(
                f"❌ This command needs Google Sheets, which is not available: "
                f"{self.bot.sheets.reason}"
            )
        if not draft["sheet_url"]:
            raise rules.DraftError(
                "❌ This draft has no Google Sheet linked. An admin can add one with `/edit`."
            )


async def setup(bot):
    await bot.add_cog(ExtrasCog(bot))
