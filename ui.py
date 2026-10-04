"""
The bridge between draft rules and Discord.

draft.py returns lists of plain "event" dictionaries. This module turns them
into chat messages, starts the next timer, and nudges Google Sheets. Keeping
it separate means the rules stay testable and the cogs stay short.
"""

import asyncio
import logging
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
        players.append((user_id, member.display_name if member else f"User {user_id}"))
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
        return (f"🤖 **{event['display_name']}**'s queued pick: **{event['pokemon']}** "
                f"for **{event['cost']}** points. *(Round {event['round']}, "
                f"pick #{event['pick_order']})*")

    if kind == "queue_exhausted":
        return (f"⚠️ <@{event['user_id']}> every Pokémon in your Round {event['round']} queue "
                f"has already been drafted. Your timer is running — pick manually with `/pick`. "
                f"*(Your timer will not be reduced for this.)*")

    if kind == "passed":
        return (f"⏭️ **{event['display_name']}** was passed over in Round {event['round']} "
                f"— {event['reason']}.")

    if kind == "skipped":
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


async def safe_send(channel, message):
    """Post to a channel. Returns False if Discord refused, never raises.

    Public announcements are best effort on purpose. Work is written to
    SQLite before anything is announced, so a missing permission must not
    undo a pick, abort the rest of a command, or stop a timer starting.
    """
    if channel is None:
        return False
    try:
        await channel.send(message)
        return True
    except discord.Forbidden:
        log.warning("not allowed to post in channel %s - check View Channel "
                    "and Send Messages for the bot there", channel.id)
        return False
    except discord.DiscordException:
        log.exception("could not post to channel %s", channel.id)
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
