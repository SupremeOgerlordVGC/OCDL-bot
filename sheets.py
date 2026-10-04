"""
Optional Google Sheets integration for the league template.

What the bot reads
------------------
* **Pokédex** - column E is the Pokémon name used in every command, column K
  is its cost. Costs are read live, so a league that re-prices the template
  works with no code changes. A Pokémon is banned when its cost cell is
  blank, holds text ("B", "Banned", "-", ...) or holds a placeholder number
  such as 99 or 999.

The Tier List tab is deliberately *not* read: it is generated from the same
Pokédex costs, so reading both could only ever produce disagreement.

What the bot writes
-------------------
* **Coaches!B2:B17** - the player usernames, in draft order. Every other tab
  in the document refers back to these cells, so writing here updates coach
  names everywhere.
* **Rosters** - the leftmost column of each coach's block, one row per round.
  The cells either side hold image and cost formulas driven by that name, so
  writing the name alone fills in the rest.

Nothing else in the document is touched.

Rules this module follows
-------------------------
* The local database is the source of truth; this only ever pushes outward.
* Every failure is caught and reported. A Google outage must never stop a
  draft, and never rewrite good local data with bad remote data.
* Nothing about the layout is guessed. Tabs and columns come from
  config.SHEET_LAYOUT, and anything missing is an explicit error naming what
  was expected and what was actually found.
* Writing stays off until config.SHEETS_WRITE_ENABLED is turned on.
"""

import logging
import os
import re
import time

import config

log = logging.getLogger("draftbot.sheets")

# gspread is optional. Without it the bot simply runs with sheets disabled.
try:
    import gspread
    GSPREAD_AVAILABLE = True
except ImportError:                                  # pragma: no cover
    gspread = None
    GSPREAD_AVAILABLE = False


class SheetError(Exception):
    """Something is wrong with the sheet, its layout, or the connection."""


def extract_key(url_or_key):
    """Pull the spreadsheet id out of a full Google Sheets URL."""
    if not url_or_key:
        return None
    match = re.search(r"/spreadsheets/d/([a-zA-Z0-9-_]+)", url_or_key)
    return match.group(1) if match else url_or_key.strip()


def normalize(name):
    return " ".join(str(name).strip().lower().split())


# Characters to ignore when reading a cost: the cost column is formatted as
# currency, so a league that types values as text can produce "$12" or
# "1,200". Only these are stripped - a marker like "B" or "Banned" still has
# no number left in it and so still counts as banned.
COST_NOISE = str.maketrans("", "", "$£€¥, \t")


def parse_cost(raw):
    """Cost as an int, or None when the Pokémon is banned or unpriced.

    Blank, anything without a number in it, and the placeholder numbers in
    config.BANNED_COST_VALUES all mean "not draftable".
    """
    text = str(raw).strip() if raw is not None else ""
    if not text:
        return None
    try:
        value = int(float(text.translate(COST_NOISE)))
    except ValueError:
        return None
    if value in config.BANNED_COST_VALUES:
        return None
    return value


def column_number(letters):
    """'A' -> 1, 'Z' -> 26, 'AA' -> 27."""
    total = 0
    for char in letters.upper():
        total = total * 26 + (ord(char) - ord("A") + 1)
    return total


def column_letters(number):
    """1 -> 'A', 27 -> 'AA'. The inverse of column_number."""
    letters = ""
    while number > 0:
        number, remainder = divmod(number - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return letters


def shift_column(reference, columns):
    """Move a cell reference sideways: shift_column('C6', 3) -> 'F6'."""
    letters, row = split_cell(reference)
    return f"{column_letters(column_number(letters) + columns)}{row}"


def split_cell(reference):
    """'C4' -> ('C', 4)."""
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", reference.strip())
    if not match:
        raise SheetError(f"`{reference}` is not a valid cell reference.")
    return match.group(1).upper(), int(match.group(2))


class SheetSync:
    """Thin wrapper around one service-account connection."""

    # How long the Pokédex may be reused before it is fetched again. Keeps
    # the bot well inside Google's rate limits during a fast draft, and makes
    # slash-command autocomplete instant.
    CACHE_SECONDS = 60

    def __init__(self, credentials_file=None):
        self.credentials_file = credentials_file or config.get_credentials_file()
        self.client = None
        self.reason = None            # why it is disabled, shown to admins
        self._books = {}              # spreadsheet key -> gspread Spreadsheet
        self._pool_cache = {}         # spreadsheet key -> (timestamp, entries)

        if not GSPREAD_AVAILABLE:
            self.reason = "the `gspread` package is not installed (`pip install gspread google-auth`)"
        elif not os.path.exists(self.credentials_file):
            self.reason = f"no service-account key found at `{self.credentials_file}`"
        else:
            try:
                self.client = gspread.service_account(filename=self.credentials_file)
            except Exception as error:               # pragma: no cover
                self.reason = f"could not sign in to Google: {error}"
                log.exception("Google sign-in failed")

    @property
    def enabled(self):
        return self.client is not None

    @property
    def can_write(self):
        return self.enabled and config.SHEETS_WRITE_ENABLED

    # ---- low level helpers ---------------------------------------------

    def _book(self, doc_url):
        """Open a spreadsheet, reusing the handle between calls."""
        if not self.enabled:
            raise SheetError(f"Google Sheets is not available: {self.reason}")

        key = extract_key(doc_url)
        if not key:
            raise SheetError("This draft has no Google Sheet configured.")

        if key not in self._books:
            try:
                self._books[key] = self.client.open_by_key(key)
            except Exception as error:
                raise SheetError(
                    f"Could not open the spreadsheet: {error}. Check the link, and make sure "
                    f"the sheet is shared with the service account email."
                ) from error
        return self._books[key]

    def _tab(self, doc_url, tab_name):
        book = self._book(doc_url)
        try:
            return book.worksheet(tab_name)
        except Exception as error:
            available = ", ".join(w.title for w in book.worksheets())
            raise SheetError(
                f"Tab **{tab_name}** was not found. The spreadsheet has: {available}. "
                f"Fix the tab names in `config.SHEET_LAYOUT`."
            ) from error

    def forget_cache(self, doc_url):
        self._pool_cache.pop(extract_key(doc_url), None)

    # ---- reading the Pokédex -------------------------------------------

    def pool(self, doc_url, force=False):
        """Every Pokémon in the Pokédex.

        Returns a list of {key, name, cost, banned, row}. `cost` is None for
        a banned Pokémon.
        """
        key = extract_key(doc_url)
        cached = self._pool_cache.get(key)
        if cached and not force and time.time() - cached[0] < self.CACHE_SECONDS:
            return cached[1]

        layout = config.SHEET_LAYOUT
        tab = self._tab(doc_url, layout["pokedex_tab"])
        first = layout["pokedex_first_row"]
        name_col = layout["pokedex_name_column"]
        cost_col = layout["pokedex_cost_column"]

        try:
            # One request for both columns. UNFORMATTED_VALUE matters: the
            # cost column is formatted as currency, so the default would
            # hand back "$12" instead of 12.
            names, costs = tab.batch_get(
                [f"{name_col}{first}:{name_col}", f"{cost_col}{first}:{cost_col}"],
                value_render_option="UNFORMATTED_VALUE",
            )
        except Exception as error:
            raise SheetError(f"Could not read the {layout['pokedex_tab']} tab: {error}") from error

        entries = []
        for offset in range(len(names)):
            name = (names[offset][0] if names[offset] else "").strip()
            if not name:
                continue
            raw = costs[offset][0] if offset < len(costs) and costs[offset] else ""
            cost = parse_cost(raw)
            entries.append({
                "key": normalize(name),
                "name": name,
                "cost": cost,
                "banned": cost is None,
                "row": first + offset,
            })

        if not entries:
            raise SheetError(
                f"No Pokémon names found in {layout['pokedex_tab']}!"
                f"{name_col}{first}:{name_col}. Check `config.SHEET_LAYOUT`."
            )

        self._pool_cache[key] = (time.time(), entries)
        return entries

    def lookup(self, doc_url, pokemon):
        """Find one Pokémon in the Pokédex, or None."""
        wanted = normalize(pokemon)
        for entry in self.pool(doc_url):
            if entry["key"] == wanted:
                return entry
        return None

    def at_cost(self, doc_url, cost, taken_keys):
        """Undrafted, non-banned Pokémon that cost exactly `cost`."""
        return [entry for entry in self.pool(doc_url)
                if entry["cost"] == cost and entry["key"] not in taken_keys]

    def suggest(self, doc_url, text, taken_keys, limit=25):
        """Autocomplete matches: undrafted, not banned, name contains `text`."""
        wanted = normalize(text)
        starts, contains = [], []
        for entry in self.pool(doc_url):
            if entry["banned"] or entry["key"] in taken_keys:
                continue
            if not wanted:
                starts.append(entry)
            elif entry["key"].startswith(wanted):
                starts.append(entry)
            elif wanted in entry["key"]:
                contains.append(entry)
            if len(starts) >= limit and not wanted:
                break
        return (starts + contains)[:limit]

    # ---- writing --------------------------------------------------------

    def write_pick(self, doc_url, draft_position, round_number, pokemon):
        """Write one Pokémon into one coach's roster block.

        Called from a background thread with plain values only - never a
        database handle. The pick is already saved locally, so a failure
        here is logged and ignored.
        """
        if not self.can_write:
            return False

        layout = config.SHEET_LAYOUT
        cell = self._roster_cell(draft_position, round_number)
        tab = self._tab(doc_url, layout["rosters_tab"])
        tab.update_acell(cell, pokemon)
        return True

    @staticmethod
    def _roster_cell(draft_position, round_number):
        """Which cell holds this coach's pick for this round."""
        layout = config.SHEET_LAYOUT
        blocks = layout["roster_blocks"]
        rows = layout["roster_rows"]

        if not 1 <= draft_position <= len(blocks):
            raise SheetError(
                f"Draft position {draft_position} has no roster block. The template "
                f"has {len(blocks)} of them."
            )
        if not 1 <= round_number <= rows:
            raise SheetError(
                f"Round {round_number} does not fit: each roster block on the sheet "
                f"holds {rows} rounds."
            )

        column, first_row = split_cell(blocks[draft_position - 1])
        return f"{column}{first_row + round_number - 1}"

    @staticmethod
    def _tera_cell(draft_position, round_number):
        """The Tera Captain tick box beside a pick.

        It sits a fixed number of columns right of the name - clear of the
        image formula on one side and the cost formula on the other.
        """
        return shift_column(SheetSync._roster_cell(draft_position, round_number),
                            config.SHEET_LAYOUT["tera_flag_offset"])

    def full_sync(self, doc_url, coach_names, roster_blocks):
        """Rewrite the coach names and every roster block from local data.

        `coach_names` is a list in draft order; `roster_blocks` maps draft
        position to a list of names, one per round. Both are prepared by
        draft.export_for_sheet, on the bot's main thread.
        """
        if not self.can_write:
            raise SheetError(
                "Sheet writing is switched off. Set `SHEETS_WRITE_ENABLED = True` in "
                "config.py once the template layout has been confirmed."
            )
        if not doc_url:
            raise SheetError("This draft has no Google Sheet configured.")

        layout = config.SHEET_LAYOUT
        rows = layout["roster_rows"]
        blocks = layout["roster_blocks"]

        # ---- coach usernames ----
        first = layout["coaches_first_row"]
        last = layout["coaches_last_row"]
        seats = last - first + 1
        if len(coach_names) > seats:
            raise SheetError(
                f"This draft has {len(coach_names)} players but the template only has "
                f"{seats} coach rows."
            )
        # Pad so removing a player also clears their old name.
        padded = list(coach_names) + [""] * (seats - len(coach_names))
        column = layout["coaches_name_column"]

        coaches_tab = self._tab(doc_url, layout["coaches_tab"])
        coaches_tab.update(values=[[name] for name in padded],
                           range_name=f"{column}{first}:{column}{last}",
                           value_input_option="USER_ENTERED")

        # ---- roster blocks, all in one request ----
        updates = []
        for position, names in sorted(roster_blocks.items()):
            if position > len(blocks):
                raise SheetError(
                    f"Draft position {position} has no roster block on the sheet."
                )
            col, first_row = split_cell(blocks[position - 1])
            padded_names = (list(names) + [""] * rows)[:rows]
            updates.append({
                "range": f"{col}{first_row}:{col}{first_row + rows - 1}",
                "values": [[name] for name in padded_names],
            })

        rosters_tab = self._tab(doc_url, layout["rosters_tab"])
        if updates:
            rosters_tab.batch_update(updates, value_input_option="USER_ENTERED")

        self.forget_cache(doc_url)
        return {"coaches": len(coach_names), "rosters": len(updates)}

    # ---- inspection -----------------------------------------------------

    def describe(self, doc_url):
        """Check the document against config.SHEET_LAYOUT. Changes nothing."""
        layout = config.SHEET_LAYOUT
        book = self._book(doc_url)
        titles = [w.title for w in book.worksheets()]

        report = {"title": book.title, "tabs": titles, "problems": [], "notes": []}

        for role, tab_name in [("Pokédex", layout["pokedex_tab"]),
                               ("Coaches", layout["coaches_tab"]),
                               ("Rosters", layout["rosters_tab"])]:
            if tab_name not in titles:
                report["problems"].append(f"{role} tab `{tab_name}` is missing.")

        if report["problems"]:
            return report

        entries = self.pool(doc_url, force=True)
        draftable = [e for e in entries if not e["banned"]]
        costs = sorted({e["cost"] for e in draftable})
        report["notes"].append(
            f"Pokédex: {len(entries)} names, {len(draftable)} draftable, "
            f"{len(entries) - len(draftable)} banned or unpriced."
        )
        report["notes"].append(
            f"Point values in use: {costs[0]}–{costs[-1]}." if costs else "No costs found."
        )

        coaches_tab = self._tab(doc_url, layout["coaches_tab"])
        column = layout["coaches_name_column"]
        current = coaches_tab.get(
            f"{column}{layout['coaches_first_row']}:{column}{layout['coaches_last_row']}")
        names = [row[0] for row in current if row and row[0].strip()]
        report["notes"].append(f"Coaches!{column}: {len(names)} names, e.g. {', '.join(names[:3])}")

        report["notes"].append(
            f"Rosters: {len(layout['roster_blocks'])} blocks of {layout['roster_rows']} rounds, "
            f"starting at {', '.join(layout['roster_blocks'][:4])}, ..."
        )
        return report


class DisabledSheets:
    """Stand-in used when Google Sheets is switched off entirely."""

    enabled = False
    can_write = False
    reason = "Google Sheets is not configured"

    def __getattr__(self, name):
        def unavailable(*args, **kwargs):
            raise SheetError(self.reason)
        return unavailable
