"""
End-to-end tests of the Discord layer, using a fake channel instead of a
real connection. These cover the parts that unit tests of draft.py cannot
reach: message rendering, timer scheduling, the skip callback, and restart
recovery.

    python3 -m unittest discover -s tests
"""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord      # noqa: E402

import config       # noqa: E402
import database     # noqa: E402
import draft as rules   # noqa: E402
import timers       # noqa: E402
import ui           # noqa: E402
from cogs.drafting import DraftingCog   # noqa: E402


class FakeChannel:
    """Collects everything the bot tries to post."""

    def __init__(self, channel_id):
        self.id = channel_id
        self.messages = []

    async def send(self, content):
        self.messages.append(content)
        return content

    @property
    def text(self):
        return "\n".join(self.messages)


class SilentResponse:
    """Minimal stand-in for an aiohttp response, to build discord.Forbidden."""
    status = 403
    reason = "Forbidden"


class ForbiddenChannel(FakeChannel):
    """A channel the bot is not allowed to post in."""

    async def send(self, content):
        raise discord.Forbidden(SilentResponse(), "Missing Access")


class FakeSheets:
    enabled = False
    can_write = False
    reason = "disabled for tests"


class StubPokedex:
    """A tiny stand-in Pokédex, so sheet-backed rules can be tested offline."""

    enabled = True
    can_write = False
    reason = None

    ENTRIES = [
        {"key": "garchomp", "name": "Garchomp", "cost": 12, "banned": False, "row": 2},
        {"key": "rotom-wash", "name": "Rotom-Wash", "cost": 8, "banned": False, "row": 3},
        {"key": "bulbasaur", "name": "Bulbasaur", "cost": None, "banned": True, "row": 4},
    ]

    def lookup(self, sheet_url, pokemon):
        wanted = " ".join(str(pokemon).strip().lower().split())
        for entry in self.ENTRIES:
            if entry["key"] == wanted:
                return entry
        return None

    def suggest(self, sheet_url, text, taken, limit=25):
        wanted = str(text).strip().lower()
        return [e for e in self.ENTRIES
                if not e["banned"] and e["key"] not in taken and wanted in e["key"]][:limit]


class FakeBot:
    """Just enough of DraftBot for ui.announce to work."""

    def __init__(self, conn, channel):
        self.conn = conn
        self.sheets = FakeSheets()
        self.channel = channel
        self.expired = []
        self.timers = timers.TurnTimers(self.on_timer_expired)

    def get_channel(self, channel_id):
        return self.channel if channel_id == self.channel.id else None

    async def on_timer_expired(self, draft_id, token):
        self.expired.append((draft_id, token))
        draft = rules.get_draft(self.conn, draft_id)
        if draft["status"] != "active":
            return
        if (draft["current_round"], draft["current_turn_index"]) != tuple(token):
            return      # stale timer, the turn already moved on
        events = rules.skip_current_player(self.conn, draft_id)
        await ui.announce(self, draft_id, events)


class AnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(4242)
        self.bot = FakeBot(self.conn, self.channel)
        self.draft_id = rules.create_draft(
            self.conn, guild_id=1, channel_id=4242, admin_channel_id=1,
            admin_role_id=7, budget=100, min_picks=2, max_picks=3, base_timer=120,
            players=[(11, "Larry"), (22, "Alex"), (33, "Sarah")],
        )

    def draft(self):
        return rules.get_draft(self.conn, self.draft_id)

    async def test_starting_a_draft_pings_the_first_player(self):
        events = rules.start_draft(self.conn, self.draft())
        await ui.announce(self.bot, self.draft_id, events)

        self.assertIn("<@11>", self.channel.text)
        self.assertIn("Round 1", self.channel.text)
        self.assertIn("2m", self.channel.text)
        # The clock is running and its deadline is stored for restarts.
        self.assertIsNotNone(self.draft()["turn_deadline"])
        self.assertIn(self.draft_id, self.bot.timers.tasks)
        self.bot.timers.cancel_all()

    async def test_pick_is_announced_and_next_player_pinged(self):
        await ui.announce(self.bot, self.draft_id, rules.start_draft(self.conn, self.draft()))
        self.channel.messages.clear()

        _, events = rules.make_pick(self.conn, self.draft(), 11, 1, "Garchomp", 12)
        await ui.announce(self.bot, self.draft_id, events)

        text = self.channel.text
        self.assertIn("Larry", text)
        self.assertIn("Garchomp", text)
        self.assertIn("12", text)
        self.assertIn("<@22>", text)       # Alex is next
        self.bot.timers.cancel_all()

    async def test_timer_expiry_skips_and_announces(self):
        await ui.announce(self.bot, self.draft_id, rules.start_draft(self.conn, self.draft()))
        self.channel.messages.clear()

        token = (self.draft()["current_round"], self.draft()["current_turn_index"])
        await self.bot.on_timer_expired(self.draft_id, token)

        text = self.channel.text
        self.assertIn("skipped", text.lower())
        self.assertIn("Larry", text)
        self.assertIn("makeuppick", text)
        self.assertIn("<@22>", text)       # play continues with Alex

        larry = rules.get_player(self.conn, self.draft_id, 11)
        self.assertEqual(larry["current_timer"], 60)
        self.bot.timers.cancel_all()

    async def test_stale_timer_does_nothing(self):
        await ui.announce(self.bot, self.draft_id, rules.start_draft(self.conn, self.draft()))
        stale_token = (self.draft()["current_round"], self.draft()["current_turn_index"])

        # Larry picks in the split second before his timer fires.
        _, events = rules.make_pick(self.conn, self.draft(), 11, 1, "Garchomp", 12)
        await ui.announce(self.bot, self.draft_id, events)
        self.channel.messages.clear()

        await self.bot.on_timer_expired(self.draft_id, stale_token)

        self.assertEqual(self.channel.messages, [], "a stale timer must not skip anybody")
        alex = rules.get_player(self.conn, self.draft_id, 22)
        self.assertEqual(alex["current_timer"], 120)
        self.bot.timers.cancel_all()

    async def test_queue_auto_pick_is_announced(self):
        await ui.announce(self.bot, self.draft_id, rules.start_draft(self.conn, self.draft()))
        rules.queue_picks(self.conn, self.draft(), 22, 1, ["Garchomp", "Heatran"], [12, 10])
        self.channel.messages.clear()

        _, events = rules.make_pick(self.conn, self.draft(), 11, 1, "Garchomp", 12)
        await ui.announce(self.bot, self.draft_id, events)

        text = self.channel.text
        self.assertIn("queued pick", text)
        self.assertIn("Heatran", text)      # fell through past the stolen Garchomp
        self.assertIn("<@33>", text)        # Sarah is now on the clock
        self.bot.timers.cancel_all()

    async def test_exhausted_queue_message_explains_the_timer_is_safe(self):
        await ui.announce(self.bot, self.draft_id, rules.start_draft(self.conn, self.draft()))
        rules.queue_picks(self.conn, self.draft(), 22, 1, ["Garchomp"], [12])
        self.channel.messages.clear()

        _, events = rules.make_pick(self.conn, self.draft(), 11, 1, "Garchomp", 12)
        await ui.announce(self.bot, self.draft_id, events)

        text = self.channel.text
        self.assertIn("<@22>", text)
        self.assertIn("already been drafted", text)
        self.assertIn("will not be reduced", text)
        self.bot.timers.cancel_all()

    async def test_draft_completion_is_announced(self):
        await ui.announce(self.bot, self.draft_id, rules.start_draft(self.conn, self.draft()))
        for _ in range(9):                      # 3 players x 3 rounds
            live = self.draft()
            if live["status"] != "active":
                break
            player = rules.current_player(self.conn, live)
            _, events = rules.make_pick(self.conn, live, player["user_id"],
                                        live["current_round"],
                                        f"Mon{live['current_round']}{player['id']}", 1)
            await ui.announce(self.bot, self.draft_id, events)

        self.assertIn("draft is complete", self.channel.text.lower())
        self.assertNotIn(self.draft_id, self.bot.timers.tasks)


class TimerMechanicsTests(unittest.IsolatedAsyncioTestCase):
    async def test_timer_fires_once_and_can_be_cancelled(self):
        fired = []

        async def on_expire(draft_id, token):
            fired.append((draft_id, token))

        manager = timers.TurnTimers(on_expire)
        manager.schedule(1, 0.05, (1, 0))
        await asyncio.sleep(0.15)
        self.assertEqual(fired, [(1, (1, 0))])

        manager.schedule(2, 5, (1, 0))
        manager.cancel(2)
        await asyncio.sleep(0.05)
        self.assertEqual(len(fired), 1)

    async def test_rescheduling_replaces_the_old_countdown(self):
        fired = []

        async def on_expire(draft_id, token):
            fired.append(token)

        manager = timers.TurnTimers(on_expire)
        manager.schedule(1, 0.05, ("old", 0))
        manager.schedule(1, 0.05, ("new", 0))
        await asyncio.sleep(0.2)
        self.assertEqual(fired, [("new", 0)], "only the newest timer should fire")


class RestartRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """The behaviour a VM reboot depends on."""

    async def test_deadline_is_stored_and_can_be_recovered(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            conn = database.connect(path)
            channel = FakeChannel(555)
            bot = FakeBot(conn, channel)
            draft_id = rules.create_draft(
                conn, guild_id=1, channel_id=555, admin_channel_id=1, admin_role_id=7,
                budget=100, min_picks=1, max_picks=2, base_timer=120,
                players=[(11, "Larry"), (22, "Alex")],
            )
            await ui.announce(bot, draft_id, rules.start_draft(conn, rules.get_draft(conn, draft_id)))
            bot.timers.cancel_all()
            conn.close()

            # ---- the VM reboots here ----
            conn = database.connect(path)
            draft = rules.get_live_draft(conn, 555)
            self.assertEqual(draft["status"], "active")
            self.assertIsNotNone(draft["turn_deadline"])
            self.assertEqual(rules.current_player(conn, draft)["display_name"], "Larry")

            import time
            remaining = draft["turn_deadline"] - time.time()
            self.assertGreater(remaining, 100, "most of the timer should still be left")
            conn.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)


if __name__ == "__main__":
    unittest.main()


class PokedexRuleTests(unittest.IsolatedAsyncioTestCase):
    """Cost checks, banned Pokémon, and matching the sheet's own spelling."""

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(4242)
        self.bot = FakeBot(self.conn, self.channel)
        self.bot.sheets = StubPokedex()
        self.draft_id = rules.create_draft(
            self.conn, guild_id=1, channel_id=4242, admin_channel_id=1,
            admin_role_id=7, budget=100, min_picks=1, max_picks=3, base_timer=120,
            players=[(11, "Larry"), (22, "Alex")],
            sheet_url="https://docs.google.com/spreadsheets/d/TEST/edit",
        )
        self.draft = rules.get_draft(self.conn, self.draft_id)
        self.cog = DraftingCog(self.bot)

    async def test_the_sheets_spelling_is_what_gets_stored(self):
        # Whatever the player types, the Pokédex spelling is what is used -
        # the spreadsheet's VLOOKUPs and budget formula depend on it.
        for typed in ["garchomp", "  GARCHOMP ", "GarChomp"]:
            with self.subTest(typed=typed):
                name = await self.cog.resolve_pokemon(self.draft, typed, 12)
                self.assertEqual(name, "Garchomp")

    async def test_banned_pokemon_are_refused(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.cog.resolve_pokemon(self.draft, "Bulbasaur", 5)
        self.assertIn("banned", str(caught.exception).lower())

    async def test_wrong_cost_is_refused(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.cog.resolve_pokemon(self.draft, "Garchomp", 9)
        self.assertIn("12", str(caught.exception))

    async def test_unknown_pokemon_is_refused(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.cog.resolve_pokemon(self.draft, "Missingno", 3)
        self.assertIn("not in the Pokédex", str(caught.exception))

    async def test_a_sheet_outage_never_blocks_a_pick(self):
        class BrokenSheets(StubPokedex):
            def lookup(self, sheet_url, pokemon):
                raise RuntimeError("Google is down")

        self.bot.sheets = BrokenSheets()
        name = await self.cog.resolve_pokemon(self.draft, "Anything At All", 7)
        self.assertEqual(name, "Anything At All", "the draft carries on locally")

    async def test_no_sheet_means_no_cost_checking(self):
        self.bot.sheets = FakeSheets()
        name = await self.cog.resolve_pokemon(self.draft, "Homebrew Mon", 3)
        self.assertEqual(name, "Homebrew Mon")


class PermissionFailureTests(unittest.IsolatedAsyncioTestCase):
    """A channel the bot cannot post in must not break the draft.

    This is the case that actually bit in testing: /draft saved the draft as
    active, then the announcement was refused, and the draft was left running
    with nobody on the clock.
    """

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = ForbiddenChannel(4242)
        self.bot = FakeBot(self.conn, self.channel)
        self.draft_id = rules.create_draft(
            self.conn, guild_id=1, channel_id=4242, admin_channel_id=1,
            admin_role_id=7, budget=100, min_picks=1, max_picks=3, base_timer=120,
            players=[(11, "Larry"), (22, "Alex")],
        )

    def draft(self):
        return rules.get_draft(self.conn, self.draft_id)

    async def test_safe_send_reports_refusal_instead_of_raising(self):
        self.assertFalse(await ui.safe_send(self.channel, "hello"))
        self.assertTrue(await ui.safe_send(FakeChannel(1), "hello"))
        self.assertFalse(await ui.safe_send(None, "hello"))

    async def test_a_pick_survives_a_failed_announcement(self):
        await ui.announce(self.bot, self.draft_id, rules.start_draft(self.conn, self.draft()))
        _, events = rules.make_pick(self.conn, self.draft(), 11, 1, "Garchomp", 12)
        await ui.announce(self.bot, self.draft_id, events)      # must not raise

        # The pick is recorded and the turn has moved on, even though nobody
        # could be told. Only the clock waits.
        picks = rules.round_picks(self.conn, self.draft_id, 1)
        self.assertEqual([p["pokemon"] for p in picks], ["Garchomp"])
        self.assertEqual(rules.current_player(self.conn, self.draft())["display_name"], "Alex")
        self.assertEqual(rules.get_player(self.conn, self.draft_id, 11)["budget_remaining"], 88)
        self.assertIsNone(self.draft()["turn_deadline"], "clock paused until posting works")
        self.bot.timers.cancel_all()

    async def test_the_clock_is_paused_rather_than_skipping_players_silently(self):
        # Running a timer nobody can see would skip players for a permission
        # problem that is not theirs. The turn waits instead.
        events = rules.start_draft(self.conn, self.draft())
        await ui.announce(self.bot, self.draft_id, events)

        live = self.draft()
        self.assertEqual(live["status"], "active")
        self.assertIsNone(live["turn_deadline"], "the clock should be paused, not running")
        self.assertNotIn(self.draft_id, self.bot.timers.tasks)

        # Nobody has been skipped or penalised.
        larry = rules.get_player(self.conn, self.draft_id, 11)
        self.assertEqual(larry["current_timer"], 120)
        self.assertEqual(larry["skip_count"], 0)
        self.assertEqual(rules.current_player(self.conn, live)["display_name"], "Larry")

    async def test_the_paused_turn_resumes_once_posting_works(self):
        await ui.announce(self.bot, self.draft_id, rules.start_draft(self.conn, self.draft()))
        self.assertIsNone(self.draft()["turn_deadline"])

        # Permission granted, bot restarted: the turn opens properly.
        working = FakeChannel(4242)
        self.bot.channel = working
        events = rules.begin_turn(self.conn, self.draft_id)
        await ui.announce(self.bot, self.draft_id, events)

        self.assertIsNotNone(self.draft()["turn_deadline"])
        self.assertIn("<@11>", working.text)
        self.bot.timers.cancel_all()

    async def test_a_timer_expiry_still_skips_when_the_bot_cannot_post(self):
        # If a clock was already running when posting broke, the expiry still
        # has to resolve cleanly rather than raising.
        rules.start_draft(self.conn, self.draft())
        token = (self.draft()["current_round"], self.draft()["current_turn_index"])
        await self.bot.on_timer_expired(self.draft_id, token)

        larry = rules.get_player(self.conn, self.draft_id, 11)
        self.assertEqual(larry["current_timer"], 60)
        self.assertEqual(rules.current_player(self.conn, self.draft())["display_name"], "Alex")
        self.bot.timers.cancel_all()
