"""
The commands players use during a draft:
/pick, /queuepick, /makeuppick, /editpick, /status, /progress, /roster.

Every command finds its draft from `interaction.channel_id`, which is what
keeps parallel drafts in different channels completely separate.

Informational replies (/status, /progress, /roster) and all error messages
are ephemeral - only the person who ran the command sees them. Real draft
events (picks, skips, makeup deadlines) are announced publicly by ui.announce.
"""

import asyncio
import logging
import time

import discord
from discord import app_commands
from discord.ext import commands

import config
import draft as rules
import ui

log = logging.getLogger("draftbot.drafting")


async def tiering_tab_for(bot, draft):
    """Which tab of the document holds the price list, if any.

    A template names its tiering tab on its Settings page; a standalone
    tiering document is just its first sheet. The answer is cached per
    league, because it never changes mid-draft.
    """
    if draft["doc_kind"] != "template":
        return None

    cached = bot.tiering_tabs.get(draft["league_id"])
    if cached is None:
        cached = await asyncio.to_thread(bot.sheets.tiering_tab, draft["doc_url"])
        bot.tiering_tabs[draft["league_id"]] = cached
    return cached


async def pokemon_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest Pokémon from the linked price list as the user types.

    Only offers Pokémon that are still undrafted and draftable. Falls back
    to an empty list whenever there is no document, so drafts without one
    just get a plain text box. Must never raise: Discord shows nothing at
    all if an autocomplete callback fails.
    """
    try:
        bot = interaction.client
        draft = rules.get_channel_pool(bot.conn, interaction.channel_id)
        if draft is None or not bot.sheets.enabled or not draft["doc_url"]:
            return []

        tab = await tiering_tab_for(bot, draft)
        taken = rules.drafted_keys(bot.conn, draft["id"])
        matches = await asyncio.to_thread(
            bot.sheets.suggest, draft["doc_url"], current, taken, 25, tab
        )
        return [app_commands.Choice(
            name=f"{entry['name']} — {entry['cost']} pts"
                 + (f" / Tera {entry['tera_cost']}" if entry["tera_cost"] is not None else ""),
            value=entry["name"]) for entry in matches]
    except Exception:
        log.exception("Pokémon autocomplete failed")
        return []


class DraftingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---- Google Sheets helpers -----------------------------------------

    async def sheet_entry(self, draft, pokemon):
        """Look a Pokémon up in the league's price list.

        Returns one of:
          ("unavailable", None) - no document, or Google is not responding.
                                  The draft carries on with no cost check.
          ("missing", None)     - the list works but has no such Pokémon.
          ("ok", entry)         - found.
        """
        if not self.bot.sheets.enabled or not draft["doc_url"]:
            return "unavailable", None
        try:
            tab = await tiering_tab_for(self.bot, draft)
            entry = await asyncio.to_thread(
                self.bot.sheets.lookup, draft["doc_url"], pokemon, tab)
        except Exception:
            log.exception("price lookup failed; continuing without a cost check")
            return "unavailable", None
        return ("ok", entry) if entry else ("missing", None)

    async def resolve_pokemon(self, draft, pokemon, cost, tera_captain=False):
        """Check a Pokémon against the price list and return its proper name.

        Returning the list's own spelling matters: the roster block feeds
        VLOOKUPs for the image and the cost, and the coach's budget is worked
        out from those. A name that does not match exactly would leave the
        sheet unable to price the pick.

        A Tera Captain is checked against the Tera price, since that is what
        it costs from both budgets.

        When there is no document, or Google is not answering, the player's
        own spelling is used and no cost check happens - the draft carries on.
        """
        if tera_captain and not draft["tera_budget"]:
            raise rules.DraftError(
                "❌ This draft is not a Tera format, so there are no Tera Captains."
            )

        status, entry = await self.sheet_entry(draft, pokemon)

        if status == "missing":
            raise rules.DraftError(
                f"❌ **{pokemon}** is not in this league's tiering list. "
                f"Check the spelling, or use `/search`."
            )
        if status == "ok":
            if entry["banned"]:
                raise rules.DraftError(
                    f"❌ **{entry['name']}** is banned in this league — it has no price "
                    f"in the tiering list."
                )
            if tera_captain:
                if entry["tera_banned"]:
                    raise rules.DraftError(
                        f"❌ **{entry['name']}** is Tera banned — it has no Tera price, so "
                        f"it cannot be a Tera Captain."
                    )
                if entry["tera_cost"] != cost:
                    raise rules.DraftError(
                        f"❌ Incorrect cost. As a Tera Captain **{entry['name']}** costs "
                        f"**{entry['tera_cost']}** points, not {cost}."
                    )
            elif entry["cost"] != cost:
                raise rules.DraftError(
                    f"❌ Incorrect cost. The tiering list prices **{entry['name']}** at "
                    f"**{entry['cost']}** points, not {cost}."
                )
            return entry["name"]

        return str(pokemon).strip()

    # ---- /pick ----------------------------------------------------------

    @app_commands.command(name="pick", description="Draft a Pokémon on your turn.")
    @app_commands.describe(
        round="The round you are picking in",
        pokemon="The Pokémon you want",
        cost="Its draft cost in points",
    )
    @app_commands.autocomplete(pokemon=pokemon_autocomplete)
    async def pick(self, interaction: discord.Interaction, round: int, pokemon: str, cost: int):
        draft = rules.require_active_pool(self.bot.conn, interaction.channel_id)
        await interaction.response.defer(ephemeral=True)

        is_admin = ui.has_admin_rights(interaction, draft)
        pokemon = await self.resolve_pokemon(draft, pokemon, cost)

        pick, events = rules.make_pick(self.bot.conn, draft, interaction.user.id,
                                       round, pokemon, cost, acting_as_admin=is_admin)

        await interaction.followup.send(
            f"✅ **{pick['pokemon']}** drafted for **{cost}** points.", ephemeral=True
        )
        await ui.announce(self.bot, draft["id"], events, channel=interaction.channel)

    # ---- /queuepick -----------------------------------------------------

    @app_commands.command(
        name="queuepick",
        description="Queue up to 5 Pokémon in priority order for one of your upcoming turns.",
    )
    @app_commands.describe(
        round="The round the queue is for",
        pokemon="Up to 5 Pokémon separated by commas, best first",
        costs="Their costs, separated by commas. Optional if a document is linked.",
        tera="Tera formats only: yes/no for each Pokémon, e.g. `no, yes, no`",
    )
    async def queuepick(self, interaction: discord.Interaction, round: int,
                        pokemon: str, costs: str = None, tera: str = None):
        draft = rules.require_active_pool(self.bot.conn, interaction.channel_id)
        await interaction.response.defer(ephemeral=True)

        names = rules.parse_pokemon_list(pokemon, config.MAX_QUEUE_LENGTH)

        # Costs come from the command if given, otherwise from the price list.
        given = []
        if costs:
            for part in costs.split(","):
                part = part.strip()
                if not part.isdigit():
                    raise rules.DraftError(f"❌ `{part}` is not a whole number of points.")
                given.append(int(part))
            if len(given) != len(names):
                raise rules.DraftError(
                    f"❌ You listed {len(names)} Pokémon but {len(given)} costs."
                )

        captains = [False] * len(names)
        if tera:
            if not draft["tera_budget"]:
                raise rules.DraftError("❌ This draft is not a Tera format.")
            flags = [part.strip().lower() for part in tera.split(",")]
            if len(flags) != len(names):
                raise rules.DraftError(
                    f"❌ You listed {len(names)} Pokémon but {len(flags)} Tera answers."
                )
            for index, flag in enumerate(flags):
                if flag in ("yes", "y", "true", "1"):
                    captains[index] = True
                elif flag not in ("no", "n", "false", "0", ""):
                    raise rules.DraftError(f"❌ `{flag}` is not yes or no.")

        resolved = []
        unknown = []
        banned = []
        for index, name in enumerate(names):
            status, entry = await self.sheet_entry(draft, name)
            if status == "missing":
                raise rules.DraftError(
                    f"❌ **{name}** is not in this league's tiering list."
                )
            if status == "ok":
                if entry["banned"]:
                    banned.append(f"{entry['name']} (banned)")
                    continue
                if captains[index] and entry["tera_banned"]:
                    banned.append(f"{entry['name']} (Tera banned)")
                    continue
                # Store the list's spelling so an automatic pick writes a
                # name the spreadsheet can actually look up.
                names[index] = entry["name"]
                official = entry["tera_cost"] if captains[index] else entry["cost"]
                resolved.append(given[index] if given else official)
            elif given:
                resolved.append(given[index])
            else:
                resolved.append(None)
                unknown.append(name)

        if banned:
            raise rules.DraftError(f"❌ Not draftable: {', '.join(banned)}.")
        if unknown:
            raise rules.DraftError(
                f"❌ No cost known for: {', '.join(unknown)}. "
                f"Add them with `costs: 12, 10, 8`."
            )

        rules.queue_picks(self.bot.conn, draft, interaction.user.id, round, names,
                          resolved, captains)

        listing = "\n".join(
            f"{i}. **{n}** — {c} points" + (" ⭐ Tera Captain" if t else "")
            for i, (n, c, t) in enumerate(zip(names, resolved, captains), 1))
        await interaction.followup.send(
            f"📝 Queue saved for **Round {round}**:\n{listing}\n\n"
            f"When your turn arrives the bot drafts the first one that is still "
            f"available and affordable.",
            ephemeral=True,
        )

    # ---- /makeuppick ----------------------------------------------------

    @app_commands.command(
        name="makeuppick",
        description="Make a pick for a round you were skipped in.",
    )
    @app_commands.describe(
        round="The round you were skipped in",
        pokemon="The Pokémon you want",
        cost="Its draft cost in points (the Tera price if it is a Tera Captain)",
        tera_captain="Tera formats only: make this one of your Tera Captains",
    )
    @app_commands.autocomplete(pokemon=pokemon_autocomplete)
    async def makeuppick(self, interaction: discord.Interaction, round: int,
                         pokemon: str, cost: int, tera_captain: bool = False):
        draft = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        await interaction.response.defer(ephemeral=True)

        pokemon = await self.resolve_pokemon(draft, pokemon, cost, tera_captain)
        pick = rules.make_makeup_pick(self.bot.conn, draft, interaction.user.id,
                                      round, pokemon, cost, tera_captain=tera_captain)

        await interaction.followup.send(
            f"✅ Round {round} makeup pick recorded: **{pick['pokemon']}** for {cost} points.",
            ephemeral=True,
        )
        # A makeup pick never changes whose turn it is, so it is announced on
        # its own rather than going through the turn machinery.
        player = rules.get_player(self.bot.conn, draft["id"], interaction.user.id)
        await ui.safe_send(
            interaction.channel,
            f"✅ **{player['display_name']}** made their **Round {round} makeup pick**: "
            f"**{pick['pokemon']}** for **{cost}** points. "
            f"*(The current turn is unaffected.)*",
        )
        ui.sync_pick_to_sheet(self.bot, draft["id"], {
            "pick_order": pick["pick_order"], "round": pick["round"],
            "display_name": player["display_name"], "pokemon": pick["pokemon"],
            "cost": pick["cost"], "coach_number": player["coach_number"],
            "tera_captain": bool(pick["tera_captain"]),
        })

    # ---- /editpick ------------------------------------------------------

    @app_commands.command(name="editpick", description="Change a pick that has already been made.")
    @app_commands.describe(
        round="The round of the pick to change",
        pokemon="The Pokémon it should be instead",
        cost="Its draft cost in points",
        player="Admins only: whose pick to edit",
    )
    @app_commands.autocomplete(pokemon=pokemon_autocomplete)
    async def editpick(self, interaction: discord.Interaction, round: int, pokemon: str,
                       cost: int, player: discord.Member = None):
        draft = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        await interaction.response.defer(ephemeral=True)

        is_admin = ui.has_admin_rights(interaction, draft)
        pokemon = await self.resolve_pokemon(draft, pokemon, cost)

        result = rules.edit_pick(
            self.bot.conn, draft, interaction.user.id, round, pokemon, cost,
            target_user_id=player.id if player else None, acting_as_admin=is_admin,
        )

        await interaction.followup.send(
            "✅ Pick added." if result["created"] else "✅ Pick updated.", ephemeral=True
        )
        captain = " ⭐ Tera Captain" if result["tera_captain"] else ""
        if result["created"]:
            announcement = (
                f"✏️ A moderator recorded a Round {result['round']} pick for "
                f"**{result['player']['display_name']}**: "
                f"**{result['new_pokemon']} ({result['new_cost']})**{captain}."
            )
        else:
            announcement = (
                f"✏️ **{result['player']['display_name']}**'s Round {result['round']} pick was "
                f"changed: ~~{result['old_pokemon']} ({result['old_cost']})~~ → "
                f"**{result['new_pokemon']} ({result['new_cost']})**{captain}."
            )
        await ui.safe_send(
            interaction.channel,
            f"{announcement}\nBudget remaining: **{result['player']['budget_remaining']}**.",
        )
        # An edit can free up the old Pokémon, so push the whole picture.
        ui.resync_sheet(self.bot, draft["id"])

    # ---- /status --------------------------------------------------------

    @app_commands.command(name="status", description="Show the current state of this draft.")
    async def status(self, interaction: discord.Interaction):
        draft = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        view = rules.status_view(self.bot.conn, draft)

        lines = [f"**{draft['league_name']} — Pool {draft['label']}** ({draft['status']})"]

        if draft["status"] == "setup":
            lines.append("The draft has not started yet. An admin starts it with `/draft`.")
        else:
            lines.append(f"• Round **{draft['current_round']}** of **{draft['max_picks']}**")

        player = view["current_player"]
        if player is not None:
            remaining = ""
            if draft["turn_deadline"]:
                left = int(draft["turn_deadline"] - time.time())
                remaining = f" (about {ui.format_seconds(max(0, left))} left)"
            lines += [
                f"• On the clock: **{player['display_name']}**{remaining}",
                f"• Their timer: **{ui.format_seconds(player['current_timer'])}** "
                f"(base {ui.format_seconds(draft['base_timer'])}, "
                f"reduced {player['skip_count']}×)",
                f"• Their budget: **{player['budget_remaining']}** of {draft['budget']}",
            ]
            if draft["tera_budget"]:
                lines.append(
                    f"• Their Tera budget: **{player['tera_budget_remaining']}** of "
                    f"{draft['tera_budget']} "
                    f"({rules.tera_captains(self.bot.conn, draft['id'], player['id'])}"
                    f"/{config.MAX_TERA_CAPTAINS} captains)")

        lines.append(f"• Picks completed: **{view['total_picks']}** of {view['expected_picks']}")
        if draft["max_trades"]:
            lines.append(f"• Trades allowed per player: **{draft['max_trades']}**")

        if view["pending_makeups"]:
            owed = ", ".join(f"{name} (Round {r})" for name, r in view["pending_makeups"])
            lines.append(f"• Pending makeup picks: {owed}")
        else:
            lines.append("• Pending makeup picks: none")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---- /progress ------------------------------------------------------

    @app_commands.command(name="progress", description="Show the picks made so far.")
    @app_commands.describe(round="Show only this round. Leave blank for every completed round.")
    async def progress(self, interaction: discord.Interaction, round: int = None):
        draft = rules.require_channel_pool(self.bot.conn, interaction.channel_id)
        await interaction.response.defer(ephemeral=True)

        rounds = [round] if round else rules.completed_rounds(self.bot.conn, draft)
        if not rounds:
            await interaction.followup.send("No picks have been made yet.", ephemeral=True)
            return

        blocks = []
        for number in rounds:
            picks = rules.round_picks(self.bot.conn, draft["id"], number)
            if not picks:
                blocks.append(f"**Round {number}**\nNo picks recorded.")
                continue
            # pick_order keeps the true chronological order, which is what
            # makes a snake draft readable.
            body = "\n".join(
                f"{index}. {pick['display_name']} → {pick['pokemon']} — {pick['cost']} points"
                + ("  *(makeup)*" if pick["is_makeup"] else "")
                for index, pick in enumerate(picks, 1)
            )
            blocks.append(f"**Round {number}**\n{body}")

        for message in ui.pack_messages(blocks):
            await interaction.followup.send(message, ephemeral=True)

    # ---- /roster --------------------------------------------------------

    @app_commands.command(name="roster", description="Show a player's team.")
    @app_commands.describe(player="Whose roster to show. Defaults to you.")
    async def roster(self, interaction: discord.Interaction, player: discord.Member = None):
        draft = rules.require_channel_pool(self.bot.conn, interaction.channel_id)

        target_id = player.id if player else interaction.user.id
        target = rules.get_player(self.bot.conn, draft["id"], target_id)
        if target is None:
            who = player.display_name if player else "You are"
            raise rules.DraftError(f"❌ {who} not a player in this channel's draft.")

        view = rules.roster_view(self.bot.conn, draft, target)
        if view["entries"]:
            body = "\n".join(
                f"{index}. **{name}** — {cost} points" + (" ⭐ **Tera Captain**" if tera else "")
                for index, (name, cost, tera) in enumerate(view["entries"], 1))
        else:
            body = "*No Pokémon drafted yet.*"

        lines = [
            f"**{target['display_name']}'s roster** — {draft['league_name']} "
            f"Pool {draft['label']}",
            body,
            "",
            f"• Spent: **{view['spent']}** points",
            f"• Remaining: **{view['remaining']}** of {draft['budget']}",
            f"• Picks: **{view['count']}** / {view['max_picks']} "
            f"(minimum {view['min_picks']})",
        ]
        if view["tera_format"]:
            lines.append(
                f"• Tera budget: **{view['tera_remaining']}** of {view['tera_budget']} "
                f"({len(view['tera_captains'])}/{view['max_tera']} captains)")
        if draft["max_trades"]:
            lines.append(
                f"• Trades used: **{rules.trades_used(self.bot.conn, draft['id'], target['id'])}**"
                f" / {draft['max_trades']}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)


async def setup(bot):
    await bot.add_cog(DraftingCog(bot))
