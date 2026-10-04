"""
Entry point for the Pokémon Draft League bot.

    python3 bot.py

Everything Discord-specific is wired together here: the database
connection, the turn timers, the optional Google Sheets client, and the
three cogs that hold the slash commands.
"""

import asyncio
import logging
import sys
import time

import discord
from discord.ext import commands, tasks

import config
import database
import draft as rules
import sheets
import timers
import ui

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

COGS = ["cogs.admin", "cogs.drafting", "cogs.extras"]


class DraftBot(commands.Bot):
    def __init__(self):
        # Only the default intents are needed. Slash commands do not require
        # the privileged "message content" or "server members" intents.
        super().__init__(command_prefix="!", intents=discord.Intents.default())

        self.conn = database.connect()
        self.sheets = sheets.SheetSync()
        self.timers = timers.TurnTimers(self.on_timer_expired)
        # Which tab of each league's template holds its price list. Looked up
        # once per league, since it cannot change mid-draft.
        self.tiering_tabs = {}
        self._resumed = False

    # ---- startup --------------------------------------------------------

    async def setup_hook(self):
        for extension in COGS:
            await self.load_extension(extension)
            log.info("loaded %s", extension)

        self.tree.on_error = self.on_app_command_error

        # Syncing to one guild makes commands appear instantly while you are
        # testing. A global sync can take up to an hour to show up.
        dev_guild = config.get_dev_guild_id()
        if dev_guild:
            guild = discord.Object(id=dev_guild)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("commands synced to test guild %s", dev_guild)
        else:
            await self.tree.sync()
            log.info("commands synced globally (may take up to an hour to appear)")

        self.automatic_backup.start()

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

        events = rules.skip_current_player(self.conn, draft_id, reason="timer expired")
        await ui.announce(self, draft_id, events)

    # ---- housekeeping ---------------------------------------------------

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

    async def on_app_command_error(self, interaction, error):
        """Show rule violations to the user; log anything unexpected."""
        original = getattr(error, "original", error)

        if isinstance(original, rules.DraftError):
            message = str(original)
        elif isinstance(original, sheets.SheetError):
            message = f"⚠️ Google Sheets problem: {original}\nThe draft itself is unaffected."
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

    bot = DraftBot()
    if bot.sheets.enabled:
        log.info("Google Sheets is available (writing enabled: %s)", config.SHEETS_WRITE_ENABLED)
    else:
        log.info("Google Sheets is disabled: %s", bot.sheets.reason)

    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
