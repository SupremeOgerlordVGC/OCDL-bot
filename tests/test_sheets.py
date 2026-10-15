"""
Tests for the Google Sheets layer that need no network connection.

These cover the parts most likely to go wrong quietly: working out which
cell a pick belongs in, and deciding whether a Pokémon is banned.
"""

import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config      # noqa: E402
import sheets      # noqa: E402


class CostParsingTests(unittest.TestCase):
    def test_numbers_are_costs(self):
        self.assertEqual(sheets.parse_cost("12"), 12)
        self.assertEqual(sheets.parse_cost(" 7 "), 7)
        self.assertEqual(sheets.parse_cost("3.0"), 3)
        self.assertEqual(sheets.parse_cost(1), 1)

    def test_tera_flag_column_sits_beside_the_name(self):
        # The tick box is three columns right of the Pokemon name, and must
        # never land on the image or cost formulas either side of it.
        self.assertEqual(sheets.SheetSync._tera_cell(1, 1), "F6")
        self.assertEqual(sheets.SheetSync._tera_cell(2, 1), "M6")
        self.assertEqual(sheets.SheetSync._tera_cell(20, 12), "AH64")

    def test_currency_formatting_is_tolerated(self):
        # The template's cost column is formatted as currency. The bot asks
        # Google for unformatted values, but a league that types costs as
        # text can still produce "$12" - which must not read as banned.
        self.assertEqual(sheets.parse_cost("$12"), 12)
        self.assertEqual(sheets.parse_cost("$2"), 2)
        self.assertEqual(sheets.parse_cost(" $ 14 "), 14)
        self.assertEqual(sheets.parse_cost("1,200"), 1200)
        # A currency symbol does not rescue a banned marker or a placeholder.
        self.assertIsNone(sheets.parse_cost("$99"))
        self.assertIsNone(sheets.parse_cost("$"))
        self.assertIsNone(sheets.parse_cost("$B"))

    def test_banned_and_unpriced_forms(self):
        # Blank, any text, and the placeholder numbers all mean "not draftable".
        for raw in ["", None, " ", "B", "Banned", "BANNED", "banned", "-", "n/a",
                    "99", "999", 99, 999]:
            with self.subTest(raw=raw):
                self.assertIsNone(sheets.parse_cost(raw))

    def test_placeholder_numbers_come_from_config(self):
        self.assertIn(99, config.BANNED_COST_VALUES)
        self.assertIn(999, config.BANNED_COST_VALUES)


class CellMappingTests(unittest.TestCase):
    """Each coach's picks must land in that coach's block and nowhere else."""

    def test_first_round_lands_on_the_block_start(self):
        for position, start in enumerate(config.SHEET_LAYOUT["roster_blocks"], start=1):
            with self.subTest(position=position):
                self.assertEqual(sheets.SheetSync._roster_cell(position, 1), start)

    def test_rounds_walk_down_the_same_column(self):
        # Coach 1's block starts at C6 and is 12 rounds tall.
        self.assertEqual(sheets.SheetSync._roster_cell(1, 1), "C6")
        self.assertEqual(sheets.SheetSync._roster_cell(1, 12), "C17")
        # Coach 11 is the first of pool B, bottom half of the grid.
        self.assertEqual(sheets.SheetSync._roster_cell(11, 1), "C38")
        # Coach 20 sits in the bottom right.
        self.assertEqual(sheets.SheetSync._roster_cell(20, 1), "AE53")
        self.assertEqual(sheets.SheetSync._roster_cell(20, 12), "AE64")

    def test_no_two_coaches_share_a_cell(self):
        rows = config.SHEET_LAYOUT["roster_rows"]
        seen = set()
        for position in range(1, len(config.SHEET_LAYOUT["roster_blocks"]) + 1):
            for round_number in range(1, rows + 1):
                cell = sheets.SheetSync._roster_cell(position, round_number)
                self.assertNotIn(cell, seen, f"{cell} used twice")
                seen.add(cell)
        self.assertEqual(len(seen), len(config.SHEET_LAYOUT["roster_blocks"]) * rows)

    def test_out_of_range_is_refused(self):
        rows = config.SHEET_LAYOUT["roster_rows"]
        with self.assertRaises(sheets.SheetError):
            sheets.SheetSync._roster_cell(21, 1)          # no such coach
        with self.assertRaises(sheets.SheetError):
            sheets.SheetSync._roster_cell(1, rows + 1)    # block is not that tall
        with self.assertRaises(sheets.SheetError):
            sheets.SheetSync._roster_cell(0, 1)


class ReferenceParsingTests(unittest.TestCase):
    def test_split_cell(self):
        self.assertEqual(sheets.split_cell("C4"), ("C", 4))
        self.assertEqual(sheets.split_cell("AA15"), ("AA", 15))
        self.assertEqual(sheets.split_cell(" x49 "), ("X", 49))
        with self.assertRaises(sheets.SheetError):
            sheets.split_cell("C")

    def test_spreadsheet_key_from_a_url(self):
        url = ("https://docs.google.com/spreadsheets/d/"
               "1bdBCB-W_qJlY0fn1wPwNhHbwYqdne5rpTMCVAPOx7PM/edit?usp=sharing")
        self.assertEqual(sheets.extract_key(url), "1bdBCB-W_qJlY0fn1wPwNhHbwYqdne5rpTMCVAPOx7PM")
        self.assertEqual(sheets.extract_key("plain-key"), "plain-key")
        self.assertIsNone(sheets.extract_key(None))


class FakeTab:
    """A worksheet that records what was written to it."""

    def __init__(self, coaches):
        self.coaches = coaches          # {cell: name} for the coach cells
        self.updates = []
        self.option = None

    def batch_get(self, ranges, value_render_option=None):
        return [[[self.coaches[cell]]] if self.coaches.get(cell) else []
                for cell in ranges]

    def batch_update(self, updates, value_input_option=None):
        self.updates += updates
        self.option = value_input_option


class MatchWritingTests(unittest.TestCase):
    """Where a match's kills and deaths land.

    The geometry was read off the live Cornerstone document and is described
    in KD-NOTES.md: week 1 starts at column B, side 0's block at row 4, the
    second side eleven rows below, each match band 26 rows.
    """

    def setUp(self):
        self.sync = sheets.SheetSync.__new__(sheets.SheetSync)       # no credentials
        self.tab = FakeTab({"B4": "nirvanacokun", "B15": "bananamilker_",
                            "B30": "someone else",
                            # week 2's coach cells, sixteen columns right
                            "R4": "nirvanacokun", "R15": "bananamilker_"})
        self.sync._tab = lambda doc_url, tab_name: self.tab
        self.sync.forget_cache = lambda doc_url: None
        self.match = [
            {"coach": "nirvanacokun",
             "pokemon": [{"name": "Goodra-Hisui", "games": [(3, 0), (3, 0)]},
                         {"name": "Sneasler", "games": [(0, 0), (1, 1)]},
                         {"name": "Volcarona", "games": [(1, 1), None]}],
             "results": ["W", "W"]},
            {"coach": "bananamilker_",
             "pokemon": [{"name": "Alcremie", "games": [(0, 1), (1, 1)]}],
             "results": ["L", "L"]},
        ]

    def write(self, week=1, replays=None, sides=None):
        with unittest.mock.patch.object(type(self.sync), "can_write", True):
            return self.sync.write_match("doc", week, sides or self.match, replays)

    def ranges(self):
        return {update["range"]: update["values"] for update in self.tab.updates}

    def test_each_side_is_found_by_name_not_by_seat(self):
        report = self.write()
        self.assertEqual(report["rows"], {"nirvanacokun": 4, "bananamilker_": 15})
        self.assertEqual(report["games"], 2)

    def test_the_cells_are_the_ones_the_template_leaves_for_data(self):
        self.write()
        written = self.ranges()
        # Six name rows under the coach, in column B for week 1.
        self.assertEqual(written["B7:B12"],
                         [["Goodra-Hisui"], ["Sneasler"], ["Volcarona"], [""], [""], [""]])
        # Game 1 kills and deaths in C and D; game 2 in F and G.
        self.assertEqual(written["C7:D12"],
                         [[3, 0], [0, 0], [1, 1], ["", ""], ["", ""], ["", ""]])
        self.assertEqual(written["F7:G12"],
                         [[3, 0], [1, 1], ["", ""], ["", ""], ["", ""], ["", ""]],
                         "Volcarona did not play game 2, so its cells are cleared")
        # The W/L row is ten rows under the coach.
        self.assertEqual(written["C14"], [["W"]])
        self.assertEqual(written["F14"], [["W"]])
        # The second side sits eleven rows down.
        self.assertEqual(written["B18:B23"],
                         [["Alcremie"], [""], [""], [""], [""], [""]])
        self.assertEqual(written["C25"], [["L"]])

    def test_nothing_is_written_over_a_formula(self):
        self.write()
        # E, H, K are per-game K/D; L, M, N are totals; rows 13 and 24 are
        # the Total rows. None of them may appear in an update.
        forbidden = {f"{column}{row}" for column in "EHKLMN" for row in range(4, 30)}
        forbidden |= {f"{column}{row}" for column in "BCDEFGHIJKLMN" for row in (13, 24)}
        for update in self.tab.updates:
            for cell in str(update["range"]).split(":"):
                self.assertNotIn(cell, forbidden, f"{update['range']} touches a formula")

    def test_week_two_is_sixteen_columns_right(self):
        self.write(week=2)
        written = self.ranges()
        self.assertIn("R7:R12", written, "week 2's names are in column R")
        self.assertEqual(written["S7:T12"][0], [3, 0], "game 1 kills and deaths in S and T")
        self.assertEqual(written["V14"], [["W"]], "game 2's result")

    def test_replays_go_on_the_match_band(self):
        self.write(replays=["https://replay.ogreclan.com/a", None])
        self.assertEqual(self.ranges()["C26"], [["https://replay.ogreclan.com/a"]])

    def test_a_coach_the_sheet_does_not_have_is_refused(self):
        sides = [dict(self.match[0], coach="somebodyelse"), self.match[1]]
        with self.assertRaises(sheets.SheetError) as caught:
            self.write(sides=sides)
        self.assertIn("not on the Matches tab", str(caught.exception))
        self.assertEqual(self.tab.updates, [], "and nothing was written")

    def test_a_seventh_pokemon_is_reported_rather_than_dropped_quietly(self):
        many = [{"name": f"Mon{index}", "games": [(1, 0)]} for index in range(8)]
        report = self.write(sides=[dict(self.match[0], pokemon=many), self.match[1]])
        self.assertEqual(report["overflow"], {"nirvanacokun": ["Mon6", "Mon7"]})
        self.assertEqual(len(self.ranges()["B7:B12"]), 6)

    def test_more_games_than_the_sheet_holds_are_reported(self):
        five = ["W", "L", "W", "L", "W"]
        report = self.write(sides=[dict(self.match[0], results=five),
                                   dict(self.match[1], results=five)])
        self.assertEqual((report["games"], report["skipped_games"]), (3, 2))
        self.assertIn("I14", self.ranges(), "game 3 is written")
        self.assertNotIn("L14", self.ranges(), "game 4 would land on a formula")

    def test_writing_can_be_switched_off(self):
        self.sync.client = None
        with self.assertRaises(sheets.SheetError):
            self.sync.write_match("doc", 1, self.match)


class DisabledBehaviourTests(unittest.TestCase):
    def test_a_draft_works_with_sheets_switched_off(self):
        stub = sheets.DisabledSheets()
        self.assertFalse(stub.enabled)
        self.assertFalse(stub.can_write)
        with self.assertRaises(sheets.SheetError):
            stub.pool("anything")

    def test_write_switch_is_readable(self):
        # Writing is on now the layout has been confirmed. Flipping this to
        # False must leave everything else working - it only stops writes.
        self.assertIsInstance(config.SHEETS_WRITE_ENABLED, bool)


if __name__ == "__main__":
    unittest.main()
