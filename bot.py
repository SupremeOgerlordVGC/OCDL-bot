"""
Entry point for the Pokémon Draft League bot.

    python3 bot.py

Everything Discord-specific is wired together here: the database
connection, the turn timers, the optional Google Sheets client, and the
three cogs that hold the slash commands.
"""

import asyncio
import hashlib
import json
import logging
import os
import sys
import time

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database
import draft as rules
import sheets
import timers
import ui
import webapi

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(name)s: %(message)s",
)
log = logging.getLogger("draftbot")


class HideIrrelevantWarnings(logging.Filter):
    """Drop two discord.py warnings that do not apply to this bot.

    * PyNaCl / voice - this bot never joins a voice channel.
    * "Privileged message content intent is missing" - discord.py warns
      because a command prefix is set, but every command here is a slash
      command, which does not need that intent. Left in, the warning sends
      you hunting for a problem that is not there.
    """

    IGNORED = ("PyNaCl is not installed",
               "Privileged message content intent is missing")

    def filter(self, record):
        return not record.getMessage().startswith(self.IGNORED)


for _name in ("discord.client", "discord.ext.commands.bot"):
    logging.getLogger(_name).addFilter(HideIrrelevantWarnings())

COGS = ["cogs.admin", "cogs.drafting", "cogs.extras", "cogs.results"]


class DraftBot(commands.Bot):
    def __init__(self, members_intent=None):
        # Slash commands need no privileged intents. The members intent is
        # only asked for so that players can be listed by role; if it has
        # not been enabled in the Developer Portal, main() retries without
        # it rather than refusing to start.
        intents = discord.Intents.default()
        if members_intent is None:
            members_intent = config.wants_members_intent()
        intents.members = bool(members_intent)
        self.members_intent = bool(members_intent)
        super().__init__(command_prefix="!", intents=intents)

        self.conn = database.connect()
        self.sheets = sheets.SheetSync()
        self.timers = timers.TurnTimers(self.on_timer_expired)
        # Which tab of each league's template holds its price list. Looked up
        # once per league, since it cannot change mid-draft.
        self.tiering_tabs = {}
        self._resumed = False
        # draft.ogreclan.com's private web API (webapi.py), if it's set up
        self.webapi = None

    # ---- startup --------------------------------------------------------

    async def setup_hook(self):
        for extension in COGS:
            await self.load_extension(extension)
            log.info("loaded %s", extension)

        self.tree.on_error = self.on_app_command_error

        api = config.get_ogreclan_api()
        if api:
            self.webapi = webapi.WebApi(self, *api)
            try:
                await self.webapi.start()
            except OSError:
                # a busy port must not stop the draft bot itself
                log.exception("could not start the web API for draft.ogreclan.com")
                self.webapi = None

        # Syncing to one guild makes commands appear instantly while you are
        # testing. A global sync can take up to an hour to show up.
        dev_guild = config.get_dev_guild_id()
        target = discord.Object(id=dev_guild) if dev_guild else None

        # Registering commands is heavily rate limited - a global sync can
        # take the best part of a minute - and the result does not change
        # unless the commands themselves do. So it is skipped when nothing
        # has changed, which keeps restarts quick and stays well clear of
        # Discord's limits even if the service restarts repeatedly.
        signature = self._command_signature(dev_guild)
        if signature == self._last_sync_signature():
            log.info("commands unchanged since the last sync, skipping it")
        else:
            if target is not None:
                self.tree.copy_global_to(guild=target)
                await self.tree.sync(guild=target)

                # Also clear any global registrations. Without this, a bot
                # that once synced globally and later synced to a guild
                # shows every command TWICE in the menu - once from each
                # registration - and Discord gives no hint why.
                self.tree.clear_commands(guild=None)
                await self.tree.sync()

                log.info("commands synced to guild %s (global copies cleared)", dev_guild)
            else:
                await self.tree.sync()
                log.info("commands synced globally (may take up to an hour to appear)")
            self._remember_sync(signature)

        self.automatic_backup.start()
        self.start_scheduled_drafts.start()

    # ---- command sync bookkeeping ---------------------------------------

    def _command_signature(self, dev_guild):
        """A short fingerprint of the commands as Discord would see them.

        Covers the sync target too, so switching DEV_GUILD_ID on or off
        forces a fresh sync rather than being mistaken for "no change".
        """
        shape = [dev_guild or 0]
        for command in sorted(self.tree.get_commands(), key=lambda c: c.name):
            shape.append([
                command.name,
                command.description,
                [[p.name, p.description, str(p.type), p.required]
                 for p in command.parameters],
            ])
        digest = hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()
        return digest[:32]

    @property
    def _sync_marker(self):
        return os.path.join(config.BASE_DIR, ".command-sync")

    def _last_sync_signature(self):
        try:
            with open(self._sync_marker, "r", encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return None

    def _remember_sync(self, signature):
        try:
            with open(self._sync_marker, "w", encoding="utf-8") as handle:
                handle.write(signature)
        except OSError:
            # Not being able to remember just means syncing again next time.
            log.warning("could not record the command sync marker")

    async def on_ready(self):
        log.info("logged in as %s", self.user)
        if not self._resumed:          # on_ready fires again after a reconnect
            self._resumed = True
            await self.resume_drafts()

    async def resume_drafts(self):
        """Put every running draft back on the clock after a restart.

        If the bot was offline when a deadline passed, the timer is restarted
        instead of skipping the player: downtime is the host's fault, not the
        drafter's.
        """
        active = self.conn.execute(
            "SELECT * FROM pool_view WHERE status = 'active'"
        ).fetchall()

        for draft in active:
            channel = self.get_channel(draft["channel_id"])
            player = rules.current_player(self.conn, draft)
            if player is None:
                continue

            token = (draft["current_round"], draft["current_turn_index"])
            remaining = (draft["turn_deadline"] - time.time()) if draft["turn_deadline"] else None

            if remaining is not None and remaining > 5:
                deadline = self.timers.schedule(draft["id"], remaining, token)
                rules.open_turn_clock(self.conn, draft["id"], deadline)
                log.info("draft %s resumed with %.0fs left", draft["id"], remaining)
                continue

            # No deadline stored, or it lapsed while the bot was down.
            seconds = player["current_timer"]
            deadline = self.timers.schedule(draft["id"], seconds, token)
            rules.open_turn_clock(self.conn, draft["id"], deadline)
            log.info("draft %s restarted the clock (%ss)", draft["id"], seconds)
            if draft["site_only"]:
                # it lives on draft.ogreclan.com, which shows the new deadline
                continue

            posted = await ui.safe_send(
                channel,
                f"🔄 The bot restarted. <@{player['user_id']}>, you are still on the "
                f"clock for **Round {draft['current_round']}** and your timer has been "
                f"restarted at **{ui.format_seconds(seconds)}**.",
            )
            if not posted:
                # No point running a clock nobody can see - see ui.announce.
                self.timers.cancel(draft["id"])
                rules.open_turn_clock(self.conn, draft["id"], None)
                log.warning(
                    "draft %s: clock PAUSED - I cannot post in channel %s. Grant the bot "
                    "View Channel and Send Messages there, then restart.",
                    draft["id"], draft["channel_id"],
                )

    # ---- timers ---------------------------------------------------------

    async def on_timer_expired(self, draft_id, token):
        """A player's clock ran out."""
        draft = rules.get_pool(self.conn, draft_id)
        if draft is None or draft["status"] != "active":
            return

        # Guard against a stale timer: the player may have picked in the
        # split second before this fired, moving the draft on.
        if (draft["current_round"], draft["current_turn_index"]) != tuple(token):
            return

        events = rules.skip_current_player(self.conn, draft_id, reason=rules.TIMER_SKIP)
        await ui.announce(self, draft_id, events)

    # ---- housekeeping ---------------------------------------------------

    @tasks.loop(seconds=20)
    async def start_scheduled_drafts(self):
        """Begin any draft whose scheduled time has arrived.

        The schedule lives in the database rather than in memory, so this
        picks it up again after a restart - a draft set to begin overnight
        still begins even if the bot was redeployed in between.
        """
        try:
            due = rules.due_scheduled_pools(self.conn)
        except Exception:
            log.exception("could not look up scheduled drafts")
            return

        for pool in due:
            try:
                events = rules.start_pool(self.conn, pool)
            except rules.DraftError as error:
                log.warning("scheduled start for pool %s refused: %s", pool["id"], error)
                rules.cancel_scheduled_start(self.conn, pool)
                continue
            except Exception:
                log.exception("scheduled start failed for pool %s", pool["id"])
                continue

            log.info("pool %s (%s pool %s) started on schedule",
                     pool["id"], pool["league_name"], pool["label"])

            channel = self.get_channel(pool["channel_id"])
            cog = self.get_cog("AdminCog")
            if cog is not None and channel is not None:
                await cog.announce_start(channel, pool)
            await ui.announce(self, pool["id"], events, channel=channel)

    @start_scheduled_drafts.before_loop
    async def before_scheduled_drafts(self):
        await self.wait_until_ready()

    @tasks.loop(hours=config.BACKUP_INTERVAL_HOURS)
    async def automatic_backup(self):
        try:
            path = await asyncio.to_thread(database.backup, self.conn)
            await asyncio.to_thread(database.prune_backups, None, config.BACKUPS_TO_KEEP)
            log.info("automatic backup written to %s", path)
        except Exception:
            log.exception("automatic backup failed")

    @automatic_backup.before_loop
    async def before_backup(self):
        await self.wait_until_ready()

    async def owns(self, user_id):
        """Whether a Discord ID is this bot's owner.

        `is_owner` wants a user object and the web API only ever has an ID,
        so it is handed a bare reference; only the id is ever read.
        """
        if not user_id:
            return False
        return await self.is_owner(discord.Object(id=int(user_id)))

    async def on_app_command_error(self, interaction, error):
        """Show rule violations to the user; log anything unexpected."""
        original = getattr(error, "original", error)

        if isinstance(original, rules.DraftError):
            message = str(original)
        elif isinstance(original, sheets.SheetError):
            message = f"⚠️ Google Sheets problem: {original}\nThe draft itself is unaffected."
        elif isinstance(original, app_commands.TransformerError):
            # Typing a name into a player: option instead of picking from the
            # list, most often somebody who has since left the server. Worth
            # saying plainly - mid-season departures are normal.
            message = (
                f"❌ I could not find **{original.value}** on this server. "
                f"Pick the person from the list Discord offers rather than typing "
                f"their name, and if they have left the server use the name the "
                f"draft knows them by — `/progress` lists everybody in it."
            )
        else:
            log.exception("unhandled command error", exc_info=original)
            message = "❌ Something went wrong running that command. The draft has not been changed."

        try:
            if interaction.response.is_done():
                await interaction.followup.send(message, ephemeral=True)
            else:
                await interaction.response.send_message(message, ephemeral=True)
        except discord.DiscordException:
            log.exception("could not report an error to the user")

    async def close(self):
        self.timers.cancel_all()
        if self.webapi:
            await self.webapi.stop()
        await super().close()
        self.conn.close()


def main():
    config.load_env()
    token = config.get_token()
    if not token:
        print("No DISCORD_TOKEN found.\n"
              "Copy .env.example to .env and paste your bot token into it, "
              "or set the DISCORD_TOKEN environment variable.")
        sys.exit(1)

    def start(members_intent):
        bot = DraftBot(members_intent=members_intent)
        if bot.sheets.enabled:
            log.info("Google Sheets is available (writing enabled: %s)",
                     config.SHEETS_WRITE_ENABLED)
        else:
            log.info("Google Sheets is disabled: %s", bot.sheets.reason)
        bot.run(token, log_handler=None)

    wants_members = config.wants_members_intent()
    if not wants_members:
        log.info("members intent not requested, so players cannot be listed by role "
                 "- @mention them instead (see REQUEST_MEMBERS_INTENT in config.py)")

    try:
        start(wants_members)
    except discord.PrivilegedIntentsRequired:
        # Asking for an intent the Developer Portal has not approved makes
        # Discord refuse the gateway, and it refuses it on every retry - so
        # there is no point trying again with it. Carry on without.
        log.warning(
            "The Server Members Intent is NOT enabled for this bot, so it was refused. "
            "Starting without it: players must be @mentioned rather than given as a "
            "role. To enable it, go to https://discord.com/developers/applications -> "
            "your app -> Bot -> Privileged Gateway Intents -> Server Members Intent, "
            "then set REQUEST_MEMBERS_INTENT=true in .env."
        )
        start(False)


if __name__ == "__main__":
    main()
