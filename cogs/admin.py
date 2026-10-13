"""
Commands for setting a league up and looking after it:
/setup, /edit, /draft, /canceldraft, /archive, /draft-moderator,
/postschedule, /sync, /inspectsheet, /backup.

A league is created by running /setup in the channel where you want trades
approved. That channel becomes the admin channel, and the draft itself
happens in the pool channel (or two) named in the command.
"""

import asyncio
import json
import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

import config
import database
import draft as rules
import sheets
import ui
from cogs.drafting import tiering_tab_for

log = logging.getLogger("draftbot.admin")

MODES = [
    app_commands.Choice(name="Full season (draft, trades and schedule)", value="full_season"),
    app_commands.Choice(name="Draft + schedule (no trades)", value="draft_schedule"),
    app_commands.Choice(name="Draft only (archive once the draft finishes)", value="draft_only"),
]

VISIBILITY = [
    app_commands.Choice(name="Public: anyone can see it on draft.ogreclan.com", value="public"),
    app_commands.Choice(name="Private: only its players (signed in with Discord) can see it", value="private"),
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
        players_role="Instead of listing them: a role whose members the bot shuffles",
        min_picks="Minimum Pokémon each player must draft",
        max_picks="Maximum Pokémon each player may draft (this is the number of rounds)",
        timer="Base pick timer in seconds, e.g. 120",
        mode="Whether the bot handles the whole season or only the draft",
        budget="Points each player may spend. Read from a template if you link one.",
        tera_budget="Tera Captain points. 0 means no Tera format.",
        max_trades="Most Pokémon one player may pick up in trades. 0 = unlimited.",
        template_link="Full league template. Gives rosters, settings and the schedule.",
        tiering_link="Simple tiering sheet: names in A, prices in B, Tera prices in C.",
        draft_channel_b="Optional second pool's channel (needs a template)",
        players_b="Optional second pool's players, @mentioned in draft order",
        players_role_b="Instead of listing them: a role for the second pool",
        weeks="Season length when the bot generates the schedule itself",
        visibility="Who can see it on draft.ogreclan.com. Public unless you choose Private.",
        website="Use draft.ogreclan.com: picks there, Ready battles on play.ogreclan.com, kills and deaths recorded. On unless you turn it off.",
    )
    @app_commands.choices(mode=MODES, visibility=VISIBILITY)
    async def setup_league(self, interaction: discord.Interaction, name: str,
                           draft_channel: discord.TextChannel,
                           min_picks: int, max_picks: int, timer: int,
                           mode: app_commands.Choice[str],
                           players: str = None,
                           players_role: discord.Role = None,
                           budget: int = None, tera_budget: int = None,
                           max_trades: int = 0, template_link: str = None,
                           tiering_link: str = None,
                           draft_channel_b: discord.TextChannel = None,
                           players_b: str = None,
                           players_role_b: discord.Role = None,
                           weeks: int = None,
                           visibility: app_commands.Choice[str] = None,
                           website: bool = True):
        if interaction.guild is None:
            raise rules.DraftError("❌ Leagues can only be created inside a server.")
        ui.require_moderator(self.bot, interaction)

        await interaction.response.defer(ephemeral=True)

        has_pool_b_players = bool(players_b) or players_role_b is not None
        if not rules.second_pool_is_consistent(draft_channel_b is not None,
                                               has_pool_b_players):
            raise rules.DraftError(
                "❌ A second pool needs both a channel and its players — give "
                "`draft_channel_b` plus either `players_b` or `players_role_b`. "
                "Leave all three out for a single-pool draft."
            )

        roster_a, shuffled_a = await ui.roster_from_options(
            interaction.guild, players, players_role, "A")
        rosters = {"A": roster_a}
        shuffled = {"A": shuffled_a}

        if draft_channel_b is not None:
            roster_b, shuffled_b = await ui.roster_from_options(
                interaction.guild, players_b, players_role_b, "B")
            rosters["B"] = roster_b
            shuffled["B"] = shuffled_b

        channels = {"A": draft_channel.id}
        if draft_channel_b is not None:
            channels["B"] = draft_channel_b.id

        league, pool_rows, notes, coach_slots = await self.build_league(
            guild_id=interaction.guild_id, admin_channel_id=interaction.channel_id, name=name,
            rosters=rosters, channels=channels, min_picks=min_picks, max_picks=max_picks,
            timer=timer, mode=mode.value, budget=budget, tera_budget=tera_budget,
            max_trades=max_trades, template_link=template_link, tiering_link=tiering_link,
            weeks=weeks, visibility=visibility.value if visibility else "public", website=website,
            created_by=interaction.user.id,
        )
        await self.announce_setup(league, pool_rows, coach_slots, shuffled, interaction.channel)

        extra = ("\n" + "\n".join(notes)) if notes else ""
        await interaction.followup.send(
            f"✅ **{league['name']}** created with "
            f"{len(pool_rows)} pool{'s' if len(pool_rows) > 1 else ''}.{extra}",
            ephemeral=True,
        )

    async def build_league(self, *, guild_id, admin_channel_id, name, rosters, channels,
                           min_picks, max_picks, timer, mode, budget=None, tera_budget=None,
                           max_trades=0, template_link=None, tiering_link=None, weeks=None,
                           visibility="public", website=True, site_only=False, created_by=None):
        """The heart of /setup, shared with draft.ogreclan.com (webapi.py).

        Reads the document (a template's budgets and Coaches tab), creates the
        league and its pools, generates a schedule when there's no template,
        and writes the sheet. `rosters` is {"A": [player entries], "B": ...} in
        draft order and `channels` is {"A": channel_id, ...}. Returns
        (league, pool_rows, notes, coach_slots).
        """
        if template_link and tiering_link:
            raise rules.DraftError(
                "❌ Link a template **or** a tiering sheet, not both. The template already "
                "contains its own tiering tab."
            )
        doc_kind = "template" if template_link else ("tiering" if tiering_link else "none")
        doc_url = template_link or tiering_link

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

        # With a template, the Coaches tab decides which roster blocks exist
        # and who owns them. Only ticked "In schedule" rows take part, and a
        # row already naming somebody belongs to them - so players are
        # matched to their row rather than filled in mention order.
        coach_slots = None
        write_rows = set()
        if doc_kind == "template":
            slots, sheet_names = await asyncio.to_thread(
                self.bot.sheets.read_coach_slots, doc_url)
            coach_slots = {}

            for label, roster in list(rosters.items()):
                available = slots.get(label, [])
                if not available:
                    raise rules.DraftError(
                        f"❌ No coach on the template is marked as pool {label} with "
                        f"its **In schedule** box ticked, so there is nowhere to put "
                        f"those players."
                    )
                if len(roster) > len(available):
                    raise rules.DraftError(
                        f"❌ Pool {label} has {len(roster)} players but only "
                        f"{len(available)} ticked coach rows on the template "
                        f"(rows {', '.join(str(n) for n in available)}). Tick more "
                        f"**In schedule** boxes, or list fewer players."
                    )

                assignment, to_write, unclaimed, leftover = rules.match_coaches(
                    roster, available, sheet_names)
                if leftover:
                    raise rules.DraftError(
                        f"❌ Could not place these pool {label} players: "
                        f"{', '.join(p['display_name'] for p in leftover)}. "
                        f"Every ticked row is already taken by somebody named on the "
                        f"sheet."
                    )

                # Adopt the spreadsheet's spelling for anybody it already
                # named. The bot then writes that same value back, so the
                # Coaches tab is left exactly as the host typed it - and the
                # names the bot shows match the ones on the sheet, which is
                # what lets /postschedule tie fixtures back to accounts.
                for number, entry in assignment.items():
                    if number not in to_write:
                        entry["display_name"] = sheet_names[number]

                # Re-order the roster so seat order follows the sheet's rows.
                ordered = [assignment[number] for number in sorted(assignment)]
                rosters[label] = ordered
                coach_slots[label] = sorted(assignment)
                write_rows.update(to_write)

                matched = len(ordered) - len(to_write)
                if matched:
                    notes.append(f"Pool {label}: matched {matched} player(s) to their "
                                 f"row on the Coaches tab")
                if to_write:
                    notes.append(f"Pool {label}: filled {len(to_write)} unnamed row(s)")
                if unclaimed:
                    named = ", ".join(f"{sheet_names[n]} (row {n})" for n in unclaimed)
                    notes.append(f"⚠️ Pool {label}: ticked but nobody listed them — {named}")

        pools = [(label, channels[label], rosters[label]) for label in sorted(rosters)]

        league_id = rules.create_league(
            self.bot.conn,
            guild_id=guild_id,
            admin_channel_id=admin_channel_id,
            name=name,
            pools=pools,
            budget=budget,
            min_picks=min_picks,
            max_picks=max_picks,
            base_timer=timer,
            mode=mode,
            doc_kind=doc_kind,
            doc_url=doc_url,
            tera_budget=tera_budget,
            max_trades=max_trades,
            season_weeks=weeks or 0,
            coach_slots=coach_slots,
            visibility=visibility,
            website=website,
            site_only=site_only,
            created_by=created_by,
        )
        league = rules.get_league(self.bot.conn, league_id)

        # Without a template there is no schedule to read, so the bot makes
        # its own for any mode that posts one.
        generated_weeks = 0
        if league["mode"] in config.SCHEDULE_MODES and doc_kind != "template":
            generated_weeks = rules.generate_fixtures(
                self.bot.conn, league_id, weeks=weeks)
            notes.append(f"Generated a {generated_weeks}-week schedule "
                         f"(`/postschedule` to post a week)")

        # Push the coach names and pool letters straight to the template;
        # every other tab in it reads the names from there.
        pool_rows = rules.get_league_pools(self.bot.conn, league_id)
        ui.resync_sheet(self.bot, pool_rows[0]["id"])

        return league, pool_rows, notes, coach_slots

    async def announce_setup(self, league, pool_rows, coach_slots, shuffled, admin_channel):
        """/setup's summary in the admin channel, and a note in each pool's channel"""
        document = {"template": "full template", "tiering": "tiering sheet",
                    "none": "none"}[league["doc_kind"]]
        if league["doc_url"] and not self.bot.sheets.enabled:
            document += f" (inactive: {self.bot.sheets.reason})"
        tera_budget = league["tera_budget"]
        summary = (
            f"📋 **{league['name']}** has been set up.\n"
            f"• Mode: **{'full season' if league['mode'] == 'full_season' else 'draft only'}**\n"
            f"• Budget: **{league['budget']}** points"
            + (f" · Tera budget **{tera_budget}** (max {config.MAX_TERA_CAPTAINS} captains)\n"
               if tera_budget else "\n")
            + f"• Picks: **{league['min_picks']}–{league['max_picks']}** per player "
            f"({league['max_picks']} rounds)\n"
            f"• Base timer: **{ui.format_seconds(league['base_timer'])}**\n"
            + (f"• Trade pickups per player: **{league['max_trades'] or 'unlimited'}** Pokémon\n"
               if league["mode"] == "full_season" else "")
            + f"• Document: {document}\n"
            f"• On draft.ogreclan.com: **{league['visibility']}**"
            + (", website compatible" if league["website"] else ", not using the website") + "\n"
            + (f"• Trade approvals: {admin_channel.mention}\n" if admin_channel is not None else "")
        )
        for pool in pool_rows:
            seats = rules.get_players(self.bot.conn, pool["id"])
            order = "\n".join(f"{p['draft_position']}. <@{p['user_id']}>" for p in seats)
            if coach_slots:
                how = " *(seat order follows the Coaches tab)*"
            elif shuffled.get(pool["label"]):
                how = " *(order randomised by the bot)*"
            else:
                how = " *(order as listed)*"
            summary += (f"\n**Pool {pool['label']}** — <#{pool['channel_id']}> "
                        f"({len(seats)} players){how}\n{order}\n")

        await ui.safe_send(admin_channel, summary)
        for pool in pool_rows:
            await ui.safe_send(
                self.bot.get_channel(pool["channel_id"]),
                f"📋 **{league['name']} — Pool {pool['label']}** will be drafted here.\n"
                f"Settings can still be changed with `/edit`. A moderator starts the "
                f"draft with `/draft`."
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
        max_trades="New limit on Pokémon each player may pick up in trades (0 = unlimited)",
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
            f"• Trade pickups per player: **{updated['max_trades'] or 'unlimited'}** Pokémon\n"
            f"• Document: {updated['doc_url'] or 'none'}",
            ephemeral=True,
        )

    # ---- /draft ---------------------------------------------------------

    @app_commands.command(
        name="draft",
        description="Start this pool's draft, now or at a scheduled time.",
    )
    @app_commands.describe(
        at="Optional: a Discord timestamp like <t:1760000000:F> to start automatically",
    )
    async def start_draft(self, interaction: discord.Interaction, at: str = None):
        pool = rules.require_live_pool(self.bot.conn, interaction.channel_id)
        ui.require_moderator(self.bot, interaction)

        if at:
            when = rules.parse_start_time(at)
            rules.schedule_pool_start(self.bot.conn, pool, when)

            stamp = int(when)
            await interaction.response.send_message(
                f"🗓️ **{pool['league_name']} — Pool {pool['label']}** will start "
                f"automatically at <t:{stamp}:F> (<t:{stamp}:R>).\n"
                f"Run `/draft` with no time to start it sooner.",
                ephemeral=True,
            )
            await ui.safe_send(
                interaction.channel,
                f"🗓️ **{pool['league_name']} — Pool {pool['label']}** starts "
                f"<t:{stamp}:R>, at <t:{stamp}:F>.\n"
                f"`/queuepick round:1 pokemon:...` now and the bot will make your "
                f"first pick for you if you cannot be here.",
            )
            return

        events = rules.start_pool(self.bot.conn, pool)
        posted = await self.announce_start(interaction.channel, pool)
        # announce() starts the first timer whether or not it can post, so
        # the draft is never left active with nobody on the clock.
        await ui.announce(self.bot, pool["id"], events, channel=interaction.channel)

        note = "" if posted else ui.permission_hint(interaction.channel)
        await interaction.response.send_message(
            f"✅ Pool {pool['label']} of **{pool['league_name']}** has started.{note}",
            ephemeral=True)

    async def announce_start(self, channel, pool):
        """The public "we have begun" message. Shared with the scheduler."""
        tera = ""
        if pool["tera_budget"]:
            tera = (f" · **{pool['tera_budget']}** Tera points, up to "
                    f"{config.MAX_TERA_CAPTAINS} Tera Captains")
        return await ui.safe_send(
            channel,
            f"🚨 **{pool['league_name']} — Pool {pool['label']} HAS BEGUN!** 🚨\n"
            f"{rules.player_count(self.bot.conn, pool['id'])} players · "
            f"{pool['max_picks']} rounds · **{pool['budget']}** points each{tera}.\n"
            f"`/edit` is now locked. `/pick` and `/queuepick` are open.",
        )

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
        league = rules.require_season_league_by_name(self.bot.conn, interaction.guild_id, name)

        for pool in rules.get_league_pools(self.bot.conn, league["id"]):
            self.bot.timers.cancel(pool["id"])
        rules.archive_league(self.bot.conn, league["id"])

        await interaction.response.send_message(
            f"🗃️ **{league['name']}** is archived. Rosters and history stay readable, and "
            f"its channels are free for a new `/setup`.",
            ephemeral=True,
        )

    @app_commands.command(
        name="deleteleague",
        description="Moderator only: delete a league and everything in it (a backup is kept).",
    )
    @app_commands.describe(name="The league's name", confirm="Type the league's name again to confirm")
    async def deleteleague(self, interaction: discord.Interaction, name: str, confirm: str):
        ui.require_moderator(self.bot, interaction)
        league = rules.require_any_league_by_name(self.bot.conn, interaction.guild_id, name)
        if " ".join(confirm.split()).lower() != league["name"].lower():
            raise rules.DraftError("❌ Type the league's name in the confirm box to delete it.")
        pools = rules.get_league_pools(self.bot.conn, league["id"])
        path = database.backup(self.bot.conn)
        for pool in pools:
            self.bot.timers.cancel(pool["id"])
        rules.delete_league(self.bot.conn, league["id"])
        log.info("league %s (%s) deleted by %s; backup %s", league["id"], league["name"], interaction.user.id, path)
        await interaction.response.send_message(
            f"🗑️ **{league['name']}** is deleted, with everything in it. A backup was taken first "
            f"(`{os.path.basename(path)}`), and its channels are free for a new `/setup`.",
            ephemeral=True,
        )
        for pool in pools:
            await ui.safe_send(self.bot.get_channel(pool["channel_id"]),
                               f"🗑️ **{league['name']}** has been deleted by a moderator.")

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

    # ---- /website -------------------------------------------------------

    @app_commands.command(
        name="website",
        description="Moderator only: whether a league uses draft.ogreclan.com.",
    )
    @app_commands.describe(name="The league's name",
                           on="On: picks on the site, Ready battles on play.ogreclan.com, kills and deaths recorded")
    async def website(self, interaction: discord.Interaction, name: str, on: bool):
        ui.require_moderator(self.bot, interaction)
        league = rules.require_any_league_by_name(self.bot.conn, interaction.guild_id, name)
        league = rules.set_website(self.bot.conn, league["id"], on)
        await interaction.response.send_message(
            f"✅ **{league['name']}** " + (
                "uses draft.ogreclan.com: picks and queues there, matches with Ready on play.ogreclan.com, "
                "and kills and deaths recorded from those battles." if league["website"] else
                "doesn't use draft.ogreclan.com for picks or matches: players report kills and deaths "
                "themselves (or give the bot replay links)."),
            ephemeral=True,
        )

    # ---- /visibility ----------------------------------------------------

    @app_commands.command(
        name="visibility",
        description="Moderator only: who can see a league on draft.ogreclan.com.",
    )
    @app_commands.describe(name="The league's name", setting="Public or private")
    @app_commands.choices(setting=VISIBILITY)
    async def visibility(self, interaction: discord.Interaction, name: str,
                         setting: app_commands.Choice[str]):
        ui.require_moderator(self.bot, interaction)
        league = rules.require_any_league_by_name(self.bot.conn, interaction.guild_id, name)
        league = rules.set_visibility(self.bot.conn, league["id"], setting.value)
        who = ("everyone" if league["visibility"] == "public" else
               "only its players, signed in with their Discord account")
        await interaction.response.send_message(
            f"✅ **{league['name']}** is **{league['visibility']}** on draft.ogreclan.com: {who} can see it.",
            ephemeral=True,
        )

    # ---- /postschedule --------------------------------------------------

    @app_commands.command(
        name="postschedule",
        description="Moderator only: post a week's matchups, pinging both players.",
    )
    @app_commands.describe(week="Which week to post", name="The league's name")
    async def postschedule(self, interaction: discord.Interaction, week: int, name: str):
        ui.require_moderator(self.bot, interaction)
        league = rules.require_season_league_by_name(self.bot.conn, interaction.guild_id, name)
        await interaction.response.defer(ephemeral=True)
        result = await self.post_week(league, week)
        if result is None:
            await interaction.followup.send(f"No matches found for week {week}.", ephemeral=True)
            return
        for message in ui.pack_messages(result["lines"]):
            await ui.safe_send(interaction.channel, message)
        await interaction.followup.send(result["report"], ephemeral=True)

    async def post_week(self, league, week):
        """/postschedule's work, shared with draft.ogreclan.com (webapi.py): reads the week's
        matches (the template's, or the bot's own schedule), puts approved trades into
        effect, records the week, and rewrites the sheet. Returns the lines to post and a
        report for the moderator, or None when the week has no matches."""
        if league["mode"] not in config.SCHEDULE_MODES:
            raise rules.DraftError(
                f"❌ **{league['name']}** is a draft-only league, so it has no schedule. "
                f"Use the `draft_schedule` or `full_season` mode for that."
            )
        if week < 1:
            raise rules.DraftError("❌ Weeks start at 1.")

        if league["doc_kind"] == "template":
            # The template owns the fixtures, so read them from the sheet.
            if not self.bot.sheets.enabled:
                raise rules.DraftError(
                    f"❌ Google Sheets is not available: {self.bot.sheets.reason}")
            sheet_matches = await asyncio.to_thread(
                self.bot.sheets.read_schedule, league["doc_url"], week)

            mentions = {}
            seats = {}
            for pool in rules.get_league_pools(self.bot.conn, league["id"]):
                for player in rules.get_players(self.bot.conn, pool["id"]):
                    mentions[rules.normalize(player["display_name"])] = player["user_id"]
                    seats[rules.normalize(player["display_name"])] = player["id"]

            unknown = []
            pairs = [(_who(home, mentions, unknown), _who(away, mentions, unknown))
                     for home, away in sheet_matches]
            # the week's matches, kept for draft.ogreclan.com
            matched = [(seats[rules.normalize(home)], seats[rules.normalize(away)])
                       for home, away in sheet_matches
                       if rules.normalize(home) in seats and rules.normalize(away) in seats]
            footnote = (f"*Not matched to a Discord account: "
                        f"{', '.join(sorted(set(unknown)))}. Run `/sync` so the sheet's "
                        f"coach names match the bot's.*" if unknown else "")
        else:
            # The bot generated the schedule itself.
            fixtures = rules.get_fixtures(self.bot.conn, league["id"], week)
            pairs = [(f"<@{home['user_id']}>", f"<@{away['user_id']}>")
                     for home, away in fixtures]
            available = rules.fixture_weeks(self.bot.conn, league["id"])
            footnote = (f"*Season runs weeks {available[0]}–{available[-1]}.*"
                        if available else "")

        if not pairs:
            return None

        # Approved trades change rosters from the week that's posted next.
        applied = rules.apply_trades(self.bot.conn, league["id"], week)
        rules.set_current_week(self.bot.conn, league["id"], week)
        if league["doc_kind"] == "template":
            rules.record_week(self.bot.conn, league["id"], week, matched)
        if applied:
            await self.price_trades(applied)

        lines = [f"📅 **{league['name']} — Week {week}**", ""]
        lines += [f"{home} **vs** {away}" for home, away in pairs]
        if applied:
            lines += ["", f"🔁 **Trades in effect from week {week}**"]
            for trade, _ in applied:
                proposer = rules.get_player_by_id(self.bot.conn, trade["proposer_id"])
                partner = (rules.get_player_by_id(self.bot.conn, trade["partner_id"])
                           if trade["partner_id"] else None)
                lines.append(
                    f"• #{trade['trade_number']} **{proposer['display_name']}** gives up "
                    f"{rules.trade_side(trade['drops'])} and receives "
                    f"{rules.trade_side(trade['gets'])}"
                    + (f" (with **{partner['display_name']}**)" if partner else " (free agency)"))
        if footnote:
            lines += ["", footnote]

        for pool_id in {trade["pool_id"] for trade, _ in applied}:
            ui.resync_sheet(self.bot, pool_id)

        report = f"✅ Posted {len(pairs)} match(es) for week {week}."
        if applied:
            report += f" {len(applied)} approved trade(s) are now in effect."
            notes = [f"#{trade['trade_number']}: {note}" for trade, notes in applied for note in notes]
            if notes:
                report += "\n\nNotes:\n" + "\n".join(f"• {note}" for note in notes)
        return {"lines": lines, "report": report, "matches": len(pairs), "applied": len(applied)}

    async def price_trades(self, applied):
        """A free-agency pickup has no price until the tiering list is consulted."""
        if not self.bot.sheets.enabled:
            return
        for trade, _ in applied:
            pool = rules.get_pool(self.bot.conn, trade["pool_id"])
            if not pool["doc_url"]:
                continue
            for pokemon in json.loads(trade["gets"]):
                try:
                    tab = await tiering_tab_for(self.bot, pool)
                    entry = await asyncio.to_thread(
                        self.bot.sheets.lookup, pool["doc_url"], pokemon, tab)
                except Exception:
                    log.exception("could not read the price of %s after a trade", pokemon)
                    continue
                if entry and entry["cost"] is not None:
                    rules.apply_sheet_cost(self.bot.conn, pool, pokemon, entry["cost"])

    @app_commands.command(
        name="reschedule",
        description="Moderator only: regenerate the bot's own schedule at random.",
    )
    @app_commands.describe(name="The league's name", weeks="Season length, or leave blank")
    async def reschedule(self, interaction: discord.Interaction, name: str,
                         weeks: int = None):
        ui.require_moderator(self.bot, interaction)
        league = rules.require_season_league_by_name(self.bot.conn, interaction.guild_id, name)

        if league["doc_kind"] == "template":
            raise rules.DraftError(
                "❌ This league's schedule lives on its template, so change it there."
            )
        if league["mode"] not in config.SCHEDULE_MODES:
            raise rules.DraftError(
                f"❌ **{league['name']}** does not use a schedule."
            )

        made = rules.generate_fixtures(self.bot.conn, league["id"], weeks=weeks)
        await interaction.response.send_message(
            f"🎲 Generated a fresh {made}-week schedule for **{league['name']}**. "
            f"Post a week with `/postschedule`.",
            ephemeral=True,
        )

    # ---- /setskips ------------------------------------------------------

    @app_commands.command(
        name="setskips",
        description="Moderator only: correct a player's skip count and timer.",
    )
    @app_commands.describe(
        player="The player to adjust",
        skips="Their new skip count. 0 restores their full base timer.",
        timer="Optional: set their timer to exactly this many seconds instead",
    )
    async def setskips(self, interaction: discord.Interaction, player: discord.Member,
                       skips: int, timer: int = None):
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        ui.require_moderator(self.bot, interaction)

        result = rules.set_skip_count(self.bot.conn, pool, player.id, skips, timer)
        before = result["before"]

        how = ("recalculated from the new skip count"
               if result["recalculated"] else "set explicitly")
        await interaction.response.send_message(
            f"✅ **{result['display_name']}**: skips "
            f"{before['skips']} → **{result['skips']}**, timer "
            f"{ui.format_seconds(before['timer'])} → "
            f"**{ui.format_seconds(result['timer'])}** *({how})*.\n"
            f"It takes effect on their next turn — a clock already running is "
            f"not cut short.",
            ephemeral=True,
        )
        await ui.safe_send(
            interaction.channel,
            f"⏱️ A moderator adjusted <@{result['user_id']}>: skip count "
            f"**{result['skips']}**, timer **{ui.format_seconds(result['timer'])}**.",
        )

    # ---- /skipturn ------------------------------------------------------

    @app_commands.command(
        name="skipturn",
        description="Moderator only: skip whoever is on the clock, as a timer running out would.",
    )
    @app_commands.describe(
        player="Safety check: refuses unless this is the person actually on the clock",
        penalty="Set to False to skip them without reducing their timer",
    )
    async def skipturn(self, interaction: discord.Interaction,
                       player: discord.Member = None, penalty: bool = True):
        pool = rules.require_active_pool(self.bot.conn, interaction.channel_id)
        ui.require_moderator(self.bot, interaction)
        await interaction.response.defer(ephemeral=True)

        on_clock = rules.current_player(self.bot.conn, pool)
        if player is not None and on_clock["user_id"] != player.id:
            # Turns can move on while a moderator is typing, and skipping the
            # wrong person costs them a round. Naming them makes it safe.
            raise rules.DraftError(
                f"❌ **{on_clock['display_name']}** is on the clock, not "
                f"**{player.display_name}** — the turn has moved on since you "
                f"looked. Nothing was skipped."
            )

        round_number = pool["current_round"]
        events = rules.skip_current_player(
            self.bot.conn, pool["id"], reason=rules.MODERATOR_SKIP,
            halve=None if penalty else False,
        )

        timer_note = ("Their timer was reduced, exactly as the clock running out "
                      "would have done — `/setskips` puts it back."
                      if penalty else "Their timer was left alone.")
        await interaction.followup.send(
            f"⏭️ Skipped **{on_clock['display_name']}** in Round {round_number}. "
            f"They owe a Round {round_number} makeup pick, due before their next "
            f"turn.\n{timer_note}",
            ephemeral=True,
        )
        await ui.announce(self.bot, pool["id"], events)

    # ---- /reopendrafting ------------------------------------------------

    @app_commands.command(
        name="reopendrafting",
        description="Moderator only: put a player who finished early back on the clock.",
    )
    @app_commands.describe(player="The player to put back into the draft")
    async def reopendrafting(self, interaction: discord.Interaction,
                             player: discord.Member):
        pool = rules.require_active_pool(self.bot.conn, interaction.channel_id)
        ui.require_moderator(self.bot, interaction)
        await interaction.response.defer(ephemeral=True)

        events = rules.reopen_drafting(self.bot.conn, pool, player.id)

        await interaction.followup.send(
            f"↩️ **{player.display_name}** is drafting again. The rounds that "
            f"went by while they were finished are gone — they were passed "
            f"over, so no makeup pick is owed and no timer was reduced.",
            ephemeral=True,
        )
        await ui.announce(self.bot, pool["id"], events)

    # ---- /removeplayer --------------------------------------------------

    @app_commands.command(
        name="removeplayer",
        description="Moderator only: take a player out of this pool's draft.",
    )
    @app_commands.describe(
        player="The player to remove",
        keep_roster="Keep their Pokémon out of the pool instead of releasing them",
    )
    async def removeplayer(self, interaction: discord.Interaction,
                           player: discord.Member, keep_roster: bool = False):
        pool = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        ui.require_moderator(self.bot, interaction)

        await interaction.response.defer(ephemeral=True)
        result = rules.remove_player(self.bot.conn, pool, player.id,
                                     release_roster=not keep_roster)

        if result["seat_deleted"]:
            detail = (f"The draft had not started, so their seat is gone and the "
                      f"order closes up: **{result['players_left']}** players left.")
        else:
            detail = ("The draft is under way, so their seat stays in the order and "
                      "is passed over from now on — removing it would have moved "
                      "everybody else's turn.")

        released = ""
        if result["released"]:
            released = (f"\n• Back in the pool: {', '.join(result['released'])}")
        elif keep_roster:
            released = "\n• Their Pokémon stay out of the pool."

        await ui.safe_send(
            interaction.channel,
            f"🚪 **{result['display_name']}** has been removed from Pool "
            f"{result['pool']} by a moderator.\n• {detail}{released}",
        )
        if pool["doc_kind"] == "template":
            ui.resync_sheet(self.bot, pool["id"])

        await interaction.followup.send(
            f"✅ {player.mention} removed from Pool {result['pool']}.", ephemeral=True)

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
