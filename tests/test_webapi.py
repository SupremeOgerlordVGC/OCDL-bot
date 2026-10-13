"""
Tests of the private web API for draft.ogreclan.com (webapi.py): picks and queues made on the site go through the
same rules, timers, announcements and price checks as the slash commands.

    python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from aiohttp.test_utils import TestClient, TestServer   # noqa: E402

import database     # noqa: E402
import draft as rules   # noqa: E402
import ui           # noqa: E402
import webapi       # noqa: E402
from cogs.drafting import DraftingCog   # noqa: E402
from tests.test_flow import FakeBot, FakeChannel, StubPriceList, make_pool   # noqa: E402

SECRET = "a-test-secret-that-is-long-enough"


class PriceList(StubPriceList):
    def pool(self, doc_url, tab=None, force=False):
        return self.ENTRIES


class SiteBot(FakeBot):
    """The fake bot, with the drafting cog (which the web API reuses)"""

    def get_cog(self, name):
        return self.cog if name == "DraftingCog" else None


class WebApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(4242)
        self.bot = SiteBot(self.conn, self.channel)
        self.bot.sheets = PriceList()
        self.bot.cog = DraftingCog(self.bot)
        self.pool_id = make_pool(
            self.conn, 4242, [(11, "Larry"), (22, "Alex")], budget=100, min_picks=1, max_picks=3, base_timer=120,
            doc_url="https://docs.google.com/spreadsheets/d/TEST/edit")
        self.league_id = rules.get_pool(self.conn, self.pool_id)["league_id"]
        self.client = TestClient(TestServer(webapi.WebApi(self.bot, 0, SECRET).app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.bot.timers.cancel_all()

    async def call(self, path, secret=SECRET, **body):
        body = {"league_id": self.league_id, "pool": "A", **body}
        response = await self.client.post(path, json=body, headers={"X-OgreClan-Secret": secret})
        return response.status, await response.json()

    async def start_draft(self):
        events = rules.start_pool(self.conn, rules.get_pool(self.conn, self.pool_id))
        await ui.announce(self.bot, self.pool_id, events)

    async def test_only_with_the_secret(self):
        status, body = await self.call("/prices", secret="wrong")
        self.assertEqual(status, 403)

    async def test_a_pick_on_the_site_is_a_pick(self):
        await self.start_draft()
        status, body = await self.call("/pick", user_id="22", round=1, pokemon="garchomp")
        self.assertEqual(status, 400, "not Alex's turn")
        self.assertNotIn("❌", body["error"])

        status, body = await self.call("/pick", user_id="11", round=1, pokemon="garchomp")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["pokemon"], "Garchomp", "the price list's spelling")
        self.assertEqual(body["cost"], 12, "priced from the list when no cost is given")
        roster = rules.get_player(self.conn, self.pool_id, 11)
        self.assertEqual(roster["budget_remaining"], 88)
        self.assertIn("Garchomp", self.channel.text, "announced in the draft's channel")
        draft = rules.get_pool(self.conn, self.pool_id)
        self.assertIsNotNone(draft["turn_deadline"], "the next player's timer started")

        status, body = await self.call("/pick", user_id="22", round=1, pokemon="Garchomp")
        self.assertEqual(status, 400)
        status, body = await self.call("/pick", user_id="22", round=1, pokemon="Rotom-Wash", cost=5)
        self.assertEqual(status, 400, "a wrong cost is refused, as for /pick")
        self.assertIn("8", body["error"])

    async def test_the_price_list_leaves_out_what_is_taken_or_banned(self):
        status, body = await self.call("/prices")
        self.assertEqual([e["name"] for e in body["entries"]], ["Garchomp", "Rotom-Wash"])
        await self.start_draft()
        await self.call("/pick", user_id="11", round=1, pokemon="Garchomp")
        status, body = await self.call("/prices")
        self.assertEqual([e["name"] for e in body["entries"]], ["Rotom-Wash"])

    async def test_queues_are_kept_private_per_player(self):
        status, body = await self.call("/queue/set", user_id="22", round=1, pokemon=["garchomp", "rotom-wash"])
        self.assertEqual(status, 200, body)
        self.assertEqual([(e["pokemon"], e["cost"]) for e in body["entries"]], [("Garchomp", 12), ("Rotom-Wash", 8)])

        status, body = await self.call("/queue", user_id="22")
        self.assertEqual(body["rounds"][0]["round"], 1)
        self.assertEqual([e["pokemon"] for e in body["rounds"][0]["entries"]], ["Garchomp", "Rotom-Wash"])
        status, body = await self.call("/queue", user_id="11")
        self.assertEqual(body["rounds"], [], "Larry sees only his own")

        status, body = await self.call("/queue/set", user_id="22", round=1, pokemon=["bulbasaur"])
        self.assertEqual(status, 400, "banned")
        status, body = await self.call("/queue/cancel", user_id="22", round=1)
        self.assertEqual(body["removed"], 2)

    async def test_a_queued_pick_fires_when_the_turn_comes(self):
        await self.call("/queue/set", user_id="11", round=1, pokemon=["Rotom-Wash"])
        await self.start_draft()
        roster = self.conn.execute("SELECT pokemon FROM rosters WHERE pool_id = ?", (self.pool_id,)).fetchall()
        self.assertEqual([row["pokemon"] for row in roster], ["Rotom-Wash"], "drafted by itself, as with /queuepick")


if __name__ == "__main__":
    unittest.main()
