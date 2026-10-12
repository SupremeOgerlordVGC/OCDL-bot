"""
Tests for the draft rules.

These run without Discord and without a network connection:

    python3 -m unittest discover -s tests -v

Each test builds a throwaway in-memory database, so tests never interfere
with a real draft.
"""

import collections
import itertools
import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config          # noqa: E402
import database        # noqa: E402
import draft           # noqa: E402


def make_draft(conn, channel_id=100, players=4, budget=100, min_picks=2,
               max_picks=3, base_timer=120, max_trades=0, tera_budget=0,
               doc_kind="none", mode="full_season", name=None, start=True):
    """Create (and usually start) a one-pool league, returning the pool."""
    roster = [(1000 + i, f"Player{i}") for i in range(1, players + 1)]
    draft.create_league(
        conn, guild_id=1, admin_channel_id=999,
        name=name or f"League {channel_id}",
        pools=[("A", channel_id, roster)],
        budget=budget, min_picks=min_picks, max_picks=max_picks,
        base_timer=base_timer, mode=mode, doc_kind=doc_kind,
        tera_budget=tera_budget, max_trades=max_trades,
    )
    pool = draft.get_live_pool(conn, channel_id)
    if start:
        draft.start_pool(conn, pool)
        pool = draft.get_pool(conn, pool["id"])
    return pool


def make_two_pool_league(conn, name="Two Pools", channel_a=201, channel_b=202,
                         players=3, budget=100, max_picks=3, start=True):
    """A template-backed league with both pools, returning (pool_a, pool_b)."""
    roster_a = [(1000 + i, f"A{i}") for i in range(1, players + 1)]
    roster_b = [(2000 + i, f"B{i}") for i in range(1, players + 1)]
    draft.create_league(
        conn, guild_id=1, admin_channel_id=999, name=name,
        pools=[("A", channel_a, roster_a), ("B", channel_b, roster_b)],
        budget=budget, min_picks=1, max_picks=max_picks, base_timer=120,
        doc_kind="template", doc_url="https://example/sheet",
    )
    pool_a = draft.get_live_pool(conn, channel_a)
    pool_b = draft.get_live_pool(conn, channel_b)
    if start:
        draft.start_pool(conn, pool_a)
        draft.start_pool(conn, pool_b)
        pool_a = draft.get_pool(conn, pool_a["id"])
        pool_b = draft.get_pool(conn, pool_b["id"])
    return pool_a, pool_b


def user(n):
    """User id of generated PlayerN."""
    return 1000 + n


def on_clock(conn, draft_row):
    return draft.current_player(conn, draft.get_pool(conn, draft_row["id"]))["display_name"]


class SnakeOrderTests(unittest.TestCase):
    def test_sixteen_player_pattern(self):
        self.assertEqual(draft.snake_order(16, 1), list(range(1, 17)))
        self.assertEqual(draft.snake_order(16, 2), list(range(16, 0, -1)))
        self.assertEqual(draft.snake_order(16, 3), list(range(1, 17)))

    def test_every_supported_player_count(self):
        for count in range(config.MIN_PLAYERS, config.MAX_PLAYERS + 1):
            forward = draft.snake_order(count, 1)
            backward = draft.snake_order(count, 2)
            self.assertEqual(forward, sorted(forward))
            self.assertEqual(backward, list(reversed(forward)))
            # Every seat picks exactly once per round.
            self.assertEqual(sorted(backward), list(range(1, count + 1)))

    def test_turn_progression_across_round_boundaries(self):
        conn = database.connect(":memory:")
        row = make_draft(conn, players=4, max_picks=3)

        seen = []
        for _ in range(4 * 3):
            live = draft.get_pool(conn, row["id"])
            player = draft.current_player(conn, live)
            seen.append((live["current_round"], player["draft_position"]))
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"Mon{len(seen)}", 1)

        self.assertEqual(seen[0:4], [(1, 1), (1, 2), (1, 3), (1, 4)])
        self.assertEqual(seen[4:8], [(2, 4), (2, 3), (2, 2), (2, 1)])
        self.assertEqual(seen[8:12], [(3, 1), (3, 2), (3, 3), (3, 4)])
        self.assertEqual(draft.get_pool(conn, row["id"])["status"], "complete")


class ChannelIsolationTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")

    def test_two_channels_are_independent(self):
        a = make_draft(self.conn, channel_id=111, players=2, budget=100)
        b = make_draft(self.conn, channel_id=222, players=2, budget=50)

        # The same person plays in both drafts.
        draft.make_pick(self.conn, a, user(1), 1, "Garchomp", 12)
        # Garchomp is taken in draft A but must still be free in draft B.
        draft.make_pick(self.conn, b, user(1), 1, "Garchomp", 9)

        a_player = draft.get_player(self.conn, a["id"], user(1))
        b_player = draft.get_player(self.conn, b["id"], user(1))
        self.assertEqual(a_player["budget_remaining"], 88)
        self.assertEqual(b_player["budget_remaining"], 41)

        self.assertEqual(len(draft.round_picks(self.conn, a["id"], 1)), 1)
        self.assertEqual(len(draft.round_picks(self.conn, b["id"], 1)), 1)

    def test_second_draft_in_same_channel_is_refused(self):
        make_draft(self.conn, channel_id=111, players=2)
        with self.assertRaises(draft.DraftError):
            make_draft(self.conn, channel_id=111, players=2)

    def test_editing_one_draft_leaves_the_other_alone(self):
        a = make_draft(self.conn, channel_id=111, players=2, budget=100, start=False)
        b = make_draft(self.conn, channel_id=222, players=2, budget=100, start=False)
        draft.update_settings(self.conn, a, budget=500)
        self.assertEqual(draft.get_pool(self.conn, b["id"])["budget"], 100)
        self.assertEqual(draft.get_player(self.conn, b["id"], user(1))["budget_remaining"], 100)


class PickValidationTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=3, budget=20, max_picks=3)

    def test_duplicate_pokemon_is_refused(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 5)
        live = draft.get_pool(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError) as caught:
            draft.make_pick(self.conn, live, user(2), 1, "garchomp", 5)
        self.assertIn("already been drafted", str(caught.exception))

    def test_wrong_round_is_refused(self):
        with self.assertRaises(draft.DraftError) as caught:
            draft.make_pick(self.conn, self.draft, user(1), 2, "Garchomp", 5)
        self.assertIn("Round 1", str(caught.exception))

    def test_out_of_turn_is_refused(self):
        with self.assertRaises(draft.DraftError) as caught:
            draft.make_pick(self.conn, self.draft, user(2), 1, "Garchomp", 5)
        self.assertIn("not your turn", str(caught.exception))

    def test_admin_may_pick_for_the_player_on_the_clock(self):
        draft.make_pick(self.conn, self.draft, 88888, 1, "Garchomp", 5,
                        acting_as_admin=True)
        picks = draft.round_picks(self.conn, self.draft["id"], 1)
        self.assertEqual(picks[0]["display_name"], "Player1")

    def test_insufficient_budget_is_refused(self):
        with self.assertRaises(draft.DraftError) as caught:
            draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 21)
        self.assertIn("budget", str(caught.exception))

    def test_failed_pick_does_not_touch_the_database(self):
        try:
            draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 999)
        except draft.DraftError:
            pass
        self.assertEqual(draft.picks_made(self.conn, self.draft["id"],
                                          draft.get_player(self.conn, self.draft["id"], user(1))["id"]), 0)
        self.assertEqual(draft.get_player(self.conn, self.draft["id"], user(1))["budget_remaining"], 20)
        # Still Player1's turn.
        self.assertEqual(on_clock(self.conn, self.draft), "Player1")

    def test_budget_is_deducted_correctly(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        player = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(player["budget_remaining"], 8)

    def test_maximum_pick_limit(self):
        small = make_draft(self.conn, channel_id=777, players=2, budget=100, max_picks=2)
        # Two rounds x two players: everyone reaches the cap exactly.
        for _ in range(4):
            live = draft.get_pool(self.conn, small["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                            f"Cap{player['id']}{live['current_round']}", 1)
        p1 = draft.get_player(self.conn, small["id"], user(1))
        self.assertEqual(draft.picks_made(self.conn, small["id"], p1["id"]), 2)
        self.assertEqual(draft.get_pool(self.conn, small["id"])["status"], "complete")


class TimerTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=3, budget=100, max_picks=6, base_timer=120)

    def test_timer_halves_once_per_missed_turn(self):
        """120 -> 60 -> 30 -> 15, one halving per forfeited turn, never lower.

        Player2 is used because with three players seats 1 and 3 sit on the
        turn of the snake, where a pair of lost rounds shares a single
        halving. Seat 2 always has a gap between its turns, so it shows the
        plain progression. The boundary case has its own tests.
        """
        target = draft.get_player(self.conn, self.draft["id"], user(2))
        player_id = target["id"]
        self.assertEqual(target["current_timer"], 120)

        # Player1 opens, then Player2 lets the clock run out.
        draft.make_pick(self.conn, self.draft, user(1), 1, "Opener", 1)

        timers = []
        events = draft.skip_current_player(self.conn, self.draft["id"])
        timers += [e["new_timer"] for e in events
                   if e["type"] == "skipped" and e["player_id"] == player_id]

        # Everybody else keeps picking; Player2 never responds again.
        for _ in range(60):
            live = draft.get_pool(self.conn, self.draft["id"])
            if live["status"] != "active":
                break
            player = draft.current_player(self.conn, live)
            _, events = draft.make_pick(self.conn, live, player["user_id"],
                                        live["current_round"],
                                        f"F{player['id']}-{live['current_round']}", 1)
            timers += [e["new_timer"] for e in events
                       if e["type"] == "skipped" and e["player_id"] == player_id]

        self.assertEqual(timers[:4], [60, 30, 15, 15],
                         "each forfeited turn halves once, down to the floor")
        self.assertGreaterEqual(min(timers), config.MINIMUM_TIMER_SECONDS)

    def test_boundary_seat_shares_one_halving_across_two_lost_rounds(self):
        """Seat 1 is on the turn of the snake, so pairs of rounds cost one halving."""
        player_id = draft.get_player(self.conn, self.draft["id"], user(1))["id"]

        timers = []
        events = draft.skip_current_player(self.conn, self.draft["id"])   # misses round 1
        timers += [e["new_timer"] for e in events
                   if e["type"] == "skipped" and e["player_id"] == player_id]

        for _ in range(60):
            live = draft.get_pool(self.conn, self.draft["id"])
            if live["status"] != "active":
                break
            player = draft.current_player(self.conn, live)
            _, events = draft.make_pick(self.conn, live, player["user_id"],
                                        live["current_round"],
                                        f"G{player['id']}-{live['current_round']}", 1)
            timers += [e["new_timer"] for e in events
                       if e["type"] == "skipped" and e["player_id"] == player_id]

        # Rounds 2+3 are one back-to-back pair, 4+5 the next.
        self.assertEqual(timers[:5], [60, 30, 30, 15, 15])

    def test_one_players_reduction_does_not_affect_others(self):
        draft.skip_current_player(self.conn, self.draft["id"])   # Player1 skipped
        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        p2 = draft.get_player(self.conn, self.draft["id"], user(2))
        p3 = draft.get_player(self.conn, self.draft["id"], user(3))
        self.assertEqual(p1["current_timer"], 60)
        self.assertEqual(p2["current_timer"], 120)
        self.assertEqual(p3["current_timer"], 120)
        self.assertEqual(p1["skip_count"], 1)
        self.assertEqual(p2["skip_count"], 0)

    def test_successful_pick_does_not_restore_the_timer(self):
        draft.skip_current_player(self.conn, self.draft["id"])
        live = draft.get_pool(self.conn, self.draft["id"])
        player2 = draft.current_player(self.conn, live)
        draft.make_pick(self.conn, live, player2["user_id"], 1, "Heatran", 10)
        self.assertEqual(draft.get_player(self.conn, self.draft["id"], user(1))["current_timer"], 60)


class MakeupPickTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=3, budget=100, max_picks=4, base_timer=120)

    def play_until(self, kind):
        """Make filler picks until an event of this kind appears.

        Returns the event list from the call that produced it, so a test can
        inspect exactly what happened on that one turn.
        """
        for _ in range(50):
            live = draft.get_pool(self.conn, self.draft["id"])
            if live["status"] != "active":
                raise AssertionError(f"draft ended before any {kind!r} event")
            player = draft.current_player(self.conn, live)
            _, events = draft.make_pick(self.conn, live, player["user_id"],
                                        live["current_round"],
                                        f"F{player['id']}-{live['current_round']}", 1)
            if any(event["type"] == kind for event in events):
                return events
        raise AssertionError(f"no {kind!r} event appeared")

    def advance_to(self, position, expected_round=None):
        """Play filler picks until `position` is on the clock."""
        for _ in range(50):
            live = draft.get_pool(self.conn, self.draft["id"])
            if live["status"] != "active":
                return live
            player = draft.current_player(self.conn, live)
            if player["draft_position"] == position and (
                    expected_round is None or live["current_round"] == expected_round):
                return live
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                            f"F{player['id']}-{live['current_round']}", 1)
        raise AssertionError("never reached that seat")

    def test_skip_creates_a_pending_makeup(self):
        draft.skip_current_player(self.conn, self.draft["id"])
        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(draft.pending_makeup_rounds(self.conn, self.draft["id"], p1["id"]), [1])

    def test_makeup_pick_resolves_the_round_and_keeps_the_turn(self):
        draft.skip_current_player(self.conn, self.draft["id"])
        before = draft.get_pool(self.conn, self.draft["id"])
        current_before = draft.current_player(self.conn, before)["id"]

        pick = draft.make_makeup_pick(self.conn, before, user(1), 1, "Garchomp", 12)

        after = draft.get_pool(self.conn, self.draft["id"])
        self.assertEqual(pick["round"], 1)               # recorded to the missed round
        self.assertEqual(pick["is_makeup"], 1)
        self.assertEqual(draft.current_player(self.conn, after)["id"], current_before)
        self.assertEqual(after["current_round"], before["current_round"])
        self.assertEqual(after["current_turn_index"], before["current_turn_index"])

        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(p1["budget_remaining"], 88)
        self.assertEqual(draft.pending_makeup_rounds(self.conn, self.draft["id"], p1["id"]), [])

    def test_makeup_pick_for_a_round_you_did_not_miss(self):
        with self.assertRaises(draft.DraftError) as caught:
            draft.make_makeup_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        self.assertIn("do not have a makeup pick", str(caught.exception))

    def test_makeup_usable_until_the_next_turn_opens(self):
        # Player1 misses round 1. Order is 1,2,3 then 3,2,1, so their next
        # turn is the last of round 2 - everything before that is the window.
        draft.skip_current_player(self.conn, self.draft["id"])
        for _ in range(3):                       # P2, P3 (round 1), then P3 (round 2)
            live = draft.get_pool(self.conn, self.draft["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                            f"F{player['id']}-{live['current_round']}", 1)

        live = draft.get_pool(self.conn, self.draft["id"])
        self.assertEqual(live["current_round"], 2)
        self.assertEqual(draft.current_player(self.conn, live)["display_name"], "Player2")

        pick = draft.make_makeup_pick(self.conn, live, user(1), 1, "Garchomp", 12)
        self.assertEqual(pick["round"], 1)
        # The makeup did not steal Player2's turn.
        after = draft.get_pool(self.conn, self.draft["id"])
        self.assertEqual(draft.current_player(self.conn, after)["display_name"], "Player2")

    def test_missing_the_deadline_forfeits_the_next_turn(self):
        """missed pick -> skipped -> halved -> no makeup -> next turn skipped."""
        draft.skip_current_player(self.conn, self.draft["id"])       # misses round 1
        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(p1["current_timer"], 60)

        events = self.play_until("makeup_expired")
        kinds = [event["type"] for event in events]
        self.assertIn("makeup_expired", kinds)
        self.assertIn("skipped", kinds)

        expired = [e for e in events if e["type"] == "makeup_expired"][0]
        self.assertEqual(expired["round"], 1)

        live = draft.get_pool(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError) as caught:
            draft.make_makeup_pick(self.conn, live, user(1), 1, "Garchomp", 12)
        self.assertIn("expired", str(caught.exception))

    def test_only_one_timer_reduction_per_turn(self):
        # The expiry and the forfeited turn land together, but a turn may
        # only ever cost one halving: 60 -> 30, not 60 -> 15.
        draft.skip_current_player(self.conn, self.draft["id"])
        self.assertEqual(draft.get_player(self.conn, self.draft["id"], user(1))["current_timer"], 60)

        events = self.play_until("makeup_expired")
        skips = [e for e in events if e["type"] == "skipped"]
        self.assertTrue(skips[0]["halved"])
        self.assertEqual(skips[0]["new_timer"], 30, "one halving for that turn, not two")

    def test_snake_boundary_loses_both_rounds_but_halves_only_once(self):
        """The turn of the snake: seat 1 picks last in round 2, first in round 3.

        Being skipped in round 2 kills the makeup instantly, because the
        deadline - their next turn - is the very next turn of the draft. Both
        rounds are lost, but the pair only costs one timer halving.
        """
        self.advance_to(1, expected_round=2)
        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(p1["current_timer"], 120)

        events = draft.skip_current_player(self.conn, self.draft["id"])
        kinds = [event["type"] for event in events]
        self.assertIn("skipped", kinds)
        self.assertIn("makeup_expired", kinds)

        expired = [e for e in events if e["type"] == "makeup_expired"][0]
        self.assertEqual(expired["round"], 2)
        self.assertTrue(expired["back_to_back"])

        # Round 2 and round 3 are both gone...
        skips = [e for e in events if e["type"] == "skipped"]
        self.assertEqual([s["round"] for s in skips], [2, 3])
        # ...but only the first of them reduced the timer.
        self.assertTrue(skips[0]["halved"])
        self.assertFalse(skips[1]["halved"])

        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(p1["current_timer"], 60, "120 -> 60 once, not twice")
        self.assertEqual(p1["skip_count"], 1)
        # They now owe round 3 instead, due before their round 4 turn.
        self.assertEqual(draft.pending_makeup_rounds(self.conn, self.draft["id"], p1["id"]), [3])

    def test_snake_boundary_with_a_sniped_queue_never_halves(self):
        """Queue wiped out at the boundary: both rounds lost, timer untouched."""
        # Player1 queues for round 2, then plays round 1 normally.
        draft.queue_picks(self.conn, self.draft, user(1), 2, ["Sniped"], [5])
        for _ in range(3):                       # finish round 1
            live = draft.get_pool(self.conn, self.draft["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], 1,
                            f"R1-{player['id']}", 1)

        # Round 2 runs 3, 2, 1 - Player3 takes the queued Pokémon first.
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(3), 2, "Sniped", 5)
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 2, "R2-2", 1)

        live = draft.get_pool(self.conn, self.draft["id"])
        self.assertEqual(draft.current_player(self.conn, live)["display_name"], "Player1")
        self.assertEqual(live["queue_exhausted"], 1)

        events = draft.skip_current_player(self.conn, self.draft["id"])
        skips = [e for e in events if e["type"] == "skipped"]
        self.assertEqual([s["round"] for s in skips], [2, 3])
        self.assertFalse(any(s["halved"] for s in skips))

        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(p1["current_timer"], 120, "a sniped queue never costs the timer")
        self.assertEqual(p1["skip_count"], 0)


class QueuedPickTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=3, budget=100, max_picks=3, base_timer=120)

    def test_queue_drafts_the_first_available_pokemon(self):
        # Player2 queues three Pokemon for round 1.
        draft.queue_picks(self.conn, self.draft, user(2), 1,
                          ["Garchomp", "Heatran", "Rotom-Wash"], [12, 10, 8])
        # Player1 takes Garchomp, so the queue should fall through to Heatran.
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)

        picks = draft.round_picks(self.conn, self.draft["id"], 1)
        self.assertEqual([(p["display_name"], p["pokemon"]) for p in picks],
                         [("Player1", "Garchomp"), ("Player2", "Heatran")])
        self.assertEqual(picks[1]["is_auto"], 1)
        # The turn moved on to Player3 automatically.
        self.assertEqual(on_clock(self.conn, self.draft), "Player3")

    def test_queue_respects_budget(self):
        broke = make_draft(self.conn, channel_id=321, players=2, budget=10, max_picks=2)
        draft.queue_picks(self.conn, broke, user(2), 1, ["Expensive", "Cheap"], [50, 3])
        draft.make_pick(self.conn, broke, user(1), 1, "Something", 1)
        picks = draft.round_picks(self.conn, broke["id"], 1)
        self.assertEqual(picks[1]["pokemon"], "Cheap")

    def test_exhausted_queue_does_not_halve_the_timer(self):
        draft.queue_picks(self.conn, self.draft, user(2), 1, ["Garchomp"], [12])
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)   # steals it

        live = draft.get_pool(self.conn, self.draft["id"])
        self.assertEqual(live["queue_exhausted"], 1)
        self.assertEqual(draft.current_player(self.conn, live)["display_name"], "Player2")

        # The clock now runs out on Player2 anyway.
        events = draft.skip_current_player(self.conn, self.draft["id"])
        skipped = [e for e in events if e["type"] == "skipped"][0]
        self.assertFalse(skipped["halved"])

        p2 = draft.get_player(self.conn, self.draft["id"], user(2))
        self.assertEqual(p2["current_timer"], 120, "queue wipe-out must not shrink the timer")
        self.assertEqual(p2["skip_count"], 0)
        # They still owe the round, though.
        self.assertEqual(draft.pending_makeup_rounds(self.conn, self.draft["id"], p2["id"]), [1])

    def test_normal_skip_still_halves(self):
        events = draft.skip_current_player(self.conn, self.draft["id"])
        skipped = [e for e in events if e["type"] == "skipped"][0]
        self.assertTrue(skipped["halved"])

    def test_cannot_queue_for_a_turn_that_has_passed(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        live = draft.get_pool(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError):
            draft.queue_picks(self.conn, live, user(1), 1, ["Heatran"], [10])

    def test_chained_queues_resolve_in_one_go(self):
        draft.queue_picks(self.conn, self.draft, user(2), 1, ["Heatran"], [10])
        draft.queue_picks(self.conn, self.draft, user(3), 1, ["Landorus"], [15])
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)

        picks = draft.round_picks(self.conn, self.draft["id"], 1)
        self.assertEqual([p["pokemon"] for p in picks], ["Garchomp", "Heatran", "Landorus"])
        live = draft.get_pool(self.conn, self.draft["id"])
        self.assertEqual(live["current_round"], 2)
        self.assertEqual(draft.current_player(self.conn, live)["display_name"], "Player3")


class EditPickTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=2, budget=50, max_picks=3)
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)

    def test_edit_updates_roster_budget_and_availability(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.edit_pick(self.conn, live, user(1), 1, "Heatran", 10)

        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(p1["budget_remaining"], 40)
        view = draft.roster_view(self.conn, live, p1)
        self.assertEqual(view["pokemon"], [("Heatran", 10)])
        # Garchomp is free again; Heatran is not.
        self.assertIsNone(draft.find_owner(self.conn, self.draft["id"], "garchomp"))
        self.assertIsNotNone(draft.find_owner(self.conn, self.draft["id"], "heatran"))

    def test_edit_cannot_duplicate_another_players_pokemon(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 1, "Heatran", 10)
        live = draft.get_pool(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError):
            draft.edit_pick(self.conn, live, user(1), 1, "Heatran", 10)
        # The rejected edit changed nothing.
        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(p1["budget_remaining"], 38)
        self.assertEqual(draft.roster_view(self.conn, live, p1)["pokemon"], [("Garchomp", 12)])

    def test_edit_rejected_when_over_budget(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError):
            draft.edit_pick(self.conn, live, user(1), 1, "Kyogre", 500)
        self.assertEqual(draft.get_player(self.conn, self.draft["id"], user(1))["budget_remaining"], 38)

    def test_player_cannot_edit_someone_else(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError):
            draft.edit_pick(self.conn, live, user(2), 1, "Heatran", 10, target_user_id=user(1))

    def test_admin_can_edit_someone_else(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        result = draft.edit_pick(self.conn, live, 99999, 1, "Heatran", 10,
                                 target_user_id=user(1), acting_as_admin=True)
        self.assertEqual(result["old_pokemon"], "Garchomp")
        self.assertEqual(result["new_pokemon"], "Heatran")

    def test_editing_your_own_pokemon_price_only(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.edit_pick(self.conn, live, user(1), 1, "Garchomp", 5)
        self.assertEqual(draft.get_player(self.conn, self.draft["id"], user(1))["budget_remaining"], 45)


class TradeTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=2, budget=100, max_picks=2)
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 1, "Heatran", 10)
        self.live = draft.get_pool(self.conn, self.draft["id"])

    def test_trade_ids_count_up_from_one(self):
        first, _ = draft.propose_trade(self.conn, self.live, user(1), ["Garchomp"], ["Heatran"],
                                       partner_user_id=user(2))
        second, _ = draft.propose_trade(self.conn, self.live, user(2), ["Heatran"], ["Garchomp"],
                                        partner_user_id=user(1))
        self.assertEqual(first["trade_number"], 1)
        self.assertEqual(second["trade_number"], 2)

    def test_proposal_does_not_change_rosters(self):
        draft.propose_trade(self.conn, self.live, user(1), ["Garchomp"], ["Heatran"],
                            partner_user_id=user(2))
        owner = draft.find_owner(self.conn, self.draft["id"], "garchomp")
        self.assertEqual(owner["player_id"], draft.get_player(self.conn, self.draft["id"], user(1))["id"])

    def test_approval_swaps_rosters_and_budgets(self):
        draft.propose_trade(self.conn, self.live, user(1), ["Garchomp"], ["Heatran"],
                            partner_user_id=user(2))
        draft.approve_trade(self.conn, self.live, 1, approver_user_id=42)

        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        p2 = draft.get_player(self.conn, self.draft["id"], user(2))
        self.assertEqual(draft.roster_view(self.conn, self.live, p1)["pokemon"], [("Heatran", 10)])
        self.assertEqual(draft.roster_view(self.conn, self.live, p2)["pokemon"], [("Garchomp", 12)])
        # 88 + 12 - 10, and 90 + 10 - 12
        self.assertEqual(p1["budget_remaining"], 90)
        self.assertEqual(p2["budget_remaining"], 88)

    def test_trade_cannot_be_approved_twice(self):
        draft.propose_trade(self.conn, self.live, user(1), ["Garchomp"], ["Heatran"],
                            partner_user_id=user(2))
        draft.approve_trade(self.conn, self.live, 1, approver_user_id=42)
        with self.assertRaises(draft.DraftError) as caught:
            draft.approve_trade(self.conn, self.live, 1, approver_user_id=42)
        self.assertIn("already been approved", str(caught.exception))

    def test_trading_a_pokemon_you_do_not_own_warns_but_does_not_block(self):
        # Admins judge trades, so the bot records it and flags the problem.
        record, warnings = draft.propose_trade(self.conn, self.live, user(1),
                                               ["Heatran"], ["Garchomp"],
                                               partner_user_id=user(2))
        self.assertEqual(record["status"], "pending")
        self.assertTrue(any("Heatran" in note for note in warnings))

    def test_free_agency_returns_the_dropped_pokemon_to_the_pool(self):
        draft.propose_trade(self.conn, self.live, user(1), ["Garchomp"], ["Skarmory"])
        draft.approve_trade(self.conn, self.live, 1, approver_user_id=42)
        self.assertIsNone(draft.find_owner(self.conn, self.draft["id"], "garchomp"))
        self.assertIsNotNone(draft.find_owner(self.conn, self.draft["id"], "skarmory"))
        # History still records that Player1 drafted Garchomp in round 1.
        picks = draft.round_picks(self.conn, self.draft["id"], 1)
        self.assertIn("Garchomp", [p["pokemon"] for p in picks])

    def test_history_survives_a_trade(self):
        draft.propose_trade(self.conn, self.live, user(1), ["Garchomp"], ["Heatran"],
                            partner_user_id=user(2))
        draft.approve_trade(self.conn, self.live, 1, approver_user_id=42)
        picks = draft.round_picks(self.conn, self.draft["id"], 1)
        self.assertEqual([(p["display_name"], p["pokemon"]) for p in picks],
                         [("Player1", "Garchomp"), ("Player2", "Heatran")])


class RestartTests(unittest.TestCase):
    def test_state_survives_reopening_the_database(self):
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            conn = database.connect(path)
            row = make_draft(conn, players=3, budget=100, max_picks=3)
            draft.make_pick(conn, row, user(1), 1, "Garchomp", 12)
            draft.skip_current_player(conn, row["id"])       # Player2 misses round 1
            conn.close()

            # Pretend the VM rebooted.
            conn = database.connect(path)
            live = draft.get_live_pool(conn, 100)
            self.assertIsNotNone(live)
            self.assertEqual(live["status"], "active")
            self.assertEqual(live["current_round"], 1)
            self.assertEqual(draft.current_player(conn, live)["display_name"], "Player3")

            p2 = draft.get_player(conn, live["id"], user(2))
            self.assertEqual(p2["current_timer"], 60)
            self.assertEqual(draft.pending_makeup_rounds(conn, live["id"], p2["id"]), [1])
            self.assertEqual(len(draft.round_picks(conn, live["id"], 1)), 1)
            conn.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=3, budget=100, min_picks=2, max_picks=3)

    def test_progress_preserves_chronological_snake_order(self):
        names = ["Garchomp", "Rotom-Wash", "Heatran"]
        for name in names:
            live = draft.get_pool(self.conn, self.draft["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], 1, name, 5)

        picks = draft.round_picks(self.conn, self.draft["id"], 1)
        self.assertEqual([p["pokemon"] for p in picks], names)
        self.assertEqual([p["display_name"] for p in picks], ["Player1", "Player2", "Player3"])

        # Round 2 runs backwards.
        for name in ["A", "B", "C"]:
            live = draft.get_pool(self.conn, self.draft["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], 2, name, 5)
        picks = draft.round_picks(self.conn, self.draft["id"], 2)
        self.assertEqual([p["display_name"] for p in picks], ["Player3", "Player2", "Player1"])

    def test_roster_view_totals(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        live = draft.get_pool(self.conn, self.draft["id"])
        player = draft.get_player(self.conn, self.draft["id"], user(1))
        view = draft.roster_view(self.conn, live, player)
        self.assertEqual(view["spent"], 12)
        self.assertEqual(view["remaining"], 88)
        self.assertEqual(view["count"], 1)
        self.assertEqual(view["max_picks"], 3)

    def test_status_view_reports_pending_makeups(self):
        draft.skip_current_player(self.conn, self.draft["id"])
        live = draft.get_pool(self.conn, self.draft["id"])
        status = draft.status_view(self.conn, live)
        self.assertEqual(status["pending_makeups"], [("Player1", 1)])
        self.assertEqual(status["current_player"]["display_name"], "Player2")
        self.assertEqual(status["expected_picks"], 9)


class SetupValidationTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")

    def test_player_count_bounds(self):
        with self.assertRaises(draft.DraftError):
            make_draft(self.conn, channel_id=1, players=1)
        with self.assertRaises(draft.DraftError):
            make_draft(self.conn, channel_id=2, players=config.MAX_PLAYERS + 1)

    def test_max_below_min_is_refused(self):
        with self.assertRaises(draft.DraftError):
            make_draft(self.conn, channel_id=3, players=2, min_picks=5, max_picks=2)

    def test_edit_blocked_once_started(self):
        row = make_draft(self.conn, channel_id=4, players=2, start=True)
        with self.assertRaises(draft.DraftError) as caught:
            draft.update_settings(self.conn, row, budget=200)
        self.assertIn("already started", str(caught.exception))

    def test_edit_allowed_before_start(self):
        row = make_draft(self.conn, channel_id=5, players=2, start=False)
        updated = draft.update_settings(self.conn, row, budget=200, doc_url="http://x")
        self.assertEqual(updated["budget"], 200)
        self.assertEqual(updated["doc_url"], "http://x")
        self.assertEqual(draft.get_player(self.conn, row["id"], user(1))["budget_remaining"], 200)

    def test_pick_before_start_is_refused(self):
        row = make_draft(self.conn, channel_id=6, players=2, start=False)
        with self.assertRaises(draft.DraftError):
            draft.make_pick(self.conn, row, user(1), 1, "Garchomp", 5)


class FullDraftSimulationTests(unittest.TestCase):
    """Play complete drafts with random picks, skips and queues, then check
    that the invariants which really matter still hold."""

    def run_one(self, players, seed):
        rng = random.Random(seed)
        conn = database.connect(":memory:")
        row = make_draft(conn, channel_id=seed, players=players, budget=200,
                         min_picks=2, max_picks=4, base_timer=120)

        counter = 0
        for _ in range(players * 4 * 6):
            live = draft.get_pool(conn, row["id"])
            if live["status"] != "active":
                break
            player = draft.current_player(conn, live)
            roll = rng.random()

            if roll < 0.15:
                draft.skip_current_player(conn, row["id"])
                continue

            if roll < 0.30:
                # Queue for a later round, then pick normally now.
                future = min(live["max_picks"], live["current_round"] + 1)
                if draft.turn_is_upcoming(conn, live, player, future):
                    names = [f"Q{counter}-{i}" for i in range(3)]
                    counter += 1
                    draft.queue_picks(conn, live, player["user_id"], future, names, [2, 2, 2])

            if roll < 0.40:
                # Resolve an outstanding makeup pick if there is one.
                pending = draft.pending_makeup_rounds(conn, row["id"], player["id"])
                if pending:
                    counter += 1
                    draft.make_makeup_pick(conn, live, player["user_id"], pending[0],
                                           f"M{counter}", rng.randint(1, 5))
                    live = draft.get_pool(conn, row["id"])

            counter += 1
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"P{counter}", rng.randint(1, 12))

        return conn, draft.get_pool(conn, row["id"])

    def test_invariants_hold_for_every_player_count(self):
        for players in range(config.MIN_PLAYERS, config.MAX_PLAYERS + 1):
            with self.subTest(players=players):
                conn, live = self.run_one(players, seed=players * 17)

                self.assertEqual(live["status"], "complete",
                                 "the draft should always reach the final round")

                # No Pokemon is ever owned twice.
                rows = conn.execute(
                    "SELECT pokemon_key, COUNT(*) AS n FROM rosters WHERE pool_id = ? "
                    "GROUP BY pokemon_key HAVING n > 1", (live["id"],)).fetchall()
                self.assertEqual(rows, [])

                for player in draft.get_players(conn, live["id"]):
                    view = draft.roster_view(conn, live, player)
                    self.assertGreaterEqual(player["budget_remaining"], 0)
                    self.assertEqual(view["spent"] + view["remaining"], live["budget"],
                                     "spent plus remaining must equal the starting budget")
                    self.assertLessEqual(draft.picks_made(conn, live["id"], player["id"]),
                                         live["max_picks"])
                    self.assertGreaterEqual(player["current_timer"], config.MINIMUM_TIMER_SECONDS)

                # Chronological pick order has no gaps or repeats.
                orders = [r["pick_order"] for r in conn.execute(
                    "SELECT pick_order FROM picks WHERE pool_id = ? ORDER BY pick_order",
                    (live["id"],))]
                self.assertEqual(orders, list(range(1, len(orders) + 1)))

                # Every player picked at most once per round.
                dupes = conn.execute(
                    "SELECT player_id, round, COUNT(*) AS n FROM picks WHERE pool_id = ? "
                    "GROUP BY player_id, round HAVING n > 1", (live["id"],)).fetchall()
                self.assertEqual(dupes, [])
                conn.close()


class AfterTheDraftTests(unittest.TestCase):
    """A finished draft still needs rosters, history, trades and corrections."""

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, channel_id=808, players=2, budget=100, max_picks=2)
        for _ in range(4):
            live = draft.get_pool(self.conn, self.draft["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                            f"Mon{player['id']}{live['current_round']}", 10)
        self.finished = draft.get_pool(self.conn, self.draft["id"])
        self.assertEqual(self.finished["status"], "complete")

    def test_live_lookup_is_empty_but_channel_lookup_still_works(self):
        self.assertIsNone(draft.get_live_pool(self.conn, 808))
        found = draft.require_channel_pool(self.conn, 808)
        self.assertEqual(found["id"], self.draft["id"])

    def test_rosters_and_history_survive(self):
        player = draft.get_player(self.conn, self.draft["id"], user(1))
        view = draft.roster_view(self.conn, self.finished, player)
        self.assertEqual(view["count"], 2)
        self.assertEqual(draft.completed_rounds(self.conn, self.finished), [1, 2])

    def test_trades_work_after_the_draft(self):
        record, _ = draft.propose_trade(self.conn, self.finished, user(1),
                                        ["Mon11"], ["Mon22"], partner_user_id=user(2))
        self.assertEqual(record["trade_number"], 1)
        draft.approve_trade(self.conn, self.finished, 1, approver_user_id=5)
        owner = draft.find_owner(self.conn, self.draft["id"], "mon11")
        self.assertEqual(owner["player_id"], draft.get_player(self.conn, self.draft["id"], user(2))["id"])

    def test_a_new_draft_can_start_in_the_same_channel(self):
        second = make_draft(self.conn, channel_id=808, players=2, budget=50,
                            min_picks=1, max_picks=1)
        self.assertNotEqual(second["id"], self.draft["id"])
        # The old draft's picks are untouched by the new one.
        self.assertEqual(len(draft.round_picks(self.conn, self.draft["id"], 1)), 2)
        self.assertEqual(len(draft.round_picks(self.conn, second["id"], 1)), 0)

    def test_final_round_makeup_survives_to_the_end(self):
        conn = database.connect(":memory:")
        row = make_draft(conn, channel_id=909, players=2, budget=100, max_picks=2)
        for _ in range(3):      # three normal picks
            live = draft.get_pool(conn, row["id"])
            player = draft.current_player(conn, live)
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"X{player['id']}{live['current_round']}", 5)
        draft.skip_current_player(conn, row["id"])      # last pick of the draft is missed

        finished = draft.get_pool(conn, row["id"])
        self.assertEqual(finished["status"], "complete")
        player = draft.current_player(conn, finished)
        self.assertIsNone(player)

        # The player skipped in the final round can still catch up.
        late = draft.get_player(conn, row["id"], user(1))
        pending = draft.pending_makeup_rounds(conn, row["id"], late["id"])
        self.assertEqual(pending, [2])
        pick = draft.make_makeup_pick(conn, finished, user(1), 2, "Latecomer", 7)
        self.assertEqual(pick["round"], 2)


class BudgetExhaustionTests(unittest.TestCase):
    """The draft also ends when nobody can afford another pick."""

    def test_draft_ends_early_when_everyone_is_broke(self):
        conn = database.connect(":memory:")
        row = make_draft(conn, channel_id=1200, players=2, budget=10,
                         min_picks=1, max_picks=6)

        # Two picks each at 5 points empties both budgets after round 2.
        for _ in range(4):
            live = draft.get_pool(conn, row["id"])
            player = draft.current_player(conn, live)
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"M{player['id']}{live['current_round']}", 5)

        finished = draft.get_pool(conn, row["id"])
        self.assertEqual(finished["status"], "complete")
        self.assertEqual(finished["current_round"], 2,
                         "ended in round 2, well before the sixth round")
        for player in draft.get_players(conn, row["id"]):
            self.assertEqual(player["budget_remaining"], 0)

    def test_a_broke_player_is_passed_over_not_skipped(self):
        conn = database.connect(":memory:")
        row = make_draft(conn, channel_id=1201, players=3, budget=10,
                         min_picks=1, max_picks=4)

        # Player1 spends everything at once; the others stay cheap.
        draft.make_pick(conn, row, user(1), 1, "Expensive", 10)
        for _ in range(2):
            live = draft.get_pool(conn, row["id"])
            player = draft.current_player(conn, live)
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"Cheap{player['id']}", 1)

        # Round 2 runs 3,2,1 - Player1 should be passed over without penalty.
        live = draft.get_pool(conn, row["id"])
        collected = []
        for _ in range(2):
            live = draft.get_pool(conn, row["id"])
            if live["status"] != "active":
                break
            player = draft.current_player(conn, live)
            _, events = draft.make_pick(conn, live, player["user_id"],
                                        live["current_round"], f"X{player['id']}", 1)
            collected += events

        passed = [e for e in collected if e["type"] == "passed"]
        self.assertTrue(passed, "the broke player should be passed over")
        self.assertEqual(passed[0]["display_name"], "Player1")
        self.assertEqual(passed[0]["reason"], "out of budget")

        p1 = draft.get_player(conn, row["id"], user(1))
        self.assertEqual(p1["current_timer"], 120, "being broke is not a skip")
        self.assertEqual(p1["skip_count"], 0)
        self.assertEqual(draft.pending_makeup_rounds(conn, row["id"], p1["id"]), [],
                         "no makeup pick is owed for a turn they could never take")


class TradeLimitTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, channel_id=1300, players=2, budget=100,
                                min_picks=1, max_picks=3, max_trades=1)
        for _ in range(6):
            live = draft.get_pool(self.conn, self.draft["id"])
            if live["status"] != "active":
                break
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                            f"P{player['id']}R{live['current_round']}", 5)
        self.live = draft.get_pool(self.conn, self.draft["id"])

    def test_setup_stores_the_trade_cap(self):
        self.assertEqual(self.live["max_trades"], 1)

    def test_second_trade_is_refused_once_the_cap_is_reached(self):
        record, _ = draft.propose_trade(self.conn, self.live, user(1), ["P1R1"], ["Wild"])
        draft.approve_trade(self.conn, self.live, record["trade_number"], 999)
        self.assertEqual(draft.trades_used(self.conn, self.draft["id"],
                                           draft.get_player(self.conn, self.draft["id"],
                                                            user(1))["id"]), 1)

        with self.assertRaises(draft.DraftError) as caught:
            draft.propose_trade(self.conn, self.live, user(1), ["P1R2"], ["Another"])
        self.assertIn("already used all 1", str(caught.exception))

    def test_partner_trades_count_against_both_players(self):
        record, _ = draft.propose_trade(self.conn, self.live, user(1), ["P1R1"], ["P2R1"],
                                        partner_user_id=user(2))
        draft.approve_trade(self.conn, self.live, record["trade_number"], 999)
        p2 = draft.get_player(self.conn, self.draft["id"], user(2))
        self.assertEqual(draft.trades_used(self.conn, self.draft["id"], p2["id"]), 1)
        with self.assertRaises(draft.DraftError):
            draft.propose_trade(self.conn, self.live, user(2), ["P1R1"], ["Wild"])

    def test_zero_means_unlimited(self):
        conn = database.connect(":memory:")
        row = make_draft(conn, channel_id=1301, players=2, budget=100,
                         min_picks=1, max_picks=1, max_trades=0)
        draft.make_pick(conn, row, user(1), 1, "A", 5)
        live = draft.get_pool(conn, row["id"])
        draft.make_pick(conn, live, user(2), 1, "B", 5)
        live = draft.get_pool(conn, row["id"])
        for name in ["C", "D", "E"]:
            record, _ = draft.propose_trade(conn, live, user(1), ["A"], [name])
            draft.approve_trade(conn, live, record["trade_number"], 999)
            live = draft.get_pool(conn, row["id"])
            # the newly acquired Pokemon becomes the next thing to trade away
            conn.execute("UPDATE rosters SET pokemon = 'A', pokemon_key = 'a' "
                         "WHERE pool_id = ? AND pokemon_key = ?", (row["id"], name.lower()))
            conn.commit()
        self.assertEqual(draft.trades_used(conn, row["id"],
                                           draft.get_player(conn, row["id"], user(1))["id"]), 3)


class AdminEditPickTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=3, budget=100, max_picks=3)

    def test_admin_can_fill_in_a_round_with_no_pick(self):
        draft.skip_current_player(self.conn, self.draft["id"])     # Player1 misses round 1
        live = draft.get_pool(self.conn, self.draft["id"])

        result = draft.edit_pick(self.conn, live, 99999, 1, "Garchomp", 12,
                                 target_user_id=user(1), acting_as_admin=True)

        self.assertTrue(result["created"])
        self.assertIsNone(result["old_pokemon"])
        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(p1["budget_remaining"], 88)
        self.assertEqual(draft.roster_view(self.conn, live, p1)["pokemon"], [("Garchomp", 12)])
        # Filling the round in also settles the makeup pick they owed.
        self.assertEqual(draft.pending_makeup_rounds(self.conn, self.draft["id"], p1["id"]), [])

    def test_a_player_cannot_invent_a_pick_for_themselves(self):
        draft.skip_current_player(self.conn, self.draft["id"])
        live = draft.get_pool(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError) as caught:
            draft.edit_pick(self.conn, live, user(1), 1, "Garchomp", 12)
        self.assertIn("makeuppick", str(caught.exception))

    def test_created_pick_still_obeys_the_rules(self):
        draft.skip_current_player(self.conn, self.draft["id"])
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 1, "Garchomp", 12)
        live = draft.get_pool(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError):
            draft.edit_pick(self.conn, live, 99999, 1, "Garchomp", 12,
                            target_user_id=user(1), acting_as_admin=True)


class SheetExportTests(unittest.TestCase):
    """What gets written into the spreadsheet's roster blocks."""

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=2, budget=100, min_picks=1, max_picks=3)

    def test_block_puts_each_pick_in_its_round_row(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 1, "Heatran", 10)
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 2, "Skarmory", 8)

        live = draft.get_pool(self.conn, self.draft["id"])
        p2 = draft.get_player(self.conn, self.draft["id"], user(2))
        block = draft.roster_block(self.conn, live, p2, 12)

        names = [name for name, _ in block]
        self.assertEqual(names[0], "Heatran")     # round 1 -> first row
        self.assertEqual(names[1], "Skarmory")    # round 2 -> second row
        self.assertEqual(names[2:], [""] * 10)
        self.assertEqual(len(block), 12)

    def test_dropped_pokemon_leave_their_row_and_gains_take_the_first_free_one(self):
        # Player2 drafts three rounds, then drops the round 1 pick and gains
        # two free agents. Row 1 empties, and the new Pokémon fill from the
        # top: first the empty row 1, then the next free row.
        for round_number in range(1, 4):
            for _ in range(2):
                live = draft.get_pool(self.conn, self.draft["id"])
                if live["status"] != "active":
                    break
                player = draft.current_player(self.conn, live)
                draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                                f"{player['display_name']}-R{live['current_round']}", 5)

        live = draft.get_pool(self.conn, self.draft["id"])
        record, _ = draft.propose_trade(self.conn, live, user(2),
                                        ["Player2-R1"], ["FreeAgentA", "FreeAgentB"])
        draft.approve_trade(self.conn, live, record["trade_number"], 999)

        live = draft.get_pool(self.conn, self.draft["id"])
        p2 = draft.get_player(self.conn, self.draft["id"], user(2))
        names = [name for name, _ in draft.roster_block(self.conn, live, p2, 12)]

        self.assertEqual(names[:4], ["FreeAgentA", "Player2-R2", "Player2-R3", "FreeAgentB"])
        self.assertNotIn("Player2-R1", names)

    def test_traded_pokemon_fill_the_first_free_row(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 1, "Heatran", 10)

        live = draft.get_pool(self.conn, self.draft["id"])
        record, _ = draft.propose_trade(self.conn, live, user(1), ["Garchomp"], ["Heatran"],
                                        partner_user_id=user(2))
        draft.approve_trade(self.conn, live, record["trade_number"], 999)

        live = draft.get_pool(self.conn, self.draft["id"])
        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        names = [name for name, _ in draft.roster_block(self.conn, live, p1, 12)]
        # Heatran has no round of its own on Player1's sheet, so it takes the
        # first free row - keeping the block a true picture of the roster.
        self.assertEqual(names[0], "Heatran")
        self.assertEqual(names.count("Heatran"), 1)

    def test_export_is_keyed_by_coach_number_with_pool_letters(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        coaches, blocks, overflow = draft.export_for_sheet(
            self.conn, self.draft["league_id"])
        self.assertEqual(coaches, {1: ("Player1", "A"), 2: ("Player2", "A")})
        self.assertEqual(sorted(blocks), [1, 2])
        self.assertEqual(blocks[1][0], ("Garchomp", False))
        self.assertEqual(overflow, {})

    def test_a_roster_too_big_for_a_block_is_reported(self):
        # A block only has room for so many rounds. Trades can push a roster
        # past that, and the sheet cannot show what will not fit.
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        live = draft.get_pool(self.conn, self.draft["id"])
        record, _ = draft.propose_trade(self.conn, live, user(1),
                                        ["Garchomp"], ["A", "B", "C"])
        draft.approve_trade(self.conn, live, record["trade_number"], 999)

        _, blocks, overflow = draft.export_for_sheet(self.conn, self.draft["league_id"])
        self.assertEqual(overflow, {})          # three fit fine in twelve rows
        self.assertEqual([n for n, _ in blocks[1] if n], ["A", "B", "C"])



class TeraCaptainTests(unittest.TestCase):
    """A Tera Captain costs its Tera Tax from BOTH budgets.

    Numbers follow the real example: 115 budget, 28 Tera budget, Great Tusk
    at COST 18 / TT 17.
    """

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, channel_id=400, players=2, budget=115,
                                min_picks=1, max_picks=5, tera_budget=28)

    def p1(self):
        return draft.get_player(self.conn, self.draft["id"], user(1))

    def test_a_normal_pick_leaves_the_tera_budget_alone(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Great Tusk", 18)
        player = self.p1()
        self.assertEqual(player["budget_remaining"], 115 - 18)
        self.assertEqual(player["tera_budget_remaining"], 28)

    def test_a_captain_costs_its_tera_tax_from_both_budgets(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Great Tusk", 17,
                        tera_captain=True)
        player = self.p1()
        self.assertEqual(player["budget_remaining"], 115 - 17)
        self.assertEqual(player["tera_budget_remaining"], 28 - 17)

    def wait_for_player_one(self):
        """Play filler picks until Player1 is on the clock, then return the pool."""
        for _ in range(40):
            live = draft.get_pool(self.conn, self.draft["id"])
            player = draft.current_player(self.conn, live)
            if player["user_id"] == user(1):
                return live
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                            f"Filler{player['id']}R{live['current_round']}", 1)
        raise AssertionError("Player1 never came up")

    def test_at_most_three_captains(self):
        for index in range(1, config.MAX_TERA_CAPTAINS + 2):
            live = self.wait_for_player_one()
            if index <= config.MAX_TERA_CAPTAINS:
                draft.make_pick(self.conn, live, user(1), live["current_round"],
                                f"Cap{index}", 5, tera_captain=True)
            else:
                with self.assertRaises(draft.DraftError) as caught:
                    draft.make_pick(self.conn, live, user(1), live["current_round"],
                                    f"Cap{index}", 5, tera_captain=True)
                self.assertIn("maximum of 3 Tera Captains", str(caught.exception))
                # The fourth is fine as a normal pick.
                draft.make_pick(self.conn, live, user(1), live["current_round"],
                                f"Cap{index}", 5)

        player = self.p1()
        self.assertEqual(draft.tera_captains(self.conn, self.draft["id"], player["id"]), 3)
        self.assertEqual(player["tera_budget_remaining"], 28 - 15)
        self.assertEqual(player["budget_remaining"], 115 - 20)

    def test_tera_budget_is_enforced_separately(self):
        # Affordable on the normal budget, but far beyond the Tera budget.
        with self.assertRaises(draft.DraftError) as caught:
            draft.make_pick(self.conn, self.draft, user(1), 1, "Miraidon", 30,
                            tera_captain=True)
        self.assertIn("Tera budget", str(caught.exception))
        self.assertEqual(self.p1()["budget_remaining"], 115, "nothing was charged")

    def test_no_captains_outside_a_tera_format(self):
        plain = make_draft(self.conn, channel_id=401, players=2, budget=100,
                           min_picks=1, max_picks=3, tera_budget=0)
        with self.assertRaises(draft.DraftError) as caught:
            draft.make_pick(self.conn, plain, user(1), 1, "Great Tusk", 18,
                            tera_captain=True)
        self.assertIn("not a Tera format", str(caught.exception))

    def test_tera_format_works_without_any_document(self):
        # The price comes from /pick, so no sheet is needed.
        self.assertEqual(self.draft["doc_kind"], "none")
        self.assertTrue(self.draft["tera_budget"])
        draft.make_pick(self.conn, self.draft, user(1), 1, "Homebrew", 9,
                        tera_captain=True)
        self.assertEqual(self.p1()["tera_budget_remaining"], 28 - 9)

    def test_editing_a_captain_refunds_both_budgets(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Great Tusk", 17,
                        tera_captain=True)
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.edit_pick(self.conn, live, user(1), 1, "Iron Valiant", 12,
                        tera_captain=True)
        player = self.p1()
        self.assertEqual(player["budget_remaining"], 115 - 12)
        self.assertEqual(player["tera_budget_remaining"], 28 - 12)

    def test_demoting_a_captain_returns_the_tera_points(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Great Tusk", 17,
                        tera_captain=True)
        live = draft.get_pool(self.conn, self.draft["id"])
        # Same Pokemon, now a normal pick at its full COST.
        draft.edit_pick(self.conn, live, user(1), 1, "Great Tusk", 18,
                        tera_captain=False)
        player = self.p1()
        self.assertEqual(player["budget_remaining"], 115 - 18)
        self.assertEqual(player["tera_budget_remaining"], 28, "Tera points refunded")
        self.assertEqual(draft.tera_captains(self.conn, self.draft["id"], player["id"]), 0)

    def test_roster_view_reports_captains(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Great Tusk", 17,
                        tera_captain=True)
        live = draft.get_pool(self.conn, self.draft["id"])
        view = draft.roster_view(self.conn, live, self.p1())
        self.assertTrue(view["tera_format"])
        self.assertEqual(view["tera_captains"], [("Great Tusk", 17)])
        self.assertEqual(view["tera_remaining"], 11)
        self.assertEqual(view["entries"], [("Great Tusk", 17, True)])


class TwoPoolTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.a, self.b = make_two_pool_league(self.conn, players=3)

    def test_pools_are_separate_drafts_in_separate_channels(self):
        self.assertEqual(self.a["label"], "A")
        self.assertEqual(self.b["label"], "B")
        self.assertEqual(self.a["league_id"], self.b["league_id"])
        self.assertNotEqual(self.a["id"], self.b["id"])
        self.assertEqual(draft.get_live_pool(self.conn, 201)["id"], self.a["id"])
        self.assertEqual(draft.get_live_pool(self.conn, 202)["id"], self.b["id"])

    def test_the_same_pokemon_can_go_once_in_each_pool(self):
        draft.make_pick(self.conn, self.a, 1001, 1, "Garchomp", 12)
        draft.make_pick(self.conn, self.b, 2001, 1, "Garchomp", 12)

        self.assertIsNotNone(draft.find_owner(self.conn, self.a["id"], "garchomp"))
        self.assertIsNotNone(draft.find_owner(self.conn, self.b["id"], "garchomp"))
        # ...but not twice inside one pool.
        live = draft.get_pool(self.conn, self.a["id"])
        with self.assertRaises(draft.DraftError):
            draft.make_pick(self.conn, live, 1002, 1, "garchomp", 12)

    def test_turns_advance_independently(self):
        draft.make_pick(self.conn, self.a, 1001, 1, "Mon1", 5)
        a = draft.get_pool(self.conn, self.a["id"])
        b = draft.get_pool(self.conn, self.b["id"])
        self.assertEqual(draft.current_player(self.conn, a)["display_name"], "A2")
        self.assertEqual(draft.current_player(self.conn, b)["display_name"], "B1",
                         "pool B should not have moved")

    def test_coach_numbers_split_the_spreadsheet_grid(self):
        a_numbers = [p["coach_number"] for p in draft.get_players(self.conn, self.a["id"])]
        b_numbers = [p["coach_number"] for p in draft.get_players(self.conn, self.b["id"])]
        self.assertEqual(a_numbers, [1, 2, 3])
        self.assertEqual(b_numbers, [11, 12, 13])

    def test_export_covers_both_pools_with_their_letters(self):
        draft.make_pick(self.conn, self.a, 1001, 1, "Garchomp", 12)
        draft.make_pick(self.conn, self.b, 2001, 1, "Garchomp", 12)
        coaches, blocks, _ = draft.export_for_sheet(self.conn, self.a["league_id"])
        self.assertEqual(coaches[1], ("A1", "A"))
        self.assertEqual(coaches[11], ("B1", "B"))
        self.assertEqual(blocks[1][0], ("Garchomp", False))
        self.assertEqual(blocks[11][0], ("Garchomp", False))

    def test_league_finishes_only_when_both_pools_do(self):
        def play_out(pool):
            for _ in range(60):
                live = draft.get_pool(self.conn, pool["id"])
                if live["status"] != "active":
                    return
                player = draft.current_player(self.conn, live)
                draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                                f"{live['label']}{player['id']}R{live['current_round']}", 1)

        play_out(self.a)
        league = draft.get_league(self.conn, self.a["league_id"])
        self.assertEqual(draft.get_pool(self.conn, self.a["id"])["status"], "complete")
        self.assertEqual(league["status"], "active", "pool B is still drafting")

        play_out(self.b)
        league = draft.get_league(self.conn, self.a["league_id"])
        self.assertEqual(league["status"], "complete")

    def test_two_pools_need_a_template(self):
        with self.assertRaises(draft.DraftError) as caught:
            draft.create_league(
                self.conn, guild_id=1, admin_channel_id=999, name="No Template",
                pools=[("A", 301, [(1, "x"), (2, "y")]), ("B", 302, [(3, "p"), (4, "q")])],
                budget=100, min_picks=1, max_picks=2, base_timer=120, doc_kind="none")
        self.assertIn("template", str(caught.exception))

    def test_two_pools_are_capped_at_ten_each(self):
        self.assertEqual(draft.max_players_per_pool("template", 2), 10)
        self.assertEqual(draft.max_players_per_pool("template", 1), config.MAX_PLAYERS)
        self.assertEqual(draft.coach_numbers_for("A", 3), [1, 2, 3])
        self.assertEqual(draft.coach_numbers_for("B", 3), [11, 12, 13])


class LeagueNameTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")

    def test_names_must_be_unique_while_running(self):
        make_draft(self.conn, channel_id=501, players=2, name="Season 4")
        with self.assertRaises(draft.DraftError) as caught:
            make_draft(self.conn, channel_id=502, players=2, name="season 4")
        self.assertIn("already running", str(caught.exception))

    def test_a_finished_name_can_be_reused(self):
        pool = make_draft(self.conn, channel_id=503, players=2, min_picks=1,
                          max_picks=1, name="Cup")
        for _ in range(2):
            live = draft.get_pool(self.conn, pool["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], 1, f"M{player['id']}", 1)
        make_draft(self.conn, channel_id=504, players=2, name="Cup")

    def test_lookup_by_name(self):
        make_draft(self.conn, channel_id=505, players=2, name="Ogerpon Cup")
        found = draft.require_league_by_name(self.conn, 1, "ogerpon cup")
        self.assertEqual(found["name"], "Ogerpon Cup")
        with self.assertRaises(draft.DraftError) as caught:
            draft.require_league_by_name(self.conn, 1, "Nope")
        self.assertIn("Ogerpon Cup", str(caught.exception), "should list what is running")

    def test_several_leagues_share_one_admin_channel(self):
        make_draft(self.conn, channel_id=506, players=2, name="One")
        make_draft(self.conn, channel_id=507, players=2, name="Two")
        rows = draft.leagues_in_admin_channel(self.conn, 999)
        self.assertEqual([r["name"] for r in rows], ["One", "Two"])


class SubstitutionTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, channel_id=600, players=3, budget=100,
                                min_picks=1, max_picks=3, base_timer=120)

    def test_a_player_asks_to_sub_out(self):
        record = draft.request_substitution(self.conn, self.draft, user(1),
                                            "Player1", "exams next week")
        self.assertEqual(record["kind"], "out")
        self.assertEqual(record["request_number"], 1)
        self.assertEqual(record["status"], "open")

    def test_an_outsider_asks_to_sub_in(self):
        record = draft.request_substitution(self.conn, self.draft, 7777,
                                            "Newcomer", "happy to help")
        self.assertEqual(record["kind"], "in")

    def test_a_reason_is_required(self):
        with self.assertRaises(draft.DraftError):
            draft.request_substitution(self.conn, self.draft, user(1), "Player1", "   ")

    def test_only_one_open_request_each(self):
        draft.request_substitution(self.conn, self.draft, user(1), "Player1", "busy")
        with self.assertRaises(draft.DraftError) as caught:
            draft.request_substitution(self.conn, self.draft, user(1), "Player1", "still busy")
        self.assertIn("already have an open", str(caught.exception))

    def test_substituting_hands_over_the_whole_team(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        draft.skip_current_player(self.conn, self.draft["id"])      # Player2 misses round 1
        live = draft.get_pool(self.conn, self.draft["id"])
        before = draft.get_player(self.conn, self.draft["id"], user(2))
        self.assertEqual(before["current_timer"], 60)

        result = draft.substitute_player(self.conn, live, user(2), 8888, "Stand In")

        self.assertEqual(result["out_name"], "Player2")
        self.assertEqual(result["in_name"], "Stand In")
        self.assertIsNone(draft.get_player(self.conn, self.draft["id"], user(2)))

        after = draft.get_player(self.conn, self.draft["id"], 8888)
        self.assertEqual(after["id"], before["id"], "same seat")
        self.assertEqual(after["draft_position"], before["draft_position"])
        self.assertEqual(after["coach_number"], before["coach_number"])
        self.assertEqual(after["budget_remaining"], before["budget_remaining"])
        # A replacement does not inherit somebody else's penalties.
        self.assertEqual(after["current_timer"], 120)
        self.assertEqual(after["skip_count"], 0)
        # The makeup pick they inherited is still owed.
        self.assertEqual(draft.pending_makeup_rounds(self.conn, self.draft["id"], after["id"]), [1])

    def test_the_roster_travels_with_the_seat(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.substitute_player(self.conn, live, user(1), 8888, "Stand In")

        after = draft.get_player(self.conn, self.draft["id"], 8888)
        view = draft.roster_view(self.conn, live, after)
        self.assertEqual(view["pokemon"], [("Garchomp", 12)])
        # History records the pick under the seat, so it reads as the new name.
        picks = draft.round_picks(self.conn, self.draft["id"], 1)
        self.assertEqual(picks[0]["display_name"], "Stand In")

    def test_cannot_sub_in_somebody_already_drafting(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError) as caught:
            draft.substitute_player(self.conn, live, user(1), user(2), "Player2")
        self.assertIn("already drafting", str(caught.exception))

    def test_substituting_closes_the_open_requests(self):
        draft.request_substitution(self.conn, self.draft, user(1), "Player1", "busy")
        draft.request_substitution(self.conn, self.draft, 8888, "Stand In", "available")
        self.assertEqual(len(draft.open_sub_requests(self.conn, self.draft["league_id"])), 2)

        live = draft.get_pool(self.conn, self.draft["id"])
        draft.substitute_player(self.conn, live, user(1), 8888, "Stand In")
        self.assertEqual(draft.open_sub_requests(self.conn, self.draft["league_id"]), [])


class DraftOnlyModeTests(unittest.TestCase):
    def test_a_draft_only_league_archives_itself(self):
        conn = database.connect(":memory:")
        pool = make_draft(conn, channel_id=700, players=2, budget=50, min_picks=1,
                          max_picks=2, mode="draft_only")
        for _ in range(4):
            live = draft.get_pool(conn, pool["id"])
            player = draft.current_player(conn, live)
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"M{player['id']}{live['current_round']}", 1)

        league = draft.get_league(conn, pool["league_id"])
        self.assertEqual(league["status"], "archived")
        # The channel is free again, but the history is still readable.
        self.assertIsNone(draft.get_live_pool(conn, 700))
        self.assertEqual(len(draft.round_picks(conn, pool["id"], 1)), 2)

    def test_a_full_season_league_stays_open_for_trades(self):
        conn = database.connect(":memory:")
        pool = make_draft(conn, channel_id=701, players=2, budget=50, min_picks=1,
                          max_picks=2, mode="full_season")
        for _ in range(4):
            live = draft.get_pool(conn, pool["id"])
            player = draft.current_player(conn, live)
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"M{player['id']}{live['current_round']}", 1)

        league = draft.get_league(conn, pool["league_id"])
        self.assertEqual(league["status"], "complete")



class ScheduleTests(unittest.TestCase):
    """The bot generates its own schedule when there is no template."""

    def test_round_robin_is_a_real_round_robin(self):
        for count in range(2, config.MAX_PLAYERS + 1):
            with self.subTest(players=count):
                entrants = list(range(1, count + 1))
                schedule = draft.round_robin(entrants, rng=random.Random(count))

                # A full cycle takes one week fewer than the number of
                # entrants, rounded up for a bye.
                self.assertEqual(len(schedule), count - 1 if count % 2 == 0 else count)

                met = collections.Counter()
                for pairs in schedule:
                    self.assertEqual(len(pairs), count // 2)
                    playing = [name for pair in pairs for name in pair]
                    self.assertEqual(len(playing), len(set(playing)),
                                     "nobody may play twice in a week")
                    for home, away in pairs:
                        met[frozenset((home, away))] += 1

                everyone = {frozenset(pair)
                            for pair in itertools.combinations(entrants, 2)}
                self.assertEqual(set(met), everyone, "everybody must meet everybody")
                self.assertTrue(all(times == 1 for times in met.values()),
                                "nobody plays the same opponent twice")

    def test_the_order_is_randomised(self):
        entrants = list(range(1, 9))
        first = draft.round_robin(entrants, rng=random.Random(1))
        second = draft.round_robin(entrants, rng=random.Random(2))
        self.assertNotEqual(first, second)

    def test_a_season_can_be_shortened_or_extended(self):
        short = draft.round_robin(list(range(1, 11)), weeks=3, rng=random.Random(1))
        self.assertEqual(len(short), 3)
        long = draft.round_robin(list(range(1, 7)), weeks=10, rng=random.Random(1))
        self.assertEqual(len(long), 10, "cycles repeat for a longer season")

    def test_too_few_entrants_has_no_fixtures(self):
        self.assertEqual(draft.round_robin([1]), [])
        self.assertEqual(draft.round_robin([]), [])

    def test_fixtures_are_stored_and_readable_by_week(self):
        conn = database.connect(":memory:")
        pool = make_draft(conn, channel_id=900, players=4, min_picks=1, max_picks=2,
                          mode="draft_schedule")
        weeks = draft.generate_fixtures(conn, pool["league_id"], rng=random.Random(5))
        self.assertEqual(weeks, 3)
        self.assertEqual(draft.fixture_weeks(conn, pool["league_id"]), [1, 2, 3])

        pairs = draft.get_fixtures(conn, pool["league_id"], 1)
        self.assertEqual(len(pairs), 2)
        names = {row["display_name"] for pair in pairs for row in pair}
        self.assertEqual(names, {"Player1", "Player2", "Player3", "Player4"})

    def test_fixtures_can_pair_players_from_different_pools(self):
        conn = database.connect(":memory:")
        pool_a, pool_b = make_two_pool_league(conn, name="Cross", players=2)
        draft.generate_fixtures(conn, pool_a["league_id"], rng=random.Random(3))

        pool_of = {}
        for pool in (pool_a, pool_b):
            for player in draft.get_players(conn, pool["id"]):
                pool_of[player["id"]] = pool["label"]

        crossings = 0
        for week in draft.fixture_weeks(conn, pool_a["league_id"]):
            for home, away in draft.get_fixtures(conn, pool_a["league_id"], week):
                if pool_of[home["id"]] != pool_of[away["id"]]:
                    crossings += 1
        self.assertGreater(crossings, 0, "the season should cross the pools")

    def test_regenerating_replaces_the_old_schedule(self):
        conn = database.connect(":memory:")
        pool = make_draft(conn, channel_id=901, players=4, min_picks=1, max_picks=2,
                          mode="draft_schedule")
        draft.generate_fixtures(conn, pool["league_id"], rng=random.Random(1))
        draft.generate_fixtures(conn, pool["league_id"], weeks=2, rng=random.Random(2))
        self.assertEqual(draft.fixture_weeks(conn, pool["league_id"]), [1, 2])

    def test_withdrawn_players_are_left_out_of_a_new_schedule(self):
        conn = database.connect(":memory:")
        pool = make_draft(conn, channel_id=902, players=4, min_picks=1, max_picks=2,
                          mode="draft_schedule")
        draft.remove_player(conn, pool, user(4))
        live = draft.get_pool(conn, pool["id"])
        draft.generate_fixtures(conn, live["league_id"], rng=random.Random(1))

        names = set()
        for week in draft.fixture_weeks(conn, live["league_id"]):
            for home, away in draft.get_fixtures(conn, live["league_id"], week):
                names.add(home["display_name"])
                names.add(away["display_name"])
        self.assertNotIn("Player4", names)


class RemovePlayerTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")

    def test_before_the_draft_starts_the_seat_is_deleted(self):
        pool = make_draft(self.conn, channel_id=1000, players=4, start=False)
        result = draft.remove_player(self.conn, pool, user(2))

        self.assertTrue(result["seat_deleted"])
        self.assertEqual(result["players_left"], 3)
        self.assertIsNone(draft.get_player(self.conn, pool["id"], user(2)))

        # The order closes up, so seats stay 1..N with no gaps.
        seats = draft.get_players(self.conn, pool["id"])
        self.assertEqual([p["draft_position"] for p in seats], [1, 2, 3])
        self.assertEqual([p["display_name"] for p in seats],
                         ["Player1", "Player3", "Player4"])
        self.assertEqual([p["coach_number"] for p in seats], [1, 2, 3],
                         "roster blocks close up too")

    def test_after_the_draft_starts_the_seat_is_kept_and_passed_over(self):
        pool = make_draft(self.conn, channel_id=1001, players=3, budget=100,
                          min_picks=1, max_picks=3)
        draft.make_pick(self.conn, pool, user(1), 1, "Garchomp", 12)
        live = draft.get_pool(self.conn, pool["id"])

        result = draft.remove_player(self.conn, live, user(2))
        self.assertFalse(result["seat_deleted"], "removing a seat mid-draft would "
                                                 "move everybody else's turn")
        self.assertEqual(result["players_left"], 3)

        gone = draft.get_player(self.conn, pool["id"], user(2))
        self.assertEqual(gone["status"], "withdrawn")
        self.assertFalse(draft.player_can_pick(self.conn, live, gone))

        # Player2 was on the clock; the draft moves past them on its own.
        events = draft.begin_turn(self.conn, pool["id"])
        passed = [e for e in events if e["type"] == "passed"]
        self.assertTrue(passed)
        self.assertEqual(passed[0]["display_name"], "Player2")
        live = draft.get_pool(self.conn, pool["id"])
        self.assertEqual(draft.current_player(self.conn, live)["display_name"], "Player3")

    def test_their_pokemon_go_back_in_the_pool(self):
        pool = make_draft(self.conn, channel_id=1002, players=2, budget=100,
                          min_picks=1, max_picks=3)
        draft.make_pick(self.conn, pool, user(1), 1, "Garchomp", 12)
        live = draft.get_pool(self.conn, pool["id"])

        draft.remove_player(self.conn, live, user(1))
        self.assertIsNone(draft.find_owner(self.conn, pool["id"], "garchomp"),
                          "Garchomp should be draftable again")
        # History still records that they drafted it.
        picks = draft.round_picks(self.conn, pool["id"], 1)
        self.assertEqual([p["pokemon"] for p in picks], ["Garchomp"])

    def test_their_pokemon_can_be_kept_out_of_the_pool(self):
        pool = make_draft(self.conn, channel_id=1003, players=2, budget=100,
                          min_picks=1, max_picks=3)
        draft.make_pick(self.conn, pool, user(1), 1, "Garchomp", 12)
        live = draft.get_pool(self.conn, pool["id"])

        draft.remove_player(self.conn, live, user(1), release_roster=False)
        self.assertIsNotNone(draft.find_owner(self.conn, pool["id"], "garchomp"))

    def test_pending_makeups_and_queues_are_cleared(self):
        pool = make_draft(self.conn, channel_id=1004, players=3, budget=100,
                          min_picks=1, max_picks=3)
        draft.queue_picks(self.conn, pool, user(2), 2, ["Heatran"], [10])
        draft.skip_current_player(self.conn, pool["id"])        # Player1 misses round 1
        live = draft.get_pool(self.conn, pool["id"])

        player1 = draft.get_player(self.conn, pool["id"], user(1))
        self.assertEqual(draft.pending_makeup_rounds(self.conn, pool["id"], player1["id"]), [1])

        draft.remove_player(self.conn, live, user(1))
        self.assertEqual(
            draft.pending_makeup_rounds(self.conn, pool["id"], player1["id"]), [])

        player2 = draft.get_player(self.conn, pool["id"], user(2))
        draft.remove_player(self.conn, live, user(2))
        self.assertEqual(draft.get_queue(self.conn, pool["id"], player2["id"], 2), [])

    def test_removing_somebody_twice_is_refused(self):
        pool = make_draft(self.conn, channel_id=1005, players=3, min_picks=1, max_picks=2)
        live = draft.get_pool(self.conn, pool["id"])
        draft.remove_player(self.conn, live, user(2))
        with self.assertRaises(draft.DraftError) as caught:
            draft.remove_player(self.conn, live, user(2))
        self.assertIn("already been removed", str(caught.exception))

    def test_removing_somebody_who_is_not_playing_is_refused(self):
        pool = make_draft(self.conn, channel_id=1006, players=2)
        with self.assertRaises(draft.DraftError):
            draft.remove_player(self.conn, pool, 999999)

    def test_the_draft_ends_when_everybody_left_has_withdrawn(self):
        pool = make_draft(self.conn, channel_id=1007, players=2, budget=100,
                          min_picks=1, max_picks=4)
        live = draft.get_pool(self.conn, pool["id"])
        draft.remove_player(self.conn, live, user(1))
        live = draft.get_pool(self.conn, pool["id"])
        draft.remove_player(self.conn, live, user(2))

        draft.begin_turn(self.conn, pool["id"])
        self.assertEqual(draft.get_pool(self.conn, pool["id"])["status"], "complete")


class LeagueModeTests(unittest.TestCase):
    def test_all_three_modes_are_accepted(self):
        conn = database.connect(":memory:")
        for index, mode in enumerate(config.LEAGUE_MODES):
            pool = make_draft(conn, channel_id=1100 + index, players=2,
                              min_picks=1, max_picks=2, mode=mode,
                              name=f"League {mode}")
            self.assertEqual(pool["mode"], mode)

    def test_an_unknown_mode_is_refused(self):
        conn = database.connect(":memory:")
        with self.assertRaises(draft.DraftError):
            make_draft(conn, channel_id=1110, players=2, mode="whatever")

    def test_draft_schedule_completes_rather_than_archiving(self):
        conn = database.connect(":memory:")
        pool = make_draft(conn, channel_id=1111, players=2, budget=50, min_picks=1,
                          max_picks=2, mode="draft_schedule")
        for _ in range(4):
            live = draft.get_pool(conn, pool["id"])
            player = draft.current_player(conn, live)
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"M{player['id']}{live['current_round']}", 1)

        league = draft.get_league(conn, pool["league_id"])
        self.assertEqual(league["status"], "complete",
                         "it still has a season to run, so it is not archived")



class SkipCountTests(unittest.TestCase):
    """Moderators can correct a skip count, and the timer follows it."""

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, channel_id=1200, players=3, budget=100,
                                min_picks=1, max_picks=4, base_timer=120)

    def p1(self):
        return draft.get_player(self.conn, self.draft["id"], user(1))

    def test_timer_for_skips_matches_the_real_progression(self):
        self.assertEqual([draft.timer_for_skips(120, n) for n in range(5)],
                         [120, 60, 30, 15, 15])
        self.assertGreaterEqual(draft.timer_for_skips(120, 99),
                                config.MINIMUM_TIMER_SECONDS)

    def test_resetting_to_zero_restores_the_full_timer(self):
        draft.skip_current_player(self.conn, self.draft["id"])
        self.assertEqual(self.p1()["current_timer"], 60)
        self.assertEqual(self.p1()["skip_count"], 1)

        live = draft.get_pool(self.conn, self.draft["id"])
        result = draft.set_skip_count(self.conn, live, user(1), 0)

        self.assertEqual(result["before"], {"skips": 1, "timer": 60})
        self.assertEqual(self.p1()["skip_count"], 0)
        self.assertEqual(self.p1()["current_timer"], 120)
        self.assertTrue(result["recalculated"])

    def test_raising_the_count_shrinks_the_timer_to_match(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.set_skip_count(self.conn, live, user(1), 2)
        self.assertEqual(self.p1()["current_timer"], 30)

    def test_an_explicit_timer_overrides_the_calculation(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        result = draft.set_skip_count(self.conn, live, user(1), 1, timer=90)
        self.assertEqual(self.p1()["current_timer"], 90)
        self.assertEqual(self.p1()["skip_count"], 1)
        self.assertFalse(result["recalculated"])

    def test_the_floor_still_applies(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError) as caught:
            draft.set_skip_count(self.conn, live, user(1), 0, timer=1)
        self.assertIn(str(config.MINIMUM_TIMER_SECONDS), str(caught.exception))

    def test_negative_counts_are_refused(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError):
            draft.set_skip_count(self.conn, live, user(1), -1)

    def test_only_that_player_is_affected(self):
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.set_skip_count(self.conn, live, user(1), 3)
        self.assertEqual(self.p1()["current_timer"], 15)
        self.assertEqual(
            draft.get_player(self.conn, self.draft["id"], user(2))["current_timer"], 120)

    def test_a_corrected_player_keeps_drafting_normally(self):
        draft.skip_current_player(self.conn, self.draft["id"])      # Player1 skipped
        live = draft.get_pool(self.conn, self.draft["id"])
        draft.set_skip_count(self.conn, live, user(1), 0)

        # Their makeup pick is untouched - only the clock was corrected.
        player = self.p1()
        self.assertEqual(
            draft.pending_makeup_rounds(self.conn, self.draft["id"], player["id"]), [1])
        pick = draft.make_makeup_pick(self.conn, live, user(1), 1, "Garchomp", 12)
        self.assertEqual(pick["round"], 1)

    def test_an_unknown_player_is_refused(self):
        with self.assertRaises(draft.DraftError):
            draft.set_skip_count(self.conn, self.draft, 999999, 0)



class SecondPoolOptionTests(unittest.TestCase):
    """The optional second pool needs a channel and players, or neither.

    This shipped inverted - a one-pool /setup was rejected while a
    half-specified pool B was accepted - so all four cases are pinned.
    """

    def test_neither_is_a_single_pool_draft(self):
        self.assertTrue(draft.second_pool_is_consistent(False, False))

    def test_both_is_a_two_pool_draft(self):
        self.assertTrue(draft.second_pool_is_consistent(True, True))

    def test_a_channel_with_no_players_is_incomplete(self):
        self.assertFalse(draft.second_pool_is_consistent(True, False))

    def test_players_with_no_channel_are_incomplete(self):
        self.assertFalse(draft.second_pool_is_consistent(False, True))


class CoachMatchingTests(unittest.TestCase):
    """Only ticked Coaches rows take part, and players go to the row that
    names them - so the spreadsheet decides the blocks, not mention order."""

    def entries(self, *pairs):
        return [{"user_id": 100 + i, "display_name": display, "username": username}
                for i, (display, username) in enumerate(pairs)]

    def test_placeholder_rows_are_free_but_named_rows_are_not(self):
        for name in ("Coach 1", "coach 20", "", "   ", None):
            self.assertTrue(draft.is_placeholder_coach(name), name)
        for name in ("bottledducky", "Coach", "Coach Larry", "itsbug"):
            self.assertFalse(draft.is_placeholder_coach(name), name)

    def test_players_are_matched_by_username(self):
        players = self.entries(("Larry", "bottledducky"), ("Alex", "itsbug"))
        slots = [1, 2, 3, 4]
        names = {1: "itsbug", 2: "Coach 2", 3: "bottledducky", 4: "Coach 4"}

        assignment, to_write, unclaimed, leftover = draft.match_coaches(
            players, slots, names)

        # Mention order was Larry then Alex, but the sheet says otherwise.
        self.assertEqual(assignment[1]["display_name"], "Alex")
        self.assertEqual(assignment[3]["display_name"], "Larry")
        self.assertEqual(to_write, [])
        self.assertEqual(unclaimed, [])
        self.assertEqual(leftover, [])

    def test_matching_falls_back_to_the_display_name(self):
        players = self.entries(("Zupho", "some_other_handle"))
        assignment, _, _, leftover = draft.match_coaches(players, [1, 2], {1: "Zupho"})
        self.assertEqual(assignment[1]["display_name"], "Zupho")
        self.assertEqual(leftover, [])

    def test_matching_ignores_case_and_spacing(self):
        players = self.entries(("Larry", "BottledDucky"))
        assignment, _, _, _ = draft.match_coaches(
            players, [1], {1: "  bottledducky "})
        self.assertIn(1, assignment)

    def test_unnamed_rows_are_filled_in_order(self):
        players = self.entries(("Larry", "larry"), ("Alex", "alex"))
        assignment, to_write, _, _ = draft.match_coaches(
            players, [2, 5, 7], {2: "Coach 2", 5: "Coach 5", 7: "Coach 7"})
        self.assertEqual([assignment[n]["display_name"] for n in sorted(assignment)],
                         ["Larry", "Alex"])
        self.assertEqual(to_write, [2, 5], "the bot writes the names it filled in")

    def test_a_ticked_row_nobody_listed_is_reported(self):
        players = self.entries(("Larry", "bottledducky"))
        _, _, unclaimed, _ = draft.match_coaches(
            players, [1, 2], {1: "bottledducky", 2: "nozzermat"})
        self.assertEqual(unclaimed, [2])

    def test_a_player_with_nowhere_to_go_is_reported(self):
        players = self.entries(("Larry", "larry"), ("Ghost", "ghost"))
        _, _, _, leftover = draft.match_coaches(
            players, [1], {1: "someone-else"})
        self.assertEqual([p["display_name"] for p in leftover], ["Larry", "Ghost"])

    def test_a_named_row_is_never_given_to_the_wrong_player(self):
        # Row 2 belongs to nozzermat, who was not listed. An unmatched player
        # must not be dropped into it - their roster would end up in somebody
        # else's block on the spreadsheet.
        players = self.entries(("Larry", "bottledducky"), ("Newcomer", "newcomer"))
        assignment, to_write, unclaimed, leftover = draft.match_coaches(
            players, [1, 2], {1: "bottledducky", 2: "nozzermat"})

        self.assertEqual(assignment[1]["display_name"], "Larry")
        self.assertNotIn(2, assignment)
        self.assertEqual(to_write, [])
        self.assertEqual(unclaimed, [2])
        self.assertEqual([p["display_name"] for p in leftover], ["Newcomer"])

    def test_matched_names_can_be_adopted_so_the_sheet_is_left_alone(self):
        # The cog adopts the sheet's spelling for matched rows, so writing
        # the roster back does not replace a league username with a Discord
        # nickname. This checks the data that makes that possible.
        players = self.entries(("Larry", "bottledducky"))
        assignment, to_write, _, _ = draft.match_coaches(
            players, [1, 2], {1: "bottledducky", 2: "Coach 2"})

        self.assertEqual(sorted(assignment), [1])
        self.assertNotIn(1, to_write, "row 1 already had a name, so leave it")
        # The caller can tell matched rows from filled ones, which is exactly
        # what it needs to know which names are safe to overwrite.
        self.assertEqual(to_write, [])

    def test_coach_slots_keep_the_gaps_from_unticked_rows(self):
        conn = database.connect(":memory:")
        # Rows 5 and 10 unticked, so eight players go to 1-4 and 6-9.
        slots = {"A": [1, 2, 3, 4, 6, 7, 8, 9]}
        roster = [{"user_id": 500 + i, "display_name": f"P{i}", "username": f"p{i}"}
                  for i in range(1, 9)]
        draft.create_league(
            conn, guild_id=1, admin_channel_id=99, name="Ticked",
            pools=[("A", 1300, roster)], budget=115, min_picks=1, max_picks=3,
            base_timer=120, doc_kind="template", doc_url="https://example/x",
            coach_slots=slots)

        pool = draft.get_live_pool(conn, 1300)
        assigned = [p["coach_number"] for p in draft.get_players(conn, pool["id"])]
        self.assertEqual(assigned, [1, 2, 3, 4, 6, 7, 8, 9],
                         "block 5 is skipped because its box is unticked")
        self.assertEqual([p["draft_position"] for p in draft.get_players(conn, pool["id"])],
                         list(range(1, 9)))

    def test_more_players_than_ticked_rows_is_refused(self):
        conn = database.connect(":memory:")
        roster = [{"user_id": 600 + i, "display_name": f"P{i}", "username": f"p{i}"}
                  for i in range(1, 5)]
        with self.assertRaises(draft.DraftError) as caught:
            draft.create_league(
                conn, guild_id=1, admin_channel_id=99, name="Too Many",
                pools=[("A", 1301, roster)], budget=115, min_picks=1, max_picks=3,
                base_timer=120, doc_kind="template", doc_url="https://example/x",
                coach_slots={"A": [1, 2]})
        self.assertIn("only has 2 ticked coach row", str(caught.exception))

    def test_removal_shuffles_players_up_through_the_gaps(self):
        conn = database.connect(":memory:")
        roster = [{"user_id": 700 + i, "display_name": f"P{i}", "username": f"p{i}"}
                  for i in range(1, 5)]
        draft.create_league(
            conn, guild_id=1, admin_channel_id=99, name="Gaps",
            pools=[("A", 1302, roster)], budget=115, min_picks=1, max_picks=3,
            base_timer=120, doc_kind="template", doc_url="https://example/x",
            coach_slots={"A": [1, 3, 6, 9]})
        pool = draft.get_live_pool(conn, 1302)
        self.assertEqual([p["coach_number"] for p in draft.get_players(conn, pool["id"])],
                         [1, 3, 6, 9])

        draft.remove_player(conn, pool, 702)        # P2, who held block 3
        left = draft.get_players(conn, pool["id"])
        self.assertEqual([p["display_name"] for p in left], ["P1", "P3", "P4"])
        self.assertEqual([p["coach_number"] for p in left], [1, 3, 6],
                         "they move up into the earliest blocks the pool owns")



class ScheduledStartTests(unittest.TestCase):
    """A draft can be set to begin by itself at a Discord timestamp."""

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.pool = make_draft(self.conn, channel_id=1400, players=3, budget=100,
                               min_picks=1, max_picks=3, start=False)

    def test_discord_timestamps_in_every_style(self):
        now = 1760000000
        for raw in ["<t:1760000100:F>", "<t:1760000100:R>", "<t:1760000100:t>",
                    "<t:1760000100>", "1760000100",
                    "starts at <t:1760000100:F> sharp"]:
            with self.subTest(raw=raw):
                self.assertEqual(draft.parse_start_time(raw, now=now), 1760000100.0)

    def test_unreadable_times_are_refused(self):
        for raw in ["tomorrow", "8pm", "", None, "<t:abc>"]:
            with self.subTest(raw=raw):
                with self.assertRaises(draft.DraftError):
                    draft.parse_start_time(raw, now=1760000000)

    def test_a_time_in_the_past_is_refused(self):
        with self.assertRaises(draft.DraftError) as caught:
            draft.parse_start_time("<t:1759999999:F>", now=1760000000)
        self.assertIn("already passed", str(caught.exception))

    def test_scheduling_stores_the_time_and_does_not_start(self):
        when = 1760000100.0
        updated = draft.schedule_pool_start(self.conn, self.pool, when)
        self.assertEqual(updated["scheduled_start"], when)
        self.assertEqual(updated["status"], "setup", "it must not start yet")

    def test_a_pool_becomes_due_only_at_its_time(self):
        when = 1760000100.0
        draft.schedule_pool_start(self.conn, self.pool, when)

        self.assertEqual(draft.due_scheduled_pools(self.conn, now=when - 1), [])
        due = draft.due_scheduled_pools(self.conn, now=when)
        self.assertEqual([row["id"] for row in due], [self.pool["id"]])

    def test_starting_clears_the_schedule(self):
        draft.schedule_pool_start(self.conn, self.pool, 1760000100.0)
        live = draft.get_pool(self.conn, self.pool["id"])
        draft.start_pool(self.conn, live)

        started = draft.get_pool(self.conn, self.pool["id"])
        self.assertEqual(started["status"], "active")
        self.assertIsNone(started["scheduled_start"],
                          "a started pool must not be picked up again")
        self.assertEqual(draft.due_scheduled_pools(self.conn, now=2000000000), [])

    def test_a_schedule_can_be_cancelled(self):
        draft.schedule_pool_start(self.conn, self.pool, 1760000100.0)
        live = draft.get_pool(self.conn, self.pool["id"])
        draft.cancel_scheduled_start(self.conn, live)
        self.assertIsNone(draft.get_pool(self.conn, self.pool["id"])["scheduled_start"])

    def test_an_already_started_pool_cannot_be_scheduled(self):
        live = draft.get_pool(self.conn, self.pool["id"])
        draft.start_pool(self.conn, live)
        live = draft.get_pool(self.conn, self.pool["id"])
        with self.assertRaises(draft.DraftError):
            draft.schedule_pool_start(self.conn, live, 1760000100.0)


class QueueBeforeTheDraftTests(unittest.TestCase):
    """Queueing before the draft starts is what makes an unattended round 1
    pick possible."""

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.pool = make_draft(self.conn, channel_id=1401, players=3, budget=100,
                               min_picks=1, max_picks=3, start=False)

    def test_a_queue_can_be_set_before_the_draft_starts(self):
        self.assertEqual(self.pool["status"], "setup")
        draft.queue_picks(self.conn, self.pool, user(1), 1,
                          ["Garchomp", "Heatran"], [12, 10])

        player = draft.get_player(self.conn, self.pool["id"], user(1))
        queued = draft.get_queue(self.conn, self.pool["id"], player["id"], 1)
        self.assertEqual([row["pokemon"] for row in queued], ["Garchomp", "Heatran"])

    def test_round_one_fires_the_moment_the_draft_starts(self):
        draft.queue_picks(self.conn, self.pool, user(1), 1, ["Garchomp"], [12])
        draft.queue_picks(self.conn, self.pool, user(2), 1, ["Heatran"], [10])

        live = draft.get_pool(self.conn, self.pool["id"])
        events = draft.start_pool(self.conn, live)

        auto = [e for e in events if e["type"] == "auto_pick"]
        self.assertEqual([(e["display_name"], e["pokemon"]) for e in auto],
                         [("Player1", "Garchomp"), ("Player2", "Heatran")])

        # Play rolls straight on to the first player who did not queue.
        after = draft.get_pool(self.conn, self.pool["id"])
        self.assertEqual(draft.current_player(self.conn, after)["display_name"], "Player3")
        self.assertEqual(draft.get_player(self.conn, self.pool["id"],
                                          user(1))["budget_remaining"], 88)

    def test_everybody_queueing_plays_the_whole_first_round(self):
        for seat in (1, 2, 3):
            draft.queue_picks(self.conn, self.pool, user(seat), 1, [f"Mon{seat}"], [5])

        live = draft.get_pool(self.conn, self.pool["id"])
        draft.start_pool(self.conn, live)

        picks = draft.round_picks(self.conn, self.pool["id"], 1)
        self.assertEqual([p["pokemon"] for p in picks], ["Mon1", "Mon2", "Mon3"])
        after = draft.get_pool(self.conn, self.pool["id"])
        self.assertEqual(after["current_round"], 2, "round 1 completed on its own")

    def test_a_queue_still_cannot_be_set_for_a_finished_draft(self):
        conn = database.connect(":memory:")
        pool = make_draft(conn, channel_id=1402, players=2, budget=50,
                          min_picks=1, max_picks=1)
        for _ in range(2):
            live = draft.get_pool(conn, pool["id"])
            player = draft.current_player(conn, live)
            draft.make_pick(conn, live, player["user_id"], 1, f"M{player['id']}", 1)

        finished = draft.get_pool(conn, pool["id"])
        self.assertEqual(finished["status"], "complete")
        with self.assertRaises(draft.DraftError):
            draft.queue_picks(conn, finished, user(1), 1, ["Late"], [5])



class MigrationTests(unittest.TestCase):
    """An upgrade must not break an existing database.

    CREATE TABLE IF NOT EXISTS does nothing to a table that already exists,
    so adding a column used to leave a deployed bot throwing "no such
    column" on every command that touched it. This happened twice.
    """

    def build_old_database(self):
        import sqlite3
        handle, path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        old = sqlite3.connect(path)
        old.executescript("""
            CREATE TABLE leagues (id INTEGER PRIMARY KEY, guild_id INTEGER, name TEXT,
              admin_channel_id INTEGER, mode TEXT, doc_kind TEXT, doc_url TEXT,
              budget INTEGER, tera_budget INTEGER, min_picks INTEGER, max_picks INTEGER,
              base_timer INTEGER, max_trades INTEGER, status TEXT, created_at TEXT,
              started_at TEXT, finished_at TEXT);
            CREATE TABLE pools (id INTEGER PRIMARY KEY, league_id INTEGER, label TEXT,
              channel_id INTEGER, status TEXT, current_round INTEGER,
              current_turn_index INTEGER, turn_deadline REAL, queue_exhausted INTEGER,
              halved_this_turn INTEGER, started_at TEXT, finished_at TEXT);
            CREATE VIEW pool_view AS SELECT pools.*, leagues.name AS league_name
              FROM pools JOIN leagues ON leagues.id = pools.league_id;
            INSERT INTO leagues (id, name, status, budget, min_picks, max_picks,
              base_timer, mode, doc_kind) VALUES
              (1, 'Old League', 'setup', 115, 1, 3, 120, 'full_season', 'none');
            INSERT INTO pools (id, league_id, label, channel_id, status)
              VALUES (1, 1, 'A', 4242, 'setup');
        """)
        old.commit()
        old.close()
        return path

    def test_missing_columns_are_added_and_data_survives(self):
        path = self.build_old_database()
        try:
            conn = database.connect(path)

            columns = {row["name"] for row in conn.execute("PRAGMA table_info(pools)")}
            self.assertIn("scheduled_start", columns)

            league = conn.execute("SELECT name FROM leagues").fetchone()
            self.assertEqual(league["name"], "Old League", "existing data must survive")
            conn.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)

    def test_stale_views_are_rebuilt(self):
        path = self.build_old_database()
        try:
            conn = database.connect(path)
            columns = [d[0] for d in
                       conn.execute("SELECT * FROM pool_view LIMIT 0").description]
            # The old view had almost none of these.
            for needed in ("scheduled_start", "tera_budget", "max_picks", "doc_kind"):
                self.assertIn(needed, columns)

            # And the queries that use it actually run.
            self.assertEqual(draft.due_scheduled_pools(conn), [])
            self.assertIsNotNone(draft.get_live_pool(conn, 4242))
            conn.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)

    def test_migrating_twice_is_harmless(self):
        path = self.build_old_database()
        try:
            database.connect(path).close()
            conn = database.connect(path)
            self.assertEqual(database.migrate(conn), [],
                             "a second run should have nothing to add")
            conn.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.remove(path + suffix)

    def test_every_added_column_names_a_real_table(self):
        conn = database.connect(":memory:")
        tables = {row["name"] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        for table, column, _ in database.ADDED_COLUMNS:
            with self.subTest(column=f"{table}.{column}"):
                self.assertIn(table, tables)
                columns = {row["name"] for row in conn.execute(
                    f"PRAGMA table_info({table})")}
                self.assertIn(column, columns,
                              "ADDED_COLUMNS must match the current schema")



if __name__ == "__main__":
    unittest.main()
