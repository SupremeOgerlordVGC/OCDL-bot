"""
Commands for setting a draft up and looking after it:
/setup, /edit, /draft, /canceldraft, /sync, /backup, /inspectsheet.
"""

import asyncio
import discord
from discord import app_commands
from discord.ext import commands

import config
import database
import draft as rules
import sheets
import ui


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---- helpers --------------------------------------------------------

    def require_admin(self, interaction, draft):
        if not ui.has_admin_rights(interaction, draft):
            role = interaction.guild.get_role(draft["admin_role_id"]) if interaction.guild else None
            role_name = role.mention if role else "the moderator role"
            raise rules.DraftError(f"❌ This command is for admins only ({role_name}).")

    # ---- /setup ---------------------------------------------------------

    @app_commands.command(
        name="setup",
        description="Create a new draft in this channel (does not start it).",
    )
    @app_commands.describe(
        budget="Starting budget for every player, e.g. 120",
        min_picks="Minimum Pokémon each player must draft",
        max_picks="Maximum Pokémon each player may draft (this is the number of rounds)",
        admin_role="Role allowed to run moderator commands for this draft",
        timer="Base pick timer in seconds, e.g. 120",
        max_trades="Most trades one player may make (player or free agent). 0 = unlimited.",
        admin_channel="Channel where trades are sent for approval",
        players="The players, @mentioned in draft order: @A @B @C",
        sheet_link="Optional Google Sheets link for this draft",
    )
    @app_commands.default_permissions(manage_guild=True)
    async def setup_draft(self, interaction: discord.Interaction, budget: int,
                          min_picks: int, max_picks: int, admin_role: discord.Role,
                          timer: int, admin_channel: discord.TextChannel,
                          players: str, max_trades: int = 0, sheet_link: str = None):
        if interaction.guild is None:
            raise rules.DraftError("❌ Drafts can only be created inside a server.")
        if not interaction.user.guild_permissions.manage_guild \
                and interaction.guild.owner_id != interaction.user.id:
            raise rules.DraftError("❌ You need the **Manage Server** permission to create a draft.")

        await interaction.response.defer(ephemeral=True)

        # Each coach's roster block on the spreadsheet is a fixed height, so
        # a sheet-linked draft cannot have more rounds than it has rows.
        block_rows = config.SHEET_LAYOUT["roster_rows"]
        if sheet_link and max_picks > block_rows:
            raise rules.DraftError(
                f"❌ The spreadsheet gives each coach {block_rows} roster rows, so a draft "
                f"linked to it can have at most {block_rows} rounds (you asked for {max_picks})."
            )

        roster = await ui.parse_players(interaction.guild, players)

        draft_id = rules.create_draft(
            self.bot.conn,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            admin_channel_id=admin_channel.id,
            admin_role_id=admin_role.id,
            budget=budget,
            min_picks=min_picks,
            max_picks=max_picks,
            base_timer=timer,
            players=roster,
            max_trades=max_trades,
            sheet_url=sheet_link,
        )

        order = "\n".join(f"{seat}. <@{user_id}>" for seat, (user_id, _) in enumerate(roster, 1))
        sheet_note = "linked ✅" if sheet_link else "none"
        if sheet_link and not self.bot.sheets.enabled:
            sheet_note = f"linked, but inactive ({self.bot.sheets.reason})"

        # Push the coach names straight to the spreadsheet; every other tab
        # in the template reads them from there.
        ui.resync_sheet(self.bot, draft_id)

        posted = await ui.safe_send(interaction.channel, 
            f"📋 **A new draft has been set up!**\n"
            f"• Budget: **{budget}** points\n"
            f"• Picks: **{min_picks}–{max_picks}** per player ({max_picks} rounds)\n"
            f"• Base timer: **{ui.format_seconds(timer)}**\n"
            f"• Trades per player: **{max_trades or 'unlimited'}**\n"
            f"• Moderators: {admin_role.mention}\n"
            f"• Trade approvals: {admin_channel.mention}\n"
            f"• Google Sheet: {sheet_note}\n\n"
            f"**Draft order**\n{order}\n\n"
            f"Settings can still be changed with `/edit`. "
            f"An admin starts the draft with `/draft`."
        )
        note = "" if posted else ui.permission_hint(interaction.channel)
        await interaction.followup.send(
            f"✅ Draft **#{draft_id}** created in {interaction.channel.mention}.{note}",
            ephemeral=True,
        )

    # ---- /edit ----------------------------------------------------------

    @app_commands.command(
        name="edit",
        description="Change this draft's settings (only before /draft starts it).",
    )
    @app_commands.describe(
        budget="New starting budget",
        min_picks="New minimum picks per player",
        max_picks="New maximum picks per player",
        sheet_link="New Google Sheets link",
    )
    async def edit_draft(self, interaction: discord.Interaction, budget: int = None,
                         min_picks: int = None, max_picks: int = None,
                         sheet_link: str = None):
        draft = rules.require_live_draft(self.bot.conn, interaction.channel_id)
        self.require_admin(interaction, draft)

        if budget is None and min_picks is None and max_picks is None and sheet_link is None:
            raise rules.DraftError("❌ Give at least one setting to change.")

        updated = rules.update_settings(self.bot.conn, draft, budget=budget,
                                        min_picks=min_picks, max_picks=max_picks,
                                        sheet_url=sheet_link)

        await interaction.response.send_message(
            f"✅ Draft updated.\n"
            f"• Budget: **{updated['budget']}**\n"
            f"• Picks: **{updated['min_picks']}–{updated['max_picks']}**\n"
            f"• Google Sheet: {updated['sheet_url'] or 'none'}",
            ephemeral=True,
        )

    # ---- /draft ---------------------------------------------------------

    @app_commands.command(name="draft", description="Start the draft in this channel.")
    async def start_draft(self, interaction: discord.Interaction):
        draft = rules.require_live_draft(self.bot.conn, interaction.channel_id)
        self.require_admin(interaction, draft)

        events = rules.start_draft(self.bot.conn, draft)

        posted = await ui.safe_send(
            interaction.channel,
            f"🚨 **THE DRAFT HAS BEGUN!** 🚨\n"
            f"{rules.player_count(self.bot.conn, draft['id'])} players · "
            f"{draft['max_picks']} rounds · **{draft['budget']}** points each.\n"
            f"`/edit` is now locked. `/pick` and `/queuepick` are open.",
        )
        # announce() starts the first timer whether or not it can post, so
        # the draft is never left active with nobody on the clock.
        await ui.announce(self.bot, draft["id"], events, channel=interaction.channel)

        note = "" if posted else ui.permission_hint(interaction.channel)
        await interaction.response.send_message(
            f"✅ The draft has started.{note}", ephemeral=True)

    # ---- /canceldraft ---------------------------------------------------

    @app_commands.command(
        name="canceldraft",
        description="Admin only: cancel this channel's draft. History is kept, not deleted.",
    )
    @app_commands.describe(confirm="Type the word CANCEL to confirm.")
    async def cancel_draft(self, interaction: discord.Interaction, confirm: str):
        draft = rules.require_live_draft(self.bot.conn, interaction.channel_id)
        self.require_admin(interaction, draft)

        if confirm.strip().upper() != "CANCEL":
            raise rules.DraftError("❌ Type `CANCEL` in the confirm box to cancel this draft.")

        self.bot.timers.cancel(draft["id"])
        rules.cancel_draft(self.bot.conn, draft["id"])
        await interaction.response.send_message(
            f"🛑 Draft **#{draft['id']}** has been cancelled. Its picks and rosters are kept "
            f"in the database, and this channel is free for a new `/setup`.",
            ephemeral=True,
        )
        await ui.safe_send(interaction.channel,
                           f"🛑 Draft **#{draft['id']}** has been cancelled by an admin.")

    # ---- /sync ----------------------------------------------------------

    @app_commands.command(
        name="sync",
        description="Push this draft's data from the database to the Google Sheet.",
    )
    async def sync_sheet(self, interaction: discord.Interaction):
        draft = rules.require_channel_draft(self.bot.conn, interaction.channel_id)
        self.require_admin(interaction, draft)

        if not self.bot.sheets.enabled:
            raise rules.DraftError(f"❌ Google Sheets is not available: {self.bot.sheets.reason}")
        if not draft["sheet_url"]:
            raise rules.DraftError("❌ This draft has no Google Sheet. Add one with `/edit`.")

        await interaction.response.defer(ephemeral=True)

        # Read everything out of SQLite here, then hand plain rows to the
        # worker thread. The database is never touched off the main thread.
        coaches, rosters, overflow = rules.export_for_sheet(self.bot.conn, draft["id"])
        try:
            written = await asyncio.to_thread(
                self.bot.sheets.full_sync, draft["sheet_url"], coaches, rosters
            )
        except sheets.SheetError as error:
            # A failed sync leaves the local database untouched, which is the
            # whole point of treating SQLite as the source of truth.
            await interaction.followup.send(
                f"⚠️ Synchronisation failed: {error}\n"
                f"**No local draft data was changed.** Try `/sync` again later.",
                ephemeral=True,
            )
            return

        message = (f"✅ Synchronised to Google Sheets: **{written['coaches']}** coach names and "
                   f"**{written['rosters']}** roster blocks written from the database.")
        if overflow:
            # Only reachable when trades have made a roster larger than a
            # block. The database still holds everything; the sheet cannot.
            too_big = ", ".join(f"{name} (+{count})" for name, count in overflow.items())
            message += (f"\n\n⚠️ These rosters no longer fit in a "
                        f"{config.SHEET_LAYOUT['roster_rows']}-row block, so the extra Pokémon "
                        f"are not shown on the sheet: {too_big}. `/roster` still shows them all.")
        await interaction.followup.send(message, ephemeral=True)

    # ---- /backup --------------------------------------------------------

    @app_commands.command(name="backup", description="Save a timestamped copy of the database.")
    @app_commands.default_permissions(manage_guild=True)
    async def backup_database(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        path = await asyncio.to_thread(database.backup, self.bot.conn)
        await asyncio.to_thread(database.prune_backups, None, config.BACKUPS_TO_KEEP)
        await interaction.followup.send(
            f"💾 Backup saved to `{path}`.\n"
            f"The bot also backs up automatically every "
            f"{config.BACKUP_INTERVAL_HOURS} hours (newest {config.BACKUPS_TO_KEEP} kept).",
            ephemeral=True,
        )

    # ---- /inspectsheet --------------------------------------------------

    @app_commands.command(
        name="inspectsheet",
        description="Admin only: report the structure of the linked Google Sheet. Reads only.",
    )
    async def inspect_sheet(self, interaction: discord.Interaction):
        draft = rules.require_channel_draft(self.bot.conn, interaction.channel_id)
        self.require_admin(interaction, draft)

        if not self.bot.sheets.enabled:
            raise rules.DraftError(f"❌ Google Sheets is not available: {self.bot.sheets.reason}")
        if not draft["sheet_url"]:
            raise rules.DraftError("❌ This draft has no Google Sheet. Add one with `/edit`.")

        await interaction.response.defer(ephemeral=True)
        report = await asyncio.to_thread(self.bot.sheets.describe, draft["sheet_url"])

        lines = [f"**{report['title']}**", f"Tabs: {', '.join(report['tabs'])}", ""]
        if report["problems"]:
            lines.append("**Problems**")
            lines += [f"❌ {problem}" for problem in report["problems"]]
        else:
            lines.append("**Layout checks**")
            lines += [f"✅ {note}" for note in report["notes"]]
        lines.append("")
        lines.append(f"Sheet writing is currently "
                     f"**{'ON' if config.SHEETS_WRITE_ENABLED else 'OFF'}** "
                     f"(`SHEETS_WRITE_ENABLED` in config.py).")

        for message in ui.pack_messages(lines):
            await interaction.followup.send(message, ephemeral=True)


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
