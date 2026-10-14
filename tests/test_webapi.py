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

    def approve_host(self, user_id=77):
        """Hosting on the site is by approval, so a test host needs one."""
        rules.apply_to_host(self.conn, user_id, "Host", "a league")
        rules.decide_host(self.conn, user_id, True, decided_by=1)

    async def test_hosting_on_the_site_needs_approval_first(self):
        # The bot asks nobody's permission; the website does.
        body = {"site_only": True, "name": "Unapproved", "mode": "full_season",
                "budget": 100, "min_picks": 1, "max_picks": 2, "timer": 120,
                "created_by": 77,
                "pools": [{"players": [{"user_id": "11", "display_name": "Larry"},
                                       {"user_id": "22", "display_name": "Alex"}]}]}
        response = await self.client.post("/setup", json=body,
                                          headers={"X-OgreClan-Secret": SECRET})
        answer = await response.json()
        self.assertFalse(answer["ok"])
        self.assertIn("needs approval first", answer["error"])

        rules.apply_to_host(self.conn, 77, "Host", "a league")
        response = await self.client.post("/setup", json=body,
                                          headers={"X-OgreClan-Secret": SECRET})
        answer = await response.json()
        self.assertIn("still being looked at", answer["error"])

        rules.decide_host(self.conn, 77, True, decided_by=1)
        response = await self.client.post("/setup", json=body,
                                          headers={"X-OgreClan-Secret": SECRET})
        answer = await response.json()
        self.assertTrue(answer["ok"], answer)

    async def test_an_application_can_be_made_and_read_back(self):
        status, body = await self.call("/hosts/apply", user_id=77,
                                       display_name="Host", about="a VGC league")
        self.assertEqual(body["status"], "pending")
        status, body = await self.call("/hosts/status", user_id=77)
        self.assertEqual((body["status"], body["may_host"]), ("pending", False))

        rules.decide_host(self.conn, 77, True, decided_by=1)
        status, body = await self.call("/hosts/status", user_id=77)
        self.assertEqual((body["status"], body["may_host"]), ("approved", True))

    async def test_a_site_only_league_is_made_and_started_from_the_site(self):
        self.approve_host()
        status, body = await self.client.post("/setup", json={
            "site_only": True, "name": "Site Cup", "mode": "full_season", "budget": 100, "min_picks": 1,
            "max_picks": 2, "timer": 120, "weeks": 1, "visibility": "private",
            "created_by": 77,
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
                                       created_by=77,
                                       min_picks=1, max_picks=2, timer=120, budget=100, pools=[{"channel_id": "3"}])
        self.assertEqual(status, 400)
        self.assertIn("isn't in that Discord server", body["error"])

    async def test_a_site_only_season_trades_and_posts_weeks_on_the_site(self):
        self.approve_host()
        setup = await self.client.post("/setup", json={
            "site_only": True, "name": "Site Season", "mode": "full_season", "created_by": 77,
            "budget": 100, "min_picks": 1,
            "max_picks": 1, "timer": 120, "weeks": 1,
            "pools": [{"players": [{"user_id": "11", "display_name": "Larry"}, {"user_id": "22", "display_name": "Alex"}]}],
        }, headers={"X-OgreClan-Secret": SECRET})
        league_id = (await setup.json())["league_id"]
        await self.call("/start", league_id=league_id)
        # no spreadsheet, so picks carry their cost
        status, body = await self.call("/pick", league_id=league_id, user_id="11", round=1, pokemon="Garchomp", cost=12)
        self.assertEqual(status, 200, body)
        await self.call("/pick", league_id=league_id, user_id="22", round=1, pokemon="Rotom-Wash", cost=8)

        status, body = await self.call("/trade", league_id=league_id, user_id="11", drops=["Garchomp"], gets=["Heatran"])
        self.assertEqual(status, 200, body)
        number = body["trade_number"]
        status, body = await self.call("/approvetrade", league_id=league_id, trade_number=number, user_id="99")
        self.assertEqual((status, body["week"]), (200, 1))
        pool = self.conn.execute("SELECT id FROM pools WHERE league_id = ?", (league_id,)).fetchone()["id"]
        self.assertIsNotNone(rules.find_owner(self.conn, pool, "garchomp"), "not until the week is posted")

        status, body = await self.call("/postweek", league_id=league_id, week=1)
        self.assertEqual((status, body["matches"], body["waiting"]), (200, 1, 1), body)
        self.assertIsNotNone(rules.find_owner(self.conn, pool, "garchomp"),
                             "posting a week no longer moves anything")
        self.assertEqual(rules.get_league(self.conn, league_id)["current_week"], 1)
        status, body = await self.call("/postweek", league_id=league_id, week=9)
        self.assertEqual(status, 400)

        # Reporting the match is what puts it into effect, and not before the
        # match is over: a best-of-three takes two games.
        status, body = await self.call("/gameresult", league_id=league_id, week=1, match=1, game=1,
                                       source="battle", winner_user_id="11")
        self.assertEqual((status, body["trades_applied"]), (200, 0), body)
        self.assertIsNotNone(rules.find_owner(self.conn, pool, "garchomp"), "one game of three")

        status, body = await self.call("/gameresult", league_id=league_id, week=1, match=1, game=2,
                                       source="battle", winner_user_id="11")
        self.assertEqual((status, body["trades_applied"]), (200, 1), body)
        self.assertIsNone(rules.find_owner(self.conn, pool, "garchomp"), "2-0 ends the match")
        self.assertIsNotNone(rules.find_owner(self.conn, pool, "heatran"))

        status, body = await self.call("/trade", league_id=league_id, user_id="22", drops=[], gets=["Bulbasaur"])
        self.assertEqual(status, 200, "drops can be nothing")
        status, body = await self.call("/rejecttrade", league_id=league_id, trade_number=body["trade_number"])
        self.assertEqual(status, 200)

    async def test_results_are_reported_from_the_site(self):
        import results
        await self.start_draft()
        await self.call("/pick", user_id="11", round=1, pokemon="Garchomp")
        await self.call("/pick", user_id="22", round=1, pokemon="Rotom-Wash")
        rules.generate_fixtures(self.conn, self.league_id, weeks=1)

        # a battle played with Ready: both sides, the species as the battle names them
        status, body = await self.call("/gameresult", week=1, match=1, game=1, source="battle", winner_user_id="11",
                                       replay="https://replay.ogreclan.com/gen9-1",
                                       stats=[{"user_id": "11", "pokemon": "Garchomp", "kills": 2, "deaths": 0},
                                              {"user_id": "22", "pokemon": "rotom-wash", "kills": 0, "deaths": 1}])
        self.assertEqual(status, 200, body)
        self.assertEqual((body["wins"], body["decided"]), ({"1": 1}, False),
                         "the series, as the site shows it")
        view = results.match_view(self.conn, self.league_id, 1, 1)
        larry = rules.get_player(self.conn, self.pool_id, 11)
        alex = rules.get_player(self.conn, self.pool_id, 22)
        self.assertEqual(view["games"][0]["stats"][alex["id"]], [("Rotom-Wash", 0, 1)], "the roster's spelling")
        self.assertEqual(view["wins"], {larry["id"]: 1})
        self.assertIsNone(view["winner_id"], "1-0 of a best-of-three decides nothing")

        # a player's own side by hand: someone else's Pokémon are ignored
        status, body = await self.call("/gameresult", week=1, user_id="22", game=2, source="manual", winner_user_id="22",
                                       stats=[{"user_id": "22", "pokemon": "Rotom-Wash", "kills": 1, "deaths": 0},
                                              {"user_id": "11", "pokemon": "Garchomp", "kills": 9, "deaths": 9}])
        self.assertEqual(status, 200, body)
        game = results.match_view(self.conn, self.league_id, 1, 1)["games"][1]
        self.assertEqual(list(game["stats"]), [alex["id"]])

        status, body = await self.call("/champions", week=1, user_id="11")
        self.assertFalse(body["champions"])
        status, body = await self.call("/champions", week=1, user_id="22")
        self.assertTrue(body["champions"])
        status, body = await self.call("/pokepaste", week=1, user_id="11", url="https://pokepast.es/abc")
        self.assertEqual(status, 200, body)

    async def test_staff_can_award_a_match_from_the_site(self):
        import results
        await self.start_draft()
        await self.call("/pick", user_id="11", round=1, pokemon="Garchomp")
        await self.call("/pick", user_id="22", round=1, pokemon="Rotom-Wash")
        rules.generate_fixtures(self.conn, self.league_id, weeks=1)
        alex = rules.get_player(self.conn, self.pool_id, 22)

        status, body = await self.call("/setresult", week=1, match=1, winner_user_id="22",
                                       reason="Larry never showed")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["winner_id"], body["walkover"], body["note"]),
                         (alex["id"], True, "Larry never showed"))
        self.assertIn("was awarded the match — Larry never showed", self.channel.text)
        league = rules.get_league(self.conn, self.league_id)
        self.assertTrue(results.match_finished(self.conn, league, 1, 1))

        status, body = await self.call("/setresult", week=1, match=1)
        self.assertEqual((status, body["winner_id"], body["walkover"]), (200, None, False),
                         "no winner given takes the award back")
        self.assertFalse(results.match_finished(self.conn, league, 1, 1))

        status, body = await self.call("/setresult", week=1, match=1, winner_user_id="77")
        self.assertEqual(status, 400)
        self.assertIn("one of the match's two players", body["error"])

    async def test_a_tera_captain_can_be_nominated_from_the_site(self):
        # The site's league is not a Tera format, so this one gets its own.
        tera_pool = make_pool(self.conn, 4343, [(11, "Larry"), (22, "Alex")], budget=100,
                              tera_budget=28, min_picks=1, max_picks=2, base_timer=120,
                              doc_url="https://docs.google.com/spreadsheets/d/TEST/edit")
        league_id = rules.get_pool(self.conn, tera_pool)["league_id"]
        rules.start_pool(self.conn, rules.get_pool(self.conn, tera_pool))
        rules.make_pick(self.conn, rules.get_pool(self.conn, tera_pool), 11, 1, "Garchomp", 12)
        larry = rules.get_player(self.conn, tera_pool, 11)

        status, body = await self.call("/teracaptain", league_id=league_id, user_id="11",
                                       pokemon="garchomp")
        self.assertEqual(status, 200, body)
        self.assertEqual((body["pokemon"], body["tera_captain"], body["cost"]),
                         ("Garchomp", True, 11))
        self.assertEqual(body["tera_budget_remaining"], 28 - 11)
        self.assertEqual(
            rules.find_owner(self.conn, tera_pool, "garchomp")["tera_captain"], 1)

        status, body = await self.call("/teracaptain", league_id=league_id, user_id="11",
                                       pokemon="garchomp", on=False)
        self.assertEqual(status, 400, "only staff take one off")
        status, body = await self.call("/teracaptain", league_id=league_id, user_id="11",
                                       pokemon="garchomp", on=False, as_admin=True)
        self.assertEqual((status, body["tera_captain"], body["cost"]), (200, False, 12))
        self.assertEqual(
            rules.get_player(self.conn, tera_pool, 11)["tera_budget_remaining"], 28)
        self.assertEqual(larry["id"], rules.get_player(self.conn, tera_pool, 11)["id"])

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
