"""
Tests for the Google Sheets layer that need no network connection.

These cover the parts most likely to go wrong quietly: working out which
cell a pick belongs in, and deciding whether a Pokémon is banned.
"""

import os
import sys
import unittest

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
