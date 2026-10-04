"""
Commands for setting a league up and looking after it:
/setup, /edit, /draft, /canceldraft, /archive, /draft-moderator,
/postschedule, /sync, /inspectsheet, /backup.

A league is created by running /setup in the channel where you want trades
approved. That channel becomes the admin channel, and the draft itself
happens in the pool channel (or two) named in the command.
"""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

import config
import database
import draft as rules
import sheets
import ui

log = logging.getLogger("draftbot.admin")

MODES = [
    app_commands.Choice(name="Full season (draft, trades and schedule)", value="full_season"),
    app_commands.Choice(name="Draft only (archive once the draft finishes)", value="draft_only"),
]


class AdminCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---- /setup ---------------------------------------------------------

    @app_commands.command(
        name="setup",
        description="Create a league. Run this in the channel where trades should be approved.",
    )
    @app_commands.describe(
        name="A name for this draft. Must be unique on this server.",
        draft_channel="Channel where pool A drafts",
        players="Pool A players, @mentioned in draft order",
        min_picks="Minimum Pokémon each player must draft",
        max_picks="Maximum Pokémon each player may draft (this is the number of rounds)",
        timer="Base pick timer in seconds, e.g. 120",
        mode="Whether the bot handles the whole season or only the draft",
        budget="Points each player may spend. Read from a template if you link one.",
        tera_budget="Tera Captain points. 0 means no Tera format.",
        max_trades="Most trades one player may make. 0 = unlimited.",
        template_link="Full league template. Gives rosters, settings and the schedule.",
        tiering_link="Simple tiering sheet: names in A, prices in B, Tera prices in C.",
        draft_channel_b="Optional second pool's channel (needs a template)",
        players_b="Optional second pool's players, @mentioned in draft order",
    )
    @app_commands.choices(mode=MODES)
    async def setup_league(self, interaction: discord.Interaction, name: str,
                           draft_channel: discord.TextChannel, players: str,
                           min_picks: int, max_picks: int, timer: int,
                           mode: app_commands.Choice[str],
                           budget: int = None, tera_budget: int = None,
                           max_trades: int = 0, template_link: str = None,
                           tiering_link: str = None,
                           draft_channel_b: discord.TextChannel = None,
                           players_b: str = None):
        if interaction.guild is None:
            raise rules.DraftError("❌ Leagues can only be created inside a server.")
        ui.require_moderator(self.bot, interaction)

        await interaction.response.defer(ephemeral=True)

        if template_link and tiering_link:
            raise rules.DraftError(
                "❌ Link a template **or** a tiering sheet, not both. The template already "
                "contains its own tiering tab."
            )
        doc_kind = "template" if template_link else ("tiering" if tiering_link else "none")
        doc_url = template_link or tiering_link

        if (draft_channel_b is None) != (players_b is None):
            raise rules.DraftError(
                "❌ A second pool needs both `draft_channel_b` and `players_b`."
            )

        # A template declares the league's own budget, so the sheet wins.
        notes = []
        if doc_kind == "template":
            if not self.bot.sheets.enabled:
                raise rules.DraftError(
                    f"❌ A template needs Google Sheets, which is not available: "
                    f"{self.bot.sheets.reason}"
                )
            settings = await asyncio.to_thread(self.bot.sheets.read_settings, doc_url)
            sheet_budget = _as_int(settings.get("budget"))
            sheet_tera = _as_int(settings.get("tera_budget")) or 0
            if not bool(settings.get("tera_format")):
                sheet_tera = 0

            if sheet_budget is None:
                raise rules.DraftError(
                    "❌ The template's Settings tab has no budget. Fill in its `Budget` cell."
                )
            for given, found, label in ((budget, sheet_budget, "budget"),
                                        (tera_budget, sheet_tera, "Tera budget")):
                if given is not None and given != found:
                    raise rules.DraftError(
                        f"❌ You gave a {label} of {given} but the template says {found}. "
                        f"The spreadsheet works the budgets out itself, so change it there "
                        f"or leave the option out."
                    )
            budget, tera_budget = sheet_budget, sheet_tera
            notes.append(f"Budgets read from the template: {budget} points"
                         + (f", {tera_budget} Tera points" if tera_budget else ""))
        else:
            if budget is None:
                raise rules.DraftError(
                    "❌ Without a template you need to give a `budget`."
                )
            tera_budget = tera_budget or 0

        roster_a = await ui.parse_players(interaction.guild, players)
        pools = [("A", draft_channel.id, roster_a)]
        if draft_channel_b is not None:
            roster_b = await ui.parse_players(interaction.guild, players_b)
            pools.append(("B", draft_channel_b.id, roster_b))

        league_id = rules.create_league(
            self.bot.conn,
            guild_id=interaction.guild_id,
            admin_channel_id=interaction.channel_id,
            name=name,
            pools=pools,
            budget=budget,
            min_picks=min_picks,
            max_picks=max_picks,
            base_timer=timer,
            mode=mode.value,
            doc_kind=doc_kind,
            doc_url=doc_url,
            tera_budget=tera_budget,
            max_trades=max_trades,
        )
        league = rules.get_league(self.bot.conn, league_id)

        # Push the coach names and pool letters straight to the template;
        # every other tab in it reads the names from there.
        pool_rows = rules.get_league_pools(self.bot.conn, league_id)
        ui.resync_sheet(self.bot, pool_rows[0]["id"])

        document = {"template": "full template", "tiering": "tiering sheet",
                    "none": "none"}[doc_kind]
        if doc_url and not self.bot.sheets.enabled:
            document += f" (inactive: {self.bot.sheets.reason})"

        summary = (
            f"📋 **{league['name']}** has been set up.\n"
            f"• Mode: **{'full season' if league['mode'] == 'full_season' else 'draft only'}**\n"
            f"• Budget: **{budget}** points"
            + (f" · Tera budget **{tera_budget}** (max {config.MAX_TERA_CAPTAINS} captains)\n"
               if tera_budget else "\n")
            + f"• Picks: **{min_picks}–{max_picks}** per player ({max_picks} rounds)\n"
            f"• Base timer: **{ui.format_seconds(timer)}**\n"
            f"• Trades per player: **{max_trades or 'unlimited'}**\n"
            f"• Document: {document}\n"
            f"• Trade approvals: {interaction.channel.mention}\n"
        )
        for pool in pool_rows:
            seats = rules.get_players(self.bot.conn, pool["id"])
            order = "\n".join(f"{p['draft_position']}. <@{p['user_id']}>" for p in seats)
            summary += (f"\n**Pool {pool['label']}** — <#{pool['channel_id']}> "
                        f"({len(seats)} players)\n{order}\n")

        await ui.safe_send(interaction.channel, summary)
        for pool in pool_rows:
            await ui.safe_send(
                self.bot.get_channel(pool["channel_id"]),
                f"📋 **{league['name']} — Pool {pool['label']}** will be drafted here.\n"
                f"Settings can still be changed with `/edit`. A moderator starts the "
                f"draft with `/draft`."
            )

        extra = ("\n" + "\n".join(notes)) if notes else ""
        await interaction.followup.send(
            f"✅ **{league['name']}** created with "
            f"{len(pool_rows)} pool{'s' if len(pool_rows) > 1 else ''}.{extra}",
            ephemeral=True,
        )

    # ---- /edit ----------------------------------------------------------

    @app_commands.command(
        name="edit",
        description="Change this league's settings (only before /draft starts it).",
    )
    @app_commands.describe(
        budget="New starting budget",
        tera_budget="New Tera budget (0 for no Tera format)",
        min_picks="New minimum picks per player",
        max_picks="New maximum picks per player",
        max_trades="New trade limit per player (0 = unlimited)",
        document_link="New template or tiering link",
    )
    async def edit_league(self, interaction: discord.Interaction, budget: int = None,
                          tera_budget: int = None, min_picks: int = None,
                          max_picks: int = None, max_trades: int = None,
                          document_link: str = None):
        pool = rules.require_live_pool(self.bot.conn, interaction.channel_id)
        ui.require_moderator(self.bot, interaction)

        if all(value is None for value in
               (budget, tera_budget, min_picks, max_picks, max_trades, document_link)):
            raise rules.DraftError("❌ Give at least one setting to change.")

        updated = rules.update_settings(
            self.bot.conn, pool, budget=budget, min_picks=min_picks,
            max_picks=max_picks, doc_url=document_link,
            tera_budget=tera_budget, max_trades=max_trades,
        )
        await interaction.response.send_message(
            f"✅ **{updated['name']}** updated.\n"
            f"• Budget: **{updated['budget']}**"
            + (f" · Tera **{updated['tera_budget']}**" if updated["tera_budget"] else "")
            + f"\n• Picks: **{updated['min_picks']}–{updated['max_picks']}**\n"
            f"• Trades per player: **{updated['max_trades'] or 'unlimited'}**\n"
            f"• Document: {updated['doc_url'] or 'none'}",
            ephemeral=True,
        )

    # ---- /draft ---------------------------------------------------------

    @app_commands.command(name="draft", description="Start the draft in this pool's channel.")
    async def start_draft(self, interaction: discord.Interaction):
        pool = rules.require_live_pool(self.bot.conn, interaction.channel_id)
        ui.require_moderator(self.bot, interaction)

        events = rules.start_pool(self.bot.conn, pool)

        tera = ""
        if pool["tera_budget"]:
            tera = (f" · **{pool['tera_budget']}** Tera points, up to "
                    f"{config.MAX_TERA_CAPTAINS} Tera Captains")
        posted = await ui.safe_send(
            interaction.channel,
            f"🚨 **{pool['league_name']} — Pool {pool['label']} HAS BEGUN!** 🚨\n"
            f"{rules.player_count(self.bot.conn, pool['id'])} players · "
            f"{pool['max_picks']} rounds · **{pool['budget']}** points each{tera}.\n"
            f"`/edit` is now locked. `/pick` and `/queuepick` are open.",
        )
        # announce() starts the first timer whether or not it can post, so
        # the draft is never left active with nobody on the clock.
        await ui.announce(self.bot, pool["id"], events, channel=interaction.channel)

        note = "" if posted else ui.permission_hint(interaction.channel)
        await interaction.response.send_message(
            f"✅ Pool {pool['label']} of **{pool['league_name']}** has started.{note}",
            ephemeral=True)

    # ---- /canceldraft and /archive --------------------------------------

    @app_commands.command(
        name="canceldraft",
        description="Moderator only: cancel a league. History is kept, not deleted.",
    )
    @app_commands.describe(name="The league's name", confirm="Type CANCEL to confirm.")
    async def cancel_draft(self, interaction: discord.Interaction, name: str, confirm: str):
        ui.require_moderator(self.bot, interaction)
        league = rules.require_league_by_name(self.bot.conn, interaction.guild_id, name)

        if confirm.strip().upper() != "CANCEL":
            raise rules.DraftError("❌ Type `CANCEL` in the confirm box to cancel this league.")

        pool_rows = rules.get_league_pools(self.bot.conn, league["id"])
        for pool in pool_rows:
            self.bot.timers.cancel(pool["id"])
        rules.cancel_league(self.bot.conn, league["id"])

        await interaction.response.send_message(
            f"🛑 **{league['name']}** has been cancelled. Its picks and rosters are kept "
            f"in the database, and its channels are free for a new `/setup`.",
            ephemeral=True,
        )
        for pool in pool_rows:
            await ui.safe_send(self.bot.get_channel(pool["channel_id"]),
                               f"🛑 **{league['name']}** has been cancelled by a moderator.")

    @app_commands.command(
        name="archive",
        description="Moderator only: archive a finished league and free its channels.",
    )
    @app_commands.describe(name="The league's name")
    async def archive(self, interaction: discord.Interaction, name: str):
        ui.require_moderator(self.bot, interaction)
        league = rules.require_league_by_name(self.bot.conn, interaction.guild_id, name)

        for pool in rules.get_league_pools(self.bot.conn, league["id"]):
            self.bot.timers.cancel(pool["id"])
        rules.archive_league(self.bot.conn, league["id"])

        await interaction.response.send_message(
            f"🗃️ **{league['name']}** is archived. Rosters and history stay readable, and "
            f"its channels are free for a new `/setup`.",
            ephemeral=True,
        )

    # ---- /draft-moderator -----------------------------------------------

    @app_commands.command(
        name="draft-moderator",
        description="Administrator only: choose which role may run moderator commands.",
    )
    @app_commands.describe(role="The role to grant or revoke",
                           remove="Set to True to take the role's access away")
    @app_commands.default_permissions(administrator=True)
    async def draft_moderator(self, interaction: discord.Interaction,
                              role: discord.Role = None, remove: bool = False):
        if interaction.guild is None:
            raise rules.DraftError("❌ This command only works inside a server.")
        if not ui.is_administrator(interaction):
            raise rules.DraftError(
                "❌ Only somebody with the **Administrator** permission can change who "
                "the draft moderators are."
            )

        if role is None:
            current = rules.moderator_role_ids(self.bot.conn, interaction.guild_id)
            listing = ", ".join(f"<@&{role_id}>" for role_id in current) or "none"
            await interaction.response.send_message(
                f"**Draft moderator roles:** {listing}\n"
                f"Administrators and the server owner always have access.",
                ephemeral=True,
            )
            return

        if remove:
            existed = rules.remove_moderator_role(self.bot.conn, interaction.guild_id, role.id)
            message = (f"✅ {role.mention} can no longer run moderator commands."
                       if existed else f"{role.mention} was not a moderator role.")
        else:
            rules.add_moderator_role(self.bot.conn, interaction.guild_id, role.id,
                                     interaction.user.id)
            message = (f"✅ {role.mention} can now run every moderator command for drafts "
                       f"on this server — approving trades, running `/setup`, and so on.")

        await interaction.response.send_message(message, ephemeral=True)

    # ---- /postschedule --------------------------------------------------

    @app_commands.command(
        name="postschedule",
        description="Moderator only: post a week's matchups, pinging both players.",
    )
    @app_commands.describe(week="Which week to post", name="The league's name")
    async def postschedule(self, interaction: discord.Interaction, week: int, name: str):
        ui.require_moderator(self.bot, interaction)
        league = rules.require_league_by_name(self.bot.conn, interaction.guild_id, name)

        if league["mode"] != "full_season":
            raise rules.DraftError(
                "❌ Schedules are a full-season feature. **{}** is a draft-only league."
                .format(league["name"])
            )
        if league["doc_kind"] != "template":
            raise rules.DraftError(
                "❌ The schedule lives on the league template, so this needs a template link."
            )
        if not self.bot.sheets.enabled:
            raise rules.DraftError(
                f"❌ Google Sheets is not available: {self.bot.sheets.reason}")
        if week < 1:
            raise rules.DraftError("❌ Weeks start at 1.")

        await interaction.response.defer(ephemeral=True)
        matches = await asyncio.to_thread(
            self.bot.sheets.read_schedule, league["doc_url"], week)

        if not matches:
            await interaction.followup.send(
                f"No matches found for week {week} on the schedule.", ephemeral=True)
            return

        # Coach names on the sheet are the usernames the bot wrote there, so
        # they map straight back to Discord accounts.
        mentions = {}
        for pool in rules.get_league_pools(self.bot.conn, league["id"]):
            for player in rules.get_players(self.bot.conn, pool["id"]):
                mentions[rules.normalize(player["display_name"])] = player["user_id"]

        lines = [f"📅 **{league['name']} — Week {week}**", ""]
        unknown = []
        for home, away in matches:
            lines.append(f"{_who(home, mentions, unknown)} **vs** {_who(away, mentions, unknown)}")

        if unknown:
            lines += ["", f"*Not matched to a Discord account: "
                          f"{', '.join(sorted(set(unknown)))}. Run `/sync` so the sheet's "
                          f"coach names match the bot's.*"]

        for message in ui.pack_messages(lines):
            await ui.safe_send(interaction.channel, message)
        await interaction.followup.send(
            f"✅ Posted {len(matches)} match(es) for week {week}.", ephemeral=True)

    # ---- /sync ----------------------------------------------------------

    @app_commands.command(
        name="sync",
        description="Push this league's data from the database to its template.",
    )
    async def sync_sheet(self, interaction: discord.Interaction):
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        ui.require_moderator(self.bot, interaction)

        if pool["doc_kind"] != "template":
            raise rules.DraftError(
                "❌ There is nothing to write: this league has no template. A tiering "
                "sheet is read-only."
            )
        if not self.bot.sheets.enabled:
            raise rules.DraftError(
                f"❌ Google Sheets is not available: {self.bot.sheets.reason}")

        await interaction.response.defer(ephemeral=True)

        # Read everything out of SQLite here, then hand plain rows to the
        # worker thread. The database is never touched off the main thread.
        coaches, rosters, overflow = rules.export_for_sheet(
            self.bot.conn, pool["league_id"])
        try:
            written = await asyncio.to_thread(
                self.bot.sheets.full_sync, pool["doc_url"],
                bool(pool["tera_budget"]), coaches, rosters)
        except sheets.SheetError as error:
            # A failed sync leaves the local database untouched, which is the
            # whole point of treating SQLite as the source of truth.
            await interaction.followup.send(
                f"⚠️ Synchronisation failed: {error}\n"
                f"**No local draft data was changed.** Try `/sync` again later.",
                ephemeral=True,
            )
            return

        message = (f"✅ Synchronised to **{written['tab']}**: **{written['coaches']}** coach "
                   f"names and **{written['rosters']}** roster blocks written from the database.")
        if overflow:
            too_big = ", ".join(f"{name} (+{count})" for name, count in overflow.items())
            message += (f"\n\n⚠️ These rosters no longer fit in a "
                        f"{config.SHEET_LAYOUT['roster_rows']}-row block, so the extra Pokémon "
                        f"are not shown on the sheet: {too_big}. `/roster` still shows them all.")
        await interaction.followup.send(message, ephemeral=True)

    # ---- /inspectsheet --------------------------------------------------

    @app_commands.command(
        name="inspectsheet",
        description="Moderator only: report the structure of this league's document. Reads only.",
    )
    async def inspect_sheet(self, interaction: discord.Interaction):
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        ui.require_moderator(self.bot, interaction)

        if not pool["doc_url"]:
            raise rules.DraftError("❌ This league has no document. Add one with `/edit`.")
        if not self.bot.sheets.enabled:
            raise rules.DraftError(
                f"❌ Google Sheets is not available: {self.bot.sheets.reason}")

        await interaction.response.defer(ephemeral=True)
        report = await asyncio.to_thread(
            self.bot.sheets.describe, pool["doc_url"], pool["doc_kind"])

        lines = [f"**{report['title']}** — read as a {pool['doc_kind']} document",
                 f"Tabs: {', '.join(report['tabs'])}", ""]
        if report["problems"]:
            lines.append("**Problems**")
            lines += [f"❌ {problem}" for problem in report["problems"]]
        else:
            lines.append("**Layout checks**")
            lines += [f"✅ {note}" for note in report["notes"]]
        lines += ["", f"Sheet writing is currently "
                      f"**{'ON' if config.SHEETS_WRITE_ENABLED else 'OFF'}** "
                      f"(`SHEETS_WRITE_ENABLED` in config.py)."]

        for message in ui.pack_messages(lines):
            await interaction.followup.send(message, ephemeral=True)

    # ---- /backup --------------------------------------------------------

    @app_commands.command(name="backup", description="Save a timestamped copy of the database.")
    @app_commands.default_permissions(manage_guild=True)
    async def backup_database(self, interaction: discord.Interaction):
        ui.require_moderator(self.bot, interaction)
        await interaction.response.defer(ephemeral=True)

        path = await asyncio.to_thread(database.backup, self.bot.conn)
        await asyncio.to_thread(database.prune_backups, None, config.BACKUPS_TO_KEEP)
        await interaction.followup.send(
            f"💾 Backup saved to `{path}`.\n"
            f"The bot also backs up automatically every "
            f"{config.BACKUP_INTERVAL_HOURS} hours (newest {config.BACKUPS_TO_KEEP} kept).",
            ephemeral=True,
        )


def _as_int(value):
    """Spreadsheet numbers arrive as floats; settings cells may be blank."""
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _who(coach_name, mentions, unknown):
    """A coach's ping if the bot knows them, otherwise their sheet name."""
    user_id = mentions.get(rules.normalize(coach_name))
    if user_id is None:
        unknown.append(coach_name)
        return f"**{coach_name}**"
    return f"<@{user_id}>"


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
