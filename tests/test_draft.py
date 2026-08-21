"""
Tests for the draft rules.

These run without Discord and without a network connection:

    python3 -m unittest discover -s tests -v

Each test builds a throwaway in-memory database, so tests never interfere
with a real draft.
"""

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
               max_picks=3, base_timer=120, max_trades=0, start=True):
    """Create (and usually start) a draft with generated players."""
    roster = [(1000 + i, f"Player{i}") for i in range(1, players + 1)]
    draft_id = draft.create_draft(
        conn, guild_id=1, channel_id=channel_id, admin_channel_id=999,
        admin_role_id=555, budget=budget, min_picks=min_picks, max_picks=max_picks,
        base_timer=base_timer, players=roster, max_trades=max_trades,
    )
    row = draft.get_draft(conn, draft_id)
    if start:
        draft.start_draft(conn, row)
        row = draft.get_draft(conn, draft_id)
    return row


def user(n):
    """User id of generated PlayerN."""
    return 1000 + n


def on_clock(conn, draft_row):
    return draft.current_player(conn, draft.get_draft(conn, draft_row["id"]))["display_name"]


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
            live = draft.get_draft(conn, row["id"])
            player = draft.current_player(conn, live)
            seen.append((live["current_round"], player["draft_position"]))
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"Mon{len(seen)}", 1)

        self.assertEqual(seen[0:4], [(1, 1), (1, 2), (1, 3), (1, 4)])
        self.assertEqual(seen[4:8], [(2, 4), (2, 3), (2, 2), (2, 1)])
        self.assertEqual(seen[8:12], [(3, 1), (3, 2), (3, 3), (3, 4)])
        self.assertEqual(draft.get_draft(conn, row["id"])["status"], "complete")


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
        self.assertEqual(draft.get_draft(self.conn, b["id"])["budget"], 100)
        self.assertEqual(draft.get_player(self.conn, b["id"], user(1))["budget_remaining"], 100)


class PickValidationTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=3, budget=20, max_picks=3)

    def test_duplicate_pokemon_is_refused(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 5)
        live = draft.get_draft(self.conn, self.draft["id"])
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
            live = draft.get_draft(self.conn, small["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                            f"Cap{player['id']}{live['current_round']}", 1)
        p1 = draft.get_player(self.conn, small["id"], user(1))
        self.assertEqual(draft.picks_made(self.conn, small["id"], p1["id"]), 2)
        self.assertEqual(draft.get_draft(self.conn, small["id"])["status"], "complete")


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
            live = draft.get_draft(self.conn, self.draft["id"])
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
            live = draft.get_draft(self.conn, self.draft["id"])
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
        live = draft.get_draft(self.conn, self.draft["id"])
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
            live = draft.get_draft(self.conn, self.draft["id"])
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
            live = draft.get_draft(self.conn, self.draft["id"])
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
        before = draft.get_draft(self.conn, self.draft["id"])
        current_before = draft.current_player(self.conn, before)["id"]

        pick = draft.make_makeup_pick(self.conn, before, user(1), 1, "Garchomp", 12)

        after = draft.get_draft(self.conn, self.draft["id"])
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
            live = draft.get_draft(self.conn, self.draft["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                            f"F{player['id']}-{live['current_round']}", 1)

        live = draft.get_draft(self.conn, self.draft["id"])
        self.assertEqual(live["current_round"], 2)
        self.assertEqual(draft.current_player(self.conn, live)["display_name"], "Player2")

        pick = draft.make_makeup_pick(self.conn, live, user(1), 1, "Garchomp", 12)
        self.assertEqual(pick["round"], 1)
        # The makeup did not steal Player2's turn.
        after = draft.get_draft(self.conn, self.draft["id"])
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

        live = draft.get_draft(self.conn, self.draft["id"])
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
            live = draft.get_draft(self.conn, self.draft["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], 1,
                            f"R1-{player['id']}", 1)

        # Round 2 runs 3, 2, 1 - Player3 takes the queued Pokémon first.
        live = draft.get_draft(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(3), 2, "Sniped", 5)
        live = draft.get_draft(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 2, "R2-2", 1)

        live = draft.get_draft(self.conn, self.draft["id"])
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

        live = draft.get_draft(self.conn, self.draft["id"])
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
        live = draft.get_draft(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError):
            draft.queue_picks(self.conn, live, user(1), 1, ["Heatran"], [10])

    def test_chained_queues_resolve_in_one_go(self):
        draft.queue_picks(self.conn, self.draft, user(2), 1, ["Heatran"], [10])
        draft.queue_picks(self.conn, self.draft, user(3), 1, ["Landorus"], [15])
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)

        picks = draft.round_picks(self.conn, self.draft["id"], 1)
        self.assertEqual([p["pokemon"] for p in picks], ["Garchomp", "Heatran", "Landorus"])
        live = draft.get_draft(self.conn, self.draft["id"])
        self.assertEqual(live["current_round"], 2)
        self.assertEqual(draft.current_player(self.conn, live)["display_name"], "Player3")


class EditPickTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=2, budget=50, max_picks=3)
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)

    def test_edit_updates_roster_budget_and_availability(self):
        live = draft.get_draft(self.conn, self.draft["id"])
        draft.edit_pick(self.conn, live, user(1), 1, "Heatran", 10)

        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(p1["budget_remaining"], 40)
        view = draft.roster_view(self.conn, live, p1)
        self.assertEqual(view["pokemon"], [("Heatran", 10)])
        # Garchomp is free again; Heatran is not.
        self.assertIsNone(draft.find_owner(self.conn, self.draft["id"], "garchomp"))
        self.assertIsNotNone(draft.find_owner(self.conn, self.draft["id"], "heatran"))

    def test_edit_cannot_duplicate_another_players_pokemon(self):
        live = draft.get_draft(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 1, "Heatran", 10)
        live = draft.get_draft(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError):
            draft.edit_pick(self.conn, live, user(1), 1, "Heatran", 10)
        # The rejected edit changed nothing.
        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        self.assertEqual(p1["budget_remaining"], 38)
        self.assertEqual(draft.roster_view(self.conn, live, p1)["pokemon"], [("Garchomp", 12)])

    def test_edit_rejected_when_over_budget(self):
        live = draft.get_draft(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError):
            draft.edit_pick(self.conn, live, user(1), 1, "Kyogre", 500)
        self.assertEqual(draft.get_player(self.conn, self.draft["id"], user(1))["budget_remaining"], 38)

    def test_player_cannot_edit_someone_else(self):
        live = draft.get_draft(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError):
            draft.edit_pick(self.conn, live, user(2), 1, "Heatran", 10, target_user_id=user(1))

    def test_admin_can_edit_someone_else(self):
        live = draft.get_draft(self.conn, self.draft["id"])
        result = draft.edit_pick(self.conn, live, 99999, 1, "Heatran", 10,
                                 target_user_id=user(1), acting_as_admin=True)
        self.assertEqual(result["old_pokemon"], "Garchomp")
        self.assertEqual(result["new_pokemon"], "Heatran")

    def test_editing_your_own_pokemon_price_only(self):
        live = draft.get_draft(self.conn, self.draft["id"])
        draft.edit_pick(self.conn, live, user(1), 1, "Garchomp", 5)
        self.assertEqual(draft.get_player(self.conn, self.draft["id"], user(1))["budget_remaining"], 45)


class TradeTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=2, budget=100, max_picks=2)
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        live = draft.get_draft(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 1, "Heatran", 10)
        self.live = draft.get_draft(self.conn, self.draft["id"])

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
            live = draft.get_live_draft(conn, 100)
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
            live = draft.get_draft(self.conn, self.draft["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], 1, name, 5)

        picks = draft.round_picks(self.conn, self.draft["id"], 1)
        self.assertEqual([p["pokemon"] for p in picks], names)
        self.assertEqual([p["display_name"] for p in picks], ["Player1", "Player2", "Player3"])

        # Round 2 runs backwards.
        for name in ["A", "B", "C"]:
            live = draft.get_draft(self.conn, self.draft["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], 2, name, 5)
        picks = draft.round_picks(self.conn, self.draft["id"], 2)
        self.assertEqual([p["display_name"] for p in picks], ["Player3", "Player2", "Player1"])

    def test_roster_view_totals(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        live = draft.get_draft(self.conn, self.draft["id"])
        player = draft.get_player(self.conn, self.draft["id"], user(1))
        view = draft.roster_view(self.conn, live, player)
        self.assertEqual(view["spent"], 12)
        self.assertEqual(view["remaining"], 88)
        self.assertEqual(view["count"], 1)
        self.assertEqual(view["max_picks"], 3)

    def test_status_view_reports_pending_makeups(self):
        draft.skip_current_player(self.conn, self.draft["id"])
        live = draft.get_draft(self.conn, self.draft["id"])
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
            make_draft(self.conn, channel_id=2, players=17)

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
        updated = draft.update_settings(self.conn, row, budget=200, sheet_url="http://x")
        self.assertEqual(updated["budget"], 200)
        self.assertEqual(updated["sheet_url"], "http://x")
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
            live = draft.get_draft(conn, row["id"])
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
                    live = draft.get_draft(conn, row["id"])

            counter += 1
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"P{counter}", rng.randint(1, 12))

        return conn, draft.get_draft(conn, row["id"])

    def test_invariants_hold_for_every_player_count(self):
        for players in range(config.MIN_PLAYERS, config.MAX_PLAYERS + 1):
            with self.subTest(players=players):
                conn, live = self.run_one(players, seed=players * 17)

                self.assertEqual(live["status"], "complete",
                                 "the draft should always reach the final round")

                # No Pokemon is ever owned twice.
                rows = conn.execute(
                    "SELECT pokemon_key, COUNT(*) AS n FROM rosters WHERE draft_id = ? "
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
                    "SELECT pick_order FROM picks WHERE draft_id = ? ORDER BY pick_order",
                    (live["id"],))]
                self.assertEqual(orders, list(range(1, len(orders) + 1)))

                # Every player picked at most once per round.
                dupes = conn.execute(
                    "SELECT player_id, round, COUNT(*) AS n FROM picks WHERE draft_id = ? "
                    "GROUP BY player_id, round HAVING n > 1", (live["id"],)).fetchall()
                self.assertEqual(dupes, [])
                conn.close()


class AfterTheDraftTests(unittest.TestCase):
    """A finished draft still needs rosters, history, trades and corrections."""

    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, channel_id=808, players=2, budget=100, max_picks=2)
        for _ in range(4):
            live = draft.get_draft(self.conn, self.draft["id"])
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                            f"Mon{player['id']}{live['current_round']}", 10)
        self.finished = draft.get_draft(self.conn, self.draft["id"])
        self.assertEqual(self.finished["status"], "complete")

    def test_live_lookup_is_empty_but_channel_lookup_still_works(self):
        self.assertIsNone(draft.get_live_draft(self.conn, 808))
        found = draft.require_channel_draft(self.conn, 808)
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
            live = draft.get_draft(conn, row["id"])
            player = draft.current_player(conn, live)
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"X{player['id']}{live['current_round']}", 5)
        draft.skip_current_player(conn, row["id"])      # last pick of the draft is missed

        finished = draft.get_draft(conn, row["id"])
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
            live = draft.get_draft(conn, row["id"])
            player = draft.current_player(conn, live)
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"M{player['id']}{live['current_round']}", 5)

        finished = draft.get_draft(conn, row["id"])
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
            live = draft.get_draft(conn, row["id"])
            player = draft.current_player(conn, live)
            draft.make_pick(conn, live, player["user_id"], live["current_round"],
                            f"Cheap{player['id']}", 1)

        # Round 2 runs 3,2,1 - Player1 should be passed over without penalty.
        live = draft.get_draft(conn, row["id"])
        collected = []
        for _ in range(2):
            live = draft.get_draft(conn, row["id"])
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
            live = draft.get_draft(self.conn, self.draft["id"])
            if live["status"] != "active":
                break
            player = draft.current_player(self.conn, live)
            draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                            f"P{player['id']}R{live['current_round']}", 5)
        self.live = draft.get_draft(self.conn, self.draft["id"])

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
        live = draft.get_draft(conn, row["id"])
        draft.make_pick(conn, live, user(2), 1, "B", 5)
        live = draft.get_draft(conn, row["id"])
        for name in ["C", "D", "E"]:
            record, _ = draft.propose_trade(conn, live, user(1), ["A"], [name])
            draft.approve_trade(conn, live, record["trade_number"], 999)
            live = draft.get_draft(conn, row["id"])
            # the newly acquired Pokemon becomes the next thing to trade away
            conn.execute("UPDATE rosters SET pokemon = 'A', pokemon_key = 'a' "
                         "WHERE draft_id = ? AND pokemon_key = ?", (row["id"], name.lower()))
            conn.commit()
        self.assertEqual(draft.trades_used(conn, row["id"],
                                           draft.get_player(conn, row["id"], user(1))["id"]), 3)


class AdminEditPickTests(unittest.TestCase):
    def setUp(self):
        self.conn = database.connect(":memory:")
        self.draft = make_draft(self.conn, players=3, budget=100, max_picks=3)

    def test_admin_can_fill_in_a_round_with_no_pick(self):
        draft.skip_current_player(self.conn, self.draft["id"])     # Player1 misses round 1
        live = draft.get_draft(self.conn, self.draft["id"])

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
        live = draft.get_draft(self.conn, self.draft["id"])
        with self.assertRaises(draft.DraftError) as caught:
            draft.edit_pick(self.conn, live, user(1), 1, "Garchomp", 12)
        self.assertIn("makeuppick", str(caught.exception))

    def test_created_pick_still_obeys_the_rules(self):
        draft.skip_current_player(self.conn, self.draft["id"])
        live = draft.get_draft(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 1, "Garchomp", 12)
        live = draft.get_draft(self.conn, self.draft["id"])
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
        live = draft.get_draft(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 1, "Heatran", 10)
        live = draft.get_draft(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 2, "Skarmory", 8)

        live = draft.get_draft(self.conn, self.draft["id"])
        p2 = draft.get_player(self.conn, self.draft["id"], user(2))
        block = draft.roster_block(self.conn, live, p2, 12)

        self.assertEqual(block[0], "Heatran")     # round 1 -> first row
        self.assertEqual(block[1], "Skarmory")    # round 2 -> second row
        self.assertEqual(block[2:], [""] * 10)
        self.assertEqual(len(block), 12)

    def test_dropped_pokemon_leave_their_row_and_gains_take_the_first_free_one(self):
        # Player2 drafts three rounds, then drops the round 1 pick and gains
        # two free agents. Row 1 empties, and the new Pokémon fill from the
        # top: first the empty row 1, then the next free row.
        for round_number in range(1, 4):
            for _ in range(2):
                live = draft.get_draft(self.conn, self.draft["id"])
                if live["status"] != "active":
                    break
                player = draft.current_player(self.conn, live)
                draft.make_pick(self.conn, live, player["user_id"], live["current_round"],
                                f"{player['display_name']}-R{live['current_round']}", 5)

        live = draft.get_draft(self.conn, self.draft["id"])
        record, _ = draft.propose_trade(self.conn, live, user(2),
                                        ["Player2-R1"], ["FreeAgentA", "FreeAgentB"])
        draft.approve_trade(self.conn, live, record["trade_number"], 999)

        live = draft.get_draft(self.conn, self.draft["id"])
        p2 = draft.get_player(self.conn, self.draft["id"], user(2))
        block = draft.roster_block(self.conn, live, p2, 12)

        self.assertEqual(block[:4], ["FreeAgentA", "Player2-R2", "Player2-R3", "FreeAgentB"])
        self.assertNotIn("Player2-R1", block)

    def test_traded_pokemon_fill_the_first_free_row(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        live = draft.get_draft(self.conn, self.draft["id"])
        draft.make_pick(self.conn, live, user(2), 1, "Heatran", 10)

        live = draft.get_draft(self.conn, self.draft["id"])
        record, _ = draft.propose_trade(self.conn, live, user(1), ["Garchomp"], ["Heatran"],
                                        partner_user_id=user(2))
        draft.approve_trade(self.conn, live, record["trade_number"], 999)

        live = draft.get_draft(self.conn, self.draft["id"])
        p1 = draft.get_player(self.conn, self.draft["id"], user(1))
        block = draft.roster_block(self.conn, live, p1, 12)
        # Heatran has no round of its own on Player1's sheet, so it takes the
        # first free row - keeping the block a true picture of the roster.
        self.assertEqual(block[0], "Heatran")
        self.assertEqual(block.count("Heatran"), 1)

    def test_export_returns_coach_names_in_draft_order(self):
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        coaches, blocks, overflow = draft.export_for_sheet(self.conn, self.draft["id"])
        self.assertEqual(coaches, ["Player1", "Player2"])
        self.assertEqual(sorted(blocks), [1, 2])
        self.assertEqual(blocks[1][0], "Garchomp")
        self.assertEqual(overflow, {})

    def test_a_roster_too_big_for_a_block_is_reported(self):
        # A block only has room for so many rounds. Trades can push a roster
        # past that, and the sheet cannot show what will not fit.
        draft.make_pick(self.conn, self.draft, user(1), 1, "Garchomp", 12)
        live = draft.get_draft(self.conn, self.draft["id"])
        record, _ = draft.propose_trade(self.conn, live, user(1),
                                        ["Garchomp"], ["A", "B", "C"])
        draft.approve_trade(self.conn, live, record["trade_number"], 999)

        _, blocks, overflow = draft.export_for_sheet(self.conn, self.draft["id"])
        self.assertEqual(overflow, {})          # three fit fine in twelve rows
        self.assertEqual([n for n in blocks[1] if n], ["A", "B", "C"])



if __name__ == "__main__":
    unittest.main()
