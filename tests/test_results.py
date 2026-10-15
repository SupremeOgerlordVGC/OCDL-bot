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

    def test_the_winner_is_whoever_wins_the_games_it_takes(self):
        record = lambda game, winner: results.record_game(  # noqa: E731
            self.conn, self.league_id, 1, self.match, game, "replay", winner_id=winner, stats={})
        view = record(1, self.one["id"])
        self.assertIsNone(view["winner_id"], "1-0 of a best-of-three wins nothing")
        self.assertEqual(view["wins"][self.one["id"]], 1)
        self.assertIsNone(record(2, self.two["id"])["winner_id"], "level at 1-1")
        self.assertEqual(record(3, self.two["id"])["winner_id"], self.two["id"])

        view = results.clear_game(self.conn, self.league_id, 1, self.match, 3)
        self.assertIsNone(view["winner_id"], "clearing a game reopens the match")
        self.assertEqual(len(view["games"]), 2)

    def test_a_game_the_match_cannot_have_is_refused(self):
        record = lambda game, winner: results.record_game(  # noqa: E731
            self.conn, self.league_id, 1, self.match, game, "replay", winner_id=winner, stats={})
        with self.assertRaises(draft.DraftError) as caught:
            record(4, self.one["id"])
        self.assertIn("best of 3", str(caught.exception))

        record(1, self.one["id"])
        record(2, self.one["id"])
        with self.assertRaises(draft.DraftError) as caught:
            record(3, self.two["id"])
        self.assertIn("already over", str(caught.exception))

        # Correcting a game that is already down is always allowed: that is
        # how a mistake gets fixed.
        view = record(2, self.two["id"])
        self.assertIsNone(view["winner_id"], "2-0 corrected to 1-1")

    def test_a_moderator_can_award_a_match_that_was_not_played(self):
        league = draft.get_league(self.conn, self.league_id)
        view = results.award_match(self.conn, league, 1, self.match, self.two["id"],
                                   "opponent never showed")
        self.assertEqual((view["winner_id"], view["walkover"]), (self.two["id"], True))
        self.assertEqual(view["note"], "opponent never showed")
        self.assertTrue(results.match_finished(self.conn, league, 1, self.match))
        self.assertEqual(
            self.conn.execute("SELECT winner_id FROM match_reports WHERE league_id = ?",
                              (self.league_id,)).fetchone()["winner_id"], self.two["id"],
            "stored, so the website shows it too")

        view = results.award_match(self.conn, league, 1, self.match, None)
        self.assertEqual((view["winner_id"], view["walkover"]), (None, False))
        self.assertFalse(results.match_finished(self.conn, league, 1, self.match))

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


class MatchOverTests(unittest.TestCase):
    """What "this match is finished" means, and what it sets off.

    A finished match is what puts that week's approved trades into effect,
    so it has to mean the whole match: a roster may not change under games
    that are still to be played with it.
    """

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.pool = make_draft(self.conn, channel_id=7100, players=2, min_picks=1, max_picks=2)
        for name, cost in [("Mega Eternal-Floette", 20), ("Incineroar", 15),
                           ("Garchomp", 12), ("Rotom-Wash", 8)]:
            live = draft.get_pool(self.conn, self.pool["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"], name, cost)
        self.league_id = self.pool["league_id"]
        draft.generate_fixtures(self.conn, self.league_id, weeks=1)
        self.one = draft.get_player(self.conn, self.pool["id"], user(1))
        self.two = draft.get_player(self.conn, self.pool["id"], user(2))
        self.match = results.find_match(self.conn, self.league_id, 1, self.one["id"])["match_number"]

    def league(self):
        return draft.get_league(self.conn, self.league_id)

    def win(self, game, player):
        results.record_game(self.conn, self.league_id, 1, self.match, game, "manual",
                            winner_id=player["id"])

    def test_a_best_of_three_takes_two_games(self):
        self.assertEqual(self.league()["best_of"], 3, "the safe default")
        self.assertFalse(results.match_finished(self.conn, self.league(), 1, self.match),
                         "nothing reported yet")
        self.win(1, self.one)
        self.assertFalse(results.match_finished(self.conn, self.league(), 1, self.match),
                         "ahead on games is not finished")
        self.win(2, self.two)
        self.assertFalse(results.match_finished(self.conn, self.league(), 1, self.match))
        self.win(3, self.one)
        self.assertTrue(results.match_finished(self.conn, self.league(), 1, self.match))

    def test_a_best_of_one_takes_one(self):
        draft.update_settings(self.conn, self.pool, best_of=1)
        self.assertEqual(self.league()["best_of"], 1,
                         "changing it is allowed after the draft: it is about the season")
        self.win(1, self.two)
        self.assertTrue(results.match_finished(self.conn, self.league(), 1, self.match))

    def test_an_even_number_of_games_is_refused(self):
        with self.assertRaises(draft.DraftError):
            draft.update_settings(self.conn, self.pool, best_of=2)
        with self.assertRaises(draft.DraftError):
            draft.update_settings(self.conn, self.pool, best_of=7)

    def test_finishing_the_match_puts_both_players_trades_into_effect(self):
        live = draft.get_pool(self.conn, self.pool["id"])
        draft.propose_trade(self.conn, live, user(1), ["Rotom-Wash"], ["Skarmory"])
        draft.propose_trade(self.conn, live, user(2), ["Garchomp"], ["Corviknight"])
        draft.approve_trade(self.conn, live, 1, 99)
        draft.approve_trade(self.conn, live, 2, 99)

        self.assertEqual(results.trades_after_match(self.conn, self.league(), 1, self.match), [],
                         "the match has not been played")
        self.win(1, self.one)
        self.assertEqual(results.trades_after_match(self.conn, self.league(), 1, self.match), [],
                         "and not after one game of three")

        self.win(2, self.one)
        applied = results.trades_after_match(self.conn, self.league(), 1, self.match)
        self.assertEqual(sorted(trade["trade_number"] for trade, _ in applied), [1, 2],
                         "the match is over for both of them")
        self.assertIsNone(draft.find_owner(self.conn, self.pool["id"], "rotom-wash"))
        self.assertEqual(
            draft.find_owner(self.conn, self.pool["id"], "skarmory")["player_id"], self.one["id"])
        self.assertEqual(draft.get_trade(self.conn, self.league_id, 1)["applied_week"], 1,
                         "in effect from the week they next play")

        self.assertEqual(results.trades_after_match(self.conn, self.league(), 1, self.match), [],
                         "and not twice")

    def test_a_match_with_no_fixture_does_nothing(self):
        self.win(1, self.one)
        self.win(2, self.one)
        self.assertEqual(results.trades_after_match(self.conn, self.league(), 1, 99), [])


class SheetShapeTests(unittest.TestCase):
    """Turning a reported match into what the spreadsheet's Matches tab wants.

    The numbers are the real Cornerstone week 1 match 8: nirvanacokun beat
    bananamilker_ 2-0, recorded from two Ready battles.
    """

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.pool = make_draft(self.conn, channel_id=7200, players=2, min_picks=1, max_picks=4)
        for name, cost in [("Goodra-Hisui", 12), ("Alcremie", 4), ("Sneasler", 10),
                           ("Hatterene", 8), ("Volcarona", 9), ("Mimikyu", 7),
                           ("Toxapex", 6), ("Mr. Mime", 3)]:
            live = draft.get_pool(self.conn, self.pool["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"], name, cost)
        self.league_id = self.pool["league_id"]
        draft.generate_fixtures(self.conn, self.league_id, weeks=1)
        self.one = draft.get_player(self.conn, self.pool["id"], user(1))
        self.two = draft.get_player(self.conn, self.pool["id"], user(2))
        self.match = results.find_match(self.conn, self.league_id, 1, self.one["id"])["match_number"]

    def league(self):
        return draft.get_league(self.conn, self.league_id)

    def record(self, game, winner, stats, replay=None):
        results.record_game(self.conn, self.league_id, 1, self.match, game, "battle",
                            winner_id=winner["id"] if winner else None,
                            stats=stats, replay=replay)

    def sides(self):
        shape = results.sheet_match(self.conn, self.league(), 1, self.match)
        return {side["coach"]: side for side in shape["sides"]}, shape

    def test_the_pokemon_are_the_ones_that_played_in_the_order_they_appeared(self):
        self.record(1, self.one,
                    {self.one["id"]: [("Goodra-Hisui", 3, 0), ("Volcarona", 1, 1)],
                     self.two["id"]: [("Alcremie", 0, 1), ("Hatterene", 0, 1)]},
                    replay="https://replay.ogreclan.com/a")
        self.record(2, self.one,
                    {self.one["id"]: [("Goodra-Hisui", 3, 0), ("Sneasler", 1, 1)],
                     self.two["id"]: [("Mimikyu", 0, 1), ("Alcremie", 1, 1)]},
                    replay="https://replay.ogreclan.com/b")
        sides, shape = self.sides()

        mine = sides[self.one["display_name"]]
        self.assertEqual([entry["name"] for entry in mine["pokemon"]],
                         ["Goodra-Hisui", "Volcarona", "Sneasler"],
                         "first appearance order, so game 1's team comes first")
        self.assertEqual(mine["pokemon"][0]["games"], [(3, 0), (3, 0)])
        self.assertEqual(mine["pokemon"][1]["games"], [(1, 1), None],
                         "None is a game it did not play, which clears those cells")
        self.assertEqual(mine["results"], ["W", "W"])

        theirs = sides[self.two["display_name"]]
        self.assertEqual([entry["name"] for entry in theirs["pokemon"]],
                         ["Alcremie", "Hatterene", "Mimikyu"])
        self.assertEqual(theirs["results"], ["L", "L"])
        self.assertEqual(shape["replays"],
                         ["https://replay.ogreclan.com/a", "https://replay.ogreclan.com/b"])
        self.assertTrue(shape["decided"])
        self.assertFalse(shape["walkover"])

    def test_a_game_nobody_has_said_the_winner_of_has_no_result(self):
        self.record(1, None, {self.one["id"]: [("Goodra-Hisui", 1, 0)]})
        sides, shape = self.sides()
        self.assertEqual(sides[self.one["display_name"]]["results"], [""])
        self.assertFalse(shape["decided"])

    def test_a_walkover_is_written_as_the_wins_it_takes_and_nothing_else(self):
        results.award_match(self.conn, self.league(), 1, self.match, self.two["id"],
                            "no-show")
        sides, shape = self.sides()
        self.assertTrue(shape["walkover"])
        self.assertEqual(sides[self.two["display_name"]]["results"], ["W", "W"])
        self.assertEqual(sides[self.one["display_name"]]["results"], ["L", "L"])
        self.assertEqual(sides[self.one["display_name"]]["pokemon"], [],
                         "nothing was played, so no kills or deaths")

    def test_only_finished_matches_are_offered(self):
        self.assertEqual(results.decided_matches(self.conn, self.league(), 1), [])
        self.record(1, self.one, {self.one["id"]: [("Goodra-Hisui", 1, 0)]})
        self.assertEqual(results.decided_matches(self.conn, self.league(), 1), [])
        self.record(2, self.one, {self.one["id"]: [("Goodra-Hisui", 1, 0)]})
        self.assertEqual(results.decided_matches(self.conn, self.league(), 1), [self.match])

    def test_a_match_that_does_not_exist_is_refused(self):
        with self.assertRaises(draft.DraftError):
            results.sheet_match(self.conn, self.league(), 1, 99)
