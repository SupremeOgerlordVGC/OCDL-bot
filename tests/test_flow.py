"""
End-to-end tests of the Discord layer, using a fake channel instead of a
real connection. These cover the parts that unit tests of draft.py cannot
reach: message rendering, timer scheduling, the skip callback, and restart
recovery.

    python3 -m unittest discover -s tests
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import discord      # noqa: E402
from types import SimpleNamespace   # noqa: E402

import config       # noqa: E402
import database     # noqa: E402
import draft as rules   # noqa: E402
import timers       # noqa: E402
import ui           # noqa: E402
from cogs.admin import AdminCog         # noqa: E402
from cogs.drafting import DraftingCog   # noqa: E402
from cogs.extras import ExtrasCog       # noqa: E402


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
        self.mention = f"<#{channel_id}>"
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
            def lookup(self, doc_url, pokemon, tab=None):
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


class QueuePricingTests(unittest.IsolatedAsyncioTestCase):
    """A queue is priced as strictly as a typed pick.

    Written after a live draft auto-drafted Tyranitar for 7 points when the
    list said 12: /queuepick used to store whatever cost the player typed,
    and the pick then fired unattended at that price.
    """

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(4343)
        self.bot = FakeBot(self.conn, self.channel)
        self.bot.sheets = StubPriceList()
        self.draft_id = make_pool(
            self.conn, 4343, [(11, "Larry"), (22, "Alex")],
            budget=100, min_picks=1, max_picks=3, base_timer=120,
            tera_budget=30,
            doc_url="https://docs.google.com/spreadsheets/d/TEST/edit")
        self.draft = rules.get_pool(self.conn, self.draft_id)
        self.cog = DraftingCog(self.bot)

    async def price(self, names, given=None, captains=None):
        names = list(names)
        costs = await self.cog.resolve_queue(
            self.draft, names, given, captains or [False] * len(names))
        return names, costs

    async def test_a_wrong_cost_is_refused(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.price(["Garchomp"], [7])
        message = str(caught.exception)
        self.assertIn("12", message, "must name the real price")
        self.assertIn("7", message)

    async def test_a_correct_cost_is_accepted(self):
        names, costs = await self.price(["Garchomp"], [12])
        self.assertEqual((names, costs), (["Garchomp"], [12]))

    async def test_no_costs_given_uses_the_list(self):
        names, costs = await self.price(["Garchomp", "Rotom-Wash"])
        self.assertEqual(costs, [12, 8])

    async def test_the_lists_spelling_and_price_are_what_get_stored(self):
        names, costs = await self.price(["  garCHOMP "], [12])
        self.assertEqual((names, costs), (["Garchomp"], [12]))

    async def test_only_the_wrong_entry_is_named(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.price(["Garchomp", "Rotom-Wash"], [12, 3])
        message = str(caught.exception)
        self.assertIn("Rotom-Wash", message)
        self.assertNotIn("Garchomp", message)

    async def test_a_captain_is_priced_at_its_tera_cost(self):
        names, costs = await self.price(["Garchomp"], [11], [True])
        self.assertEqual(costs, [11])
        with self.assertRaises(rules.DraftError) as caught:
            await self.price(["Garchomp"], [12], [True])
        self.assertIn("11", str(caught.exception))

    async def test_banned_pokemon_cannot_be_queued(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.price(["Bulbasaur"], [5])
        self.assertIn("draftable", str(caught.exception))

    async def test_an_unknown_pokemon_is_refused(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.price(["Missingno"], [5])
        self.assertIn("not in this league's tiering list", str(caught.exception))

    async def test_a_sheet_outage_still_trusts_the_player(self):
        # No list to check against, so the queue must not be blocked: the
        # same trade-off /pick makes when Google is unreachable.
        class BrokenSheets(StubPriceList):
            def lookup(self, doc_url, pokemon, tab=None):
                raise RuntimeError("Google is down")

        self.bot.sheets = BrokenSheets()
        self.bot.tiering_tabs.clear()
        names, costs = await self.price(["Garchomp"], [7])
        self.assertEqual(costs, [7])

    async def test_a_queue_stored_through_the_rules_layer_fires_at_the_list_price(self):
        # The whole point: what resolve_queue returns is what gets drafted.
        names, costs = await self.price(["Garchomp"], [12])
        rules.start_pool(self.conn, self.draft)
        live = rules.get_pool(self.conn, self.draft_id)
        rules.queue_picks(self.conn, live, 22, 1, names, costs, [False])
        rules.make_pick(self.conn, live, 11, 1, "Rotom-Wash", 8)

        picks = rules.round_picks(self.conn, self.draft_id, 1)
        auto = [p for p in picks if p["is_auto"]][0]
        self.assertEqual((auto["pokemon"], auto["cost"]), ("Garchomp", 12))
        self.assertEqual(rules.get_player(self.conn, self.draft_id, 22)["budget_remaining"],
                         88)


class FakeInteraction:
    """Enough of an Interaction to run a command callback.

    Worth having: a command body only fails when somebody actually runs it,
    which is how /pick shipped without tera_captain and how two sheet write
    paths stayed broken behind a try/except.
    """

    def __init__(self, bot, channel_id, user_id, display_name="Larry",
                 moderator=False):
        self.client = bot
        self.channel_id = channel_id
        self.channel = bot.get_channel(channel_id)
        self.user = SimpleNamespace(
            id=user_id, display_name=display_name, name=display_name.lower(),
            roles=[],
            guild_permissions=SimpleNamespace(administrator=True) if moderator else None)
        self.guild = None
        self.guild_id = 1
        self.replies = []
        interaction = self

        class Response:
            def is_done(self):
                return bool(interaction.replies)

            async def defer(self, ephemeral=False):
                interaction.deferred = ephemeral

            async def send_message(self, content, ephemeral=False):
                interaction.replies.append(content)

        class FakeMessage:
            def __init__(self):
                self.edited = []

            async def edit(self, **kwargs):
                self.edited.append(kwargs)

        class Followup:
            async def send(self, content=None, ephemeral=False, embed=None,
                           view=None, wait=False):
                interaction.replies.append(content)
                interaction.embeds.append(embed)
                interaction.views.append(view)
                return FakeMessage() if wait else None

        self.deferred = None
        self.embeds = []
        self.views = []
        self.response = Response()
        self.followup = Followup()

    @property
    def text(self):
        return "\n".join(part for part in self.replies if part)

    @property
    def view(self):
        """The view actually attached to a reply, if any."""
        return next((item for item in self.views if item is not None), None)


class MyQueueCommandTests(unittest.IsolatedAsyncioTestCase):
    """Running /myqueue the way a player does."""

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(9300)
        self.bot = FakeBot(self.conn, self.channel)
        self.pool_id = make_pool(
            self.conn, 9300, [(11, "Larry"), (22, "Alex")],
            budget=115, tera_budget=28, min_picks=1, max_picks=4)
        self.cog = DraftingCog(self.bot)
        rules.start_pool(self.conn, self.pool())

    def pool(self):
        return rules.get_pool(self.conn, self.pool_id)

    def interaction(self, user_id=11):
        return FakeInteraction(self.bot, 9300, user_id)

    async def run_command(self, interaction, **kwargs):
        await self.cog.myqueue.callback(self.cog, interaction, **kwargs)
        self.bot.timers.cancel_all()
        return interaction.text

    async def test_a_queue_is_listed_in_priority_order(self):
        rules.queue_picks(self.conn, self.pool(), 11, 2,
                          ["Garchomp", "Rotom-Wash"], [12, 8], [True, False])

        text = await self.run_command(self.interaction())

        self.assertIn("Round 2", text)
        self.assertIn("1. **Garchomp** — 12 points ⭐ Tera Captain", text)
        self.assertIn("2. **Rotom-Wash** — 8 points", text)
        self.assertTrue(self.interaction().deferred is not False)

    async def test_an_empty_queue_says_so_and_suggests_the_command(self):
        text = await self.run_command(self.interaction(), round=3)
        self.assertIn("Nothing queued for Round 3", text)
        self.assertIn("/queuepick round:3", text)

    async def test_one_round_can_be_asked_for(self):
        rules.queue_picks(self.conn, self.pool(), 11, 2, ["Wanted"], [12])
        rules.queue_picks(self.conn, self.pool(), 11, 3, ["Other"], [8])

        text = await self.run_command(self.interaction(), round=3)
        self.assertIn("Other", text)
        self.assertNotIn("Wanted", text)

    async def test_an_unknown_cost_is_not_rendered_as_none(self):
        rules.queue_picks(self.conn, self.pool(), 11, 2, ["Mystery"], [None])
        text = await self.run_command(self.interaction(), round=2)
        self.assertIn("cost unknown", text)
        self.assertNotIn("None", text)

    async def test_a_drafted_round_is_marked(self):
        rules.queue_picks(self.conn, self.pool(), 11, 1, ["Garchomp"], [12])
        rules.make_pick(self.conn, self.pool(), 11, 1, "Garchomp", 12)
        # The queue is consumed by the pick, so ask about the round itself.
        text = await self.run_command(self.interaction(), round=1)
        self.assertIn("already drafted", text)

    async def test_there_is_no_way_to_name_somebody_else(self):
        # Discord shows every option a command declares to everybody who
        # opens it, so even a moderators-only `player` box would tell the
        # players their queues can be read. Looking one up is done from the
        # admin channel instead.
        self.assertNotIn("player", [p.name for p in self.cog.myqueue.parameters])

    async def test_a_moderator_in_a_draft_channel_sees_only_their_own(self):
        rules.queue_picks(self.conn, self.pool(), 22, 2, ["Secret"], [12])
        rules.queue_picks(self.conn, self.pool(), 11, 2, ["Mine"], [12])

        interaction = FakeInteraction(self.bot, 9300, 11, moderator=True)
        text = await self.run_command(interaction)

        self.assertIn("Mine", text)
        self.assertNotIn("Secret", text)


class FakeComponentInteraction:
    """A click on a select menu."""

    def __init__(self, user_id):
        self.user = SimpleNamespace(id=user_id)
        self.edits = []
        self.replies = []
        owner = self

        class Response:
            async def edit_message(self, content=None, embed=None, view=None):
                owner.edits.append({"content": content, "embed": embed, "view": view})

            async def send_message(self, content, ephemeral=False):
                owner.replies.append(content)

        self.response = Response()

    @property
    def last(self):
        return self.edits[-1]


class QueueBrowserTests(unittest.IsolatedAsyncioTestCase):
    """The admin-channel lookup: narrow by draft, then pool, then player."""

    ADMIN_CHANNEL = 9500

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(self.ADMIN_CHANNEL)
        self.bot = FakeBot(self.conn, self.channel)

    def one_pool_league(self, name="Solo", channel_id=9501):
        rules.create_league(
            self.conn, guild_id=1, admin_channel_id=self.ADMIN_CHANNEL, name=name,
            pools=[("A", channel_id, [(11, "Larry"), (22, "Alex")])],
            budget=100, min_picks=1, max_picks=3, base_timer=120)

    def two_pool_league(self, name="双", channel_a=9502, channel_b=9503):
        rules.create_league(
            self.conn, guild_id=1, admin_channel_id=self.ADMIN_CHANNEL, name=name,
            pools=[("A", channel_a, [(31, "Ann"), (32, "Bo")]),
                   ("B", channel_b, [(41, "Cy"), (42, "Di")])],
            budget=100, min_picks=1, max_picks=3, base_timer=120,
            doc_kind="template", doc_url="https://example/sheet")

    def browser(self, round_number=None, owner_id=777):
        leagues = rules.leagues_in_admin_channel(self.conn, self.ADMIN_CHANNEL)
        return ui.QueueBrowser(self.bot, owner_id, leagues, round_number)

    async def run_myqueue(self, channel_id, user_id=777, moderator=True, **kwargs):
        cog = DraftingCog(self.bot)
        interaction = FakeInteraction(self.bot, channel_id, user_id,
                                      moderator=moderator)
        await cog.myqueue.callback(cog, interaction, **kwargs)
        self.bot.timers.cancel_all()
        return interaction

    @staticmethod
    def kinds(browser):
        return [type(item).__name__ for item in browser.children]

    async def test_one_league_and_one_pool_asks_only_for_a_player(self):
        self.one_pool_league()
        browser = self.browser()
        self.assertEqual(self.kinds(browser), ["_PlayerSelect"])
        self.assertEqual([o.label for o in browser.children[0].options],
                         ["Larry", "Alex"])
        self.assertIn("Pick a player", browser.prompt())

    async def test_two_pools_ask_for_the_pool_first(self):
        self.two_pool_league()
        browser = self.browser()
        self.assertEqual(self.kinds(browser), ["_PoolSelect"])
        self.assertIn("which pool", browser.prompt())

    async def test_several_leagues_ask_for_the_draft_first(self):
        self.one_pool_league("Solo")
        self.two_pool_league("Double")
        browser = self.browser()
        self.assertEqual(self.kinds(browser), ["_LeagueSelect"])
        self.assertEqual(sorted(o.label for o in browser.children[0].options),
                         ["Double", "Solo"])

    async def test_choosing_a_player_shows_their_queue(self):
        self.one_pool_league()
        pool = rules.get_live_pool(self.conn, 9501)
        rules.queue_picks(self.conn, pool, 22, 2, ["Garchomp", "Heatran"], [12, 10])

        browser = self.browser()
        select = browser.children[0]
        alex = [p for p in select.players if p["user_id"] == 22][0]
        select._values = [str(alex["id"])]

        click = FakeComponentInteraction(777)
        await select.callback(click)

        embed = click.last["embed"]
        self.assertEqual(embed.title, "Alex's queue")
        self.assertIn("1. **Garchomp** — 12 points", embed.description)
        self.assertIn("2. **Heatran** — 10 points", embed.description)
        self.assertIn("Pool A", embed.footer.text)
        self.assertIn("All rounds", embed.footer.text)

    async def test_a_round_asked_for_is_kept(self):
        self.one_pool_league()
        pool = rules.get_live_pool(self.conn, 9501)
        rules.queue_picks(self.conn, pool, 22, 2, ["Wanted"], [12])
        rules.queue_picks(self.conn, pool, 22, 3, ["Other"], [8])

        browser = self.browser(round_number=3)
        select = browser.children[0]
        alex = [p for p in select.players if p["user_id"] == 22][0]
        select._values = [str(alex["id"])]
        await select.callback(FakeComponentInteraction(777))

        embed = browser.embed()
        self.assertIn("Other", embed.description)
        self.assertNotIn("Wanted", embed.description)
        self.assertIn("Round 3", embed.footer.text)

    async def test_an_empty_queue_says_so_in_the_embed(self):
        self.one_pool_league()
        browser = self.browser()
        select = browser.children[0]
        select._values = [str(select.players[0]["id"])]
        await select.callback(FakeComponentInteraction(777))
        self.assertEqual(browser.embed().description, "Nothing queued.")

    async def test_picking_a_pool_then_a_player_walks_down_the_levels(self):
        self.two_pool_league()
        browser = self.browser()

        pool_select = browser.children[0]
        pool_b = [p for p in pool_select.pools if p["label"] == "B"][0]
        pool_select._values = [str(pool_b["id"])]
        await pool_select.callback(FakeComponentInteraction(777))

        self.assertEqual(self.kinds(browser), ["_PoolSelect", "_PlayerSelect"])
        player_select = browser.children[1]
        self.assertEqual([o.label for o in player_select.options], ["Cy", "Di"])

    async def test_changing_the_league_forgets_the_old_pool_and_player(self):
        self.one_pool_league("Solo")
        self.two_pool_league("Double")
        browser = self.browser()

        league_select = browser.children[0]
        solo = [l for l in browser.leagues if l["name"] == "Solo"][0]
        league_select._values = [str(solo["id"])]
        await league_select.callback(FakeComponentInteraction(777))
        self.assertEqual(browser.pool["label"], "A", "a single pool is filled in")

        double = [l for l in browser.leagues if l["name"] == "Double"][0]
        league_select._values = [str(double["id"])]
        await league_select.callback(FakeComponentInteraction(777))
        self.assertIsNone(browser.pool, "two pools, so it must ask again")
        self.assertIsNone(browser.player)

    async def test_a_moderator_in_the_admin_channel_gets_the_pickers(self):
        self.one_pool_league()
        interaction = await self.run_myqueue(self.ADMIN_CHANNEL)

        browser = interaction.view
        self.assertIsInstance(browser, ui.QueueBrowser)
        self.assertEqual([type(i).__name__ for i in browser.children],
                         ["_PlayerSelect"])
        self.assertIsNone(interaction.embeds[0], "nothing to show until one is chosen")
        self.assertIs(interaction.deferred, True, "a queue lookup is always ephemeral")

    async def test_a_player_outside_a_draft_channel_is_sent_home(self):
        self.one_pool_league()
        with self.assertRaises(rules.DraftError) as caught:
            await self.run_myqueue(self.ADMIN_CHANNEL, user_id=11, moderator=False)
        self.assertIn("in your draft channel", str(caught.exception))

    async def test_a_channel_with_no_draft_at_all_says_so(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.run_myqueue(999999)
        self.assertIn("no draft in this channel", str(caught.exception))

    async def test_a_moderator_in_a_draft_channel_still_gets_their_own_queue(self):
        # The pickers are only for the admin channel; in a draft channel the
        # command behaves exactly as it does for a player.
        self.one_pool_league()
        pool = rules.get_live_pool(self.conn, 9501)
        rules.queue_picks(self.conn, pool, 11, 2, ["Mine"], [12])

        interaction = await self.run_myqueue(9501, user_id=11)
        self.assertIsNone(interaction.view, "no pickers in a draft channel")
        self.assertIn("Mine", interaction.text)

    async def test_somebody_else_cannot_drive_it(self):
        self.one_pool_league()
        browser = self.browser(owner_id=777)
        other = FakeComponentInteraction(888)
        self.assertFalse(await browser.interaction_check(other))
        self.assertIn("not your lookup", other.replies[0])
        self.assertTrue(await browser.interaction_check(FakeComponentInteraction(777)))


class SkipTurnTests(unittest.IsolatedAsyncioTestCase):
    """A moderator ending a turn that nobody is going to use."""

    CHANNEL = 9600

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(self.CHANNEL)
        self.bot = FakeBot(self.conn, self.channel)
        self.pool_id = make_pool(
            self.conn, self.CHANNEL, [(11, "Larry"), (22, "Alex"), (33, "Sam")],
            budget=100, min_picks=1, max_picks=3, base_timer=120)
        self.cog = AdminCog(self.bot)
        rules.start_pool(self.conn, self.pool())

    def pool(self):
        return rules.get_pool(self.conn, self.pool_id)

    def player(self, user_id):
        return rules.get_player(self.conn, self.pool_id, user_id)

    async def skip(self, moderator=True, **kwargs):
        interaction = FakeInteraction(self.bot, self.CHANNEL, 777,
                                      moderator=moderator)
        await self.cog.skipturn.callback(self.cog, interaction, **kwargs)
        self.bot.timers.cancel_all()
        return interaction

    async def test_the_channel_is_told_a_moderator_did_it(self):
        await self.skip()
        # Not "ran out of time": nobody's clock expired here.
        self.assertIn("was **skipped** by a moderator", self.channel.text)
        self.assertNotIn("ran out of time", self.channel.text)

    async def test_the_turn_moves_on_and_a_makeup_is_owed(self):
        interaction = await self.skip()

        larry = self.player(11)
        self.assertEqual(rules.pending_makeup_rounds(self.conn, self.pool_id,
                                                     larry["id"]), [1])
        self.assertEqual(rules.current_player(self.conn, self.pool())["display_name"],
                         "Alex")
        self.assertIn("Skipped **Larry**", interaction.text)

    async def test_by_default_the_timer_is_reduced_like_any_skip(self):
        await self.skip()
        self.assertEqual(self.player(11)["current_timer"], 60)
        self.assertEqual(self.player(11)["skip_count"], 1)

    async def test_it_can_skip_without_a_penalty(self):
        await self.skip(penalty=False)
        self.assertEqual(self.player(11)["current_timer"], 120,
                         "penalty:False must leave the timer alone")
        self.assertIn("left alone", (await self.skip(penalty=False)).text)

    async def test_naming_the_wrong_player_changes_nothing(self):
        wrong = SimpleNamespace(id=22, display_name="Alex")
        with self.assertRaises(rules.DraftError) as caught:
            await self.skip(player=wrong)
        self.assertIn("Larry", str(caught.exception))

        larry = self.player(11)
        self.assertEqual(larry["current_timer"], 120)
        self.assertEqual(larry["skip_count"], 0)
        self.assertEqual(rules.pending_makeup_rounds(self.conn, self.pool_id,
                                                     larry["id"]), [])
        self.assertEqual(rules.current_player(self.conn, self.pool())["display_name"],
                         "Larry", "still their turn")

    async def test_naming_the_right_player_goes_ahead(self):
        await self.skip(player=SimpleNamespace(id=11, display_name="Larry"))
        self.assertEqual(rules.current_player(self.conn, self.pool())["display_name"],
                         "Alex")

    async def test_a_player_cannot_skip_anybody(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.skip(moderator=False)
        self.assertIn("draft moderators", str(caught.exception))
        self.assertEqual(rules.current_player(self.conn, self.pool())["display_name"],
                         "Larry")

    async def test_the_next_player_gets_a_fresh_clock(self):
        await self.skip()
        # The skip must hand the clock over, not leave the draft stalled.
        self.assertIsNotNone(self.pool()["turn_deadline"])
        self.assertIn("you are on the clock", self.channel.text.lower())


class FinishDraftingCommandTests(unittest.IsolatedAsyncioTestCase):
    """The player-facing /finishdrafting, including its confirmation."""

    CHANNEL = 9700

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(self.CHANNEL)
        self.bot = FakeBot(self.conn, self.channel)
        self.pool_id = make_pool(
            self.conn, self.CHANNEL, [(11, "Larry"), (22, "Alex")],
            budget=100, min_picks=2, max_picks=4, base_timer=120)
        self.cog = DraftingCog(self.bot)
        self.admin = AdminCog(self.bot)
        rules.start_pool(self.conn, self.pool())

    def pool(self):
        return rules.get_pool(self.conn, self.pool_id)

    def player(self, user_id):
        return rules.get_player(self.conn, self.pool_id, user_id)

    def take_two_rounds(self):
        for round_number in (1, 2):
            while self.pool()["current_round"] == round_number:
                current = rules.current_player(self.conn, self.pool())
                rules.make_pick(self.conn, self.pool(), current["user_id"],
                                round_number, f"R{round_number}-{current['id']}", 5)

    async def ask(self, user_id=11):
        interaction = FakeInteraction(self.bot, self.CHANNEL, user_id)
        await self.cog.finishdrafting.callback(self.cog, interaction)
        self.bot.timers.cancel_all()
        return interaction

    async def test_below_the_minimum_no_button_is_offered(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.ask()
        self.assertIn("at least **2**", str(caught.exception))

    async def test_the_question_shows_what_it_costs(self):
        self.take_two_rounds()
        interaction = await self.ask()

        self.assertIsInstance(interaction.view, ui.ConfirmFinish)
        text = interaction.text
        self.assertIn("**2** of a possible **4**", text)
        self.assertIn("gives up your last **2**", text)
        self.assertIn("90", text)       # 100 - two 5-point picks
        self.assertEqual(self.player(11)["status"], rules.ACTIVE,
                         "asking must not finish anything by itself")

    async def test_confirming_finishes_and_tells_the_channel(self):
        self.take_two_rounds()
        view = (await self.ask()).view

        click = FakeComponentInteraction(11)
        await view.finish.callback(click)
        self.bot.timers.cancel_all()

        self.assertEqual(self.player(11)["status"], rules.FINISHED)
        self.assertIn("Your draft is finished", click.last["content"])
        self.assertIsNone(click.last["view"], "the buttons must go away")
        self.assertIn("finished drafting", self.channel.text)
        self.assertIn("giving up their last 2", self.channel.text)

    async def test_declining_changes_nothing(self):
        self.take_two_rounds()
        view = (await self.ask()).view

        click = FakeComponentInteraction(11)
        await view.keep.callback(click)

        self.assertEqual(self.player(11)["status"], rules.ACTIVE)
        self.assertIn("Nothing has changed", click.last["content"])
        self.assertNotIn("finished drafting", self.channel.text)

    async def test_somebody_else_cannot_press_it(self):
        self.take_two_rounds()
        view = (await self.ask()).view

        other = FakeComponentInteraction(22)
        self.assertFalse(await view.interaction_check(other))
        self.assertIn("not your draft", other.replies[0])

    async def test_a_moderator_can_reopen_it(self):
        self.take_two_rounds()
        view = (await self.ask()).view
        await view.finish.callback(FakeComponentInteraction(11))
        self.bot.timers.cancel_all()

        interaction = FakeInteraction(self.bot, self.CHANNEL, 777, moderator=True)
        await self.admin.reopendrafting.callback(
            self.admin, interaction, SimpleNamespace(id=11, display_name="Larry"))
        self.bot.timers.cancel_all()

        self.assertEqual(self.player(11)["status"], rules.ACTIVE)
        self.assertIn("drafting again", interaction.text)
        self.assertIn("back into the", self.channel.text)

    async def test_a_player_cannot_reopen_anybody(self):
        self.take_two_rounds()
        view = (await self.ask()).view
        await view.finish.callback(FakeComponentInteraction(11))
        self.bot.timers.cancel_all()

        interaction = FakeInteraction(self.bot, self.CHANNEL, 22, moderator=False)
        with self.assertRaises(rules.DraftError):
            await self.admin.reopendrafting.callback(
                self.admin, interaction, SimpleNamespace(id=11, display_name="Larry"))
        self.assertEqual(self.player(11)["status"], rules.FINISHED)


class TradeValidationCommandTests(unittest.IsolatedAsyncioTestCase):
    """/trade checked against the tiering list before admins see it."""

    CHANNEL = 9800
    ADMIN = 1

    class PriceList(StubPriceList):
        """The stub list plus a spare Pokémon to trade for."""

        ENTRIES = StubPriceList.ENTRIES + [
            {"key": "skarmory", "name": "Skarmory", "cost": 5, "tera_cost": None,
             "banned": False, "tera_banned": True, "row": 9},
        ]

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(self.CHANNEL)
        self.admin_channel = FakeChannel(self.ADMIN)
        self.bot = FakeBot(self.conn, self.channel)
        self.bot.sheets = self.PriceList()

        draft_channel = self.channel
        admin_channel = self.admin_channel

        def get_channel(channel_id):
            if channel_id == admin_channel.id:
                return admin_channel
            return draft_channel if channel_id == draft_channel.id else None

        self.bot.get_channel = get_channel

        self.pool_id = make_pool(
            self.conn, self.CHANNEL, [(11, "Larry"), (22, "Alex")],
            budget=100, min_picks=1, max_picks=4, mode="full_season",
            doc_url="https://docs.google.com/spreadsheets/d/TEST/edit")
        rules.start_pool(self.conn, self.pool())
        # Larry owns Garchomp (12); Rotom-Wash (8) is undrafted.
        rules.make_pick(self.conn, self.pool(), 11, 1, "Garchomp", 12)
        self.cog = ExtrasCog(self.bot)

    def pool(self):
        return rules.get_pool(self.conn, self.pool_id)

    async def propose(self, drops, gets, user_id=11, **kwargs):
        interaction = FakeInteraction(self.bot, self.CHANNEL, user_id)
        await self.cog.trade.callback(self.cog, interaction, drops, gets, **kwargs)
        self.bot.timers.cancel_all()
        return interaction

    def latest_trade(self):
        league_id = self.pool()["league_id"]
        return rules.get_trade(self.conn, league_id, 1)

    async def test_a_misspelling_is_refused_and_named(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.propose("Garchomp", "Rotom-Wsah")
        message = str(caught.exception)
        self.assertIn("Rotom-Wsah", message)
        self.assertIn("tiering list", message)
        self.assertIsNone(self.latest_trade(), "nothing may be filed")

    async def test_the_lists_spelling_is_what_gets_filed(self):
        await self.propose("  garchomp ", "rotom-wash")

        trade = self.latest_trade()
        self.assertEqual(json.loads(trade["drops"]), ["Garchomp"])
        self.assertEqual(json.loads(trade["gets"]), ["Rotom-Wash"])

    async def test_an_and_in_the_list_is_understood(self):
        # "X, Y, and Z" is how people write lists.
        rules.make_pick(self.conn, self.pool(), 22, 1, "Rotom-Wash", 8)
        with self.assertRaises(rules.DraftError) as caught:
            await self.propose("Garchomp, and Rotom-Wash", "Skarmory")
        # Refused for being Alex's Pokémon, not for being called
        # "and Rotom-Wash", which would have been "not on anybody's roster".
        self.assertIn("belongs to", str(caught.exception))

    async def test_a_banned_pokemon_cannot_be_picked_up(self):
        with self.assertRaises(rules.DraftError) as caught:
            await self.propose("Garchomp", "Bulbasaur")
        self.assertIn("Not draftable", str(caught.exception))

    async def test_over_budget_is_refused(self):
        # Larry has 88 left; drop nothing and ask for a 12-point Garchomp
        # they already own is impossible, so use the budget directly.
        self.conn.execute("UPDATE players SET budget_remaining = 2 WHERE user_id = 11")
        self.conn.commit()
        with self.assertRaises(rules.DraftError) as caught:
            await self.propose("nil", "Rotom-Wash")
        self.assertIn("over budget", str(caught.exception))

    async def test_the_admins_are_shown_the_arithmetic(self):
        await self.propose("Garchomp", "Rotom-Wash")
        self.assertIn("Budget: **88 → 92**", self.admin_channel.text)
        self.assertIn("back 12, costs 8", self.admin_channel.text)

    async def test_a_moderator_can_correct_the_spelling(self):
        # The live case: a trade filed as "rotom-wsah" is nearly right, so a
        # moderator fixes it rather than making the player retype it.
        await self.propose("Garchomp", "Rotom-Wash")
        interaction = FakeInteraction(self.bot, self.CHANNEL, 777, moderator=True)
        await self.cog.edittrade.callback(self.cog, interaction, 1,
                                          "garchomp", "skarmory")
        self.bot.timers.cancel_all()

        trade = self.latest_trade()
        self.assertEqual(json.loads(trade["drops"]), ["Garchomp"])
        self.assertEqual(json.loads(trade["gets"]), ["Skarmory"],
                         "the list's capitalisation is what gets stored")
        self.assertIn("Trade #1 updated", interaction.text)
        self.assertIn("corrected by a moderator", self.channel.text)

    async def test_an_edit_is_held_to_the_same_rules(self):
        await self.propose("Garchomp", "Rotom-Wash")
        self.conn.execute("UPDATE players SET budget_remaining = 0 WHERE user_id = 11")
        self.conn.commit()

        interaction = FakeInteraction(self.bot, self.CHANNEL, 777, moderator=True)
        with self.assertRaises(rules.DraftError) as caught:
            await self.cog.edittrade.callback(self.cog, interaction, 1,
                                              "nil", "Rotom-Wash")
        self.assertIn("over budget", str(caught.exception))
        self.assertEqual(json.loads(self.latest_trade()["drops"]), ["Garchomp"],
                         "a refused edit changes nothing")

    async def test_a_player_cannot_edit_trades(self):
        await self.propose("Garchomp", "Rotom-Wash")
        interaction = FakeInteraction(self.bot, self.CHANNEL, 22, moderator=False)
        with self.assertRaises(rules.DraftError) as caught:
            await self.cog.edittrade.callback(self.cog, interaction, 1,
                                              "Garchomp", "Skarmory")
        self.assertIn("draft moderators", str(caught.exception))

    async def test_a_name_is_tidied_when_there_is_no_list(self):
        # No document: nothing to check against, but "rotom-mow" should not
        # reach a roster or a spreadsheet in lower case.
        self.bot.sheets = FakeSheets()
        self.bot.tiering_tabs.clear()
        self.conn.execute("UPDATE leagues SET doc_kind = 'none', doc_url = NULL")
        self.conn.commit()

        await self.propose("garchomp", "rotom-mow")
        trade = self.latest_trade()
        self.assertEqual(json.loads(trade["gets"]), ["Rotom-Mow"])
        self.assertEqual(json.loads(trade["drops"]), ["Garchomp"])


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

    async def test_a_queued_captain_is_written_as_a_captain(self):
        # The live bug: the auto_pick event carried no tera_captain, so the
        # database had the captain right while the sheet's tick box stayed
        # clear. Two real drafts ended up with a silently demoted captain.
        await ui.announce(self.bot, self.pool_id, rules.start_pool(self.conn, self.pool()))
        rules.queue_picks(self.conn, self.pool(), 22, 1, ["Garchomp"], [11], [True])
        _, events = rules.make_pick(self.conn, self.pool(), 11, 1, "Rotom-Wash", 8)
        await ui.announce(self.bot, self.pool_id, events)
        await self.settle()

        written = {pick[4]: pick for pick in self.recorder.picks}
        self.assertIn("Garchomp", written, "the queued pick never reached the sheet")
        self.assertEqual(written["Garchomp"][2], 2, "Alex is coach 2")
        self.assertTrue(written["Garchomp"][5],
                        "a queued Tera Captain must tick the box, like a typed one")
        # And the channel says so, rather than announcing an ordinary pick.
        self.assertIn("Tera Captain", self.channel.text)
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


class TradeWeekCommandTests(unittest.IsolatedAsyncioTestCase):
    """/approvetrade and /postschedule as a moderator runs them: an approved
    trade changes rosters only when the next week is posted."""

    async def asyncSetUp(self):
        from cogs.admin import AdminCog
        from cogs.extras import ExtrasCog
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(9400)
        self.bot = FakeBot(self.conn, self.channel)
        self.pool_id = make_pool(self.conn, 9400, [(11, "Larry"), (22, "Alex")],
                                 max_picks=1, name="Season")
        pool = rules.get_pool(self.conn, self.pool_id)
        rules.start_pool(self.conn, pool)
        rules.make_pick(self.conn, rules.get_pool(self.conn, self.pool_id), 11, 1, "Garchomp", 12)
        rules.make_pick(self.conn, rules.get_pool(self.conn, self.pool_id), 22, 1, "Rotom-Wash", 8)
        rules.generate_fixtures(self.conn, pool["league_id"], weeks=2)
        rules.propose_trade(self.conn, rules.get_pool(self.conn, self.pool_id), 11,
                            ["Garchomp"], ["Rotom-Wash"], partner_user_id=22)
        self.extras = ExtrasCog(self.bot)
        self.admin = AdminCog(self.bot)

    async def asyncTearDown(self):
        self.bot.timers.cancel_all()

    def moderator(self):
        interaction = FakeInteraction(self.bot, 9400, 99, "Mod")
        interaction.user.guild_permissions = SimpleNamespace(administrator=True)
        return interaction

    def owner(self, pokemon):
        row = rules.find_owner(self.conn, self.pool_id, rules.normalize(pokemon))
        return rules.get_player_by_id(self.conn, row["player_id"])["display_name"]

    async def test_a_trade_takes_effect_when_the_next_week_is_posted(self):
        approval = self.moderator()
        await self.extras.approvetrade.callback(self.extras, approval, trade_id=1, name="Season")
        self.assertIn("takes effect when week 1 is posted", approval.text)
        self.assertIn("change from week 1", self.channel.text)
        self.assertEqual(self.owner("Garchomp"), "Larry", "nothing moves on approval")

        posting = self.moderator()
        await self.admin.postschedule.callback(self.admin, posting, week=1, name="Season")
        self.assertIn("Trades in effect from week 1", self.channel.text)
        self.assertIn("1 approved trade(s) are now in effect", posting.text)
        self.assertEqual(self.owner("Garchomp"), "Alex")
        self.assertEqual(self.owner("Rotom-Wash"), "Larry")
        league = rules.get_league(self.conn, rules.get_pool(self.conn, self.pool_id)["league_id"])
        self.assertEqual(league["current_week"], 1)

        # posting the next week doesn't apply it again
        self.channel.messages.clear()
        await self.admin.postschedule.callback(self.admin, self.moderator(), week=2, name="Season")
        self.assertNotIn("Trades in effect", self.channel.text)
        self.assertEqual(self.owner("Garchomp"), "Alex")


class SetupCommandTests(unittest.IsolatedAsyncioTestCase):
    """/setup, as a moderator runs it, and the same builder used by draft.ogreclan.com."""

    async def asyncSetUp(self):
        from cogs.admin import AdminCog
        self.conn = database.connect(":memory:")
        self.admin_channel = FakeChannel(9500)
        self.pool_channel = FakeChannel(9501)
        self.bot = FakeBot(self.conn, self.admin_channel)
        channels = {9500: self.admin_channel, 9501: self.pool_channel}
        self.bot.get_channel = channels.get
        self.cog = AdminCog(self.bot)

    async def asyncTearDown(self):
        self.bot.timers.cancel_all()

    def moderator(self):
        interaction = FakeInteraction(self.bot, 9500, 99, "Mod")
        interaction.user.guild_permissions = SimpleNamespace(administrator=True)
        members = {11: FakeMember(11, "Larry"), 22: FakeMember(22, "Alex")}
        interaction.guild = SimpleNamespace(id=1, owner_id=0, get_member=members.get)
        return interaction

    async def test_setup_creates_a_league_and_says_so(self):
        interaction = self.moderator()
        await self.cog.setup_league.callback(
            self.cog, interaction, name="Spring Cup", draft_channel=SimpleNamespace(id=9501, mention="#pool-a"),
            min_picks=1, max_picks=3, timer=120, mode=SimpleNamespace(value="full_season"),
            players="<@11> <@22>", budget=100, weeks=2)
        league = rules.require_league_by_name(self.conn, 1, "Spring Cup")
        self.assertEqual((league["website"], league["site_only"], league["admin_channel_id"]), (1, 0, 9500))
        self.assertEqual(league["created_by"], 99, "who ran /setup")
        self.assertIn("Spring Cup** has been set up", self.admin_channel.text)
        self.assertIn("1. <@11>", self.admin_channel.text)
        self.assertIn("will be drafted here", self.pool_channel.text)
        self.assertIn("created with 1 pool", interaction.text)
        self.assertEqual(rules.fixture_weeks(self.conn, league["id"]), [1, 2])

    async def test_a_site_only_league_runs_without_discord(self):
        rosters = {"A": [{"user_id": 11, "display_name": "Larry", "username": ""},
                         {"user_id": 22, "display_name": "Alex", "username": ""}]}
        channels = dict(zip(["A"], rules.site_pool_channels(1)))
        league, pools, notes, slots = await self.cog.build_league(
            guild_id=0, admin_channel_id=0, name="Site Cup", rosters=rosters, channels=channels,
            min_picks=1, max_picks=2, timer=120, mode="full_season", budget=100, weeks=1, site_only=True)
        self.assertEqual((league["site_only"], league["website"]), (1, 1))
        self.assertLess(pools[0]["channel_id"], 0)

        # its clock runs, with nothing to post to
        pool = rules.get_pool(self.conn, pools[0]["id"])
        events = rules.start_pool(self.conn, pool)
        await ui.announce(self.bot, pool["id"], events)
        self.assertIsNotNone(rules.get_pool(self.conn, pool["id"])["turn_deadline"], "not paused")
        with self.assertRaises(rules.DraftError):
            rules.set_website(self.conn, league["id"], False)
