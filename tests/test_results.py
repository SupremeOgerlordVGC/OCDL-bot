"""
Tests for match results (results.py): games reported by battle, replay or hand, Champions and Pokepastes.

    python3 -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database         # noqa: E402
import draft            # noqa: E402
import results          # noqa: E402
from tests.test_draft import make_draft, user   # noqa: E402


class ResultsTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        pool = make_draft(self.conn, channel_id=7000, players=2, min_picks=1, max_picks=2)
        for name, cost in [("Mega Eternal-Floette", 20), ("Incineroar", 15), ("Garchomp", 12), ("Rotom-Wash", 8)]:
            live = draft.get_pool(self.conn, pool["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"], name, cost)
        self.league_id = pool["league_id"]
        draft.generate_fixtures(self.conn, self.league_id, weeks=1)
        self.one = draft.get_player(self.conn, pool["id"], user(1))
        self.two = draft.get_player(self.conn, pool["id"], user(2))
        self.match = results.find_match(self.conn, self.league_id, 1, self.one["id"])["match_number"]

    def test_roster_names_from_battle_species(self):
        roster = ["Mega Eternal-Floette", "Garchomp", "Urshifu-Rapid-Strike"]
        self.assertEqual(results.roster_name("Floette-Eternal", roster), "Mega Eternal-Floette")
        self.assertEqual(results.roster_name("garchomp", roster), "Garchomp")
        self.assertEqual(results.roster_name("Urshifu-Rapid-Strike", roster), "Urshifu-Rapid-Strike")
        self.assertEqual(results.roster_name("Pikachu", roster), "Pikachu", "not drafted: kept as the battle says")

    def test_each_player_reports_their_own_side(self):
        one_roster = results.roster_of(self.conn, self.one)
        # a snake draft: one has Floette and Rotom-Wash, two has Incineroar and Garchomp
        stats = results.parse_kd("Mega Eternal-Floette 2-0, rotom-wash 1/1", one_roster)
        self.assertEqual(stats, [("Mega Eternal-Floette", 2, 0), ("Rotom-Wash", 1, 1)])
        with self.assertRaises(draft.DraftError):
            results.parse_kd("Pikachu 1-0", one_roster)
        with self.assertRaises(draft.DraftError):
            results.parse_kd("Garchomp two kills", one_roster)

        results.record_game(self.conn, self.league_id, 1, self.match, 1, "manual", winner_id=self.one["id"],
                            stats={self.one["id"]: stats}, only_player=self.one["id"])
        two_stats = results.parse_kd("Incineroar 0-1, Garchomp 1-1", results.roster_of(self.conn, self.two))
        view = results.record_game(self.conn, self.league_id, 1, self.match, 1, "manual",
                                   stats={self.two["id"]: two_stats, self.one["id"]: []}, only_player=self.two["id"])
        game = view["games"][0]
        self.assertEqual(set(game["stats"]), {self.one["id"], self.two["id"]}, "the other side is left alone")
        self.assertEqual(game["winner_id"], self.one["id"], "kept from the first report")
        self.assertEqual(view["totals"][self.one["id"]]["Rotom-Wash"], [1, 1])

    def test_the_set_winner_is_whoever_won_more_games(self):
        record = lambda game, winner: results.record_game(  # noqa: E731
            self.conn, self.league_id, 1, self.match, game, "replay", winner_id=winner, stats={})
        self.assertEqual(record(1, self.one["id"])["winner_id"], self.one["id"])
        self.assertIsNone(record(2, self.two["id"])["winner_id"], "level at 1-1")
        self.assertEqual(record(3, self.two["id"])["winner_id"], self.two["id"])
        view = results.clear_game(self.conn, self.league_id, 1, self.match, 3)
        self.assertIsNone(view["winner_id"])
        self.assertEqual(len(view["games"]), 2)

    def test_champions_needs_both_players_and_takes_pokepastes(self):
        view = results.ask_champions(self.conn, self.league_id, 1, self.match, self.one["id"])
        self.assertFalse(view["champions"])
        view = results.ask_champions(self.conn, self.league_id, 1, self.match, self.two["id"])
        self.assertTrue(view["champions"])
        self.assertFalse(results.ask_champions(self.conn, self.league_id, 1, self.match, self.two["id"], on=False)["champions"])

        with self.assertRaises(draft.DraftError):
            results.set_pokepaste(self.conn, self.league_id, 1, self.match, self.one["id"], "https://example.com/team")
        view = results.set_pokepaste(self.conn, self.league_id, 1, self.match, self.one["id"], "https://pokepast.es/abc123")
        self.assertEqual(view["pokepastes"], {self.one["id"]: "https://pokepast.es/abc123"})

    def test_a_week_without_a_match_is_refused(self):
        with self.assertRaises(draft.DraftError):
            results.find_match(self.conn, self.league_id, 9, self.one["id"])

    def test_deleting_the_league_deletes_its_results(self):
        results.record_game(self.conn, self.league_id, 1, self.match, 1, "manual", stats={self.one["id"]: [("Rotom-Wash", 1, 0)]},
                            only_player=self.one["id"])
        draft.delete_league(self.conn, self.league_id)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM game_stats").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
