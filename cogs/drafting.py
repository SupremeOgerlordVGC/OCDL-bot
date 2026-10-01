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


async def pokemon_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest Pokémon from the linked Pokédex as the user types.

    Only offers Pokémon that are still undrafted and not banned. Falls back
    to an empty list whenever there is no sheet, so drafts without one just
    get a plain text box. Must never raise: Discord shows nothing at all if
    an autocomplete callback fails.
    """
    try:
        bot = interaction.client
        draft = rules.get_channel_draft(bot.conn, interaction.channel_id)
        if draft is None or not bot.sheets.enabled or not draft["sheet_url"]:
            return []

        taken = rules.drafted_keys(bot.conn, draft["id"])
        matches = await asyncio.to_thread(
            bot.sheets.suggest, draft["sheet_url"], current, taken
        )
        return [app_commands.Choice(name=f"{entry['name']} — {entry['cost']} pts",
                                    value=entry["name"])
                for entry in matches]
    except Exception:
        log.exception("Pokémon autocomplete failed")
        return []


class DraftingCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    # ---- Google Sheets helpers -----------------------------------------

    async def sheet_entry(self, draft, pokemon):
        """Look a Pokémon up in the sheet.

        Returns one of:
          ("unavailable", None) - no sheet, or Google is not responding.
                                  The draft carries on with no cost check.
          ("missing", None)     - the sheet works but has no such Pokémon.
          ("ok", entry)         - found.
        """
        if not self.bot.sheets.enabled or not draft["sheet_url"]:
            return "unavailable", None
        try:
            entry = await asyncio.to_thread(self.bot.sheets.lookup, draft["sheet_url"], pokemon)
        except Exception:
            log.exception("sheet lookup failed; continuing without a cost check")
            return "unavailable", None
        return ("ok", entry) if entry else ("missing", None)

    async def resolve_pokemon(self, draft, pokemon, cost):
        """Check a Pokémon against the Pokédex and return its proper name.

        Returning the sheet's own spelling matters: the roster block feeds
        VLOOKUPs for the image and the cost, and the coach's budget is worked
        out from those. A name that does not match the Pokédex exactly would
        leave the sheet unable to price the pick.

        When there is no sheet, or Google is not answering, the player's own
        spelling is used and no cost check happens - the draft carries on.
        """
        status, entry = await self.sheet_entry(draft, pokemon)

        if status == "missing":
            raise rules.DraftError(
                f"❌ **{pokemon}** is not in the Pokédex on the Google Sheet. "
                f"Check the spelling, or use `/search`."
            )
        if status == "ok":
            if entry["banned"]:
                raise rules.DraftError(
                    f"❌ **{entry['name']}** is banned in this league — it has no point "
                    f"value in the Pokédex."
                )
            if entry["cost"] != cost:
                raise rules.DraftError(
                    f"❌ Incorrect cost. The Pokédex lists **{entry['name']}** at "
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
        draft = rules.require_active_draft(self.bot.conn, interaction.channel_id)
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
        costs="Their costs, separated by commas. Optional if a Google Sheet is linked.",
    )
    async def queuepick(self, interaction: discord.Interaction, round: int,
                        pokemon: str, costs: str = None):
        draft = rules.require_active_draft(self.bot.conn, interaction.channel_id)
        await interaction.response.defer(ephemeral=True)

        names = rules.parse_pokemon_list(pokemon, config.MAX_QUEUE_LENGTH)

        # Costs come from the command if given, otherwise from the Pokédex.
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

        resolved = []
        unknown = []
        banned = []
        for index, name in enumerate(names):
            status, entry = await self.sheet_entry(draft, name)
            if status == "ok":
                if entry["banned"]:
                    banned.append(entry["name"])
                    continue
                # Store the Pokédex spelling so an automatic pick writes a
                # name the spreadsheet can actually look up.
                names[index] = entry["name"]
                resolved.append(given[index] if given else entry["cost"])
            elif status == "missing":
                raise rules.DraftError(
                    f"❌ **{name}** is not in the Pokédex on the Google Sheet."
                )
            elif given:
                resolved.append(given[index])
            else:
                resolved.append(None)
                unknown.append(name)

        if banned:
            raise rules.DraftError(
                f"❌ Banned in this league: {', '.join(banned)}."
            )
        if unknown:
            raise rules.DraftError(
                f"❌ No cost known for: {', '.join(unknown)}. "
                f"Add them with `costs: 12, 10, 8`."
            )

        rules.queue_picks(self.bot.conn, draft, interaction.user.id, round, names, resolved)

        listing = "\n".join(f"{i}. **{n}** — {c} points"
                            for i, (n, c) in enumerate(zip(names, resolved), 1))
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
        cost="Its draft cost in points",
    )
    @app_commands.autocomplete(pokemon=pokemon_autocomplete)
    async def makeuppick(self, interaction: discord.Interaction, round: int,
                         pokemon: str, cost: int):
        draft = rules.require_channel_draft(self.bot.conn, interaction.channel_id)
        await interaction.response.defer(ephemeral=True)

        pokemon = await self.resolve_pokemon(draft, pokemon, cost)
        pick = rules.make_makeup_pick(self.bot.conn, draft, interaction.user.id,
                                      round, pokemon, cost)

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
            "cost": pick["cost"],
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
        draft = rules.require_channel_draft(self.bot.conn, interaction.channel_id)
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
        if result["created"]:
            announcement = (
                f"✏️ An admin recorded a Round {result['round']} pick for "
                f"**{result['player']['display_name']}**: "
                f"**{result['new_pokemon']} ({result['new_cost']})**."
            )
        else:
            announcement = (
                f"✏️ **{result['player']['display_name']}**'s Round {result['round']} pick was "
                f"changed: ~~{result['old_pokemon']} ({result['old_cost']})~~ → "
                f"**{result['new_pokemon']} ({result['new_cost']})**."
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
        draft = rules.require_channel_draft(self.bot.conn, interaction.channel_id)
        view = rules.status_view(self.bot.conn, draft)

        lines = [f"**Draft #{draft['id']} — {draft['status']}**"]

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
        draft = rules.require_channel_draft(self.bot.conn, interaction.channel_id)
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
        draft = rules.require_channel_draft(self.bot.conn, interaction.channel_id)

        target_id = player.id if player else interaction.user.id
        target = rules.get_player(self.bot.conn, draft["id"], target_id)
        if target is None:
            who = player.display_name if player else "You are"
            raise rules.DraftError(f"❌ {who} not a player in this channel's draft.")

        view = rules.roster_view(self.bot.conn, draft, target)
        if view["pokemon"]:
            body = "\n".join(f"{index}. **{name}** — {cost} points"
                             for index, (name, cost) in enumerate(view["pokemon"], 1))
        else:
            body = "*No Pokémon drafted yet.*"

        await interaction.response.send_message(
            f"**{target['display_name']}'s roster**\n{body}\n\n"
            f"• Spent: **{view['spent']}** points\n"
            f"• Remaining: **{view['remaining']}** of {draft['budget']}\n"
            f"• Picks: **{view['count']}** / {view['max_picks']} "
            f"(minimum {view['min_picks']})\n"
            + (f"• Trades used: **{rules.trades_used(self.bot.conn, draft['id'], target['id'])}**"
               f" / {draft['max_trades']}" if draft["max_trades"] else ""),
            ephemeral=True,
        )


async def setup(bot):
    await bot.add_cog(DraftingCog(bot))
