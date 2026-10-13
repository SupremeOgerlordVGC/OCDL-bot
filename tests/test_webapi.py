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
    ENTRIES = StubPriceList.ENTRIES + [
        {"key": "heatran", "name": "Heatran", "cost": 10, "tera_cost": 13,
         "banned": False, "tera_banned": False, "row": 5},
    ]

    def pool(self, doc_url, tab=None, force=False):
        return self.ENTRIES


class SiteBot(FakeBot):
    """The fake bot, with the drafting cog (which the web API reuses)"""

    guilds = []

    def get_cog(self, name):
        return {"DraftingCog": self.cog, "AdminCog": self.admin_cog}.get(name)

    def get_guild(self, guild_id):
        return None


class WebApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.conn = database.connect(":memory:")
        self.channel = FakeChannel(4242)
        self.bot = SiteBot(self.conn, self.channel)
        self.bot.sheets = PriceList()
        self.bot.cog = DraftingCog(self.bot)
        from cogs.admin import AdminCog
        self.bot.admin_cog = AdminCog(self.bot)
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

    async def test_an_admin_picks_for_whoever_is_on_the_clock(self):
        await self.start_draft()
        status, body = await self.call("/pick", user_id="22", round=1, pokemon="Garchomp")
        self.assertEqual(status, 400, "not Alex's turn, and he isn't an admin")
        status, body = await self.call("/pick", user_id="11", round=1, pokemon="Garchomp", as_admin=True)
        self.assertEqual(status, 200, body)
        larry = rules.get_player(self.conn, self.pool_id, 11)
        self.assertEqual(larry["budget_remaining"], 88)
        self.assertIn("by an admin on draft.ogreclan.com", self.channel.text)

    async def test_an_admin_edits_anyones_pick(self):
        await self.start_draft()
        await self.call("/pick", user_id="11", round=1, pokemon="Garchomp")
        await self.call("/pick", user_id="22", round=1, pokemon="Rotom-Wash")
        status, body = await self.call("/editpick", user_id="11", actor_id="999", round=1, pokemon="Rotom-Wash")
        self.assertEqual(status, 400, "taken by Alex")

        # long after the next pick, which a player couldn't do themselves
        status, body = await self.call("/editpick", user_id="11", actor_id="999", round=1, pokemon="heatran")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["old_pokemon"], body["pokemon"], body["cost"]), ("Garchomp", "Heatran", 10))
        self.assertEqual(rules.get_player(self.conn, self.pool_id, 11)["budget_remaining"], 90)
        self.assertIn("changed on draft.ogreclan.com", self.channel.text)

    async def test_the_price_list_leaves_out_what_is_taken_or_banned(self):
        status, body = await self.call("/prices")
        self.assertEqual([e["name"] for e in body["entries"]], ["Garchomp", "Rotom-Wash", "Heatran"])
        await self.start_draft()
        await self.call("/pick", user_id="11", round=1, pokemon="Garchomp")
        status, body = await self.call("/prices")
        self.assertEqual([e["name"] for e in body["entries"]], ["Rotom-Wash", "Heatran"])

    async def test_a_site_only_league_is_made_and_started_from_the_site(self):
        status, body = await self.client.post("/setup", json={
            "site_only": True, "name": "Site Cup", "mode": "full_season", "budget": 100, "min_picks": 1,
            "max_picks": 2, "timer": 120, "weeks": 1, "visibility": "private",
            "pools": [{"players": [{"user_id": "11", "display_name": "Larry"}, {"user_id": "22", "display_name": "Alex"}]}],
        }, headers={"X-OgreClan-Secret": SECRET}), None
        body = await status.json()
        self.assertTrue(body["ok"], body)
        league = rules.get_league(self.conn, body["league_id"])
        self.assertEqual((league["site_only"], league["visibility"], league["guild_id"]), (1, "private", 0))

        status, started = await self.call("/start", league_id=league["id"], pool="A")
        self.assertEqual(status, 200, started)
        pool = self.conn.execute("SELECT * FROM pool_view WHERE league_id = ?", (league["id"],)).fetchone()
        self.assertEqual(pool["status"], "active")
        self.assertIsNotNone(pool["turn_deadline"], "its clock runs with no Discord channel")

        status, body = await self.call("/setup", name="Discord Cup", guild_id="1", admin_channel_id="2",
                                       min_picks=1, max_picks=2, timer=120, budget=100, pools=[{"channel_id": "3"}])
        self.assertEqual(status, 400)
        self.assertIn("isn't in that Discord server", body["error"])

    async def test_the_tier_list_shows_everything_and_who_has_it(self):
        await self.start_draft()
        await self.call("/pick", user_id="11", round=1, pokemon="Garchomp")
        status, body = await self.call("/tierlist")
        self.assertEqual(status, 200)
        rows = {e["name"]: e for e in body["entries"]}
        self.assertEqual(sorted(rows), ["Bulbasaur", "Garchomp", "Heatran", "Rotom-Wash"])
        larry = rules.get_player(self.conn, self.pool_id, 11)
        self.assertEqual(rows["Garchomp"]["taken_by"], larry["id"])
        self.assertIsNone(rows["Rotom-Wash"]["taken_by"])
        self.assertTrue(rows["Bulbasaur"]["banned"])

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
