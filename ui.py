"""
The bridge between draft rules and Discord.

draft.py returns lists of plain "event" dictionaries. This module turns them
into chat messages, starts the next timer, and nudges Google Sheets. Keeping
it separate means the rules stay testable and the cogs stay short.
"""

import asyncio
import logging
import random
import re

import discord

import draft as rules

log = logging.getLogger("draftbot.ui")

DISCORD_MESSAGE_LIMIT = 1900   # a little under the real 2000 for safety


def format_seconds(total):
    """60 -> '1m', 90 -> '1m 30s', 45 -> '45s'."""
    total = int(total)
    minutes, seconds = divmod(total, 60)
    if minutes and seconds:
        return f"{minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def is_moderator(bot, interaction):
    """True if this user may run moderator commands on this server.

    Three ways to qualify: the Discord **Administrator** permission, being
    the server owner, or holding a role added with `/draft-moderator`.
    Moderator roles are per server rather than per draft, so one set of
    people can look after every league running on it.
    """
    user = interaction.user
    permissions = getattr(user, "guild_permissions", None)
    if permissions is not None and permissions.administrator:
        return True
    if interaction.guild and interaction.guild.owner_id == user.id:
        return True

    allowed = rules.moderator_role_ids(bot.conn, interaction.guild_id)
    if not allowed:
        return False
    return bool(allowed & {role.id for role in getattr(user, "roles", [])})


def require_moderator(bot, interaction):
    if not is_moderator(bot, interaction):
        raise rules.DraftError(
            "❌ This command is for draft moderators. Somebody with the "
            "**Administrator** permission can grant access with `/draft-moderator`."
        )


def is_administrator(interaction):
    """Only a true server Administrator may change who the moderators are."""
    permissions = getattr(interaction.user, "guild_permissions", None)
    if permissions is not None and permissions.administrator:
        return True
    return bool(interaction.guild and interaction.guild.owner_id == interaction.user.id)


def _player_entry(member, user_id=None):
    """One player as the bot records them.

    `username` is the account name, which is what a league host is most
    likely to have typed on the spreadsheet's Coaches tab; `display_name`
    is their nickname on the server. Both are kept so either can be matched.
    """
    if member is None:
        return {"user_id": user_id, "display_name": f"User {user_id}", "username": ""}
    return {
        "user_id": member.id,
        "display_name": member.display_name,
        "username": getattr(member, "name", "") or "",
    }


def players_from_role(role, rng=None):
    """Everyone holding a role, in a randomly shuffled draft order.

    Used when a league host would rather hand the bot a role than type out
    the order themselves. Bots are skipped.
    """
    members = [member for member in getattr(role, "members", []) if not member.bot]
    if not members:
        raise rules.DraftError(
            f"❌ I cannot see anybody with the {role.mention} role.\n"
            f"Either nobody has it, or the **Server Members Intent** is switched off "
            f"for this bot — without it Discord does not tell me who holds a role. "
            f"Turn it on in the Developer Portal under **Bot → Privileged Gateway "
            f"Intents**, restart the bot, and try again. You can always list players "
            f"by @mention instead."
        )

    rng = rng or random.Random()
    rng.shuffle(members)
    return [_player_entry(member) for member in members]


async def roster_from_options(guild, mentions, role, label):
    """Build one pool's player list from either @mentions or a role.

    Mentions keep the order they were typed, which is the draft order. A
    role is shuffled, so the bot picks the order for you.
    """
    if mentions and role is not None:
        raise rules.DraftError(
            f"❌ For pool {label}, give either a list of players **or** a role, not both."
        )
    if role is not None:
        return players_from_role(role), True
    if mentions:
        return await parse_players(guild, mentions), False
    raise rules.DraftError(
        f"❌ Pool {label} needs players: either @mention them in draft order, or give "
        f"a role for the bot to shuffle."
    )


async def parse_players(guild, text):
    """Turn a string of @mentions into [(user_id, display_name), ...].

    Mentions are used rather than typed names so every player is tied to a
    real Discord account, which is what makes pings and permission checks
    reliable later on.
    """
    ids = re.findall(r"<@!?(\d+)>", text or "")
    if not ids:
        raise rules.DraftError(
            "❌ List the players by @mentioning them, for example: "
            "`players: @Larry @Alex @Sarah`"
        )

    players = []
    for raw in ids:
        user_id = int(raw)
        member = guild.get_member(user_id) if guild else None
        if member is None and guild is not None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.DiscordException:
                member = None
        players.append(_player_entry(member, user_id))
    return players


# --------------------------------------------------------------------------
# Turning events into text
# --------------------------------------------------------------------------


def render_event(event, draft):
    """One event -> one line of chat. Returns None for silent events."""
    kind = event["type"]

    if kind == "turn_started":
        lines = [
            f"🎯 **Round {event['round']}** — <@{event['user_id']}>, you are on the clock! "
            f"You have **{format_seconds(event['seconds'])}**.",
            f"Use `/pick round:{event['round']} pokemon:<name> cost:<points>`.",
        ]
        if event.get("pending_makeups"):
            owed = ", ".join(str(r) for r in event["pending_makeups"])
            lines.append(
                f"⚠️ You still owe a makeup pick for Round {owed}. "
                f"Use `/makeuppick` **before this turn ends** or it expires."
            )
        return "\n".join(lines)

    if kind == "pick_made":
        captain = " ⭐ **Tera Captain**" if event.get("tera_captain") else ""
        return (f"✅ **{event['display_name']}** drafted **{event['pokemon']}** "
                f"for **{event['cost']}** points.{captain} *(Round {event['round']}, "
                f"pick #{event['pick_order']})*")

    if kind == "auto_pick":
        captain = " ⭐ **Tera Captain**" if event.get("tera_captain") else ""
        return (f"🤖 **{event['display_name']}**'s queued pick: **{event['pokemon']}** "
                f"for **{event['cost']}** points.{captain} *(Round {event['round']}, "
                f"pick #{event['pick_order']})*")

    if kind == "player_finished":
        text = (f"🎉 **{event['display_name']}** has **finished drafting** with "
                f"{event['picks']} Pokémon")
        if event["budget_left"]:
            text += f", leaving {event['budget_left']} points unspent"
        text += "."
        if event["giving_up"]:
            text += (f" They are giving up their last {event['giving_up']} "
                     f"pick(s), so their turns will be passed over from here.")
        return text

    if kind == "player_reopened":
        return (f"↩️ A moderator has put **{event['display_name']}** back into the "
                f"draft — they are picking again, {event['picks']} of "
                f"{event['max_picks']} Pokémon in. Their next turn will come "
                f"round as normal.")

    if kind == "queue_exhausted":
        return (f"⚠️ <@{event['user_id']}> every Pokémon in your Round {event['round']} queue "
                f"has already been drafted. Your timer is running — pick manually with `/pick`. "
                f"*(Your timer will not be reduced for this.)*")

    if kind == "passed":
        return (f"⏭️ **{event['display_name']}** was passed over in Round {event['round']} "
                f"— {event['reason']}.")

    if kind == "skipped":
        if event.get("reason") == rules.MODERATOR_SKIP:
            text = (f"⏭️ **{event['display_name']}** was **skipped** by a moderator "
                    f"in Round {event['round']}.")
        else:
            text = (f"⏰ **{event['display_name']}** ran out of time and was **skipped** "
                    f"in Round {event['round']}.")
        if event["halved"]:
            text += f"\nTheir timer is now **{format_seconds(event['new_timer'])}**."
        elif event.get("queue_exhausted"):
            text += "\nTheir timer is unchanged because their whole queue had been drafted."
        text += (f"\nThey owe a Round {event['round']} makeup pick — `/makeuppick "
                 f"round:{event['round']} pokemon:<name> cost:<points>`.")
        return text

    if kind == "makeup_expired":
        text = (f"❗ <@{event['user_id']}> your **Round {event['round']} makeup pick has expired** — "
                f"the deadline was your Round {event['round'] + 1} turn. That pick can no longer "
                f"be made, and this turn is forfeit as well.")
        if event.get("back_to_back"):
            text += ("\nYou pick twice in a row at this round boundary, so there was no gap "
                     "between the two — your timer is only reduced once for both.")
        return text

    if kind == "draft_complete":
        where = f" for **Pool {event['pool']}**" if event.get("pool") else ""
        if event.get("round_reached", 0) and event["round_reached"] < event["total_rounds"]:
            headline = (f"🏁 **The draft is complete{where}!** It finished early in Round "
                        f"{event['round_reached']} because no player could afford "
                        f"another pick.")
        else:
            headline = (f"🏁 **The draft is complete{where}!** All "
                        f"{event['total_rounds']} rounds have finished.")

        tail = " Use `/roster` to review teams and `/progress` for the full history."
        if event.get("free_trades_until"):
            until = int(event["free_trades_until"])
            pings = " ".join(f"<@{user_id}>" for user_id in event.get("players") or [])
            limit = (f" and don't count toward the season's limit of {event['max_trades']} "
                     f"Pokémon picked up" if event.get("max_trades") else "")
            tail += (f"\n\n📣 {pings}\n🔓 **Free trade window:** until <t:{until}:F> (<t:{until}:R>), "
                     f"trades and free-agency pickups are unlimited{limit}. Propose them with "
                     f"`/trade`; once approved, they take effect when the next week is posted.")
        if event.get("archived"):
            tail += (" This was a draft-only league, so it is now archived and the "
                     "channels are free for a new `/setup`.")
        elif not event.get("league_finished"):
            tail += " The other pool is still drafting."
        return headline + tail

    return None


def pack_messages(lines):
    """Group rendered lines into as few Discord messages as possible."""
    messages = []
    current = ""
    for line in lines:
        if not line:
            continue
        if len(current) + len(line) + 2 > DISCORD_MESSAGE_LIMIT:
            if current:
                messages.append(current)
            current = line
        else:
            current = f"{current}\n\n{line}" if current else line
    if current:
        messages.append(current)
    return messages


# --------------------------------------------------------------------------
# Announcing events
# --------------------------------------------------------------------------


# The two options people vote between on a posted matchup.
VOTE_EMOJI = ("\U0001F1E6", "\U0001F1E7")        # 🇦 🇧


async def safe_send_message(channel, message):
    """Post to a channel and hand back the message, or None if refused.

    Public announcements are best effort on purpose. Work is written to
    SQLite before anything is announced, so a missing permission must not
    undo a pick, abort the rest of a command, or stop a timer starting.
    """
    if channel is None:
        return None
    try:
        return await channel.send(message)
    except discord.Forbidden:
        log.warning("not allowed to post in channel %s - check View Channel "
                    "and Send Messages for the bot there", channel.id)
        return None
    except discord.DiscordException:
        log.exception("could not post to channel %s", channel.id)
        return None


async def safe_send(channel, message):
    """Post to a channel. Returns False if Discord refused, never raises."""
    return await safe_send_message(channel, message) is not None


async def add_vote_reactions(message):
    """Put the A and B votes on a matchup. Returns False if Discord refused.

    Needs Add Reactions in that channel; without it the matchup is still
    posted, just without anything to vote on.
    """
    if message is None:
        return False
    try:
        for emoji in VOTE_EMOJI:
            await message.add_reaction(emoji)
        return True
    except discord.Forbidden:
        log.warning("not allowed to add reactions in channel %s - check Add "
                    "Reactions for the bot there", getattr(message.channel, "id", "?"))
        return False
    except discord.DiscordException:
        log.exception("could not add vote reactions")
        return False


def permission_hint(channel):
    """Shown to an admin when the bot could not post publicly."""
    where = channel.mention if channel is not None else "that channel"
    return (f"\n\n⚠️ I could not post in {where}. Give me **View Channel** and "
            f"**Send Messages** there. Everything is saved — nothing was lost.")


async def announce(bot, draft_id, events, channel=None):
    """Post events publicly, start the next timer, and update Sheets.

    Discord or Google failing here must never undo a pick that has already
    been written to SQLite, so every outside call is wrapped.
    """
    if not events:
        return

    draft = rules.get_pool(bot.conn, draft_id)
    channel = channel or bot.get_channel(draft["channel_id"])

    lines = [render_event(event, draft) for event in events]

    for event in events:
        if event["type"] == "turn_started":
            _start_timer(bot, draft_id, event)
        elif event["type"] in ("pick_made", "auto_pick"):
            sync_pick_to_sheet(bot, draft_id, event)
        elif event["type"] == "draft_complete":
            bot.timers.cancel(draft_id)

    if draft["site_only"]:
        # it lives on draft.ogreclan.com: nothing to post, and the clock runs
        return
    if channel is None:
        log.warning("draft %s: channel %s is not visible to the bot",
                    draft_id, draft["channel_id"])
        _stop_unwatched_clock(bot, draft_id, events)
        return

    posted = False
    for message in pack_messages(lines):
        posted = await safe_send(channel, message) or posted

    if not posted:
        _stop_unwatched_clock(bot, draft_id, events)


def _stop_unwatched_clock(bot, draft_id, events):
    """Pause a turn whose announcement could not be delivered.

    Running a timer in a channel the bot cannot post in would skip players
    for a permission problem that is not theirs, and halve their timers on
    the way. So the clock is stopped instead and the draft waits. Fix the
    permission and restart the bot: resume_drafts sees a turn with no
    deadline and opens it again properly.
    """
    if not any(event["type"] == "turn_started" for event in events):
        return

    bot.timers.cancel(draft_id)
    rules.open_turn_clock(bot.conn, draft_id, None)
    log.warning(
        "draft %s: the turn clock has been PAUSED because the bot cannot post in its "
        "channel. Grant View Channel and Send Messages, then restart the bot.", draft_id
    )


def _start_timer(bot, draft_id, event):
    """Begin the countdown for a turn and remember its deadline."""
    draft = rules.get_pool(bot.conn, draft_id)
    token = (draft["current_round"], draft["current_turn_index"])
    deadline = bot.timers.schedule(draft_id, event["seconds"], token)
    rules.open_turn_clock(bot.conn, draft_id, deadline)


def sync_pick_to_sheet(bot, pool_id, event):
    """Push a single pick to the league template in the background.

    Fire and forget on purpose: the draft has already been saved locally, so
    a slow or broken Google API must not hold up the next player's turn.
    Everything the worker needs is read here, on the main thread, so it
    never touches the database.
    """
    if not bot.sheets.can_write:
        return

    draft = rules.get_pool(bot.conn, pool_id)
    if not draft["doc_url"] or draft["doc_kind"] != "template":
        return      # a tiering document has nowhere to write picks to

    doc_url = draft["doc_url"]
    tera_format = bool(draft["tera_budget"])
    coach_number = event["coach_number"]
    round_number = event["round"]
    pokemon = event["pokemon"]
    tera_captain = bool(event.get("tera_captain"))

    async def worker():
        try:
            await asyncio.to_thread(bot.sheets.write_pick, doc_url, tera_format,
                                    coach_number, round_number, pokemon, tera_captain)
        except Exception:
            log.exception("sheet update failed for pool %s (draft data is unaffected)",
                          pool_id)

    asyncio.create_task(worker())


async def grace_window_closed(bot, pool):
    """Tell a pool its free-transaction window has closed, once.

    Nothing marks the end of the grace period except the clock, so without
    this the rules change under the players in silence - which is exactly
    what happened in Cornerstone's pool A.
    """
    limit = (f"Each of you may pick up **{pool['max_trades']}** Pokémon in trades from "
             f"here on." if pool["max_trades"] else
             "There is no limit on how many Pokémon you may pick up in trades.")
    await safe_send(
        bot.get_channel(pool["channel_id"]),
        f"⏳ **Pool {pool['label']}'s free-transaction window has closed.**\n"
        f"{limit} `/trade` works the same way and a moderator still approves every "
        f"one — what changes is that these count towards your allowance.\n"
        f"An approved trade takes effect once you have finished that week's match.")
    rules.record_grace_ended(bot.conn, pool["id"])


def when_trades_land(league):
    """A phrase for "rosters and budgets change ___", true for this league now.

    Before week 1 there is no match to finish - which is exactly when the
    rush of free transactions happens - so saying "when you finish your
    match" then would be telling players to wait for nothing.
    """
    if (league["current_week"] or 0) < 1:
        return ("when a moderator puts them through with `/updatetrades`: there is no "
                "match to finish until week 1 starts")
    return "once the match for that week has been reported by both players"


async def trades_landed(bot, applied, because):
    """Finish off trades that have just taken effect.

    Everything that puts a trade into effect ends up here - a finished
    match, a moderator's /updatetrades, a substitute's window closing -
    because the same three things always follow: a free-agency pickup has
    no price until the tiering list is read, the pool wants telling, and
    the spreadsheet has to be rewritten. `because` is the reason, which
    goes into the message. Returns what it was given, so a caller can
    count it.
    """
    if not applied:
        return applied

    cog = bot.get_cog("AdminCog")
    if cog is not None:
        await cog.price_trades(applied)      # free-agency pickups have no price yet

    for trade, notes in applied:
        pool = rules.get_pool(bot.conn, trade["pool_id"])
        if pool is None:
            continue
        proposer = rules.get_player_by_id(bot.conn, trade["proposer_id"])
        partner = (rules.get_player_by_id(bot.conn, trade["partner_id"])
                   if trade["partner_id"] else None)
        log.info("trade #%s applied for %s (%s)", trade["trade_number"],
                 proposer["display_name"], because)
        lines = (f"🔁 **{proposer['display_name']}**'s trade #{trade['trade_number']} is "
                 f"in effect — {because}.\n"
                 f"• Gave up: {rules.trade_side(trade['drops'])}\n"
                 f"• Received: {rules.trade_side(trade['gets'])}")
        if partner is not None:
            lines += f"\n• With: **{partner['display_name']}**"
        if notes:
            lines += "\n" + "\n".join(f"• ⚠️ {note}" for note in notes)
        await safe_send(bot.get_channel(pool["channel_id"]), lines)

    for pool_id in {trade["pool_id"] for trade, _ in applied}:
        resync_sheet(bot, pool_id)
    return applied


def resync_sheet(bot, pool_id):
    """Rewrite the whole league on the sheet, in the background.

    Used after anything that can change more than one row: a new league, an
    edited pick, an approved trade or a substitution. It writes the coach
    list and every roster block for BOTH pools, because they share one
    document.
    """
    if not bot.sheets.can_write:
        return

    draft = rules.get_pool(bot.conn, pool_id)
    if not draft["doc_url"] or draft["doc_kind"] != "template":
        return      # a tiering document has no rosters to write to

    doc_url = draft["doc_url"]
    tera_format = bool(draft["tera_budget"])
    # Keyed by league, not pool: one document holds both pools' rosters.
    coaches, rosters, overflow = rules.export_for_sheet(bot.conn, draft["league_id"])
    if overflow:
        log.warning("league %s: rosters too big for the sheet blocks: %s",
                    draft["league_id"], overflow)

    async def worker():
        try:
            await asyncio.to_thread(bot.sheets.full_sync, doc_url, tera_format,
                                    coaches, rosters)
        except Exception:
            log.exception("sheet resync failed for pool %s (local data is fine)", pool_id)

    asyncio.create_task(worker())


# --------------------------------------------------------------------------
# Reading a queue back
# --------------------------------------------------------------------------

SELECT_LIMIT = 25       # Discord allows at most 25 options in one select


def queue_lines(view):
    """Format a rules.queue_view() result for Discord.

    Shared by the player's own `/myqueue` and the moderator's browser, so
    both show a queue exactly the same way.
    """
    lines = []
    for round_number, entries in view["rounds"]:
        drafted = " *(already drafted)*" if round_number in view["picked"] else ""
        lines.append(f"**Round {round_number}**{drafted}")
        for entry in entries:
            captain = " ⭐ Tera Captain" if entry["tera_captain"] else ""
            cost = "cost unknown" if entry["cost"] is None else f"{entry['cost']} points"
            lines.append(f"{entry['priority']}. **{entry['pokemon']}** — {cost}{captain}")
    return lines


class QueueBrowser(discord.ui.View):
    """Lets a moderator read a player's queue from the admin channel.

    An admin channel can run several leagues at once and a league can have
    two pools, so the moderator narrows down one select at a time. A level
    with only one possible answer is filled in rather than asked about,
    which means the common case is a single click.

    Narrowing by pool also keeps every player list inside Discord's limit
    of 25 options, since a pool holds at most MAX_PLAYERS.
    """

    def __init__(self, bot, owner_id, leagues, round_number=None, timeout=300):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.owner_id = owner_id
        self.leagues = leagues
        self.round_number = round_number
        self.message = None
        self.league = leagues[0] if len(leagues) == 1 else None
        self.pool = None
        self.player = None
        self._resolve_single_choices()
        self._rebuild()

    # ---- state ---------------------------------------------------------

    def _pools(self):
        if self.league is None:
            return []
        return rules.get_league_pools(self.bot.conn, self.league["id"])

    def _resolve_single_choices(self):
        pools = self._pools()
        if self.pool is None and len(pools) == 1:
            self.pool = pools[0]

    def _rebuild(self):
        """Put back one select per level the moderator can still change."""
        self.clear_items()
        if len(self.leagues) > 1:
            self.add_item(_LeagueSelect(self))
        if self.league is not None:
            pools = self._pools()
            if len(pools) > 1:
                self.add_item(_PoolSelect(self, pools))
            if self.pool is not None:
                self.add_item(_PlayerSelect(self))

    # ---- rendering -----------------------------------------------------

    def prompt(self):
        """What to say above the selects, based on what is still missing."""
        if self.league is None:
            return "🔎 Which draft?"
        if self.pool is None:
            return f"🔎 **{self.league['name']}** — which pool?"
        if self.player is None:
            return (f"🔎 **{self.league['name']}** — Pool {self.pool['label']}. "
                    f"Pick a player to see their queue.")
        return ""

    def embed(self):
        """The chosen player's queue, or None while still choosing."""
        if self.player is None:
            return None

        view = rules.queue_view(
            self.bot.conn, self.pool, self.owner_id,
            self.round_number, target_user_id=self.player["user_id"],
            acting_as_admin=True,
        )
        lines = queue_lines(view)
        scope = f"Round {self.round_number}" if self.round_number else "All rounds"
        embed = discord.Embed(
            title=f"{self.player['display_name']}'s queue",
            description="\n".join(lines) if lines else "Nothing queued.",
            colour=discord.Colour.blurple(),
        )
        embed.set_footer(
            text=f"{self.league['name']} — Pool {self.pool['label']} • {scope}")
        return embed

    async def refresh(self, interaction):
        self._resolve_single_choices()
        self._rebuild()
        await interaction.response.edit_message(
            content=self.prompt() or None, embed=self.embed(), view=self)

    # ---- housekeeping --------------------------------------------------

    async def interaction_check(self, interaction):
        """Only the moderator who opened it may drive it.

        The message is ephemeral so nobody else should see it in the first
        place; this is belt and braces.
        """
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "❌ That is not your lookup — run `/myqueue` yourself.", ephemeral=True)
        return False

    async def on_timeout(self):
        """Leave the queue on screen but take away the dead selects."""
        self.clear_items()
        if self.message is None:
            return
        try:
            await self.message.edit(view=None)
        except discord.DiscordException:
            pass        # the moderator dismissed it, which is fine


class _LeagueSelect(discord.ui.Select):
    def __init__(self, browser):
        self.browser = browser
        super().__init__(
            placeholder="Draft",
            options=[
                discord.SelectOption(
                    label=league["name"][:100], value=str(league["id"]),
                    default=browser.league is not None
                    and league["id"] == browser.league["id"])
                for league in browser.leagues[:SELECT_LIMIT]
            ],
        )

    async def callback(self, interaction):
        chosen = int(self.values[0])
        browser = self.browser
        browser.league = next(l for l in browser.leagues if l["id"] == chosen)
        browser.pool = None         # a different draft means different pools
        browser.player = None
        await browser.refresh(interaction)


class _PoolSelect(discord.ui.Select):
    def __init__(self, browser, pools):
        self.browser = browser
        self.pools = pools
        super().__init__(
            placeholder="Pool",
            options=[
                discord.SelectOption(
                    label=f"Pool {pool['label']}", value=str(pool["id"]),
                    default=browser.pool is not None
                    and pool["id"] == browser.pool["id"])
                for pool in pools[:SELECT_LIMIT]
            ],
        )

    async def callback(self, interaction):
        chosen = int(self.values[0])
        self.browser.pool = next(p for p in self.pools if p["id"] == chosen)
        self.browser.player = None
        await self.browser.refresh(interaction)


class _PlayerSelect(discord.ui.Select):
    def __init__(self, browser):
        self.browser = browser
        self.players = rules.get_players(browser.bot.conn, browser.pool["id"])
        if not self.players:
            # A select has to offer something, so say why it is empty.
            super().__init__(placeholder="No players in this pool",
                             options=[discord.SelectOption(label="—", value="0")],
                             disabled=True)
            return
        super().__init__(
            placeholder="Player",
            options=[
                discord.SelectOption(
                    label=player["display_name"][:100],
                    value=str(player["id"]),
                    description=(player["status"]
                                 if player["status"] != rules.ACTIVE else None),
                    default=browser.player is not None
                    and player["id"] == browser.player["id"])
                for player in self.players[:SELECT_LIMIT]
            ],
        )

    async def callback(self, interaction):
        chosen = int(self.values[0])
        self.browser.player = next(p for p in self.players if p["id"] == chosen)
        await self.browser.refresh(interaction)


class ConfirmFinish(discord.ui.View):
    """Double-checks a player ending their own draft early.

    Only a moderator can undo it, so the numbers are put in front of them
    and the question asked plainly, rather than trusting a slash command
    typed in a hurry.
    """

    def __init__(self, bot, pool_id, owner_id, timeout=120):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.pool_id = pool_id
        self.owner_id = owner_id
        self.message = None

    async def interaction_check(self, interaction):
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "❌ That is not your draft to finish.", ephemeral=True)
        return False

    @discord.ui.button(label="Yes, I'm done drafting",
                       style=discord.ButtonStyle.danger)
    async def finish(self, interaction, button):
        draft = rules.get_pool(self.bot.conn, self.pool_id)
        try:
            summary, events = rules.finish_drafting(
                self.bot.conn, draft, self.owner_id)
        except rules.DraftError as error:
            # Something changed between asking and answering - a moderator
            # removed them, or the draft ended.
            self.clear_items()
            await interaction.response.edit_message(content=str(error), view=None)
            self.stop()
            return

        self.clear_items()
        await interaction.response.edit_message(
            content=(f"✅ Your draft is finished with **{summary['picks']}** "
                     f"Pokémon. Your team is yours — you keep your roster, your "
                     f"place in the schedule and your trades. Ask a moderator if "
                     f"you need to pick again."),
            view=None,
        )
        self.stop()
        await announce(self.bot, self.pool_id, events)

    @discord.ui.button(label="No, keep drafting",
                       style=discord.ButtonStyle.secondary)
    async def keep(self, interaction, button):
        self.clear_items()
        await interaction.response.edit_message(
            content="👍 Nothing has changed — you are still drafting.", view=None)
        self.stop()

    async def on_timeout(self):
        """An unanswered question must not leave a live button lying around."""
        self.clear_items()
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="⌛ That timed out, so nothing changed. Run "
                        "`/finishdrafting` again if you still want to finish.",
                view=None)
        except discord.DiscordException:
            pass
