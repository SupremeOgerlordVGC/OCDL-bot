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


def make_pool(conn, channel_id, players, **settings):
    """Create a one-pool league for the Discord-layer tests."""
    options = dict(budget=100, min_picks=1, max_picks=3, base_timer=120)
    options.update(settings)
    doc_url = options.pop("doc_url", None)
    rules.create_league(
        conn, guild_id=1, admin_channel_id=1,
        name=options.pop("name", f"League {channel_id}"),
        pools=[("A", channel_id, players)],
        doc_kind="template" if doc_url else "none", doc_url=doc_url,
        **options,
    )
    return rules.get_live_pool(conn, channel_id)["id"]


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


class StubPriceList:
    """A tiny stand-in tiering list, so sheet-backed rules test offline."""

    enabled = True
    can_write = False
    reason = None

    ENTRIES = [
        {"key": "garchomp", "name": "Garchomp", "cost": 12, "tera_cost": 11,
         "banned": False, "tera_banned": False, "row": 2},
        {"key": "rotom-wash", "name": "Rotom-Wash", "cost": 8, "tera_cost": None,
         "banned": False, "tera_banned": True, "row": 3},
        {"key": "bulbasaur", "name": "Bulbasaur", "cost": None, "tera_cost": None,
         "banned": True, "tera_banned": True, "row": 4},
    ]

    def tiering_tab(self, doc_url):
        return "PriceList"

    def lookup(self, doc_url, pokemon, tab=None):
        wanted = " ".join(str(pokemon).strip().lower().split())
        for entry in self.ENTRIES:
            if entry["key"] == wanted:
                return entry
        return None

    def suggest(self, doc_url, text, taken, limit=25, tab=None, tera=False):
        wanted = str(text).strip().lower()
        return [e for e in self.ENTRIES
                if not e["banned"] and e["key"] not in taken
                and not (tera and e["tera_banned"])
                and wanted in e["key"]][:limit]


class FakeBot:
    """Just enough of DraftBot for ui.announce to work."""

    def __init__(self, conn, channel):
        self.conn = conn
        self.sheets = FakeSheets()
        self.channel = channel
        self.expired = []
        self.tiering_tabs = {}
        self.timers = timers.TurnTimers(self.on_timer_expired)

    def get_channel(self, channel_id):
        return self.channel if channel_id == self.channel.id else None

    async def on_timer_expired(self, pool_id, token):
        self.expired.append((pool_id, token))
        draft = rules.get_pool(self.conn, pool_id)
        if draft["status"] != "active":
            return
        if (draft["current_round"], draft["current_turn_index"]) != tuple(token):
            return      # stale timer, the turn already moved on
        events = rules.skip_current_player(self.conn, pool_id)
        await ui.announce(self, pool_id, events)


class AnnouncementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(4242)
        self.bot = FakeBot(self.conn, self.channel)
        self.draft_id = make_pool(
            self.conn, 4242, [(11, "Larry"), (22, "Alex"), (33, "Sarah")],
            budget=100, min_picks=2, max_picks=3, base_timer=120)

    def draft(self):
        return rules.get_pool(self.conn, self.draft_id)

    async def test_starting_a_draft_pings_the_first_player(self):
        events = rules.start_pool(self.conn, self.draft())
        await ui.announce(self.bot, self.draft_id, events)

        self.assertIn("<@11>", self.channel.text)
        self.assertIn("Round 1", self.channel.text)
        self.assertIn("2m", self.channel.text)
        # The clock is running and its deadline is stored for restarts.
        self.assertIsNotNone(self.draft()["turn_deadline"])
        self.assertIn(self.draft_id, self.bot.timers.tasks)
        self.bot.timers.cancel_all()

    async def test_pick_is_announced_and_next_player_pinged(self):
        await ui.announce(self.bot, self.draft_id, rules.start_pool(self.conn, self.draft()))
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
        await ui.announce(self.bot, self.draft_id, rules.start_pool(self.conn, self.draft()))
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
        await ui.announce(self.bot, self.draft_id, rules.start_pool(self.conn, self.draft()))
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
        await ui.announce(self.bot, self.draft_id, rules.start_pool(self.conn, self.draft()))
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
        await ui.announce(self.bot, self.draft_id, rules.start_pool(self.conn, self.draft()))
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
        await ui.announce(self.bot, self.draft_id, rules.start_pool(self.conn, self.draft()))
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
            draft_id = make_pool(conn, 555, [(11, "Larry"), (22, "Alex")], max_picks=2)
            await ui.announce(bot, draft_id, rules.start_pool(conn, rules.get_pool(conn, draft_id)))
            bot.timers.cancel_all()
            conn.close()

            # ---- the VM reboots here ----
            conn = database.connect(path)
            draft = rules.get_live_pool(conn, 555)
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


class PriceListRuleTests(unittest.IsolatedAsyncioTestCase):
    """Cost checks, banned Pokémon, Tera prices, and the sheet's spelling."""

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(4242)
        self.bot = FakeBot(self.conn, self.channel)
        self.bot.sheets = StubPriceList()
        self.draft_id = make_pool(
            self.conn, 4242, [(11, "Larry"), (22, "Alex")],
            budget=100, min_picks=1, max_picks=3, base_timer=120,
            doc_url="https://docs.google.com/spreadsheets/d/TEST/edit")
        self.draft = rules.get_pool(self.conn, self.draft_id)
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
        self.assertIn("not in this league's tiering list", str(caught.exception))

    async def test_a_sheet_outage_never_blocks_a_pick(self):
        class BrokenSheets(StubPriceList):
            def lookup(self, doc_url, pokemon):
                raise RuntimeError("Google is down")

        self.bot.sheets = BrokenSheets()
        name = await self.cog.resolve_pokemon(self.draft, "Anything At All", 7)
        self.assertEqual(name, "Anything At All", "the draft carries on locally")

    async def test_no_sheet_means_no_cost_checking(self):
        self.bot.sheets = FakeSheets()
        name = await self.cog.resolve_pokemon(self.draft, "Homebrew Mon", 3)
        self.assertEqual(name, "Homebrew Mon")

    async def test_a_captain_is_priced_at_its_tera_cost(self):
        tera_pool = make_pool(self.conn, 4343, [(31, "Kim"), (42, "Sam")],
                              tera_budget=28, doc_url="https://example/sheet")
        draft_row = rules.get_pool(self.conn, tera_pool)

        # Garchomp is 12 normally but 11 as a captain.
        self.assertEqual(await self.cog.resolve_pokemon(draft_row, "Garchomp", 12), "Garchomp")
        self.assertEqual(
            await self.cog.resolve_pokemon(draft_row, "Garchomp", 11, tera_captain=True),
            "Garchomp")

        with self.assertRaises(rules.DraftError) as caught:
            await self.cog.resolve_pokemon(draft_row, "Garchomp", 12, tera_captain=True)
        self.assertIn("11", str(caught.exception))

    async def test_tera_banned_pokemon_cannot_be_captains(self):
        tera_pool = make_pool(self.conn, 4344, [(31, "Kim"), (42, "Sam")],
                              tera_budget=28, doc_url="https://example/sheet")
        draft_row = rules.get_pool(self.conn, tera_pool)

        # Fine as a normal pick...
        self.assertEqual(await self.cog.resolve_pokemon(draft_row, "Rotom-Wash", 8),
                         "Rotom-Wash")
        # ...but it has no Tera price.
        with self.assertRaises(rules.DraftError) as caught:
            await self.cog.resolve_pokemon(draft_row, "Rotom-Wash", 8, tera_captain=True)
        self.assertIn("Tera banned", str(caught.exception))

    async def test_captains_are_refused_outside_a_tera_format(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.cog.resolve_pokemon(self.draft, "Garchomp", 11, tera_captain=True)
        self.assertIn("not a Tera format", str(caught.exception))


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
        self.draft_id = make_pool(
            self.conn, 4242, [(11, "Larry"), (22, "Alex")],
            budget=100, min_picks=1, max_picks=3, base_timer=120)

    def draft(self):
        return rules.get_pool(self.conn, self.draft_id)

    async def test_safe_send_reports_refusal_instead_of_raising(self):
        self.assertFalse(await ui.safe_send(self.channel, "hello"))
        self.assertTrue(await ui.safe_send(FakeChannel(1), "hello"))
        self.assertFalse(await ui.safe_send(None, "hello"))

    async def test_a_pick_survives_a_failed_announcement(self):
        await ui.announce(self.bot, self.draft_id, rules.start_pool(self.conn, self.draft()))
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
        events = rules.start_pool(self.conn, self.draft())
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
        await ui.announce(self.bot, self.draft_id, rules.start_pool(self.conn, self.draft()))
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
        rules.start_pool(self.conn, self.draft())
        token = (self.draft()["current_round"], self.draft()["current_turn_index"])
        await self.bot.on_timer_expired(self.draft_id, token)

        larry = rules.get_player(self.conn, self.draft_id, 11)
        self.assertEqual(larry["current_timer"], 60)
        self.assertEqual(rules.current_player(self.conn, self.draft())["display_name"], "Alex")
        self.bot.timers.cancel_all()


class SheetWritePathTests(unittest.IsolatedAsyncioTestCase):
    """The sheet write path is wrapped in try/except so a Google outage can
    never undo a pick. That also means a wrong call signature would fail
    silently, so these tests check the arguments that actually go out."""

    class RecordingSheets(StubPriceList):
        can_write = True

        def __init__(self):
            self.picks = []
            self.syncs = []

        def roster_tab_name(self, tera_format):
            return "Tera Rosters" if tera_format else "Rosters"

        def write_pick(self, doc_url, tera_format, coach_number, round_number,
                       pokemon, tera_captain=False):
            self.picks.append((doc_url, tera_format, coach_number, round_number,
                               pokemon, tera_captain))
            return True

        def full_sync(self, doc_url, tera_format, coaches, blocks):
            self.syncs.append((doc_url, tera_format, coaches, blocks))
            return {"coaches": len(coaches), "rosters": len(blocks),
                    "tab": self.roster_tab_name(tera_format)}

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(9100)
        self.bot = FakeBot(self.conn, self.channel)
        self.recorder = self.RecordingSheets()
        self.bot.sheets = self.recorder
        self.pool_id = make_pool(
            self.conn, 9100, [(11, "Larry"), (22, "Alex")],
            budget=115, tera_budget=28, max_picks=3,
            doc_url="https://docs.google.com/spreadsheets/d/TEST/edit",
        )

    def pool(self):
        return rules.get_pool(self.conn, self.pool_id)

    async def settle(self):
        """Let the fire-and-forget sheet tasks finish.

        They hand off to a worker thread, so a short real wait is needed -
        yielding to the event loop alone is not always enough. The draft's
        turn timers are long-running tasks, so this cannot simply wait for
        every pending task to finish.
        """
        self.bot.timers.cancel_all()
        await asyncio.sleep(0.05)

    async def test_a_pick_is_written_with_the_right_arguments(self):
        await ui.announce(self.bot, self.pool_id, rules.start_pool(self.conn, self.pool()))
        _, events = rules.make_pick(self.conn, self.pool(), 11, 1, "Garchomp", 11,
                                    tera_captain=True)
        await ui.announce(self.bot, self.pool_id, events)
        await self.settle()

        self.assertTrue(self.recorder.picks, "nothing was written to the sheet")
        doc_url, tera_format, coach, round_number, pokemon, captain = self.recorder.picks[0]
        self.assertIn("TEST", doc_url)
        self.assertTrue(tera_format, "a Tera league writes to the Tera Rosters tab")
        self.assertEqual(coach, 1, "Larry is coach 1, so roster block 1")
        self.assertEqual(round_number, 1)
        self.assertEqual(pokemon, "Garchomp")
        self.assertTrue(captain)
        self.bot.timers.cancel_all()

    async def test_a_normal_pick_is_not_flagged_as_a_captain(self):
        await ui.announce(self.bot, self.pool_id, rules.start_pool(self.conn, self.pool()))
        _, events = rules.make_pick(self.conn, self.pool(), 11, 1, "Garchomp", 12)
        await ui.announce(self.bot, self.pool_id, events)
        await self.settle()

        self.assertFalse(self.recorder.picks[0][5])
        self.bot.timers.cancel_all()

    async def test_a_full_sync_sends_coaches_and_blocks(self):
        rules.start_pool(self.conn, self.pool())
        rules.make_pick(self.conn, self.pool(), 11, 1, "Garchomp", 11, tera_captain=True)

        ui.resync_sheet(self.bot, self.pool_id)
        await self.settle()

        self.assertTrue(self.recorder.syncs)
        doc_url, tera_format, coaches, blocks = self.recorder.syncs[0]
        self.assertTrue(tera_format)
        self.assertEqual(coaches[1], ("Larry", "A"))
        self.assertEqual(blocks[1][0], ("Garchomp", True))
        self.bot.timers.cancel_all()

    async def test_nothing_is_written_for_a_tiering_document(self):
        # A tiering sheet is read-only: it has no roster blocks to fill in.
        self.conn.execute("UPDATE leagues SET doc_kind = 'tiering'")
        self.conn.commit()

        rules.start_pool(self.conn, self.pool())
        _, events = rules.make_pick(self.conn, self.pool(), 11, 1, "Garchomp", 12)
        await ui.announce(self.bot, self.pool_id, events)
        ui.resync_sheet(self.bot, self.pool_id)
        await self.settle()

        self.assertEqual(self.recorder.picks, [])
        self.assertEqual(self.recorder.syncs, [])
        self.bot.timers.cancel_all()


class FakeMember:
    def __init__(self, user_id, display_name, username=None, bot=False):
        self.id = user_id
        self.display_name = display_name
        self.name = username or display_name.lower()
        self.bot = bot


class FakeRole:
    def __init__(self, members):
        self.members = members
        self.mention = "@TestRole"


class RosterFromRoleTests(unittest.IsolatedAsyncioTestCase):
    """Players can be given as a role, which the bot shuffles itself."""

    def test_entries_carry_both_names_for_matching(self):
        role = FakeRole([FakeMember(7, "Larry", username="bottledducky")])
        entry = ui.players_from_role(role)[0]
        self.assertEqual(entry["user_id"], 7)
        self.assertEqual(entry["display_name"], "Larry")
        self.assertEqual(entry["username"], "bottledducky")

    def test_a_role_becomes_a_shuffled_player_list(self):
        import random
        members = [FakeMember(i, f"P{i}") for i in range(1, 9)]
        role = FakeRole(members)

        first = ui.players_from_role(role, rng=random.Random(1))
        second = ui.players_from_role(role, rng=random.Random(2))

        self.assertEqual(len(first), 8)
        self.assertEqual({entry["user_id"] for entry in first}, set(range(1, 9)))
        self.assertNotEqual(first, second, "the order should be random")

    def test_bots_in_the_role_are_skipped(self):
        role = FakeRole([FakeMember(1, "Larry"), FakeMember(2, "Helper", bot=True)])
        names = [entry["display_name"] for entry in ui.players_from_role(role)]
        self.assertEqual(names, ["Larry"])

    def test_an_empty_role_explains_the_members_intent(self):
        with self.assertRaises(rules.DraftError) as caught:
            ui.players_from_role(FakeRole([]))
        self.assertIn("Server Members Intent", str(caught.exception))

    async def test_mentions_keep_the_order_they_were_typed(self):
        rosters, shuffled = await ui.roster_from_options(
            None, "<@11> <@22> <@33>", None, "A")
        self.assertEqual([entry["user_id"] for entry in rosters], [11, 22, 33])
        self.assertFalse(shuffled)

    async def test_a_role_reports_that_it_was_shuffled(self):
        role = FakeRole([FakeMember(i, f"P{i}") for i in range(1, 5)])
        rosters, shuffled = await ui.roster_from_options(None, None, role, "A")
        self.assertEqual(len(rosters), 4)
        self.assertTrue(shuffled)

    async def test_giving_both_is_refused(self):
        role = FakeRole([FakeMember(1, "Larry")])
        with self.assertRaises(rules.DraftError) as caught:
            await ui.roster_from_options(None, "<@11>", role, "A")
        self.assertIn("not both", str(caught.exception))

    async def test_giving_neither_is_refused(self):
        with self.assertRaises(rules.DraftError) as caught:
            await ui.roster_from_options(None, None, None, "B")
        self.assertIn("needs players", str(caught.exception))
